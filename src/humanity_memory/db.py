"""SQLite 连接、sqlite-vec 加载、schema 管理（支持双库）。"""

from __future__ import annotations

import itertools
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config
from .resources import resource

SCHEMA_PATH = resource("schema.sql")

# 当前程序认识的 schema 版本。库里的 meta.schema_version 必须是这个数，
# 否则拒绝服务 —— 不做任何就地修补（docs/two-tier-memory.md §8.4）。
EXPECTED_SCHEMA_VERSION = 4

# 主表 → 向量表。两类记忆各一张主表，类型由表承载，没有行级 kind。
VEC_TABLES: dict[str, str] = {
    "knowledge": "knowledge_vec",
    "episodes": "episodes_vec",
    "logs": "logs_vec",
}

# vec0 默认 chunk_size=1024，每张表预分配 1024×dim×4 字节（1024 维即 4MB）。
# 记忆库常年只有几十到几百条，默认值会让 db 文件 96% 是空洞，显式调小。
VEC_CHUNK_SIZE = 64

# 上一代 schema 的主表名 —— 只用来**识别** v2 库，绝不据此改名或迁移。
LEGACY_TABLES = ("facts",)

_DIM_RE = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


class SchemaError(RuntimeError):
    """schema 与配置不兼容（最典型：embedding 维度变了）。"""


class SchemaVersionError(RuntimeError):
    """库文件的 schema 版本与当前程序不符。

    按 §8.4 的结论，主程序的义务只有「检测 + 拒绝」：改名、补字段、跑 DDL 修补
    全部禁止。理由是这个函数挂在**每一次打开连接**的路径上——只要它能动手改磁盘，
    任何一次「只想看一眼」的读操作都会产生不可逆副作用。
    """

    def __init__(self, path: Path, found: str, expected: int = EXPECTED_SCHEMA_VERSION):
        self.path = path
        self.found = found
        self.expected = expected
        hint = (
            f'humanity-memory upgrade "{path}"                    # 先预览会变成什么样\n'
            f'humanity-memory upgrade "{path}" --out "{path.with_name(path.stem + ".v4.db")}"'
        )
        super().__init__(
            f"库版本不符：{path}\n"
            f"  库里是 {found}，本程序只认 v{expected}，且不会自动修改库文件。\n"
            f"  升级命令（源库只读，产物另存，核验后自行替换）：\n{hint}"
        )



# ---------------------------------------------------------------- 扩展加载


def load_vec_extension(conn: sqlite3.Connection, cfg: Config | None = None) -> str:
    """加载 sqlite-vec，返回实际生效来源。

    扩展一律从 `sqlite_vec` 包内取（`loadable_path()`），不再猜 `tools/bin` 之类的
    目录：源码态和冻结态走同一条路，打包时 `--collect-all sqlite_vec` 会把 dll 一起带走。
    `cfg.vec_extension` 仍是显式覆盖，优先级高于包内自带的那个。
    """
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

            path = sqlite_vec.loadable_path()
            conn.load_extension(path)
            return f"pip:sqlite-vec ({path})"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pip 包加载失败: {exc}")

        override = getattr(cfg, "vec_extension", None)
        if override:
            try:
                conn.load_extension(str(Path(override).resolve()))
                return f"config:{override}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"配置扩展加载失败 ({override}): {exc}")

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


def set_meta_if_changed(conn: sqlite3.Connection, key: str, value: str) -> bool:
    """值没变就不写。

    看起来多余，但这决定了「只读操作会不会落一条 WAL」：`open_db` 每次都要
    记录 `vec_source`，无脑 UPSERT 会让 `stats`、`starmap` 这类纯读命令
    也产生写操作 —— 与 §8.4「读操作不该改磁盘」是同一条精神。
    """
    if get_meta(conn, key) == value:
        return False
    set_meta(conn, key, value)
    return True


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


# ---------------------------------------------------------------- 版本探测


