"""v3 → v4 单步转换器。

**这是离线升级器，不是运行时适配层。** 它故意不 import `db` / `store` /
`models` / `lifecycle`，也不依赖 `Config` / `Layout`（§8.4 边界 2）：

    「旧 schema 的 DDL 与字段映射自带字面量。升级器是冻结的快照，
      永远按写它那天的规则理解旧数据。」

代价是下面要重复几十行表名与默认值；换来的是主程序怎么改都不会让升级器失效。
未来出现 v5 时新增 `v4_to_v5.py`，各自带自己的源/目标结构，不要在这里让步。
"""

from __future__ import annotations

import sqlite3
from typing import Any

SOURCE_VERSION = 3
TARGET_VERSION = 4

# ---------------------------------------------------------------------------
# v3 的结构快照 —— 只用来**读**旧库，缺列时按这里的默认值兜底
# ---------------------------------------------------------------------------

V3_KNOWLEDGE_COLUMNS: dict[str, Any] = {
    "id": None,
    "kind": "knowledge",
    "content": "",
    "tags": "[]",
    "importance": 0.8,
    "scope": "workspace",
    "source": None,
    "created_at": 0,
    "updated_at": 0,
    "last_accessed_at": None,
    "access_count": 0,
    "valid_at": None,
    "invalid_at": None,
    "invalidated_by": None,
    "verified_at": None,
    "state_interval_days": 7.0,
    "archived_at": None,
    "embedded": 0,
    "meta": "{}",
}

V3_EPISODE_COLUMNS: dict[str, Any] = {
    "id": None,
    "kind": "narrative",
    "content": "",
    "at": 0,
    "day": "",
    "tags": "[]",
    "importance": 0.4,
    "access_count": 0,
    "created_at": 0,
    "last_accessed_at": None,
    "digest_id": None,
    "archived_at": None,
    "compression_level": 0,
    "embedded": 0,
    "meta": "{}",
}

V3_LOG_COLUMNS: dict[str, Any] = {
    "id": None,
    "at": 0,
    "day": "",
    "content": "",
    "tags": "[]",
    "session_id": None,
    "importance": 0.3,
    "access_count": 0,
    "last_accessed_at": None,
    "consolidated": 0,
    "digest_id": None,
    "embedded": 0,
    "meta": "{}",
}

V3_DIGEST_COLUMNS: dict[str, Any] = {
    "id": None,
    "day": "",
    "at": 0,
    "log_count": 0,
    "knowledge_count": 0,
    "episode_count": 0,
    "summary": None,
    "purged": 0,
    "model": None,
    "meta": "{}",
}

# ---------------------------------------------------------------------------
# 初值映射（§8.1）—— 固定数，不做 AI 批量打分
# ---------------------------------------------------------------------------

# 可信度起点：机制性内容高、易变的现状低、压过的定论中等偏中
CONFIDENCE_BY_KIND: dict[str, float] = {
    "knowledge": 0.6,
    "state": 0.3,
    "case": 0.5,
}

# 出厂 initial.bands 的快照（min_importance 降序 → level）。
# 注意这是**写这份文件时**的出厂配置，不读 TOML —— 升级结果必须可复现。
INITIAL_BANDS: tuple[tuple[float, int], ...] = (
    (0.55, 4),
    (0.30, 3),
    (0.00, 2),
)
INITIAL_LEVEL_FALLBACK = 2

# ---------------------------------------------------------------------------
# v4 的目标结构 —— 完整 DDL 快照，不从 schema.sql 读
# ---------------------------------------------------------------------------

