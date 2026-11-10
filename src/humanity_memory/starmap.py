"""只读星图生成器：把指定目录里的记忆库画成一张单文件 HTML 星图。

设计边界（玩具，刻意收紧）
------------------------
* **只在 CLI 使用**：不注册 MCP tool，不写 Skill，不对 AI 暴露。
* **绝不写库**：只用 ``sqlite3.connect(... mode=ro)``，不调用 ``open_db`` /
  ``ensure_schema``，不执行任何 DDL / DML / PRAGMA 写操作，不创建数据库。
* **零侵入**：不 import ``store``，不依赖 ``Config`` / ``Layout``，
  只用标准库 + 可选的 sqlite-vec 扩展。
* 产物只有一个 HTML 文件，数据内嵌，不依赖 CDN、不联网、不常驻进程。

用法::

    humanity-memory starmap <目录或 workspace.db> --out <HTML 路径>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import math
import sqlite3
import struct
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

MAIN_TABLES = ("knowledge", "episodes", "logs")
VEC_TABLES = {
    "knowledge": "knowledge_vec",
    "episodes": "episodes_vec",
    "logs": "logs_vec",
}
KIND_ORDER = ("knowledge", "state", "case", "narrative", "log", "digest")


class StarmapError(RuntimeError):
    """输入不是可读的 v3 记忆库。"""


# ---------------------------------------------------------------- 目录解析


def _describe_dir(parent: Path, db_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """只列目录，不读内容 —— 给星图的「数据源面板」用。"""
    files: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        entries = sorted(parent.iterdir(), key=lambda p: p.name)
    except OSError:
        return files, notes
    for p in entries:
        if not p.is_file():
            continue
        interesting = (
            p.suffix in (".db", ".md")
            or p.name.endswith(("-wal", "-shm"))
            or ".v2-" in p.name
            or p.suffix in (".json", ".bat")
        )
        if not interesting:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        files.append({"name": p.name, "size": size, "active": p == db_path})
    if any(".v2-" in f["name"] for f in files):
        notes.append("目录里有 v2 留档库（facts 表），星图只读取当前 workspace.db")
    if any(f["name"].endswith("-wal") for f in files):
        notes.append("检测到 WAL：读取的是包含 WAL 的一致性快照，未做 checkpoint")
    if any(f["name"] == "CORE_RULES.md" for f in files):
        notes.append("信条 CORE_RULES.md 不进向量库，星图不包含它")
    return files, notes


def resolve_db(target: str | Path) -> tuple[Path, list[str]]:
    """把拖进来的路径解析成一个 v3 库文件。

    接受三种：``.ai_memory`` 目录 / 项目根目录 / 直接的 ``.db`` 文件。
    绝不新建目录或文件。
    """
    raw = Path(str(target).strip().strip('"')).expanduser()
    notes: list[str] = []
    if not raw.exists():
        raise StarmapError(f"路径不存在：{raw}")

    if raw.is_file():
        if ".v2-" in raw.name:
            raise StarmapError("这是 v2 留档库（facts 表），星图只支持 v3 的 workspace.db")
        if raw.suffix.lower() != ".db":
            raise StarmapError(f"拖入的是文件但不是 .db：{raw.name}")
        db = raw
    else:
        candidates = [raw / "workspace.db", raw / ".ai_memory" / "workspace.db"]
        db = next((p for p in candidates if p.is_file()), None)
        if db is None:
            loose = sorted(p for p in raw.glob("*.db") if ".v2-" not in p.name)
            if len(loose) == 1:
                db = loose[0]
                notes.append(f"目录里没有 workspace.db，改用唯一的库文件 {db.name}")
            elif len(loose) > 1:
                raise StarmapError(
                    f"目录里有多个 .db（{', '.join(p.name for p in loose)}），"
                    "请把具体的 workspace.db 拖进来"
                )
            else:
                raise StarmapError(
                    f"{raw} 里找不到 workspace.db；请把 .ai_memory 目录、项目根目录"
                    "或 workspace.db 本身拖进来"
                )
    files, dir_notes = _describe_dir(db.parent, db)
    notes.extend(dir_notes)
    return db.resolve(), notes


# ---------------------------------------------------------------- 只读连接


def _load_vec(conn: sqlite3.Connection) -> str:
    """加载 sqlite-vec，只读连接上注册函数，不写库。"""
    conn.enable_load_extension(True)
    errors: list[str] = []
    try:
        try:
            import sqlite_vec

            sqlite_vec.load(conn)
            return "pip:sqlite-vec"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pip: {exc}")
        here = Path(__file__).resolve().parents[2] / "tools" / "bin" / "vec0.dll"
        if here.is_file():
            try:
                conn.load_extension(str(here))
                return "file:vec0.dll"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dll: {exc}")
        raise StarmapError("无法加载 sqlite-vec 扩展：" + " | ".join(errors))
    finally:
        try:
            conn.enable_load_extension(False)
        except sqlite3.OperationalError:
            pass


def open_readonly(db: Path) -> sqlite3.Connection:
    uri = "file:" + quote(db.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    _load_vec(conn)
    return conn


def inspect_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "knowledge" not in tables:
        if "facts" in tables:
            raise StarmapError("这是 v2 schema（facts 表）。星图只支持 v3（knowledge / episodes / logs）")
        raise StarmapError("库里没有 knowledge 表，不是有效的 v3 记忆库")
    meta: dict[str, str] = {}
    if "meta" in tables:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    vec_missing = [v for v in VEC_TABLES.values() if v not in tables]
    return {
        "schema_version": meta.get("schema_version", "?"),
        "embedding_dim": int(meta.get("embedding_dim") or 1024),
        "embedding_model": meta.get("embedding_model", "?"),
        "vec_missing": vec_missing,
        "meta": meta,
    }


# ---------------------------------------------------------------- 节点


def _day_of(ms: int | None) -> str:
    if not ms:
        return ""
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def read_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for table in MAIN_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        for r in rows:
            keys = list(r.keys())

            def g(col: str, default: Any = None) -> Any:
                return r[col] if col in keys else default

            try:
                tags = json.loads(g("tags") or "[]")
            except (ValueError, TypeError):
                tags = []
            if not isinstance(tags, list):
                tags = []
            created = g("created_at") or g("at") or 0
            invalid_at = g("invalid_at")
            archived_at = g("archived_at")
            nodes.append(
                {
                    "id": r["id"],
                    "table": table,
                    "kind": g("kind", "log") if table != "logs" else "log",
                    "rowid": r["rowid"],
                    "content": (r["content"] or "")[:800],
                    "tags": [str(t) for t in tags][:12],
                    "day": g("day") or _day_of(created),
                    "at": g("at") or created,
                    "importance": float(g("importance") or 0.4),
                    "access_count": int(g("access_count") or 0),
                    "scope": g("scope", "workspace") or "workspace",
                    "source": g("source"),
                    "digest_id": g("digest_id"),
                    "session_id": g("session_id"),
                    "invalidated_by": g("invalidated_by"),
                    "live": invalid_at is None,
                    "archived": archived_at is not None,
                    "compression_level": int(g("compression_level") or 0),
                    "embedded": bool(g("embedded")),
                }
            )
    return nodes


def read_vectors(conn: sqlite3.Connection, dim: int) -> dict[tuple[str, int], tuple[float, ...]]:
    vecs: dict[tuple[str, int], tuple[float, ...]] = {}
    for table, vt in VEC_TABLES.items():
        try:
            rows = conn.execute(f"SELECT rowid, embedding FROM {vt}").fetchall()  # noqa: S608
        except sqlite3.Error:
            continue
        for r in rows:
            blob = r["embedding"]
            if isinstance(blob, (bytes, bytearray, memoryview)):
                raw = bytes(blob)
                if len(raw) != dim * 4:
                    continue
                v = struct.unpack(f"{dim}f", raw)
            elif isinstance(blob, (list, tuple)) and len(blob) == dim:
                v = tuple(float(x) for x in blob)
            else:
                continue
            norm = math.sqrt(sum(x * x for x in v))
            vecs[(table, r["rowid"])] = tuple(x / norm for x in v) if norm else v
    return vecs


# ---------------------------------------------------------------- 关系边


def _add_edge(
    bucket: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    s: str,
    t: str,
    kind: str,
    weight: float,
    label: str = "",
) -> None:
    if s == t:
        return
    key = (s, t, kind) if kind == "invalidates" else (min(s, t), max(s, t), kind)
    if key in seen:
        return
    seen.add(key)
    bucket.append(
        {"s": s, "t": t, "type": kind, "weight": round(float(weight), 4), "label": label}
    )


def build_graph(
    nodes: list[dict[str, Any]],
    vecs: dict[tuple[str, int], tuple[float, ...]],
    *,
    min_score: float,
    top_k: int,
    max_nodes: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    by_id = {n["id"]: n for n in nodes}
    notes: list[str] = []

    # 1) 失效 / 换代链（有向，旧 -> 新）
    dangling = 0
    for n in nodes:
        target = n["invalidated_by"]
        if not target:
            continue
        if target in by_id:
            _add_edge(edges, seen, n["id"], target, "invalidates", 1.0, "顶替")
        else:
            dangling += 1
    if dangling:
        notes.append(f"{dangling} 条失效链的另一端不在这个库里（跨库提升或已清理）")

    # 2) 复盘簇：digest 作为虚拟恒星，日志被 purge 后簇结构仍在
    digest_ids: set[str] = set()
    for n in nodes:
        if n["digest_id"]:
            digest_ids.add(str(n["digest_id"]))
        src = n["source"] or ""
        if src.startswith("digest:"):
            digest_ids.add(src[7:])
    for d in sorted(digest_ids):
        nodes.append(
            {
                "id": f"digest:{d}",
                "table": "digests",
                "kind": "digest",
                "rowid": -1,
                "content": f"复盘批次 {d}（虚拟中心）",
                "tags": [],
                "day": "",
                "at": 0,
                "importance": 1.0,
                "access_count": 0,
                "scope": "workspace",
                "source": None,
                "digest_id": d,
                "session_id": None,
                "invalidated_by": None,
                "live": True,
                "archived": False,
                "compression_level": 0,
                "embedded": False,
            }
        )
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        if n["kind"] == "digest":
            continue
        linked: set[str] = set()
        if n["digest_id"]:
            linked.add(str(n["digest_id"]))
        src = n["source"] or ""
        if src.startswith("digest:"):
            linked.add(src[7:])
        for d in sorted(linked):
            if f"digest:{d}" in by_id:
                _add_edge(edges, seen, n["id"], f"digest:{d}", "digest", 1.0, d)

    # 3) 语义近邻：只在有向量的真实条目之间算
    real = [n for n in nodes if n["kind"] != "digest"]
    vec_keys = [(n["table"], n["rowid"], n["id"]) for n in real if (n["table"], n["rowid"]) in vecs]
    sampled = vec_keys
    if len(vec_keys) > max_nodes:
        rank = {n["id"]: (n["importance"], n["access_count"]) for n in real}
        sampled = sorted(vec_keys, key=lambda k: rank[k[2]], reverse=True)[:max_nodes]
        notes.append(
            f"条目 {len(vec_keys)} 个，超过纯 Python 全对计算上限 {max_nodes}，"
            "只对重要度最高的那批计算语义边"
        )
    best: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (ta, ra, ia), (tb, rb, ib) in itertools.combinations(sampled, 2):
        score = sum(x * y for x, y in zip(vecs[(ta, ra)], vecs[(tb, rb)]))
        if score < min_score:
            continue
        best[ia].append((score, ib))
        best[ib].append((score, ia))
    semantic = 0
    for src, lst in best.items():
        for score, dst in sorted(lst, reverse=True)[:top_k]:
            before = len(edges)
            _add_edge(edges, seen, src, dst, "semantic", score, "语义近邻")
            semantic += len(edges) > before
    # 让滑块能覆盖「至少是某一方 top_k」的全部边，score 下限即 min_score

    # 4) 标签共现：只连稀有标签、每节点限流，避免稠密毛球
    groups: dict[str, list[str]] = defaultdict(list)
    for n in real:
        for tag in n["tags"]:
            groups[tag].append(n["id"])
    quota: Counter[str] = Counter()
    tag_edges = 0
    for size, tag, ids in sorted(
        ((len(v), k, v) for k, v in groups.items() if 2 <= len(v) <= 8),
        key=lambda x: (x[0], x[1]),
    ):
        for a, b in itertools.combinations(ids, 2):
            if tag_edges >= 300 or quota[a] >= 2 or quota[b] >= 2:
                continue
            before = len(edges)
            _add_edge(edges, seen, a, b, "tag", 0.5, tag)
            if len(edges) > before:
                tag_edges += 1
                quota[a] += 1
                quota[b] += 1

    counts = Counter(e["type"] for e in edges)
    stats = {
        "semantic_pairs_computed": len(sampled) * (len(sampled) - 1) // 2,
        "semantic_edges": semantic,
        "tag_edges": tag_edges,
        "digest_nodes": len(digest_ids),
        "edge_counts": dict(counts),
        "notes": notes,
    }
    return edges, stats


# ---------------------------------------------------------------- HTML


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>HumanityMemory · 星图</title>
<style>
:root{
  --bg:#070b14; --panel:#0d1422; --line:#243044; --text:#d8e2f2; --muted:#8292ab;
  --knowledge:#6fb6ff; --state:#ffc46b; --case:#ff8f8f; --narrative:#b9a5ff;
  --log:#8898ad; --digest:#ffe08a;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
  font:13px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;overflow:hidden}
#app{display:flex;height:100%}
#side{width:268px;flex:0 0 268px;background:var(--panel);border-right:1px solid var(--line);
  padding:16px;overflow-y:auto}
#side h1{font-size:15px;font-weight:500;margin:0 0 4px}
#side .sub{color:var(--muted);font-size:12px;margin-bottom:16px}
.sec{border-top:1px solid var(--line);padding-top:12px;margin-top:14px}
.sec h2{font-size:12px;font-weight:500;color:var(--muted);margin:0 0 8px;letter-spacing:.5px}
.kv{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin:3px 0}
.kv span:first-child{color:var(--muted);flex:0 0 auto}
.kv span:last-child{text-align:right;word-break:break-all}
label.row{display:flex;align-items:center;gap:8px;margin:5px 0;cursor:pointer;font-size:12px}
input[type=range]{width:100%;accent-color:var(--knowledge)}
input[type=text]{width:100%;background:#0a101c;color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:6px 8px;font:inherit;font-size:12px}
button{background:#16223a;color:var(--text);border:1px solid var(--line);border-radius:6px;
  padding:6px 10px;cursor:pointer;font:inherit;font-size:12px;margin:4px 4px 0 0}
button:hover{border-color:var(--knowledge)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 9px}
#main{flex:1;position:relative;min-width:0}
canvas{display:block;width:100%;height:100%;cursor:grab}
canvas.drag{cursor:grabbing}
#tip{position:absolute;right:16px;top:16px;width:300px;max-height:calc(100% - 32px);
  overflow-y:auto;background:rgba(13,20,34,.94);border:1px solid var(--line);border-radius:10px;
  padding:14px;display:none;pointer-events:auto;user-select:text;-webkit-user-select:text;cursor:text}
#tip .kind{font-size:11px;color:var(--muted);letter-spacing:1px}
#tip .body{margin:8px 0;font-size:12px;white-space:pre-wrap;word-break:break-word}
#tip .meta{font-size:11px;color:var(--muted);border-top:1px solid var(--line);padding-top:8px}
#hud{position:absolute;left:16px;bottom:14px;color:var(--muted);font-size:11px;
  pointer-events:none;letter-spacing:.3px}
#legend{position:absolute;right:16px;bottom:14px;font-size:11px;color:var(--muted);
  pointer-events:none;text-align:right;line-height:1.9}
.warn{color:#ffc46b;font-size:11px;margin-top:8px;word-break:break-word}
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <h1>HumanityMemory · 星图</h1>
    <div class="sub">只读视图 · 数据内嵌 · 不联网</div>
    <div id="info"></div>
    <div class="sec">
      <h2>语义阈值 <span id="scoreVal"></span></h2>
      <input type="range" id="score" min="0.50" max="0.95" step="0.01">
      <div class="kv"><span>当前可见边</span><span id="edgeCount">-</span></div>
    </div>
    <div class="sec">
      <h2>边类型</h2>
      <div id="edgeTypes"></div>
    </div>
    <div class="sec">
      <h2>节点类型</h2>
      <div id="kinds"></div>
    </div>
    <div class="sec">
      <h2>查找</h2>
      <input type="text" id="search" placeholder="输入关键词或节点 ID，高亮命中条目">
      <label class="row"><input type="checkbox" id="labels"> 显示更多星名</label>
      <button id="reset">重排</button><button id="fit">复位视角</button>
    </div>
    <div class="sec" id="notes"></div>
  </div>
  <div id="main">
    <canvas id="cv"></canvas>
    <div id="tip"></div>
    <div id="hud">拖拽移动星点 · 滚轮缩放 · 悬停看全文 · 点击选中 / 再点或点空处取消</div>
    <div id="legend"></div>
  </div>
</div>
<script>
/*__DATA__*/
(function(){
const C={knowledge:'#6fb6ff',state:'#ffc46b',case:'#ff8f8f',narrative:'#b9a5ff',log:'#8898ad',digest:'#ffe08a'};
const L={knowledge:'知识',state:'现状',case:'案例',narrative:'叙述',log:'日志',digest:'复盘'};
const ET={semantic:'语义近邻',invalidates:'失效链',digest:'复盘溯源',tag:'标签共现'};
const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
const tip=document.getElementById('tip');
let W=0,H=0,DPR=1,view={x:0,y:0,k:1};
let alpha=1,frame=0,hover=null,selected=null,dragging=null,panning=null,fixedHover=true,downX=0,downY=0;
const enabled={semantic:true,invalidates:true,digest:true,tag:false};
const kinds={knowledge:true,state:true,case:true,narrative:true,log:true,digest:true};
let minScore=DATA.meta.min_score;
let showLabels=document.getElementById('labels').checked;

function rnd(i){const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);}
const centers={knowledge:[-.42,-.30],state:[.42,-.30],case:[-.38,.28],narrative:[.38,.28],log:[0,.02],digest:[0,0]};
const nodes=DATA.nodes.map((n,i)=>{
  const c=centers[n.kind]||[0,0];
  return Object.assign({},n,{
    x:W*.5+c[0]*340+(rnd(i)-.5)*220,
    y:H*.5+c[1]*260+(rnd(i+999)-.5)*220,
    vx:0,vy:0,fixed:false,
    r:3+Math.min(9,Math.max(0,n.importance)*8)+(n.kind==='digest'?3:0),
    deg:0
  });
});
const byId={};nodes.forEach(n=>byId[n.id]=n);
const edges=DATA.edges.map(e=>({s:byId[e.s],t:byId[e.t],type:e.type,w:e.weight,label:e.label}))
  .filter(e=>e.s&&e.t);
edges.forEach(e=>{e.s.deg++;e.t.deg++;});

function visible(e){
  if(!enabled[e.type])return false;
  if(!kinds[e.s.kind]||!kinds[e.t.kind])return false;
  if(e.type==='semantic'&&e.w<minScore)return false;
  return true;
}
function matchesQuery(n,q){
  return !!q&&(n.content.toLowerCase().includes(q)||n.tags.join(' ').toLowerCase().includes(q)||n.id.toLowerCase().includes(q));
}
function activeNodes(){
  const s=new Set();
  edges.forEach(e=>{if(visible(e)){s.add(e.s);s.add(e.t);}});
  nodes.forEach(n=>{if(kinds[n.kind])s.add(n);});
  return [...s];
}

function resize(){
  DPR=window.devicePixelRatio||1;
  const r=cv.getBoundingClientRect();
  W=r.width;H=r.height;
  cv.width=Math.round(W*DPR);cv.height=Math.round(H*DPR);
  ctx.setTransform(DPR,0,0,DPR,0,0);
}
window.addEventListener('resize',()=>{resize();resetLayout();});

function resetLayout(){
  const c=centers;
  nodes.forEach((n,i)=>{
    const p=c[n.kind]||[0,0];
    n.x=W*.5+p[0]*340+(rnd(i)-.5)*220;
    n.y=H*.5+p[1]*260+(rnd(i+999)-.5)*220;
    n.vx=0;n.vy=0;n.fixed=false;
  });
  alpha=1;frame=0;
}

function step(){
  const ns=activeNodes();
  const rep=4200, spring=0.012, damp=0.86;
  for(let i=0;i<ns.length;i++){
    const a=ns[i];
    for(let j=i+1;j<ns.length;j++){
      const b=ns[j];
      let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy+16;
      const f=rep/d2, d=Math.sqrt(d2);
      const fx=f*dx/d,fy=f*dy/d;
      a.vx-=fx;a.vy-=fy;b.vx+=fx;b.vy+=fy;
    }
  }
  for(const e of edges){
    if(!visible(e))continue;
    const a=e.s,b=e.t;
    let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)+.001;
    const rest=e.type==='digest'?130:105;
    const f=(d-rest)*spring*(e.type==='semantic'?e.w:1);
    const fx=f*dx/d,fy=f*dy/d;
    a.vx+=fx;a.vy+=fy;b.vx-=fx;b.vy-=fy;
  }
  for(const n of ns){
    n.vx+=(W*.5-n.x)*0.0009;
    n.vy+=(H*.5-n.y)*0.0009;
    if(n.fixed||n===dragging){n.vx=0;n.vy=0;continue;}
    n.vx*=damp;n.vy*=damp;
    n.x+=Math.max(-24,Math.min(24,n.vx*alpha));
    n.y+=Math.max(-24,Math.min(24,n.vy*alpha));
  }
  alpha=Math.max(0.02,alpha*0.988);
  frame++;
}

function drawStarfield(){
  ctx.fillStyle='#070b14';ctx.fillRect(0,0,W,H);
  ctx.fillStyle='rgba(150,180,230,0.16)';
  for(let i=0;i<220;i++){
    const x=rnd(i)*W,y=rnd(i+7777)*H;
    ctx.fillRect(x,y,rnd(i+31)>.8?1.6:1,rnd(i+31)>.8?1.6:1);
  }
}
function screen(n){return [(n.x+view.x)*view.k,(n.y+view.y)*view.k];}
function drawEdge(e,hot){
  const [x1,y1]=[e.s.x,e.s.y],[x2,y2]=[e.t.x,e.t.y];
  let rgb,a,w,dash=[];
  if(e.type==='semantic'){
    rgb='111,182,255';a=0.08+Math.min(.5,(e.w-.5)*.9);w=.4+Math.max(.2,(e.w-.5)*3);
  }else if(e.type==='invalidates'){
    rgb='255,196,107';a=.75;w=1.5;
  }else if(e.type==='digest'){
    rgb='255,224,138';a=.28;w=.7;dash=[3,4];
  }else{
    rgb='95,214,160';a=.42;w=.5;
  }
  if(hot){a=Math.max(a,.9);w=Math.max(w,2.2);}
  ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);
  ctx.strokeStyle='rgba('+rgb+','+a+')';ctx.lineWidth=w;ctx.setLineDash(dash);
  ctx.stroke();ctx.setLineDash([]);
  if(e.type==='invalidates'){
    const ang=Math.atan2(y2-y1,x2-x1),h=8;
    ctx.beginPath();
    ctx.moveTo(x2,y2);
    ctx.lineTo(x2-h*Math.cos(ang-.4),y2-h*Math.sin(ang-.4));
    ctx.lineTo(x2-h*Math.cos(ang+.4),y2-h*Math.sin(ang+.4));
    ctx.closePath();ctx.fillStyle=hot?'rgba(255,196,107,1)':'rgba(255,196,107,.8)';ctx.fill();
  }
}
function draw(){
  drawStarfield();
  ctx.save();
  ctx.translate(view.x*view.k,view.y*view.k);
  ctx.scale(view.k,view.k);
  const q=document.getElementById('search').value.trim().toLowerCase();
  const focusSet=new Set();
  for(const n of nodes){
    if(!kinds[n.kind])continue;
    if(n===hover||n===selected||matchesQuery(n,q))focusSet.add(n);
  }
  const hotEdges=[];
  for(const e of edges){
    if(!visible(e))continue;
    if(focusSet.has(e.s)||focusSet.has(e.t)){hotEdges.push(e);continue;}
    drawEdge(e,false);
  }
  for(const e of hotEdges)drawEdge(e,true);
  const labelCandidates=[];
  for(const n of nodes){
    if(!kinds[n.kind])continue;
    const dim=n.live===false||n.archived;
    const col=C[n.kind]||'#8898ad';
    const hit=matchesQuery(n,q);
    const focus=n===hover||n===selected||hit;
    ctx.globalAlpha=dim?.42:(focus?1:.9);
    ctx.beginPath();ctx.arc(n.x,n.y,n.r*2.4,0,6.2832);
    ctx.fillStyle=col;ctx.globalAlpha*=.10;ctx.fill();
    ctx.globalAlpha=dim?.42:(focus?1:.9);
    ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,6.2832);
    ctx.fillStyle=col;ctx.fill();
    if(n.kind==='digest'){
      ctx.strokeStyle='#fff2c8';ctx.lineWidth=1;ctx.stroke();
    }
    if(dim){ctx.beginPath();ctx.arc(n.x,n.y,n.r+2.5,0,6.2832);
      ctx.strokeStyle=col;ctx.lineWidth=.6;ctx.setLineDash([2,3]);ctx.stroke();ctx.setLineDash([]);}
    if(n.fixed){ctx.beginPath();ctx.arc(n.x,n.y,n.r+5,0,6.2832);
      ctx.strokeStyle='rgba(255,255,255,.5)';ctx.lineWidth=1;ctx.stroke();}
    if(focus){ctx.beginPath();ctx.arc(n.x,n.y,n.r+8,0,6.2832);
      ctx.strokeStyle=col;ctx.lineWidth=.8;ctx.stroke();}
    if(n===selected){ctx.beginPath();ctx.arc(n.x,n.y,n.r+11,0,6.2832);
      ctx.strokeStyle='#ffffff';ctx.lineWidth=1.4;ctx.stroke();}
    labelCandidates.push({
      n:n,t:n.content.replace(/\s+/g,' ').slice(0,14),focus:focus,
      pri:(focus?1000:0)+(n.kind==='digest'?100:0)+n.deg
    });
  }
  ctx.globalAlpha=1;
  labelCandidates.sort((a,b)=>b.pri-a.pri);
  ctx.font='11px "Microsoft YaHei",sans-serif';ctx.textBaseline='middle';
  const placed=[];let budget=showLabels?26:10;
  for(const c of labelCandidates){
    if(budget<=0)break;
    const x=c.n.x+c.n.r+5,y=c.n.y,w=c.t.length*11+8;
    let clash=false;
    for(const p of placed){
      if(Math.abs(p.x-x)<(p.w+w)/2+10&&Math.abs(p.y-y)<13){clash=true;break;}
    }
    if(clash)continue;
    ctx.fillStyle=c.focus?'#ffffff':'#9fb0c8';
    ctx.fillText(c.t,x,y);
    placed.push({x:x,y:y,w:w});budget--;
  }
  ctx.restore();
}

function showTip(n){
  if(!n){tip.style.display='none';return;}
  const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  tip.innerHTML='<div class="kind" style="color:'+(C[n.kind]||'#8898ad')+'">'+
    esc(L[n.kind]||n.kind)+' · '+esc(n.id)+'</div>'+
    '<div class="body">'+esc(n.content)+'</div>'+
    '<div class="meta">重要度 '+n.importance.toFixed(2)+' · 访问 '+n.access_count+
    ' · 邻居 '+n.deg+'<br>'+(n.day?('日期 '+esc(n.day)+' · '):'')+
    '作用域 '+esc(n.scope)+(n.archived?' · 已归档':'')+(n.live===false?' · 已失效':'')+
    (n.tags.length?('<br>标签 '+esc(n.tags.join(' / '))):'')+
    (n.source?('<br>来源 '+esc(n.source)):'')+'</div>';
  tip.style.display='block';
}
function pick(mx,my){
  let best=null,bd=1e9;
  for(const n of nodes){
    if(!kinds[n.kind])continue;
    const [x,y]=screen(n),d=(x-mx)*(x-mx)+(y-my)*(y-my);
    const lim=Math.max(10,(n.r+6)*view.k);
    if(d<lim*lim&&d<bd){bd=d;best=n;}
  }
  return best;
}

cv.addEventListener('mousemove',ev=>{
  const r=cv.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;
  if(dragging){dragging.x=(mx/view.k)-view.x;dragging.y=(my/view.k)-view.y;alpha=Math.max(alpha,.35);return;}
  if(panning){view.x+=(mx-panning.x)/view.k;view.y+=(my-panning.y)/view.k;panning={x:mx,y:my};return;}
  const n=pick(mx,my);
  if(n!==hover){hover=n;showTip(selected||n);}
});
cv.addEventListener('mousedown',ev=>{
  downX=ev.clientX;downY=ev.clientY;
  const r=cv.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;
  const n=pick(mx,my);
  if(n){dragging=n;cv.classList.add('drag');}
  else{panning={x:mx,y:my};hover=null;showTip(selected);cv.classList.add('drag');}
});
window.addEventListener('mouseup',()=>{dragging=null;panning=null;cv.classList.remove('drag');});
cv.addEventListener('click',ev=>{
  if(Math.hypot(ev.clientX-downX,ev.clientY-downY)>4)return;
  const r=cv.getBoundingClientRect(),n=pick(ev.clientX-r.left,ev.clientY-r.top);
  if(n&&n===selected){selected=null;showTip(null);return;}
  selected=n;showTip(selected||hover);
});
cv.addEventListener('wheel',ev=>{
  ev.preventDefault();
  const r=cv.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;
  const old=view.k,k=Math.max(.25,Math.min(4,old*(ev.deltaY<0?1.12:.89)));
  view.x=mx/k-mx/old+view.x;view.y=my/k-my/old+view.y;view.k=k;
},{passive:false});

function buildUI(){
  const info=document.getElementById('info'),m=DATA.meta;
  const kv=(a,b)=>'<div class="kv"><span>'+a+'</span><span>'+b+'</span></div>';
  info.innerHTML=kv('数据库',esc(m.db_name))+kv('schema','v'+esc(m.schema_version))+
    kv('向量',esc(m.embedding_model)+' · '+m.embedding_dim+' 维')+
    kv('节点',DATA.nodes.length+' （真实 '+m.real_nodes+' + 复盘 '+m.digest_nodes+'）')+
    kv('关系边',DATA.edges.length)+kv('向量覆盖',esc(m.coverage))+
    kv('生成于',esc(m.generated_at));
  if(m.warnings.length)info.innerHTML+='<div class="warn">'+m.warnings.map(esc).join('<br>')+'</div>';
  const et=document.getElementById('edgeTypes');
  et.innerHTML=Object.keys(ET).map(k=>'<label class="row"><input type="checkbox" data-et="'+k+'"'+(enabled[k]?' checked':'')+'> '+
    '<span class="dot" style="background:'+(k==='invalidates'?'#ffc46b':k==='digest'?'#ffe08a':k==='tag'?'#5fd6a0':'#6fb6ff')+'"></span>'+
    ET[k]+' <span style="color:var(--muted);margin-left:auto">'+(m.edge_counts[k]||0)+'</span></label>').join('');
  et.querySelectorAll('input').forEach(el=>el.onchange=()=>{enabled[el.dataset.et]=el.checked;refresh();});
  const kd=document.getElementById('kinds');
  kd.innerHTML=Object.keys(L).map(k=>'<label class="row"><input type="checkbox" data-k="'+k+'" checked> '+
    '<span class="dot" style="background:'+C[k]+'"></span>'+L[k]+' <span style="color:var(--muted);margin-left:auto">'+
    (m.kind_counts[k]||0)+'</span></label>').join('');
  kd.querySelectorAll('input').forEach(el=>el.onchange=()=>{kinds[el.dataset.k]=el.checked;refresh();});
  document.getElementById('legend').innerHTML=
    '<span style="color:#6fb6ff">虚线粗细 = 语义相似度</span><br>'+
    '<span style="color:#ffc46b">琥珀箭头 = 失效 / 换代</span><br>'+
    '<span style="color:#ffe08a">黄色恒星 = 复盘批次</span><br>'+
    '<span style="color:#5fd6a0">绿色实线 = 标签共现</span><br>'+
    '<span style="color:#cfe0ff">悬停 / 选中 / 搜中 = 加粗其可见连线</span>';
  const notes=document.getElementById('notes');
  notes.innerHTML='<h2>数据源说明</h2>'+(m.notes.length?m.notes.map(x=>'<div class="kv">'+esc(x)+'</div>').join(''):
    '<div class="kv"><span>无额外提示</span></div>')+
    '<div class="kv" style="margin-top:8px"><span>目录文件</span><span>'+m.dir_files.length+'</span></div>'+
    m.dir_files.slice(0,10).map(f=>'<div class="kv"><span>'+esc(f.name)+'</span><span>'+
      (f.size/1024).toFixed(0)+' KB'+(f.active?' ★':'')+'</span></div>').join('');
  const sc=document.getElementById('score');
  sc.min=String(m.min_score);sc.value=String(Math.max(m.min_score,.62));
  minScore=parseFloat(sc.value);
  document.getElementById('scoreVal').textContent=minScore.toFixed(2);
  sc.oninput=()=>{minScore=parseFloat(sc.value);
    document.getElementById('scoreVal').textContent=minScore.toFixed(2);refresh();};
  document.getElementById('labels').onchange=ev=>{showLabels=ev.target.checked;};
  document.getElementById('reset').onclick=()=>resetLayout();
  document.getElementById('fit').onclick=()=>{view={x:0,y:0,k:1};};
  document.getElementById('search').oninput=()=>{};
  refresh();
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function refresh(){
  let n=0;edges.forEach(e=>{if(visible(e))n++;});
  document.getElementById('edgeCount').textContent=n+' / '+edges.length;
}
function loop(){if(alpha>.021)step();draw();requestAnimationFrame(loop);}
resize();resetLayout();buildUI();loop();
})();
</script>
</body>
</html>
"""