def probe_schema_version(path: Path) -> str:
    """只读探测库文件的 schema 版本，返回 `"absent"` / `"empty"` / `"v2"` / `"v3"` / `"v4"` / `"unknown"`。

    **绝不修改磁盘**：`mode=ro` 打开，只读 `sqlite_master` 与 `meta`。
    这是 §8.4 的核心——旧实现在这条路径上 rename 库文件，于是任何一次读操作
    都会产生不可逆副作用，多进程并发时还会互相撞 `WinError 32`。
    """
    if not path.exists():
        return "absent"
    if path.stat().st_size == 0:
        return "empty"
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "unknown"
    try:
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        except sqlite3.DatabaseError:
            # 不是 SQLite 文件（比如把 zip 改了后缀）
            return "unknown"
        names = {str(r[0]) for r in rows}
        if not names:
            return "empty"
        if "meta" in names:
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.DatabaseError:
                return "unknown"
            if row and row[0] is not None:
                return f"v{row[0]}"
            # 有 meta 但没写版本：早期 v3 库，同样不认
            return "unknown"
        if any(t in names for t in LEGACY_TABLES):
            return "v2"
        return "unknown"
    finally:
        conn.close()


def _check_version(path: Path, found: str) -> bool:
    """返回 True 表示需要初始化新库（文件不存在 / 空文件 / 空库）。"""
    if found in ("absent", "empty"):
        return True
    expected = f"v{EXPECTED_SCHEMA_VERSION}"
    if found == expected:
        return False
    raise SchemaVersionError(path, found)


# ---------------------------------------------------------------- schema


def ensure_schema(conn: sqlite3.Connection, cfg: Config, *, fresh: bool) -> dict[str, object]:
    """建表（仅 `fresh=True`）并校验向量维度。

    `fresh=False` 时**不跑任何 DDL**：库已由升级器或上一次初始化建好，
    版本闸门已经确认它是当前版本。主程序在这条路径上只读结构、不补不改
    （§8.4「建表、补字段、跑 DDL 修补」全部禁止）。
    """
    tokenizer = detect_fts_tokenizer(conn)
    if tokenizer == "none":
        raise RuntimeError("当前 SQLite 未启用 FTS5，无法建立关键词索引。")

    if fresh:
        try:
            sql = SCHEMA_PATH.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"内置 schema.sql 缺失：{SCHEMA_PATH}。冻结态请检查打包参数是否包含 "
                '--add-data "src/humanity_memory/schema.sql;humanity_memory"'
            ) from exc
        if tokenizer != "trigram":
            sql = sql.replace("tokenize = 'trigram'", f"tokenize = '{tokenizer}'")
        conn.executescript(sql)

    info: dict[str, object] = {
        "fts_tokenizer": tokenizer,
        "vec_dims": {},
        "fresh": fresh,
    }

    for main_table, vec_table in VEC_TABLES.items():
        existing = _existing_vec_dim(conn, vec_table)
        if existing is None:
            conn.execute(
                f"CREATE VIRTUAL TABLE {vec_table} USING vec0("
                f"embedding float[{cfg.embed_dim}] distance_metric=cosine,"
                f" chunk_size={VEC_CHUNK_SIZE})"
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
            f"embedding float[{cfg.embed_dim}] distance_metric=cosine,"
            f" chunk_size={VEC_CHUNK_SIZE})"
        )
        info["vec_dims"][vec_table] = cfg.embed_dim

    set_meta_if_changed(conn, "embedding_dim", str(cfg.embed_dim))
    set_meta_if_changed(conn, "embedding_model", cfg.embed_model)
    if fresh:
        set_meta_if_changed(conn, "schema_version", str(EXPECTED_SCHEMA_VERSION))
    return info


# ---------------------------------------------------------------- 连接


def open_db(path: Path, cfg: Config) -> sqlite3.Connection:
    """打开（必要时创建）一个记忆库文件。

    顺序很重要：**先只读探测版本，再决定要不要建立/打开写连接**。
    版本不符直接抛 `SchemaVersionError` 退出，磁盘一个字节不动。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = _check_version(path, probe_schema_version(path))
    conn = sqlite3.connect(
        str(path), timeout=15.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    source = load_vec_extension(conn, cfg)
    ensure_schema(conn, cfg, fresh=fresh)
    set_meta_if_changed(conn, "vec_source", source)
    return conn


_SAVEPOINT_SEQ = itertools.count(1)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务（autocommit 模式下必须手动 BEGIN）。

    支持嵌套：已经在外层事务里时改用 SAVEPOINT，外层回滚会把内层一起回滚。
    consolidate_apply 靠这个把「写结论 + 写经历 + 写 digest + 标记日志 +
    purge」合成一次原子提交 —— 中途任何异常都不留残数据，也不删原始日志。
    """
    if conn.in_transaction:
        name = f"hm_sp_{next(_SAVEPOINT_SEQ)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
