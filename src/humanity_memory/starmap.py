"""只读星图生成器：把指定目录里的记忆库画成一张单文件 HTML 星图。

设计边界（玩具，刻意收紧）
------------------------
* **只在 CLI 使用**：不注册 MCP tool，不写 Skill，不对 AI 暴露。
* **绝不写库**：只用 ``sqlite3.connect(... mode=ro)``，不调用 ``open_db`` /
  ``ensure_schema``，不执行任何 DDL / DML / PRAGMA 写操作，不创建数据库。
* **只读配置**：等级表的档数/周期来自项目配置的 ``[[forget.levels]]``，
  所以会延迟 import ``Config`` 读一次 TOML（只解析，不建目录、不写文件）；
  配置读不到就退回「库里实际出现过的档位」。不 import ``store``、不用 ``Layout``。
* 产物只有一个 HTML 文件，数据内嵌，不依赖 CDN、不联网、不常驻进程。

用法::

    humanity-memory starmap <目录或 workspace.db> --out <HTML 路径>
"""

from __future__ import annotations

import argparse
import bisect
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
KIND_ORDER = ("experience", "episode", "log", "digest")


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


def _db_via_readonly(target: Path, notes: list[str]) -> Path | None:
    """项目根 / `.ai_memory` / 令牌文件 → 顺着身份找到真实的库文件。

    只读：解析失败就返回 `None`，由调用方给出"找不到库"的原话，
    不在这个函数里造目录、造文件、也不抛异常（星图是纯只读工具）。
    """
    try:
        from . import bindings
    except Exception:  # noqa: BLE001 - 极端情况下退化为旧的目录查找
        return None
    try:
        layout = bindings.resolve_readonly(target)
    except Exception:  # noqa: BLE001 - 含 BindingError：都按"找不到"处理
        return None
    if layout.library_dir is None:
        return None
    candidate = layout.library_dir / "workspace.db"
    if candidate.is_file():
        notes.append(
            f"库不在项目里，已按令牌/库根解析到 {layout.library_dir.name}/workspace.db"
        )
        return candidate
    return None


def resolve_db(target: str | Path) -> tuple[Path, list[str]]:
    """把拖进来的路径解析成一个 v5 库文件。

    接受三种：``.ai_memory`` 目录 / 项目根目录 / 直接的 ``.db`` 文件。
    绝不新建目录或文件。
    """
    raw = Path(str(target).strip().strip('"')).expanduser()
    notes: list[str] = []
    if not raw.exists():
        raise StarmapError(f"路径不存在：{raw}")

    if raw.is_file():
        if ".v2-" in raw.name:
            raise StarmapError("这是 v2 留档库（facts 表），星图只支持 v5 的 workspace.db")
        if raw.suffix.lower() != ".db":
            # 只给令牌也认：令牌里有 workspace + library，顺藤摸到库文件
            via_token = _db_via_readonly(raw, notes) if raw.suffix.lower() == ".json" else None
            if via_token is not None:
                db = via_token
            else:
                raise StarmapError(f"拖入的是文件但不是 .db / 令牌：{raw.name}")
        else:
            db = raw
    else:
        candidates = [raw / "workspace.db"]
        db = next((p for p in candidates if p.is_file()), None)
        if db is None:
            # 库已统一放到 <安装根>/libraries/<label>：项目里只剩 token.json。
            # 拖进来的多半是项目根（或 `.ai_memory`），这时按令牌解析库位置。
            db = _db_via_readonly(raw, notes)
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
                    f"{raw} 里找不到 workspace.db；请把项目根目录、"
                    "<安装根>/libraries/<label> 目录，或 workspace.db 本身拖进来"
                )
    files, dir_notes = _describe_dir(db.parent, db)
    notes.extend(dir_notes)
    return db.resolve(), notes


# ---------------------------------------------------------------- 只读连接


def _load_vec(conn: sqlite3.Connection) -> str:
    """加载 sqlite-vec，只读连接上注册函数，不写库。

    扩展从 `sqlite_vec` 包内取 —— 源码态与冻结态同一条路，不必猜项目目录。
    """
    conn.enable_load_extension(True)
    errors: list[str] = []
    try:
        try:
            import sqlite_vec

            path = sqlite_vec.loadable_path()
            conn.load_extension(path)
            return f"pip:sqlite-vec ({path})"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pip: {exc}")
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
            raise StarmapError("这是 v2 schema（facts 表）。星图只支持 v4（knowledge / episodes / logs）")
        raise StarmapError("库里没有 knowledge 表，不是有效的 v4 记忆库")
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