def render_html(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _HTML_TEMPLATE.replace("/*__DATA__*/", "const DATA = " + payload + ";")


# ---------------------------------------------------------------- CLI 入口


def run(args: argparse.Namespace) -> int:
    started = time.time()
    db, notes = resolve_db(args.path)
    conn = open_readonly(db)
    try:
        schema = inspect_schema(conn)
        nodes = read_nodes(conn)
        vecs = read_vectors(conn, schema["embedding_dim"])
        edges, stats = build_graph(
            nodes,
            vecs,
            min_score=args.min_score,
            top_k=args.top_k,
            max_nodes=args.max_nodes,
        )
    finally:
        conn.close()

    real_nodes = sum(1 for n in nodes if n["kind"] != "digest")
    covered = sum(
        1 for n in nodes if n["kind"] != "digest" and (n["table"], n["rowid"]) in vecs
    )
    files, dir_notes = _describe_dir(db.parent, db)
    warnings: list[str] = []
    if schema["vec_missing"]:
        warnings.append("缺少向量表：" + ", ".join(schema["vec_missing"]))
    if not real_nodes:
        warnings.append("这是一个空库，星图里没有条目")
    if covered < real_nodes:
        warnings.append(f"{real_nodes - covered} 条条目没有向量，不参与语义连线")
    all_notes = list(dict.fromkeys(notes + dir_notes + stats["notes"]))

    graph = {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "db": str(db),
            "db_name": db.name,
            "dir": str(db.parent),
            "schema_version": schema["schema_version"],
            "embedding_model": schema["embedding_model"],
            "embedding_dim": schema["embedding_dim"],
            "real_nodes": real_nodes,
            "digest_nodes": stats["digest_nodes"],
            "coverage": f"{covered}/{real_nodes}",
            "kind_counts": dict(Counter(n["kind"] for n in nodes)),
            "edge_counts": stats["edge_counts"],
            "min_score": args.min_score,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dir_files": files,
            "notes": all_notes,
            "warnings": warnings,
        },
    }

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(graph), encoding="utf-8")

    counts = stats["edge_counts"]
    print(f"星图已生成：{out}")
    print(f"  数据源：{db}  (schema v{schema['schema_version']})")
    print(f"  节点 {len(nodes)}（真实 {real_nodes} + 复盘恒星 {stats['digest_nodes']}）"
          f" · 向量覆盖 {covered}/{real_nodes}")
    print(f"  关系边 {len(edges)}：" + " · ".join(
        f"{k} {counts.get(k, 0)}" for k in ("semantic", "invalidates", "digest", "tag")
    ))
    print(f"  相似度计算 {stats['semantic_pairs_computed']} 对 · 耗时 {time.time() - started:.1f}s")
    for w in warnings:
        print(f"  提示：{w}")
    print("  全程只读，未创建或修改任何数据库。")
    return 0
