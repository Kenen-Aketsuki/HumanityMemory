"""schema v4 与升级器（§7.1 / §8.4）。

这组测试守的是两条硬规矩：

1. **主程序只检测、不修补**：版本不符必须报错退出，且磁盘一个字节不动。
2. **升级器零侵入、源库只读**：预览不改文件，产物是新文件，校验三件事全过。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from humanity_memory.config import Config
from humanity_memory.db import (
    EXPECTED_SCHEMA_VERSION,
    SchemaVersionError,
    open_db,
    probe_schema_version,
)
from humanity_memory.upgrade import LATEST_VERSION, UpgradeError, run
from humanity_memory.upgrade import v3_to_v4
from humanity_memory.upgrade.inplace import run_inplace


# ---------------------------------------------------------------- 造一个 v3 库

V3_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE knowledge (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'knowledge' CHECK (kind IN ('knowledge','state')),
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.8,
    scope TEXT NOT NULL DEFAULT 'workspace',
    source TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_accessed_at INTEGER,
    access_count INTEGER NOT NULL DEFAULT 0,
    valid_at INTEGER,
    invalid_at INTEGER,
    invalidated_by TEXT,
    verified_at INTEGER,
    state_interval_days REAL NOT NULL DEFAULT 7.0,
    archived_at INTEGER,
    embedded INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE episodes (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'narrative' CHECK (kind IN ('case','narrative')),
    content TEXT NOT NULL,
    at INTEGER NOT NULL,
    day TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.4,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_accessed_at INTEGER,
    digest_id TEXT,
    archived_at INTEGER,
    compression_level INTEGER NOT NULL DEFAULT 0,
    embedded INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE logs (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    at INTEGER NOT NULL,
    day TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    session_id TEXT,
    importance REAL NOT NULL DEFAULT 0.3,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at INTEGER,
    consolidated INTEGER NOT NULL DEFAULT 0,
    digest_id TEXT,
    embedded INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE digests (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    at INTEGER NOT NULL,
    log_count INTEGER NOT NULL DEFAULT 0,
    knowledge_count INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    purged INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
"""


def _load_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)


