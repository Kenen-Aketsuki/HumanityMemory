"""v4 → v5 单步转换器。

v4 → v5 唯一的结构变化：`logs` 表新增 `retrievals` 列（TEXT JSON），
记录"这条日志被哪些检索条件命中过"。存量日志一律写 `'[]'`：
历史检索证据只存在于临时 JSONL（后续会删），库里没有可靠来源，**不追溯**。

沿用 upgrade 的离线纪律：
1. 只在 CLI 用，不进 MCP 工具表。
2. 源库只读（`mode=ro`），不 rename、不就地改。
3. `--out` 不给默认值：给了才写，不给只报。
4. 目标结构由本模块声明，不读运行时的 `schema.sql`。
   这里复用 `v3_to_v4` 冻结的 v4 DDL 快照作为底稿（升级器之间互赖
   不违反"不依赖主程序 db/store/models"的边界），再 ALTER 出 v5 的列。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import v3_to_v4

SOURCE_VERSION = 4
TARGET_VERSION = 5

# v4 的完整 DDL 快照来自 v3_to_v4（冻结文件，不会再改）；v5 只多一列。
V5_BASE_SCHEMA = v3_to_v4.V4_SCHEMA

# 逐行搬的普通表。memory_events 是只追加审计表，也必须完整带上。
_TABLES = ("knowledge", "episodes", "logs", "digests", "memory_events")
_VEC_TABLES = ("knowledge", "episodes", "logs")

# 校验口径：v4→v5 是逐表 1:1 + 一列新增，条数与时间戳必须完全对齐。
VERIFY: dict[str, Any] = {
    "counts": (
        ("knowledge", (("knowledge", None),)),
        ("episodes", (("episodes", None),)),
        ("logs", (("logs", None),)),
        ("digests", (("digests", None),)),
    ),
    "timestamps": (
        ("knowledge", "created_at", (("knowledge", "created_at", None),)),
        ("episodes", "at", (("episodes", "at", None),)),
        ("logs", "at", (("logs", "at", None),)),
    ),
    "vectors": _VEC_TABLES,
    "notes": (
        "存量日志 retrievals 统一写 '[]'：历史检索证据只在临时 JSONL，不追溯，从升级时刻起算。",
        "period_start 原样保留（不像 v3→v4 那样重置周期）。",
    ),
}


# --------------------------------------------------------------------------- 预览


def survey(src: sqlite3.Connection) -> dict[str, Any]:
    """只读统计，供预览报告用。不做任何写操作。"""
    counts: dict[str, int] = {}
    for table in _TABLES:
        try:
            counts[table] = int(
                src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            counts[table] = 0

    vec: dict[str, int] = {}
    dims: dict[str, int] = {}
    for table in _VEC_TABLES:
        vec_table = f"{table}_vec"
        try:
            vec[vec_table] = int(
                src.execute(f"SELECT COUNT(*) FROM {vec_table}").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            vec[vec_table] = 0
        dim = v3_to_v4._vec_dim(src, vec_table)
        if dim:
            dims[vec_table] = dim

    return {
        "counts": counts,
        "vectors": vec,
        "vec_dims": dims,
        "notes": [
            "logs.retrievals 新增列，存量写 '[]'；其余表结构与数据原样保留。",
            "历史检索证据只在临时 JSONL（后续会删），不追溯。",
        ],
    }


def probe_tokenizer(conn: sqlite3.Connection) -> str:
    return v3_to_v4.probe_tokenizer(conn)


# --------------------------------------------------------------------------- 转换


def create_target(conn: sqlite3.Connection, *, tokenizer: str) -> None:
    sql = V5_BASE_SCHEMA
    if tokenizer != "trigram":
        sql = sql.replace("tokenize = 'trigram'", f"tokenize = '{tokenizer}'")
    conn.executescript(sql)
    # append-only 触发器只锁 content/at/day/id，ADD COLUMN 不受影响。
    conn.execute("ALTER TABLE logs ADD COLUMN retrievals TEXT NOT NULL DEFAULT '[]'")


def build(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    tokenizer: str,
    period_start: int,
) -> dict[str, Any]:
    """建表 → 搬数据 → 搬向量，一步到位（runner 只调这个入口）。"""
    create_target(dst, tokenizer=tokenizer)
    dims = {
        f"{t}_vec": v3_to_v4._vec_dim(src, f"{t}_vec") or 0 for t in _VEC_TABLES
    }
    made = v3_to_v4._create_vec_tables(dst, dims, v3_to_v4._fallback_dim(src))
    written = convert(src, dst, tokenizer=tokenizer, period_start=period_start)
    written["vectors"] = v3_to_v4.copy_vectors(src, dst)
    written["vec_created"] = made
    written["period_start"] = period_start
    return written


def convert(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    tokenizer: str,
    period_start: int,
) -> dict[str, Any]:
    """把 v4 数据写进已建好 v5 结构的 `dst`。

    `period_start` 在这里**不使用**：v4 已有该列且记录着真实的周期起点，
    重置它等于凭空推迟一次周期裁定。v3→v4 才需要设迁移时刻。
    """
    dst.execute("BEGIN IMMEDIATE")
    try:
        written = _convert_rows(src, dst)
        _copy_meta(src, dst)
        # FTS 是 external content 表，主表数据写完后必须重建索引
        for table in _VEC_TABLES:
            try:
                dst.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES('rebuild')")
            except sqlite3.DatabaseError:
                pass
        dst.execute("COMMIT")
    except BaseException:
        dst.execute("ROLLBACK")
        raise
    written["tokenizer"] = tokenizer
    return written


def _convert_rows(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    """按源库实际列清单逐行搬；目标库只多 logs.retrievals 一列。"""
    written: dict[str, Any] = {}
    for table in _TABLES:
        try:
            columns = [
                str(r[1])
                for r in src.execute(f"PRAGMA table_info({table})").fetchall()
            ]
        except sqlite3.DatabaseError:
            columns = []
        if not columns:
            written[table] = {"read": 0, "written": 0, "skipped": ["源表不存在或无列"]}
            continue

        # 源列里含显式的 rowid（INTEGER PRIMARY KEY AUTOINCREMENT），原样保留
        # 才能和 vec0 表的 rowid 对齐。
        dst_columns = columns + (["retrievals"] if table == "logs" else [])
        marks = ",".join("?" * len(dst_columns))
        sql = f"INSERT INTO {table}({','.join(dst_columns)}) VALUES ({marks})"

        rows = src.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
        skipped: list[str] = []
        for row in rows:
            values = list(row) + (["[]"] if table == "logs" else [])
            try:
                dst.execute(sql, values)
            except sqlite3.IntegrityError as exc:
                skipped.append(str(exc))
        written[table] = {
            "read": len(rows),
            "written": len(rows) - len(skipped),
            "skipped": skipped[:10],
        }
    return written


def _copy_meta(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    """搬 meta，版本号写目标版本；`vec_source` 是运行期信息，不搬。"""
    try:
        rows = src.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.DatabaseError:
        rows = []
    for key, value in rows:
        if key in ("schema_version", "vec_source"):
            continue
        dst.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    dst.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(TARGET_VERSION),),
    )