_TABLE_KIND = {"knowledge": "experience", "episodes": "episode", "logs": "log"}


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
            # 遗忘等级：日志表没有这一列（不参与等级机制），先给个临时值 —
            # run() 拿到配置的等级表后会统一换成表中正间那一档，这里只是兜底。
            level_raw = g("forget_level")
            forget_level = int(level_raw) if level_raw is not None else 3
            nodes.append(
                {
                    "id": r["id"],
                    "table": table,
                    "kind": _TABLE_KIND[table],
                    "rowid": r["rowid"],
                    "content": (r["content"] or "")[:800],
                    "tags": [str(t) for t in tags][:12],
                    "day": g("day") or _day_of(created),
                    "at": g("at") or created,
                    "importance": float(g("importance") or 0.4),
                    "access_count": int(g("access_count") or 0),
                    # 活跃度色标的原料：activity 列（检索命中累积，仅知识/经历有）、
                    # 取用次数、最近一次被碰的时间。日志没有 activity 列，
                    # 但 access_count / last_accessed_at 照样有，能算个诚实的「冷」。
                    "activity": float(g("activity") or 0.0),
                    "has_activity": "activity" in keys,
                    "last_accessed_at": g("last_accessed_at"),
                    "scope": g("scope", "workspace") or "workspace",
                    "source": g("source"),
                    "digest_id": g("digest_id"),
                    "session_id": g("session_id"),
                    "invalidated_by": g("invalidated_by"),
                    "live": invalid_at is None,
                    "archived": archived_at is not None,
                    "compression_level": int(g("compression_level") or 0),
                    "forget_level": forget_level,
                    "has_level": level_raw is not None,
                    "embedded": bool(g("embedded")),
                }
            )
    return nodes


# ---------------------------------------------------------------- 活跃度色标

# 辉光颜色按「活跃度」取三锚点渐变（青 0 → 黄 0.5 → 红 1）。
# 活跃度本身不能直接当 0~1 用，于是有两种映射（``--activity-mode`` 选）：
#
#   * ``ratio``（默认）—— **活跃度 ÷ 本档阈值**。不做跨节点归一，颜色就是
#     「本周期攒到升档线的几成」，1.0 恰是下一次裁定的晋升线；
#   * ``rank``  —— **秩归一**（有多少比例的节点比你更不活跃）。分值的量纲是
#     「命中相似度^幂 的累加」，一个被反复检索的老条目能到几十，线性归一后
#     其余节点会全挤在青端；秩归一让颜色沿节点均匀铺开。
#
# 两种口径各有代价：ratio 忠于阈值但在「整库都在零活跃」时会整片青；rank 好看
# 但颜色只说明排名、不说明离晋升多远。选哪个写在 meta 里，产物自己会交代。
ACT_RECENCY_TAU_DAYS = 14.0
DAY_MS = 86_400_000.0
RANK_SIGNAL = "activity + log1p(access) + 新鲜度(τ=14天)"
RATIO_SIGNAL = "activity ÷ 本档升档线"


def activity_score(node: dict[str, Any], now_ms: float) -> float:
    """一个节点的活跃度原始分：三个信号相加，各自校准到「一次典型取用 ≈ 1」。

    * ``activity`` 列 —— 检索命中累积（`excess ** curve_power` 的加总），最硬的信号；
    * ``log1p(access_count)`` —— 被取用次数，1 次 ≈ 0.69、10 次 ≈ 2.40，压掉长尾；
    * 新鲜度 ``exp(-age / 14d)`` —— 最近被动过（取用时间，没有就用录入时间），
      权重上限 1.0，约等于「今天刚碰过」送一次取用的量。

    第三条不是凑数：一个刚建立、还没被检索过的库，前两项全是 0，
    只有新鲜度能让星图说出「哪些是这两天的、哪些是半个月前的」，
    否则整张图会是一片死青，色标等于没接。
    """
    score = float(node.get("activity") or 0.0)
    score += math.log1p(max(0, int(node.get("access_count") or 0)))
    touched = node.get("last_accessed_at") or node.get("at") or 0
    if touched:
        age_days = max(0.0, (now_ms - float(touched)) / DAY_MS)
        score += math.exp(-age_days / ACT_RECENCY_TAU_DAYS)
    return score