def make_v3_db(path: Path, *, dim: int = 8) -> Path:
    """一个内容和真实库同构的 v3 库：3 种 kind + 向量 + digest。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(V3_DDL)
    _load_vec(conn)
    for table in ("knowledge", "episodes", "logs"):
        conn.execute(
            f"CREATE VIRTUAL TABLE {table}_vec USING vec0("
            f"embedding float[{dim}] distance_metric=cosine, chunk_size=64)"
        )

    now = int(time.time() * 1000)
    rows = [
        ("k_know1", "knowledge", "SQLite 的 WAL 模式允许读写并发", 0.9, now - 90_000),
        ("k_know2", "knowledge", "trigram 分词对中文子串友好", 0.4, now - 80_000),
        ("k_state1", "state", "当前 Unity 版本是 2021.3 LTS", 0.85, now - 70_000),
        ("k_state2", "state", "venv 里装了 sqlite-vec 0.1.9", 0.2, now - 60_000),
    ]
    for item_id, kind, content, importance, created in rows:
        conn.execute(
            "INSERT INTO knowledge(id, kind, content, importance, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, kind, content, importance, created, created),
        )

    eps = [
        ("e_case1", "case", "现象：exe 打不开全局库。根因：沙箱按前缀拦截。落点：移出项目树。", 0.8, now - 50_000),
        ("e_narr1", "narrative", "先怀疑权限，再怀疑路径，最后才是沙箱前缀问题。", 0.35, now - 40_000),
    ]
    for item_id, kind, content, importance, at in eps:
        conn.execute(
            "INSERT INTO episodes(id, kind, content, at, day, importance, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (item_id, kind, content, at, "2026-11-16", importance, at),
        )

    conn.execute(
        "INSERT INTO digests(id, day, at, knowledge_count, episode_count)"
        " VALUES ('d_1', '2026-11-16', ?, 2, 2)",
        (now - 30_000,),
    )

    # 向量：前两维编码身份，其余补 0，保证「检索能命中自己」
    for index, table in enumerate(("knowledge", "episodes")):
        n = 4 if table == "knowledge" else 2
        for i in range(n):
            vec = [0.0] * dim
            vec[i] = 1.0
            conn.execute(
                f"INSERT INTO {table}_vec(rowid, embedding) VALUES (?, ?)",
                (i + 1, sqlite3.Binary(_pack(vec))),
            )

    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '3')")
    conn.execute("INSERT INTO meta(key, value) VALUES ('embedding_dim', ?)", (str(dim),))
    conn.commit()
    conn.close()
    return path


def _pack(vector: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vector)}f", *vector)


@pytest.fixture()
def cfg() -> Config:
    return Config.load({"HM_EMBED_DIM": "8"})


# ---------------------------------------------------------------- 新库就是 v5


def test_new_db_is_v5_with_new_columns(tmp_path: Path, cfg: Config) -> None:
    db = tmp_path / "ws" / "workspace.db"
    conn = open_db(db, cfg)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert version == str(EXPECTED_SCHEMA_VERSION) == "5"

        k_cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)")}
        assert {"forget_level", "confidence", "activity", "period_start", "level0_since"} <= k_cols

        e_cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        assert {"forget_level", "activity", "period_start", "revive_hits", "level0_since"} <= e_cols
        assert "confidence" not in e_cols, "经历的可信度是派生的，不该落盘"

        l_cols = {r[1] for r in conn.execute("PRAGMA table_info(logs)")}
        assert "retrievals" in l_cols, "log 记检索条件"
        assert "activity" not in l_cols, "log 不记热度：热度是检索条件的派生量"

        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "memory_events" in tables
    finally:
        conn.close()
    assert probe_schema_version(db) == "v5"


def test_events_table_is_append_only(tmp_path: Path, cfg: Config) -> None:
    db = tmp_path / "ws" / "workspace.db"
    conn = open_db(db, cfg)
    try:
        conn.execute(
            "INSERT INTO memory_events(at, table_name, item_id, event, from_value, to_value)"
            " VALUES (1, 'knowledge', 'k_x', 'verify', '0.3', '0.35')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE memory_events SET event = 'hacked' WHERE item_id = 'k_x'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM memory_events WHERE item_id = 'k_x'")
    finally:
        conn.close()


# ---------------------------------------------------------------- 拒绝旧库


def test_open_db_rejects_v3_without_touching_disk(tmp_path: Path, cfg: Config) -> None:
    db = make_v3_db(tmp_path / "workspace.db")
    before = db.stat()

    with pytest.raises(SchemaVersionError) as excinfo:
        open_db(db, cfg)

    message = str(excinfo.value)
    assert "upgrade" in message
    assert "v3" in message

    after = db.stat()
    assert after.st_size == before.st_size, "拒绝服务不该改动库文件"
    assert after.st_mtime_ns == before.st_mtime_ns, "拒绝服务不该碰库文件"

    siblings = {p.name for p in db.parent.iterdir()}
    assert siblings == {"workspace.db"}, f"不该产生任何改名/附带文件：{siblings}"
    assert probe_schema_version(db) == "v3"


def test_probe_reports_legacy_and_garbage(tmp_path: Path) -> None:
    v2 = tmp_path / "old.db"
    conn = sqlite3.connect(str(v2))
    conn.execute("CREATE TABLE facts(id TEXT)")
    conn.commit()
    conn.close()
    assert probe_schema_version(v2) == "v2"

    garbage = tmp_path / "not-a-db.db"
    garbage.write_bytes(b"this is definitely not sqlite")
    assert probe_schema_version(garbage) == "unknown"

    empty = tmp_path / "empty.db"
    empty.touch()
    assert probe_schema_version(empty) == "empty"
    assert probe_schema_version(tmp_path / "missing.db") == "absent"


# ---------------------------------------------------------------- 升级器


def test_upgrade_preview_keeps_source_untouched(tmp_path: Path) -> None:
    db = make_v3_db(tmp_path / "workspace.db")
    before = db.stat()

    report = run(db)

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["source_version"] == "3"
    assert report["target_version"] == LATEST_VERSION
    assert report["chain"] == ["v3 → v4", "v4 → v5"]
    assert report["plan"]["counts"]["knowledge"] == 4
    assert report["plan"]["counts"]["episodes"] == 2
    assert report["out"] is None

    after = db.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert {p.name for p in db.parent.iterdir()} == {"workspace.db"}


def test_upgrade_writes_new_db_and_passes_checks(tmp_path: Path) -> None:
    db = make_v3_db(tmp_path / "workspace.db")
    out = tmp_path / "workspace.v4.db"

    report = run(db, out)

    assert report["ok"] is True, report
    assert out.is_file()
    assert db.is_file(), "源库必须原样保留"
    checks = report["checks"]
    assert checks["counts_ok"] is True
    assert checks["timestamps_ok"] is True
    assert checks["vectors"]["ok"] is True, checks["vectors"]

    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    _load_vec(conn)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == "5"

        # 初始等级按 importance 走出厂 bands；可信度按 kind 固定映射
        rows = {
            r["id"]: r for r in conn.execute(
                "SELECT id, kind, forget_level, confidence, activity, period_start,"
                " level0_since FROM knowledge"
            )
        }
        assert rows["k_know1"]["forget_level"] == 4      # 0.90 ≥ 0.55
        assert rows["k_know2"]["forget_level"] == 3      # 0.40
        assert rows["k_state1"]["forget_level"] == 4     # 0.85
        assert rows["k_state2"]["forget_level"] == 2     # 0.20
        assert rows["k_know1"]["confidence"] == pytest.approx(0.6)
        assert rows["k_state1"]["confidence"] == pytest.approx(0.3)
        assert all(r["activity"] == 0 for r in rows.values())
        assert all(r["level0_since"] is None for r in rows.values())
        assert all(r["period_start"] == report["written"]["period_start"] for r in rows.values())

        eps = {
            r["id"]: r for r in conn.execute(
                "SELECT id, kind, forget_level, revive_hits FROM episodes"
            )
        }
        assert eps["e_case1"]["forget_level"] == 4
        assert eps["e_narr1"]["forget_level"] == 3
        assert all(r["revive_hits"] == 0 for r in eps.values())

        # 向量行数与主表对齐，且 FTS 能查到
        assert conn.execute("SELECT COUNT(*) FROM knowledge_vec").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM episodes_vec").fetchone()[0] == 2
        hits = conn.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?", ("zzzqqq",)
        ).fetchall()
        assert hits == [], "这个词不在库里"
        hits = conn.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?", ("SQLite",)
        ).fetchall()
        assert hits, "FTS 索引必须已重建"
    finally:
        conn.close()


def test_v4_to_v5_sets_retrievals_empty(tmp_path: Path, cfg: Config) -> None:
    """v4 → v5：存量 log 的 retrievals 写 '[]'，其余数据原样保留。"""
    v4 = tmp_path / "workspace.v4.db"
    conn = open_db(v4, cfg)
    try:
        conn.execute(
            "INSERT INTO logs(id, at, day, content, access_count) VALUES (?,?,?,?,?)",
            ("l_old", 1_760_000_000_000, "2026-11-19", "一条老日志", 3),
        )
        # 把刚建好的 v5 库降级成 v4：去掉新列，改回版本号
        conn.execute("ALTER TABLE logs DROP COLUMN retrievals")
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    finally:
        conn.close()

    assert probe_schema_version(v4) == "v4"
    preview = run(v4)
    assert preview["chain"] == ["v4 → v5"]

    out = tmp_path / "workspace.v5.db"
    report = run(v4, out)
    assert report["ok"] is True, report
    assert report["checks"]["all_ok"] is True, report["checks"]
    assert probe_schema_version(out) == "v5"

    check = sqlite3.connect(str(out))
    check.row_factory = sqlite3.Row
    _load_vec(check)
    try:
        row = check.execute(
            "SELECT content, access_count, retrievals FROM logs WHERE id = ?",
            ("l_old",),
        ).fetchone()
        assert row is not None
        assert row["content"] == "一条老日志"
        assert row["access_count"] == 3
        assert row["retrievals"] == "[]", "历史 JSONL 不追溯，从升级时刻起算"
        version = check.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == "5"
    finally:
        check.close()


def test_upgrade_refuses_to_overwrite(tmp_path: Path) -> None:
    db = make_v3_db(tmp_path / "workspace.db")
    out = tmp_path / "exists.db"
    out.write_text("occupied", encoding="utf-8")

    with pytest.raises(UpgradeError) as excinfo:
        run(db, out)
    assert "拒绝覆盖" in str(excinfo.value)


def test_upgrade_rejects_already_current_and_garbage(tmp_path: Path, cfg: Config) -> None:
    current = tmp_path / "current.db"
    open_db(current, cfg).close()

    report = run(current)
    assert report["ok"] is False
    assert "已经是 v5" in report["reason"]

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not sqlite at all")
    with pytest.raises(UpgradeError) as excinfo:
        run(garbage)
    assert "不是有效的 SQLite 库" in str(excinfo.value) or "认不出" in str(excinfo.value)


def test_upgrade_resolves_workspace_directory(tmp_path: Path) -> None:
    ws = tmp_path / "proj" / ".ai_memory"
    ws.mkdir(parents=True)
    make_v3_db(ws / "workspace.db")

    assert run(tmp_path / "proj" / ".ai_memory")["source_version"] == "3"
    assert run(tmp_path / "proj")["source_version"] == "3"


# ---------------------------------------------------------------- 就地升级（拖拽用）


def test_inplace_swaps_and_keeps_v3_archive(tmp_path: Path) -> None:
    db = make_v3_db(tmp_path / "workspace.db")

    report = run_inplace(db)

    assert report["ok"] is True, report
    assert report["in_place"] is True
    assert report["swapped"] is True
    assert probe_schema_version(db) == "v5"

    # 原件必须还在，且真的是 v3 —— 回退就是把名字改回去
    archived = tmp_path / report["archived"]
    assert archived.is_file()
    assert probe_schema_version(archived) == "v3"

    conn = sqlite3.connect(str(db))
    _load_vec(conn)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM knowledge_vec").fetchone()[0] == 4
    finally:
        conn.close()


def test_inplace_is_idempotent(tmp_path: Path) -> None:
    db = make_v3_db(tmp_path / "workspace.db")
    run_inplace(db)

    again = run_inplace(db)

    assert again["ok"] is True
    assert again["noop"] is True
    assert "已经是 v5" in again["reason"]
    assert len(list(tmp_path.glob("workspace.db.v3-*"))) == 1, "重复拖不该再留一份档"


def test_inplace_leaves_source_untouched_on_bad_input(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not sqlite at all")
    before = garbage.stat()

    with pytest.raises(UpgradeError):
        run_inplace(garbage)

    after = garbage.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert {p.name for p in tmp_path.iterdir()} == {"garbage.db"}


def test_cli_out_and_inplace_are_exclusive(tmp_path: Path) -> None:
    from humanity_memory.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["upgrade", str(tmp_path / "x.db"), "--out", "a.db", "--in-place"]
        )
