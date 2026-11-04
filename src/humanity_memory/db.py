"""SQLite 连接、sqlite-vec 加载、schema 管理（支持双库）。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 主表 → 向量表
VEC_TABLES: dict[str, str] = {
    "facts": "facts_vec",
    "episodes": "episodes_vec",
    "logs": "logs_vec",
}

_DIM_RE = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


class SchemaError(RuntimeError):
    """schema 与配置不兼容（最典型：embedding 维度变了）。"""


# ---------------------------------------------------------------- 扩展加载


def _candidate_extensions(cfg: Config) -> list[Path]:
    roots = [
        Path(__file__).resolve().parents[2] / "tools" / "bin",
        cfg.home / "bin",
        Path.cwd() / "tools" / "bin",
    ]
    names = ("vec0.dll", "vec0.so", "vec0.dylib")
    return [root / n for root in roots for n in names if (root / n).is_file()]


def load_vec_extension(conn: sqlite3.Connection, cfg: Config) -> str:
    """加载 sqlite-vec，返回实际生效来源。"""
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError) as exc:  # pragma: no cover
        raise RuntimeError(
            "当前 Python 的 sqlite3 未编译 loadable extension 支持，无法加载 sqlite-vec。"
            "请换用官方发行版 Python。"
        ) from exc

    errors: list[str] = []
    try:
        try:
            import sqlite_vec

            sqlite_vec.load(conn)
            return "pip:sqlite-vec"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pip 包加载失败: {exc}")

        if cfg.vec_extension:
            try:
                conn.load_extension(str(Path(cfg.vec_extension).resolve()))
                return f"config:{cfg.vec_extension}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"配置扩展加载失败 ({cfg.vec_extension}): {exc}")

        for path in _candidate_extensions(cfg):
            try:
                conn.load_extension(str(path))
                return f"file:{path}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path} 加载失败: {exc}")

        raise RuntimeError("无法加载 sqlite-vec 扩展。尝试过：\n  - " + "\n  - ".join(errors))
    finally:
        try:
            conn.enable_load_extension(False)
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------- FTS 能力


def detect_fts_tokenizer(conn: sqlite3.Connection) -> str:
    for tokenizer in ("trigram", "unicode61"):
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x, tokenize='{tokenizer}')"
            )
            conn.execute("DROP TABLE temp.__fts_probe")
            return tokenizer
        except sqlite3.Error:
            continue
    return "none"


# ---------------------------------------------------------------- 元信息


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _existing_vec_dim(conn: sqlite3.Connection, table: str) -> int | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ? AND type = 'table'", (table,)
    ).fetchone()
    if not row or not row["sql"]:
        return None
    match = _DIM_RE.search(row["sql"])
    return int(match.group(1)) if match else None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------- schema


def ensure_schema(conn: sqlite3.Connection, cfg: Config) -> dict[str, object]:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    tokenizer = detect_fts_tokenizer(conn)
    if tokenizer == "none":
        raise RuntimeError("当前 SQLite 未启用 FTS5，无法建立关键词索引。")
    if tokenizer != "trigram":
        sql = sql.replace("tokenize = 'trigram'", f"tokenize = '{tokenizer}'")
    conn.executescript(sql)

    info: dict[str, object] = {"fts_tokenizer": tokenizer, "vec_dims": {}}

    for main_table, vec_table in VEC_TABLES.items():
        existing = _existing_vec_dim(conn, vec_table)
        if existing is None:
            conn.execute(
                f"CREATE VIRTUAL TABLE {vec_table} USING vec0("
                f"embedding float[{cfg.embed_dim}] distance_metric=cosine)"
            )
            info["vec_dims"][vec_table] = cfg.embed_dim
            continue

        if existing == cfg.embed_dim:
            info["vec_dims"][vec_table] = existing
            continue

        if _row_count(conn, main_table) > 0:
            raise SchemaError(
                f"{vec_table} 现有维度 {existing}，配置要求 {cfg.embed_dim}。"
                "换 embedding 模型会让旧向量失效。请二选一：\n"
                f"  1. 把 HM_EMBED_DIM 改回 {existing}\n"
                "  2. 运行 `humanity-memory reindex --reset-vectors` 清空后重建"
            )
        conn.execute(f"DROP TABLE {vec_table}")
        conn.execute(
            f"CREATE VIRTUAL TABLE {vec_table} USING vec0("
            f"embedding float[{cfg.embed_dim}] distance_metric=cosine)"
        )
        info["vec_dims"][vec_table] = cfg.embed_dim

    set_meta(conn, "embedding_dim", str(cfg.embed_dim))
    set_meta(conn, "embedding_model", cfg.embed_model)
    set_meta(conn, "schema_version", "2")
    return info


# ---------------------------------------------------------------- 连接


def open_db(path: Path, cfg: Config) -> sqlite3.Connection:
    """打开（必要时创建）一个记忆库文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=15.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    source = load_vec_extension(conn, cfg)
    ensure_schema(conn, cfg)
    set_meta(conn, "vec_source", source)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务（autocommit 模式下必须手动 BEGIN）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