def assign_activity_units(
    nodes: list[dict[str, Any]], now_ms: float | None = None
) -> dict[str, Any]:
    """给每个节点写 ``score``（原始分）与 ``act01``（0~1 色标值），就地修改。

    秩归一的三个边界（都有测试盯着）：

    * 最低分恒为 0 —— 一半节点是 0 活跃时，它们就该是冷的青，不能因为
      「同分占多数」被抬到中间色；
    * 同分同色 —— 用 ``bisect_left`` 取同分的最小位次，不搞中点插值；
    * 无从比较时给中性色 0.5 —— 全体同分（含只有一个节点）说明「彼此没有差别」，
      涂成青会假装它们都冷、涂成红会假装都热，中间色才是老实话；
      唯一的例外是分值真的全为 0（无时间戳、无取用、无检索累积），那才是全冷。

    复盘恒星（digest）与原始日志（log）都不参与：前者没有 activity 也没有等级，
    画固定金黄；后者表里压根没有 activity 列，画固定灰（见 :func:`_scored_nodes`）。
    """
    now = float(now_ms if now_ms is not None else time.time() * 1000.0)
    real = _scored_nodes(nodes)
    for node in nodes:
        node.setdefault("act01", 0.0)
        node.setdefault("score", 0.0)

    if not real:
        return {"mode": "rank", "signal": RANK_SIGNAL, "unit": "秩归一",
                "scored": 0, "distinct": 0, "max": 0.0, "min": 0.0}

    scores = [activity_score(n, now) for n in real]
    ordered = sorted(scores)
    lo, hi = ordered[0], ordered[-1]
    span_flat = (hi - lo) <= 1e-12
    unit_flat = 0.5 if hi > 0 else 0.0
    total = len(ordered)

    for node, s in zip(real, scores):
        if span_flat:
            unit = unit_flat
        else:
            unit = bisect.bisect_left(ordered, s) / (total - 1)
        node["act01"] = round(min(1.0, max(0.0, unit)), 4)
        node["score"] = round(float(s), 3)

    for node in nodes:
        if node["kind"] == "digest":
            node["act01"] = 1.0  # 固定金黄，不参与活跃度着色，给满值只为字段完整
    return {
        "mode": "rank",
        "signal": RANK_SIGNAL,
        "unit": "秩归一",
        "scored": total,
        "distinct": len(set(ordered)),
        "max": round(float(hi), 3),
        "min": round(float(lo), 3),
    }


def assign_activity_ratio(
    nodes: list[dict[str, Any]], level_table: list[dict[str, Any]]
) -> dict[str, Any]:
    """``act01`` = 当前活跃度 ÷ 本档阈值，就地写 ``score`` / ``gate`` / ``act01``。

    这是色标的第二种口径（用户 2026-11-17 要求试的）：不做任何跨节点归一，
    0~1 就是这个节点在本档门槛上的进度 ——

    * ``1.0`` 恰好等于升档线，意味着「下一次周期裁定就该升了」；
    * ``0.5`` 攒了一半，还在往上爬；
    * ``0`` 本周期一次都没被检索到。

    分子**必须**是库里的 ``activity`` 列，不能换成 ``activity_score``：
    阈值（``up_threshold``）是生命周期拿 ``activity`` 列比出来的，混入访问次数
    或新鲜度会让「比例」失去定义——分子分母不同量纲，1.0 就不再对应任何真实事件。

    分母取本档的 ``up_threshold``，见 :func:`level_gates`：顶档没有升档线时用
    ``down_threshold`` 兜底；没有等级但确实有 activity 的条目（配置外的残留档位）
    用全表最松的门槛，否则它们会永远钉在 0。

    原始日志不参与 —— 它没有 activity 列，算出来恒为 0，而 0 在这里的含义是
    「本周期没被检索」，对日志是句假话。它画固定灰，见 :func:`_scored_nodes`。

    返回 ``usable=False`` 表示等级表里一个阈值都没有（配置读不到、退回库内档位时
    就是这样），调用方应改用秩归一。
    """
    real = _scored_nodes(nodes)
    for node in nodes:
        node.setdefault("act01", 0.0)
        node.setdefault("score", 0.0)
        node.setdefault("gate", 0.0)

    gates = level_gates(level_table)
    if not gates:
        return {"usable": False, "mode": "ratio", "scored": 0, "distinct": 0,
                "zero": 0, "min": 0.0, "max": 0.0}
    loosest = min(gates.values())

    distinct: set[float] = set()
    zero = 0
    hot: list[float] = []
    used_gates: set[float] = set()
    for node in real:
        level = node.get("forget_level") if node.get("has_level") else None
        gate = gates.get(level) if isinstance(level, int) else None
        # 没有等级，或库里残留的档位不在配置表里 → 用最松的门槛
        # （表里只有一档时它就是那一档，不存在「松紧」可言）。
        if not gate:
            gate = loosest
        activity = float(node.get("activity") or 0.0)
        unit = activity / gate
        node["act01"] = round(min(1.0, max(0.0, unit)), 4)
        node["score"] = round(activity, 3)
        node["gate"] = round(float(gate), 3)
        distinct.add(node["act01"])
        used_gates.add(round(float(gate), 3))
        hot.append(node["act01"])
        if activity <= 0:
            zero += 1

    for node in nodes:
        if node["kind"] == "digest":
            node["act01"] = 1.0  # 固定金黄，不参与活跃度着色，给满值只为字段完整
    return {
        "usable": True,
        "mode": "ratio",
        "signal": RATIO_SIGNAL,
        "unit": "阈值比例",
        "scored": len(real),
        "distinct": len(distinct),
        "zero": zero,
        "min": round(min(hot), 3) if hot else 0.0,
        "max": round(max(hot), 3) if hot else 0.0,
        # 分母范围取「实际用到的那些」而不是整张表：面板上写 34 会让人以为
        # 真有节点拿 L9 的线当分母，实际这个库最高只到 L5。
        "gate_lo": round(min(used_gates), 3) if used_gates else 0.0,
        "gate_hi": round(max(used_gates), 3) if used_gates else 0.0,
        "gate_levels": len(used_gates),
    }