V4_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    kind             TEXT    NOT NULL DEFAULT 'knowledge'
                     CHECK (kind IN ('knowledge', 'state')),
    content          TEXT    NOT NULL,
    tags             TEXT    NOT NULL DEFAULT '[]',
    importance       REAL    NOT NULL DEFAULT 0.8,
    scope            TEXT    NOT NULL DEFAULT 'workspace',
    source           TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    last_accessed_at INTEGER,
    access_count     INTEGER NOT NULL DEFAULT 0,
    valid_at         INTEGER,
    invalid_at       INTEGER,
    invalidated_by   TEXT,
    verified_at      INTEGER,
    state_interval_days REAL NOT NULL DEFAULT 7.0,
    archived_at      INTEGER,
    embedded         INTEGER NOT NULL DEFAULT 0,
    forget_level     INTEGER NOT NULL DEFAULT 3,
    confidence       REAL    NOT NULL DEFAULT 0.3,
    activity         REAL    NOT NULL DEFAULT 0,
    period_start     INTEGER,
    level0_since     INTEGER,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_knowledge_live     ON knowledge(invalid_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind     ON knowledge(kind);
CREATE INDEX IF NOT EXISTS idx_knowledge_created  ON knowledge(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedded ON knowledge(embedded) WHERE embedded = 0;
CREATE INDEX IF NOT EXISTS idx_knowledge_archived ON knowledge(archived_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_forget   ON knowledge(forget_level);

CREATE TABLE IF NOT EXISTS episodes (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    kind             TEXT    NOT NULL DEFAULT 'narrative'
                     CHECK (kind IN ('case', 'narrative')),
    content          TEXT    NOT NULL,
    at               INTEGER NOT NULL,
    day              TEXT    NOT NULL,
    tags             TEXT    NOT NULL DEFAULT '[]',
    importance       REAL    NOT NULL DEFAULT 0.4,
    access_count     INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    last_accessed_at INTEGER,
    digest_id        TEXT,
    archived_at      INTEGER,
    compression_level INTEGER NOT NULL DEFAULT 0,
    embedded         INTEGER NOT NULL DEFAULT 0,
    forget_level     INTEGER NOT NULL DEFAULT 3,
    activity         REAL    NOT NULL DEFAULT 0,
    period_start     INTEGER,
    revive_hits      INTEGER NOT NULL DEFAULT 0,
    level0_since     INTEGER,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_episodes_day      ON episodes(day DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_at       ON episodes(at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_kind     ON episodes(kind);
CREATE INDEX IF NOT EXISTS idx_episodes_embedded ON episodes(embedded) WHERE embedded = 0;
CREATE INDEX IF NOT EXISTS idx_episodes_archived ON episodes(archived_at);
CREATE INDEX IF NOT EXISTS idx_episodes_forget   ON episodes(forget_level);

CREATE TABLE IF NOT EXISTS logs (
    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    at           INTEGER NOT NULL,
    day          TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    tags         TEXT    NOT NULL DEFAULT '[]',
    session_id   TEXT,
    importance   REAL    NOT NULL DEFAULT 0.3,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at INTEGER,
    consolidated INTEGER NOT NULL DEFAULT 0,
    digest_id    TEXT,
    embedded     INTEGER NOT NULL DEFAULT 0,
    meta         TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_logs_day      ON logs(day DESC, at);
CREATE INDEX IF NOT EXISTS idx_logs_pending  ON logs(consolidated, day);
CREATE INDEX IF NOT EXISTS idx_logs_embedded ON logs(embedded) WHERE embedded = 0;

CREATE TABLE IF NOT EXISTS digests (
    id            TEXT PRIMARY KEY,
    day           TEXT    NOT NULL,
    at            INTEGER NOT NULL,
    log_count     INTEGER NOT NULL DEFAULT 0,
    knowledge_count INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    purged        INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    meta          TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_digests_day ON digests(day DESC);

CREATE TABLE IF NOT EXISTS memory_events (
    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    table_name TEXT    NOT NULL
               CHECK (table_name IN ('knowledge', 'episodes', 'logs')),
    item_id    TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    from_value TEXT,
    to_value   TEXT,
    delta      REAL,
    reason     TEXT,
    meta       TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_item ON memory_events(table_name, item_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_events_at   ON memory_events(at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON memory_events(event, at DESC);

CREATE TRIGGER IF NOT EXISTS memory_events_append_only
BEFORE UPDATE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events 是只追加的审计表：禁止修改历史事件');
END;

CREATE TRIGGER IF NOT EXISTS memory_events_no_delete
BEFORE DELETE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events 是只追加的审计表：禁止删除历史事件');
END;

CREATE TRIGGER IF NOT EXISTS logs_append_only
BEFORE UPDATE ON logs
WHEN OLD.content <> NEW.content
  OR OLD.at      <> NEW.at
  OR OLD.day     <> NEW.day
  OR OLD.id      <> NEW.id
BEGIN
    SELECT RAISE(ABORT, 'logs 表是 append-only：禁止修改已写入的历史内容');
END;

CREATE TRIGGER IF NOT EXISTS logs_no_delete_unconsolidated
BEFORE DELETE ON logs
WHEN OLD.consolidated = 0
BEGIN
    SELECT RAISE(ABORT, '未复盘的日志不可删除，请先执行 consolidate');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_no_revive
BEFORE UPDATE ON knowledge
WHEN OLD.invalid_at IS NOT NULL AND NEW.invalid_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'knowledge 的失效是单向的：已失效条目不能被复活，请写入新条目');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_content_immutable
BEFORE UPDATE OF content ON knowledge
WHEN OLD.content <> NEW.content
BEGIN
    SELECT RAISE(ABORT, 'knowledge 正文不可修改：请追加新条目并使旧条目失效');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_kind_immutable
BEFORE UPDATE OF kind ON knowledge
WHEN OLD.kind <> NEW.kind
BEGIN
    SELECT RAISE(ABORT, 'kind 不可修改：knowledge 与 state 的生命周期不同，请写入新条目');
END;

CREATE TRIGGER IF NOT EXISTS episodes_kind_immutable
BEFORE UPDATE OF kind ON episodes
WHEN OLD.kind <> NEW.kind
BEGIN
    SELECT RAISE(ABORT, 'kind 不可修改：请新建条目');
END;

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, tags,
    content = 'knowledge', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE OF content, tags ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    content, tags,
    content = 'episodes', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE OF content, tags ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO episodes_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
    content, tags,
    content = 'logs', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS logs_fts_ai AFTER INSERT ON logs BEGIN
    INSERT INTO logs_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS logs_fts_ad AFTER DELETE ON logs BEGIN
    INSERT INTO logs_fts(logs_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
"""

# 日志表没有 UPDATE content 的 FTS 同步：logs 是 append-only，正文永不变

# ---------------------------------------------------------------------------
# 校验口径 —— 给 runner 的 `_verify` 用，声明"这一步的哪张表从哪来"
#
#   counts     : (目标表, ((源表, 过滤), ...))
#   timestamps : (目标表, 目标字段, ((源表, 源字段, 过滤), ...))
#   vectors    : 目标侧的向量表前缀
#
# 这一步是逐表 1:1 改名，所以每条都只有一个源表、不带过滤。
# ---------------------------------------------------------------------------

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
    "vectors": ("knowledge", "episodes", "logs"),
    "notes": (),
}


# ---------------------------------------------------------------------------


def initial_level(importance: Any) -> int:
    try:
        value = float(importance)
    except (TypeError, ValueError):
        return INITIAL_LEVEL_FALLBACK
    for floor, level in INITIAL_BANDS:
        if value >= floor:
            return level
    return INITIAL_LEVEL_FALLBACK


def initial_confidence(kind: str) -> float:
    return CONFIDENCE_BY_KIND.get(kind, 0.5)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(r[1]) for r in rows}


def _pick(row: dict[str, Any], want: dict[str, Any]) -> dict[str, Any]:
    """按目标列清单取值，源库缺列时用快照默认值补齐。"""
    return {k: (row.get(k) if k in row else v) for k, v in want.items()}


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(f"SELECT rowid AS __rowid, * FROM {table}")
    except sqlite3.DatabaseError:
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, raw)) for raw in cur.fetchall()]


# --------------------------------------------------------------------------- 预览


def survey(src: sqlite3.Connection) -> dict[str, Any]:
    """只读统计，供预览报告用。不做任何写操作。"""
    counts: dict[str, int] = {}
    for table in ("knowledge", "episodes", "logs", "digests"):
        try:
            counts[table] = int(
                src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            counts[table] = 0

    by_kind: dict[str, int] = {}
    try:
        for kind, n in src.execute(
            "SELECT kind, COUNT(*) FROM knowledge GROUP BY kind"
        ).fetchall():
            by_kind[f"knowledge.{kind}"] = int(n)
    except sqlite3.DatabaseError:
        pass
    try:
        for kind, n in src.execute(
            "SELECT kind, COUNT(*) FROM episodes GROUP BY kind"
        ).fetchall():
            by_kind[f"episode.{kind}"] = int(n)
    except sqlite3.DatabaseError:
        pass

    vec: dict[str, int] = {}
    dims: dict[str, int] = {}
    for table in ("knowledge", "episodes", "logs"):
        vec_table = f"{table}_vec"
        try:
            vec[vec_table] = int(
                src.execute(f"SELECT COUNT(*) FROM {vec_table}").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            vec[vec_table] = 0
        dim = _vec_dim(src, vec_table)
        if dim:
            dims[vec_table] = dim

    level_plan: dict[str, int] = {}
    for row in _rows(src, "knowledge") + _rows(src, "episodes"):
        lvl = initial_level(row.get("importance"))
        level_plan[f"level_{lvl}"] = level_plan.get(f"level_{lvl}", 0) + 1

    return {
        "counts": counts,
        "by_kind": by_kind,
        "vectors": vec,
        "vec_dims": dims,
        "level_plan": dict(sorted(level_plan.items())),
        "confidence_plan": {
            "knowledge": sum(
                1 for r in _rows(src, "knowledge") if r.get("kind") == "knowledge"
            ),
            "state": sum(1 for r in _rows(src, "knowledge") if r.get("kind") == "state"),
            "case": sum(1 for r in _rows(src, "episodes") if r.get("kind") == "case"),
            "narrative(派生不存)": sum(
                1 for r in _rows(src, "episodes") if r.get("kind") == "narrative"
            ),
        },
    }


def _vec_dim(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ? AND type = 'table'", (table,)
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row or not row[0]:
        return None
    import re

    match = re.search(r"float\s*\[\s*(\d+)\s*\]", str(row[0]), re.IGNORECASE)
    return int(match.group(1)) if match else None


def probe_tokenizer(conn: sqlite3.Connection) -> str:
    for tokenizer in ("trigram", "unicode61"):
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE temp.__hm_probe USING fts5(x, tokenize='{tokenizer}')"
            )
            conn.execute("DROP TABLE temp.__hm_probe")
            return tokenizer
        except sqlite3.DatabaseError:
            continue
    return "unicode61"


# --------------------------------------------------------------------------- 转换


def create_target(conn: sqlite3.Connection, *, tokenizer: str) -> None:
    sql = V4_SCHEMA
    if tokenizer != "trigram":
        sql = sql.replace("tokenize = 'trigram'", f"tokenize = '{tokenizer}'")
    conn.executescript(sql)


def _create_vec_tables(conn: sqlite3.Connection, dims: dict[str, int], fallback: int) -> dict[str, int]:
    made: dict[str, int] = {}
    for table in ("knowledge", "episodes", "logs"):
        vec_table = f"{table}_vec"
        dim = dims.get(vec_table) or fallback
        conn.execute(
            f"CREATE VIRTUAL TABLE {vec_table} USING vec0("
            f"embedding float[{dim}] distance_metric=cosine, chunk_size=64)"
        )
        made[vec_table] = dim
    return made


def _fallback_dim(src: sqlite3.Connection) -> int:
    """源库一张向量表都读不到维度时的兜底：读 meta，再不行 1024。"""
    try:
        row = src.execute(
            "SELECT value FROM meta WHERE key = 'embedding_dim'"
        ).fetchone()
        if row and str(row[0]).isdigit():
            return int(row[0])
    except sqlite3.DatabaseError:
        pass
    return 1024


def build(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    tokenizer: str,
    period_start: int,
) -> dict[str, Any]:
    """建表 → 搬数据 → 搬向量，一步到位。

    runner 只调这一个入口，各步之间才可能串成链：链上每一步的产物既是
    下一步的输入，中间状态（维度、rowid 映射）不该漏到 runner 去管。
    """
    create_target(dst, tokenizer=tokenizer)
    dims = {
        f"{t}_vec": _vec_dim(src, f"{t}_vec") or 0
        for t in ("knowledge", "episodes", "logs")
    }
    made = _create_vec_tables(dst, dims, _fallback_dim(src))
    written = convert(src, dst, tokenizer=tokenizer, period_start=period_start)
    written["vectors"] = copy_vectors(src, dst)
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
    """把 v3 的数据写进已建好 v4 结构的 `dst`。

    `period_start` 统一设为迁移时刻：若沿用 `created_at`（可能是一年前），
    第一次周期裁定会让全部条目同时到期、集体降级。周期从"升级完这一刻"重新起算。
    """
    dst.execute("BEGIN IMMEDIATE")
    try:
        written = _convert_rows(src, dst, period_start=period_start)
        _copy_meta(src, dst)
        # FTS 是 external content 表，主表数据写完后必须重建索引；
        # 放在同一个事务里，避免"数据进去了、索引还是空的"这种半成品状态。
        for table in ("knowledge", "episodes", "logs"):
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


def _convert_rows(
    src: sqlite3.Connection, dst: sqlite3.Connection, *, period_start: int
) -> dict[str, Any]:
    written: dict[str, Any] = {}

    # ---- knowledge ----
    rows = _rows(src, "knowledge")
    k_sql = (
        "INSERT INTO knowledge(rowid, id, kind, content, tags, importance, scope, source,"
        " created_at, updated_at, last_accessed_at, access_count, valid_at, invalid_at,"
        " invalidated_by, verified_at, state_interval_days, archived_at, embedded,"
        " forget_level, confidence, activity, period_start, level0_since, meta)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    skipped: list[str] = []
    for raw in rows:
        row = _pick(raw, V3_KNOWLEDGE_COLUMNS)
        kind = str(row["kind"] or "knowledge")
        try:
            dst.execute(
                k_sql,
                (
                    raw["__rowid"],
                    row["id"],
                    kind,
                    row["content"],
                    row["tags"],
                    row["importance"],
                    row["scope"],
                    row["source"],
                    row["created_at"],
                    row["updated_at"],
                    row["last_accessed_at"],
                    row["access_count"],
                    row["valid_at"],
                    row["invalid_at"],
                    row["invalidated_by"],
                    row["verified_at"],
                    row["state_interval_days"],
                    row["archived_at"],
                    row["embedded"],
                    initial_level(row["importance"]),
                    initial_confidence(kind),
                    0.0,
                    period_start,
                    None,
                    row["meta"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            skipped.append(f"{row['id']}: {exc}")
    written["knowledge"] = {"read": len(rows), "written": len(rows) - len(skipped), "skipped": skipped}

    # ---- episodes（confidence 不存，派生）----
    rows = _rows(src, "episodes")
    e_sql = (
        "INSERT INTO episodes(rowid, id, kind, content, at, day, tags, importance,"
        " access_count, created_at, last_accessed_at, digest_id, archived_at,"
        " compression_level, embedded, forget_level, activity, period_start,"
        " revive_hits, level0_since, meta)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    skipped = []
    for raw in rows:
        row = _pick(raw, V3_EPISODE_COLUMNS)
        try:
            dst.execute(
                e_sql,
                (
                    raw["__rowid"],
                    row["id"],
                    str(row["kind"] or "narrative"),
                    row["content"],
                    row["at"],
                    row["day"],
                    row["tags"],
                    row["importance"],
                    row["access_count"],
                    row["created_at"],
                    row["last_accessed_at"],
                    row["digest_id"],
                    row["archived_at"],
                    row["compression_level"],
                    row["embedded"],
                    initial_level(row["importance"]),
                    0.0,
                    period_start,
                    0,
                    None,
                    row["meta"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            skipped.append(f"{row['id']}: {exc}")
    written["episodes"] = {"read": len(rows), "written": len(rows) - len(skipped), "skipped": skipped}

    # ---- logs（append-only，原样搬）----
    rows = _rows(src, "logs")
    l_sql = (
        "INSERT INTO logs(rowid, id, at, day, content, tags, session_id, importance,"
        " access_count, last_accessed_at, consolidated, digest_id, embedded, meta)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    skipped = []
    for raw in rows:
        row = _pick(raw, V3_LOG_COLUMNS)
        try:
            dst.execute(
                l_sql,
                (
                    raw["__rowid"],
                    row["id"],
                    row["at"],
                    row["day"],
                    row["content"],
                    row["tags"],
                    row["session_id"],
                    row["importance"],
                    row["access_count"],
                    row["last_accessed_at"],
                    row["consolidated"],
                    row["digest_id"],
                    row["embedded"],
                    row["meta"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            skipped.append(f"{row['id']}: {exc}")
    written["logs"] = {"read": len(rows), "written": len(rows) - len(skipped), "skipped": skipped}

    # ---- digests ----
    rows = _rows(src, "digests")
    d_sql = (
        "INSERT INTO digests(id, day, at, log_count, knowledge_count, episode_count,"
        " summary, purged, model, meta) VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    skipped = []
    for raw in rows:
        row = _pick(raw, V3_DIGEST_COLUMNS)
        try:
            dst.execute(
                d_sql,
                (
                    row["id"],
                    row["day"],
                    row["at"],
                    row["log_count"],
                    row["knowledge_count"],
                    row["episode_count"],
                    row["summary"],
                    row["purged"],
                    row["model"],
                    row["meta"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            skipped.append(f"{row['id']}: {exc}")
    written["digests"] = {"read": len(rows), "written": len(rows) - len(skipped), "skipped": skipped}

    return written


def copy_vectors(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, int]:
    """搬向量 blob（rowid 与主表对齐）。维度不匹配的整批跳过，不让它静默变空。"""
    out: dict[str, int] = {}
    for table in ("knowledge", "episodes", "logs"):
        vec_table = f"{table}_vec"
        try:
            rows = src.execute(f"SELECT rowid, embedding FROM {vec_table}").fetchall()
        except sqlite3.DatabaseError:
            out[vec_table] = 0
            continue
        count = 0
        for rowid, blob in rows:
            try:
                dst.execute(
                    f"INSERT INTO {vec_table}(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
                count += 1
            except sqlite3.DatabaseError:
                continue
        out[vec_table] = count
    return out


def _copy_meta(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    """搬 meta，但版本号统一写成目标版本。

    `vec_source` 不搬：那是「这台机器上扩展从哪加载」的运行期信息，
    新库打开时会按当前环境重新写。
    """
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
