"""v2 记忆库（facts 表）→ v3（kind 分级）一次性迁移。

v3 把「句子」按生命周期拆成 kind：knowledge / state（陈述层）与
case / narrative（经历层）。v2 的 facts 表混装全部，没有这个信息，
所以这里按条目逐条判定，映射表就写在下面 —— 可审、可复算。

**判据**（与 skills/humanity-memory/SKILL.md 同一套）：
  knowledge  换掉单号 / 任务 ID / 资源名仍然成立，能指出代码落点
  state      当前为真但会变（版本、依赖、路径、资源配置），≤200 字
  case       绑一次具体事件，压成「现象 → 根因 → 落点」，≤300 字

用法：
    python tools/migrate_v2_v3.py --src D:/proj/YourProject/.ai_memory/workspace.db
        → 只构建 <src 同目录>/workspace.next.db 并校验，原库一个字节不动

    python tools/migrate_v2_v3.py --src ... --apply
        → 校验通过后做三文件交换（.db / -wal / -shm 一起换），失败自动回滚

向量不重新算：旧库 facts_vec 里的 blob 与 v3 同模型同维度，直接搬过去。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from humanity_memory.config import Config  # noqa: E402
from humanity_memory.db import get_meta, load_vec_extension, open_db  # noqa: E402


# ---------------------------------------------------------------- 条目判定

# facts.id → 目标 kind。没列在这里的一律 knowledge。
FACT_KIND: dict[str, str] = {
    # —— state：当前为真、随版本或环境变化 ——
    "f_5061944484e2": "state",  # Unity 2022.3.62f2c1
    "f_6fe10b448d79": "state",  # 热更走 HybridCLR
    "f_4befca0f98a4": "state",  # 打包走 AssetBundleCollector
    "f_38941cd0290e": "state",  # 集成 unity-mcp / adb-debugger 本地包
    "f_6447afb24061": "state",  # 三个基础点击音资源已挂好
    "f_ee1fdc0b01d7": "state",  # 本项目记忆库实体路径与禁改约定
    "f_cefb4dbf3f52": "state",  # 委托品质后端 6 档压 3 档（协议现状）
    # —— case：绑单号 / 绑一次事件 ——
    "f_88dca79c8a02": "case",  # #11809 根因之一：20s 静默锁
    "f_dc65b0a75743": "case",  # #11809 根因之二：OnThirdLoginFail 被门控
    "f_50857d565b12": "case",  # #11810 根因：先 PopPanel 再 ResetClient
    "f_9d2e409df6b6": "case",  # #11810 重连兜底方案落地
    "f_982cfae99c7e": "case",  # D5 音效清单范围（一次性任务）
    "f_80b236cc8f5c": "case",  # #11707 根因：Content_Preset 挂在底栏节点 B
    "f_a912a2099b60": "case",  # #11707 预制体改动结果
    "f_6decca975348": "case",  # #11707 ShowPresetPanel 现实现
    "f_827965f127de": "case",  # #11707 回退方案
    "f_e2521a64080e": "case",  # #10805 StockBread 三条静默出口（绑任务 40001）
    "f_31b23e0fc4b7": "case",  # #10805 修复方向
    "f_cfae6e0b6ed3": "case",  # #11088 根因：角标耦合 obtained
    "f_df64165dbf19": "case",  # #11088 改动结果
    "f_cd8bcdeb791b": "case",  # #11088 图鉴配置量参考
    "f_e3a8ea457ebc": "case",  # #11088 遗留项
    "f_aeb12e179f17": "case",  # 委托品质改档的资源删除与残留
    "f_24d9fe9545a1": "case",  # #11819 数据问题定位
    "f_a4f01b9be80b": "case",  # #11819 按 asset 复核
    "f_ae7af623ac79": "case",  # #11819 asset 与 xlsx 对比结论
}

# 已存在的 episodes：多事件日记式叙述保留 narrative，单事件压成 case。
EPISODE_KIND: dict[str, str] = {
    "e_33e69c35b124": "case",  # #10805 单事件叙述
    "e_26e6e192a8e6": "case",  # #11707 单事件叙述
}


class MigrateError(RuntimeError):
    pass


# ---------------------------------------------------------------- 读旧库


def _day_of(ms: int | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime((ms or 0) / 1000))


def _open_legacy(path: Path, cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn, cfg)
    if get_meta(conn, "schema_version") not in (None, "2", "1"):
        raise MigrateError(f"源库 schema_version={get_meta(conn, 'schema_version')}，不像 v2")
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    if "facts" not in tables:
        raise MigrateError("源库里没有 facts 表，不是 v2 库或已迁移过")
    return conn


def _vecs(conn: sqlite3.Connection, table: str) -> dict[int, bytes]:
    """旧向量表 rowid → blob。读不出来就返回空（后面补 embedding）。"""
    try:
        return {int(r[0]): bytes(r[1]) for r in conn.execute(f"select rowid, embedding from {table}")}
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------- 建新库


def build(src: Path, dst: Path, cfg: Config) -> dict[str, int]:
    if dst.exists():
        dst.unlink()
    for suffix in ("-wal", "-shm"):
        side = dst.with_name(dst.name + suffix)
        if side.exists():
            side.unlink()

    old = _open_legacy(src, cfg)
    new = open_db(dst, cfg)

    counts = {"knowledge": 0, "state": 0, "case": 0, "narrative": 0, "log": 0, "digest": 0}
    fact_vec = _vecs(old, "facts_vec")
    ep_vec = _vecs(old, "episodes_vec")
    log_vec = _vecs(old, "logs_vec")

    new.execute("BEGIN IMMEDIATE")
    try:
        # ---- facts：knowledge / state 进 knowledge 表，case 进 episodes 表
        for row in old.execute("select rowid, * from facts"):
            kind = FACT_KIND.get(row["id"], "knowledge")
            vec = fact_vec.get(int(row["rowid"]))
            if kind in ("knowledge", "state"):
                if kind == "state" and len(row["content"]) > 200:
                    kind = "knowledge"  # 超 200 字装不进 state，退回知识
                cur = new.execute(
                    "INSERT INTO knowledge(id, kind, content, tags, importance, scope, source,"
                    " created_at, updated_at, last_accessed_at, access_count, valid_at,"
                    " invalid_at, invalidated_by, verified_at, archived_at, embedded, meta)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"], kind, row["content"], row["tags"], row["importance"],
                        row["scope"], row["source"], row["created_at"], row["updated_at"],
                        row["last_accessed_at"], row["access_count"], row["valid_at"],
                        row["invalid_at"], row["invalidated_by"],
                        None,  # verified_at：v2 没这个概念，现状从此刻开始计时
                        row["archived_at"], 1 if vec else row["embedded"], row["meta"],
                    ),
                )
                if vec:
                    new.execute(
                        "INSERT INTO knowledge_vec(rowid, embedding) VALUES (?,?)",
                        (int(cur.lastrowid), vec),
                    )
            else:
                if len(row["content"]) > 300:
                    kind = "narrative"  # 超 300 字不能硬当案例
                cur = new.execute(
                    "INSERT INTO episodes(id, kind, content, at, day, tags, importance,"
                    " access_count, created_at, last_accessed_at, digest_id, archived_at,"
                    " compression_level, embedded, meta)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"], kind, row["content"], row["created_at"],
                        _day_of(row["created_at"]), row["tags"], row["importance"],
                        row["access_count"], row["created_at"], row["last_accessed_at"],
                        None, row["archived_at"], 2, 1 if vec else 0, row["meta"],
                    ),
                )
                if vec:
                    new.execute(
                        "INSERT INTO episodes_vec(rowid, embedding) VALUES (?,?)",
                        (int(cur.lastrowid), vec),
                    )
            counts[kind] += 1

        # ---- episodes：原样搬，只补 kind
        for row in old.execute("select rowid, * from episodes"):
            kind = EPISODE_KIND.get(row["id"], "narrative")
            vec = ep_vec.get(int(row["rowid"]))
            cur = new.execute(
                "INSERT INTO episodes(id, kind, content, at, day, tags, importance,"
                " access_count, created_at, last_accessed_at, digest_id, archived_at,"
                " compression_level, embedded, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], kind, row["content"], row["at"], row["day"], row["tags"],
                    row["importance"], row["access_count"], row["created_at"],
                    row["last_accessed_at"], row["digest_id"], row["archived_at"],
                    row["compression_level"], 1 if vec else row["embedded"], row["meta"],
                ),
            )
            if vec:
                new.execute(
                    "INSERT INTO episodes_vec(rowid, embedding) VALUES (?,?)",
                    (int(cur.lastrowid), vec),
                )
            counts[kind] += 1

        # ---- logs：表结构没变，整行搬
        for row in old.execute("select rowid, * from logs"):
            vec = log_vec.get(int(row["rowid"]))
            cur = new.execute(
                "INSERT INTO logs(id, at, day, content, tags, session_id, importance,"
                " access_count, last_accessed_at, consolidated, digest_id, embedded, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["at"], row["day"], row["content"], row["tags"],
                    row["session_id"], row["importance"], row["access_count"],
                    row["last_accessed_at"], row["consolidated"], row["digest_id"],
                    1 if vec else row["embedded"], row["meta"],
                ),
            )
            if vec:
                new.execute(
                    "INSERT INTO logs_vec(rowid, embedding) VALUES (?,?)",
                    (int(cur.lastrowid), vec),
                )
            counts["log"] += 1

        # ---- digests：fact_count → knowledge_count
        for row in old.execute("select * from digests"):
            new.execute(
                "INSERT INTO digests(id, day, at, log_count, knowledge_count,"
                " episode_count, summary, purged, model, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["day"], row["at"], row["log_count"], row["fact_count"],
                    row["episode_count"], row["summary"], row["purged"], row["model"],
                    row["meta"],
                ),
            )
            counts["digest"] += 1
    except BaseException:
        new.execute("ROLLBACK")
        raise
    else:
        new.execute("COMMIT")

    new.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    old.close()
    new.close()
    return counts


# ---------------------------------------------------------------- 校验


def verify(dst: Path, cfg: Config) -> dict[str, object]:
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    load_vec_extension(conn, cfg)
    out: dict[str, object] = {}

    out["schema_version"] = get_meta(conn, "schema_version")
    out["tables"] = sorted(
        r[0] for r in conn.execute("select name from sqlite_master where type='table'")
    )
    out["knowledge"] = {
        r["kind"]: r["n"] for r in conn.execute(
            "select kind, count(*) n from knowledge group by kind"
        )
    }
    out["episodes"] = {
        r["kind"]: r["n"] for r in conn.execute(
            "select kind, count(*) n from episodes group by kind"
        )
    }
    out["logs"] = conn.execute("select count(*) from logs").fetchone()[0]
    out["digests"] = conn.execute("select count(*) from digests").fetchone()[0]
    out["vec"] = {
        t: conn.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("knowledge_vec", "episodes_vec", "logs_vec")
    }
    # 失效链是否保住
    out["invalid"] = [
        dict(r) for r in conn.execute(
            "select id, invalidated_by from knowledge where invalid_at is not null"
        )
    ]
    # FTS 是否灌进去了（trigram 只索引 ≥3 字词元，2 字词走 store 里的 bigram 兜底，
    # 所以这里用长词元探针，别用「引导」「抽卡」这种）
    fts = conn.execute("select count(*) from knowledge_fts").fetchone()[0]
    out["knowledge_fts"] = fts
    probes: dict[str, int] = {}
    for term in ("HybridCLR", "ExecuteEvents", "AssetBundleCollector", "DataSheet"):
        try:
            probes[term] = conn.execute(
                "select count(*) from knowledge_fts where knowledge_fts match ?", (term,)
            ).fetchone()[0]
        except sqlite3.Error as exc:  # pragma: no cover
            probes[term] = f"ERR {exc}"
    out["fts_probes"] = probes
    conn.close()
    return out


# ---------------------------------------------------------------- 交换


def swap(src: Path, built: Path) -> list[str]:
    """把 src 三件套改名留档，再把 built 顶上。任一步失败整体回滚。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archived = src.with_name(f"{src.name}.v2-{stamp}")
    moved: list[tuple[Path, Path]] = []
    steps = [(src, archived)]
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            steps.append((side, archived.with_name(archived.name + suffix)))

    try:
        for a, b in steps:
            a.rename(b)
            moved.append((a, b))
        built.rename(src)
    except OSError as exc:
        for a, b in reversed(moved):
            try:
                b.rename(a)
            except OSError:
                pass
        raise MigrateError(
            f"文件交换失败（{exc.__class__.__name__}: {exc}），已回滚。"
            "多半是还有进程开着这个库——关掉 MCP 客户端 / 重启 WorkBuddy 后重跑本脚本。"
            f"已构建好的新库保留在 {built}"
        ) from exc

    return [str(b) for _, b in moved]


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="v2 记忆库 → v3（kind 分级）迁移")
    ap.add_argument("--src", required=True, help="v2 的 workspace.db 路径")
    ap.add_argument("--dst", default=None, help="新库路径，默认同目录 workspace.next.db")
    ap.add_argument("--apply", action="store_true", help="校验通过后交换文件（默认只构建）")
    args = ap.parse_args(argv)

    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve() if args.dst else src.with_name("workspace.next.db")
    cfg = Config.load()

    print(f"源库  : {src}")
    print(f"新库  : {dst}")
    print(f"模型  : {cfg.embed_model} / {cfg.embed_dim} 维")

    counts = build(src, dst, cfg)
    print("\n写入统计：", ", ".join(f"{k}={v}" for k, v in counts.items() if v))

    info = verify(dst, cfg)
    print("\n校验：")
    for key in ("schema_version", "knowledge", "episodes", "logs", "digests", "vec",
                "knowledge_fts", "fts_probes", "invalid"):
        print(f"  {key:<16} {info[key]}")

    old_size = src.stat().st_size
    new_size = dst.stat().st_size
    print(f"\n体积：{old_size/1024/1024:.1f} MB → {new_size/1024/1024:.2f} MB")

    problems = []
    if info["schema_version"] != "3":
        problems.append("schema_version 不是 3")
    if sum(info["knowledge"].values()) + sum(info["episodes"].values()) != (
        sum(counts.get(k, 0) for k in ("knowledge", "state", "case", "narrative"))
    ):
        problems.append("条目数与写入统计不符")
    if not any(isinstance(v, int) and v > 0 for v in info["fts_probes"].values()):
        problems.append("FTS 检索无命中")
    if problems:
        print("\n[FAIL] " + "；".join(problems))
        return 1

    print("\n[OK] 校验通过")
    if not args.apply:
        print(f"原库未改动。确认无误后加 --apply 完成交换。")
        return 0

    archived = swap(src, dst)
    print(f"[OK] 已切换，旧库留档：{archived[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