def assign_activity(
    nodes: list[dict[str, Any]],
    level_table: list[dict[str, Any]],
    *,
    mode: str = "ratio",
    now_ms: float | None = None,
) -> dict[str, Any]:
    """色标入口：``ratio`` 走阈值比例，``rank`` 走秩归一。

    ``ratio`` 算不出分母时**自动**退回 ``rank``，并在返回里挂 ``fallback`` 说明，
    不抛异常 —— 配置读不到不该让星图打不开，但也不能悄悄换一套口径。
    """
    if mode == "rank":
        return assign_activity_units(nodes, now_ms)
    stats = assign_activity_ratio(nodes, level_table)
    if not stats.get("usable"):
        ranked = assign_activity_units(nodes, now_ms)
        ranked.update({
            "mode": "rank",
            "signal": RANK_SIGNAL,
            "unit": "秩归一",
            "fallback": "等级表里没有可用阈值，已退回秩归一",
        })
        return ranked
    return stats


def _scored_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """参与活跃度着色的节点 = 有 ``activity`` 列的那些（知识 + 经历）。

    两类要排除，原因不同：

    * ``digest`` 复盘恒星 —— 没有 activity、没有等级，画固定金黄；
    * ``log`` 原始日志 —— 表里根本没有 ``activity`` 列（它不进等级体系，
      复盘完就被清掉）。给它算比例只会得到恒 0 的假值，还会白占一个秩归一的位次，
      画出来是青的「冷」，可它连「冷」都谈不上，只是没这个概念。固定灰更老实。
    """
    return [n for n in nodes if n["kind"] not in ("digest", "log")]


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
  --experience:#6fb6ff; --episode:#b9a5ff;
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
input[type=range]{width:100%;accent-color:var(--experience)}
input[type=text]{width:100%;background:#0a101c;color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:6px 8px;font:inherit;font-size:12px}
button{background:#16223a;color:var(--text);border:1px solid var(--line);border-radius:6px;
  padding:6px 10px;cursor:pointer;font:inherit;font-size:12px;margin:4px 4px 0 0}
button:hover{border-color:var(--experience)}
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
/* 活跃度色标：三锚点与画布里的辉光同源（青 0 → 黄 0.5 → 红 1） */
.glowbar{height:10px;border-radius:5px;margin:4px 0 3px;
  background:linear-gradient(90deg,#0fe0e5 0%,#f6de14 50%,#f10000 100%)}
.glowscale{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
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
      <h2>活跃度色标</h2>
      <div class="glowbar"></div>
      <div class="glowscale"><span>0 冷</span><span>0.5</span><span>1 热</span></div>
      <div class="kv"><span>口径</span><span id="actSignal">-</span></div>
      <div class="kv"><span>分母</span><span id="actGate">-</span></div>
      <div class="kv"><span>色标跨度</span><span id="actRange">-</span></div>
    </div>
    <div class="sec">
      <h2>遗忘等级（辉光透明度）</h2>
      <div id="levels"></div>
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
const C={experience:'#6fb6ff',episode:'#b9a5ff',log:'#8898ad',digest:'#ffe08a'};
const L={experience:'经验',episode:'经历',log:'日志',digest:'复盘'};
const ET={semantic:'语义近邻',invalidates:'失效链',digest:'复盘溯源',tag:'标签共现'};
const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
const tip=document.getElementById('tip');
let W=0,H=0,DPR=1,view={x:0,y:0,k:1};
let alpha=1,frame=0,hover=null,selected=null,dragging=null,panning=null,fixedHover=true,downX=0,downY=0;
const enabled={semantic:true,invalidates:true,digest:true,tag:false};
const kinds={experience:true,episode:true,log:true,digest:true};
let minScore=DATA.meta.min_score;
// 等级量程与整张等级表都由后端从配置（[[forget.levels]]）带过来。
// 不写死档数、也不假设等级是 0~N 连续整数：表的形状归配置管，这里只负责画。
const LVMAX=Math.max(0,(DATA.meta&&DATA.meta.max_level)||0);
// 顶档是 0（空库的单档占位）时，任何比值都该是 0 而不是 NaN —— 单独给个分母。
const LVDEN=Math.max(1,LVMAX);
const LEVELS=(DATA.meta&&DATA.meta.levels&&DATA.meta.levels.length)
  ?DATA.meta.levels:[{level:LVMAX}];
let showLabels=document.getElementById('labels').checked;

// 辉光颜色 = 活跃度，三锚点线性渐变：0 = 青 #0FE0E5，0.5 = 黄 #F6DE14，1 = 红 #F10000。
// 逐通道插值即可：三段跨度都不大（青→黄、黄→红各约 120° 色相），
// 不会像蓝→黄那样穿过灰带。act01 已由后端算好（默认是「活跃度 ÷ 本档阈值」），
// 这里只负责取色，不参与任何归一。
const GLOW_STOPS=[[0,[15,224,229]],[.5,[246,222,20]],[1,[241,0,0]]];
// 原始日志的固定辉光色：跟它的节点本体同色。日志没有 activity 列，不参与色标 ——
// 涂青会被读成「很冷」，可它连冷热都不参与，灰色才是「不适用」的正确表情。
const LOG_GLOW='#8898ad';
function glowColor(t){
  t=Math.max(0,Math.min(1,t||0));
  for(let i=0;i<GLOW_STOPS.length-1;i++){
    const a=GLOW_STOPS[i],b=GLOW_STOPS[i+1];
    if(t<=b[0]||i===GLOW_STOPS.length-2){
      const f=(b[0]===a[0])?0:(t-a[0])/(b[0]-a[0]);
      const c=a[1].map((v,k)=>Math.round(v+(b[1][k]-v)*f));
      return 'rgb('+c[0]+','+c[1]+','+c[2]+')';
    }
  }
  return 'rgb(241,0,0)';
}

function rnd(i){const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);}
const centers={experience:[-.42,-.18],episode:[.42,-.18],log:[0,.22],digest:[0,0]};
const nodes=DATA.nodes.map((n,i)=>{
  const c=centers[n.kind]||[0,0];
  return Object.assign({},n,{
    x:W*.5+c[0]*340+(rnd(i)-.5)*220,
    y:H*.5+c[1]*260+(rnd(i+999)-.5)*220,
    vx:0,vy:0,fixed:false,
    r:3+Math.min(9,Math.max(0,n.importance)*8)+(n.kind==='digest'?3:0),
    // 辉光颜色：复盘恒星固定金黄；原始日志固定灰（与本体同色，不去蹭色标通道）—— 它表里没有 activity 列，
    // 涂青会被读成「很冷」，可它连冷热都不参与，灰色才是「不适用」的正确表情。
    gcol:n.kind==='digest'?'#ffe08a':(n.kind==='log'?LOG_GLOW:glowColor(n.act01||0)),
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
    // 辉光走两个通道，各管一个维度，谁也不顶替谁：
    //   颜色 = 活跃度（青 0 → 黄 0.5 → 红 1，见 glowColor / 侧栏色标）；
    //   透明度 = 遗忘等级（越接近遗忘越淡，像逐渐熄灭的灯）。
    // 本体大小继续由 importance 决定。
    // 透明度与半径都按量程归一：0.02（近熄灭）→ 0.395（顶档最亮）。
    // 档数变多后相邻档的差值自然变小，这是「同样亮度跨度分更多份」的必然代价；
    // 半径同步归一，两个通道一起表达跨度，低档之间仍能靠「灯大不大」区分。
    const lvl=Math.max(0,Math.min(LVMAX,n.forget_level||0));
    const ratio=lvl/LVDEN;
    const glow=n.kind==='digest'?.26:(n.has_level?0.02+ratio*0.375:0.17);
    ctx.beginPath();ctx.arc(n.x,n.y,n.r*(2.05+ratio*0.65),0,6.2832);
    // 颜色跟活跃度走，不再统一白蓝：色相只表达「热不热」，亮度仍由等级单独控制，
    // 两个通道正交，所以「暗而红」（刚沉底的高活跃条目）和「亮而青」（顶档但没人碰）
    // 都能读得出来，不会互相污染。
    ctx.fillStyle=n.gcol;
    ctx.globalAlpha*=glow;ctx.fill();
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
    '<div class="meta">'+
    (n.has_level
      ? '遗忘等级 <b>L'+n.forget_level+'</b>'+(n.forget_level===0?'（遗忘层）':'')+' · '
      : '原始日志（不参与等级） · ')+
    '重要度 '+n.importance.toFixed(2)+' · 访问 '+n.access_count+
    ' · 邻居 '+n.deg+'<br>'+
    (n.kind==='digest'
      ? '复盘恒星（不参与活跃度着色）<br>'
      : n.kind==='log'
        ? '原始日志（灰色 · 不参与活跃度着色）<br>'
        // ratio 口径下把分母摆出来：光看到一个 0.09 没法判断「是懒还是门槛高」，
        // 写成「0.84 ÷ 9.5 = 0.09」就一目了然。rank 口径没有分母，退回旧写法。
        : (n.gate
            ? '活跃度 <b>'+(n.score||0).toFixed(2)+'</b> ÷ 阈值 <b>'+n.gate.toFixed(2)+'</b> = 色标 <b>'+
              (n.act01||0).toFixed(2)+'</b>'
            : '活跃度 <b>'+(n.score||0).toFixed(2)+'</b> · 色标 <b>'+(n.act01||0).toFixed(2)+'</b>')+
          ' <span class="dot" style="background:'+(n.gcol||'#8898ad')+';vertical-align:-1px"></span><br>')+
    (n.day?('日期 '+esc(n.day)+' · '):'')+
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
  // 等级分布：顺手把每个等级的辉光透明度画成一个小圆点 —— 既是统计也是图例，
  // 一眼就能看出「库里有没有跨度」。全挤在 L5 时，这里会诚实地显示 0。
  // 档位、周期都来自配置（LEVELS），不数 0~N：等级表可能不是连续整数，
  // 也可能只有 4 档，循环次数由数据决定。
  const lvCount={};let logN=0;
  DATA.nodes.forEach(n=>{
    if(n.kind==='digest')return;
    if(n.has_level){lvCount[n.forget_level]=(lvCount[n.forget_level]||0)+1;}else{logN++;}
  });
  const lvEl=document.getElementById('levels');
  let lvHtml='';
  LEVELS.forEach(item=>{
    const lv=item.level, c=lvCount[lv]||0;
    const a=(0.02+(lv/LVDEN)*0.375).toFixed(3);
    lvHtml+='<div class="kv"><span><span class="dot" style="background:#dbe8ff;opacity:'+a+'"></span> L'+lv+
      (item.days!=null?' · '+item.days+' 天':'')+
      (lv===0?'（遗忘层）':'')+'</span><span style="color:var(--muted)">'+c+'</span></div>';
  });
  // 配置外的等级（改过表 / 老数据残留）：照样列出来，别让节点在统计里凭空消失
  const knownLevels=new Set(LEVELS.map(x=>x.level));
  Object.keys(lvCount).map(Number).filter(lv=>!knownLevels.has(lv)).sort((a,b)=>b-a).forEach(lv=>{
    lvHtml+='<div class="kv"><span><span class="dot" style="background:#ffc46b"></span> L'+lv+
      '（配置外）</span><span style="color:var(--muted)">'+lvCount[lv]+'</span></div>';
  });
  if(logN)lvHtml+='<div class="kv"><span>日志（无等级）</span><span style="color:var(--muted)">'+logN+'</span></div>';
  // 等级表从哪来的，写在面板上 —— 读到配置和退回库内档位是两种可信度，别让人猜
  const lvSrc={config:'配置 [[forget.levels]]',db:'库内实际档位（未读到配置）',default:'空库占位'}[
    (DATA.meta&&DATA.meta.level_source)||'']||'';
  if(lvSrc)lvHtml+='<div class="kv" style="margin-top:6px"><span>等级表</span><span>'+
    LEVELS.length+' 档 · '+lvSrc+'</span></div>';
  lvEl.innerHTML=lvHtml;
  // 色标口径：把「这个颜色是拿什么算的」写在面板上，而不是留在代码注释里。
  const am=(DATA.meta&&DATA.meta.activity)||{};
  const isRatio=am.mode==='ratio';
  document.getElementById('actSignal').textContent=am.signal||'-';
  // 分母一行只在 ratio 口径下有意义；rank 是按名次分色，没有分母这回事，
  // 与其写个「-」不如直接把口径差别说清。
  document.getElementById('actGate').textContent=isRatio
    ? ((am.gate_lo==null||!am.scored)?'-'
      :(am.gate_lo===am.gate_hi
        ? '全表 '+am.gate_lo
        : '按档 '+am.gate_lo+' ~ '+am.gate_hi+(am.gate_levels?'（'+am.gate_levels+' 档）':'')))
    : '无（跨节点排名）';
  document.getElementById('actRange').textContent=am.scored
    ? ((am.min||0).toFixed(2)+' → '+(am.max||0).toFixed(2)
      +(isRatio&&am.zero?' · '+am.zero+' 个为 0':''))
    : '无节点';
  document.getElementById('legend').innerHTML=
    '<span style="color:#cfe0ff">点大小 = 重要度 · 辉光颜色 = 活跃度 · 辉光透明度 = 遗忘等级</span><br>'+
    '<span style="color:#0fe0e5">　冷 0</span><span style="color:#8292ab"> ─ </span>'+
    '<span style="color:#f6de14">中 0.5</span><span style="color:#8292ab"> ─ </span>'+
    '<span style="color:#f10000">热 1</span><span style="color:#8fa8c8">（'+
    (isRatio?'活跃度 ÷ 本档阈值，1.0 = 升档线':'秩归一，只表示排名')+'）</span><br>'+
    '<span style="color:#8fa8c8">　（辉光越淡越接近遗忘，L'+LVMAX+' 最实 · L0 近乎熄灭）</span><br>'+
    '<span style="color:#6fb6ff">虚线粗细 = 语义相似度</span><br>'+
    '<span style="color:#ffc46b">琥珀箭头 = 失效 / 换代</span><br>'+
    '<span style="color:#ffe08a">黄色恒星 = 复盘批次</span><br>'+
    '<span style="color:'+LOG_GLOW+'">灰色辉光 = 原始日志（无活跃度 / 无等级）</span><br>'+
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


# ---------------------------------------------------------------- 遗忘等级表


def _db_levels(nodes: list[dict[str, Any]]) -> list[int]:
    """库里真实出现过的等级（降序，恒含 0）。配置读不到时的退路。"""
    seen = {int(n["forget_level"]) for n in nodes if n.get("has_level")}
    seen.add(0)
    return sorted(seen, reverse=True)


def _slim_level(item: dict[str, Any]) -> dict[str, Any]:
    """配置里的一档 → 星图要的那几个字段（周期取经验侧，图例上标「几天」）。

    阈值（``up_threshold`` / ``down_threshold``）也带上：活跃度色标要拿它当分母，
    「当前的活跃度相当于本档门槛的几成」——这是色标唯一有物理意义的读法。
    顶档按配置约定没有 ``up_threshold``，缺了就不写，让下游显式退化。
    """
    level = int(item["level"])
    days = item.get("period_experience_days", item.get("period_episode_days"))
    slim: dict[str, Any] = {"level": level}
    if days is not None:
        slim["days"] = int(days)
    if item.get("compress"):
        slim["compress"] = str(item["compress"])
    for key in ("up_threshold", "down_threshold"):
        value = _as_float(item.get(key))
        if value is not None:
            slim[key] = value
    return slim


def _as_float(value: Any) -> float | None:
    """容错取小数：配置可能给 int、字符串，也可能给空。坏值一律当「没有」。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def level_gates(level_table: list[dict[str, Any]]) -> dict[int, float]:
    """每档的「分母」：升档线优先，顶档没有升档线就用它自己的降档线。

    为什么不是降档线：色标要回答的是「离升档还有多远」——分子涨到分母就是一次晋升，
    这才是活跃度在等级体系里唯一被定义过的参照。顶档升无可升，用降档线兜底，
    含义退化成「还稳不稳」，可接受；``None`` 表示这档连门槛都没有，交给调用方退化。
    """
    gates: dict[int, float] = {}
    for item in level_table:
        level = item.get("level")
        if not isinstance(level, int):
            continue
        up = _as_float(item.get("up_threshold"))
        down = _as_float(item.get("down_threshold"))
        gate = up if up and up > 0 else (down if down and down > 0 else None)
        if gate:
            gates[level] = gate
    return gates


def _levels_from_config(
    db: Path, environ: dict[str, str] | None = None
) -> list[dict[str, Any]] | None:
    """读项目配置里的 ``[[forget.levels]]``；读不到/配置坏了一律返回 None。

    等级表是**配置**定义的（档数、每档周期、压缩档），代码里不该有「一共 5 档」
    这种常数。这里只读不写：``Config.load`` 干的活就是找文件 + 解析 + 合并 + 校验。
    """
    from .config import NO_LIBRARY, Config  # 延迟 import：星图本体仍然靠标准库就能跑起来

    # 库目录 = 库文件所在目录：自有库是 `.ai_memory`，共享库是 `libraries/<label>`。
    # 若两者都不像（比如随手拷到临时目录的备份库），宁可**不读**库级配置 ——
    # 让它去猜 cwd 上面那个项目的 memory.toml 只会拿到一份无关的参数。
    parent = db.parent
    if parent.name == ".ai_memory" or (parent / "memory.toml").is_file():
        lib = parent
    else:
        lib = NO_LIBRARY
    try:
        cfg = Config.load(environ, library_dir=lib)
    except Exception:  # noqa: BLE001 —— 配置坏了也不该连星图都打不开，退回库里的档位
        return None
    levels = cfg.forget_levels
    return levels or None


def resolve_levels(
    db: Path,
    nodes: list[dict[str, Any]],
    environ: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str, int]:
    """决定星图的等级轴：返回 ``(降序等级表, 来源, 中性档)``。

    优先级依次降低：

    1. ``config`` —— 项目配置的 ``[[forget.levels]]``，唯一真源。档数、每档周期
       都从这里来，加一档不需要改代码；
    2. ``db``     —— 配置读不到时，用库里出现过的档位凑表。**只画真实存在的档**，
       不用「至少画到 5 档」之类的常数补齐 —— 那会凭空造出库里没有的等级；
    3. ``default``—— 空库（一条带等级的都没有）时给单档占位，免得图例整块空白。

    中性档用于「没有等级列」的日志当占位（透明度本来就固定 0.17，
    这一项只影响光晕半径），取表的正中一档，而不是写死 3。
    """
    configured = _levels_from_config(db, environ)
    if configured:
        table = [_slim_level(item) for item in configured]
        source = "config"
    else:
        db_levels = _db_levels(nodes)
        if any(n.get("has_level") for n in nodes):
            table = [{"level": lv} for lv in db_levels]
            source = "db"
        else:
            table = [{"level": 0}]
            source = "default"

    # 配置表里出现非整数 level 是配置校验该拦的事；真漏了就在这儿剔掉，别让前端吃脏值
    table = [item for item in table if isinstance(item.get("level"), int)]
    table.sort(key=lambda item: int(item["level"]), reverse=True)
    if not table:
        table, source = [{"level": 0}], "default"

    neutral = int(table[len(table) // 2]["level"])
    return table, source, neutral


# ---------------------------------------------------------------- CLI 入口


# 星图默认参数：CLI parser 与 webtool 共用，避免两处各写一份
STARMAP_DEFAULTS: dict[str, Any] = {
    "min_score": 0.55,
    "top_k": 6,
    "max_nodes": 400,
    "activity_mode": "ratio",
}


def _arg(args: Any, name: str) -> Any:
    """从 argparse.Namespace / SimpleNamespace / dict 取参数，缺了用默认值。"""
    if isinstance(args, dict):
        return args.get(name, STARMAP_DEFAULTS[name])
    return getattr(args, name, STARMAP_DEFAULTS[name])


def build_graph_payload(db: Path, args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """纯取数：从库文件构建星图 graph（不写文件、不打印、不解析 target 路径）。

    返回 ``(graph, extras)`` —— ``extras`` 带 CLI 才需要的诊断字段（notes/warnings/
    real_nodes/coverage 等），HTTP 端只取 graph。CLI 的 ``run()`` 是本函数的薄包装，
    两条路径共用同一份取数逻辑，保证浏览器里看到的与 starmap.bat 产物一致。
    """
    conn = open_readonly(db)
    try:
        schema = inspect_schema(conn)
        nodes = read_nodes(conn)
        vecs = read_vectors(conn, schema["embedding_dim"])
        edges, stats = build_graph(
            nodes,
            vecs,
            min_score=_arg(args, "min_score"),
            top_k=_arg(args, "top_k"),
            max_nodes=_arg(args, "max_nodes"),
        )
    finally:
        conn.close()

    db = db.resolve()
    notes: list[str] = []
    return _assemble_graph(db, schema, nodes, vecs, edges, stats, args, notes)


def _assemble_graph(
    db: Path,
    schema: dict[str, Any],
    nodes: list[dict[str, Any]],
    vecs: dict[tuple[str, int], tuple[float, ...]],
    edges: list[dict[str, Any]],
    stats: dict[str, Any],
    args: Any,
    notes: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """组装 graph 与诊断信息（原 run() 的中段，抽出来给 CLI / HTTP 复用）。"""
    started = time.time()
    real_nodes = sum(1 for n in nodes if n["kind"] != "digest")

    level_table, level_source, neutral_level = resolve_levels(db, nodes)
    level_max = int(level_table[0]["level"])
    for node in nodes:
        if node["kind"] != "digest" and not node.get("has_level"):
            node["forget_level"] = neutral_level

    act_stats = assign_activity(
        nodes, level_table, mode=_arg(args, "activity_mode")
    )
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

    if act_stats.get("fallback"):
        warnings.append(str(act_stats["fallback"]))
    if act_stats.get("mode") == "ratio" and act_stats.get("scored"):
        zero = int(act_stats.get("zero") or 0)
        if zero >= int(act_stats["scored"]):
            warnings.append(
                "所有条目的 activity 都是 0（本周期没有检索命中），活跃度色标整片偏青"
            )
        elif zero * 2 >= int(act_stats["scored"]):
            warnings.append(
                f"{zero}/{act_stats['scored']} 条条目的 activity 为 0，色标会偏青那端"
            )

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
            "min_score": _arg(args, "min_score"),
            "max_level": level_max,
            "levels": level_table,
            "level_source": level_source,
            "activity": dict(act_stats),
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dir_files": files,
            "notes": all_notes,
            "warnings": warnings,
        },
    }
    extras = {
        "started": started,
        "real_nodes": real_nodes,
        "covered": covered,
        "warnings": warnings,
        "edge_counts": stats["edge_counts"],
        "semantic_pairs_computed": stats.get("semantic_pairs_computed", 0),
        "digest_nodes": stats["digest_nodes"],
        "level_max": level_max,
        "schema": schema,
        "level_table": level_table,
        "level_source": level_source,
        "all_notes": all_notes,
    }
    return graph, extras


def run(args: argparse.Namespace) -> int:
    started = time.time()
    db, resolve_notes = resolve_db(args.path)
    graph, extras = build_graph_payload(db, args)
    # 把 resolve_db 的 notes 合进 graph（HTTP 路径没有这一步）
    graph["meta"]["notes"] = list(
        dict.fromkeys(resolve_notes + list(graph["meta"].get("notes") or []))
    )
    schema = extras["schema"]
    warnings = extras["warnings"]
    real_nodes = extras["real_nodes"]
    covered = extras["covered"]
    level_table = extras["level_table"]
    level_source = extras["level_source"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    level_max = extras["level_max"]
    stats = {
        "edge_counts": extras["edge_counts"],
        "digest_nodes": extras["digest_nodes"],
        "semantic_pairs_computed": extras["semantic_pairs_computed"],
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
    print(f"  遗忘等级 {len(level_table)} 档（L{level_max} 顶档 · 来源 {level_source}）")
    for w in warnings:
        print(f"  提示：{w}")
    print("  全程只读，未创建或修改任何数据库。")
    return 0
