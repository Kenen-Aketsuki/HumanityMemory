"""统一定位 + 只读取数适配层。

把「target → layout → cfg → 库/报告路径」这条链收在一处，各视图只管消费。
所有连接一律 `mode=ro`（路径 `quote(as_posix)` 吃中文/空格），不建表、不写列。

设计文档 §2.3 / §2.4 / §2.5 / §8。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import bindings
from ..config import Config
from ..lifecycle import LevelTable
from ..reports import judge_dir_name, retrieval_dir_name


class SourceError(Exception):
    """定位或打开数据源失败（给 API 转成统一错误契约）。"""

    def __init__(self, code: str, message: str, *, checklist: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.checklist = checklist or []

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.checklist:
            out["checklist"] = self.checklist
        return out


class WebSource:
    """一次启动解析出的全部只读上下文。持有路径与配置，不持有长连接。

    线程安全：每个请求需要连接时调 `connect()` 现开现关；ThreadingHTTPServer
    下多个请求并发不会共享同一个 sqlite3.Connection。
    """

    def __init__(self, target: str | Path | None) -> None:
        try:
            self.layout = bindings.resolve_readonly(target)
        except bindings.BindingError as exc:
            raise SourceError(
                getattr(exc, "code", None) or "no_workspace",
                str(exc),
                checklist=list(getattr(exc, "checklist", []) or []),
            ) from exc

        if self.layout.library_dir is None or self.layout.workspace_db is None:
            raise SourceError("no_workspace", "没有解析到记忆库目录或 workspace.db")
        if not self.layout.workspace_db.is_file():
            raise SourceError("no_db", f"库文件不存在：{self.layout.workspace_db}")

        self.library_dir: Path = self.layout.library_dir
        self.db: Path = self.layout.workspace_db
        try:
            self.cfg: Config = Config.load(library_dir=self.library_dir)
        except Exception as exc:  # noqa: BLE001 - 配置坏不该让服务起不来，退化到默认
            self.cfg_error: str | None = str(exc)
            self.cfg = Config.load(environ={}, library_dir=self.library_dir)
        else:
            self.cfg_error = None
        self.levels = LevelTable.from_config(self.cfg)

    # ---- 路径 ----

    @property
    def retrieval_dir(self) -> Path:
        return self.library_dir / retrieval_dir_name(self.cfg)

    @property
    def judge_dir(self) -> Path:
        return self.library_dir / judge_dir_name(self.cfg)

    def has_retrieval(self) -> bool:
        return self.retrieval_dir.is_dir()

    def has_judge(self) -> bool:
        return self.judge_dir.is_dir()

    # ---- 只读连接 ----

    def connect(self) -> sqlite3.Connection:
        """每次调用返回一条新的只读连接；调用方负责 close。"""
        uri = "file:" + quote(self.db.as_posix(), safe="/:") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 签名（§8） ----

    @staticmethod
    def _stat_sig(path: Path) -> tuple[int, int]:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    def _db_sig(self) -> tuple[int, int]:
        """库 + WAL/SHM：其他进程写入时 WAL 会变，签名跟着变。"""
        sig = self._stat_sig(self.db)
        for suffix in ("-wal", "-shm"):
            w = Path(str(self.db) + suffix)
            sig += self._stat_sig(w)
        return sig

    def sig_starmap(self) -> str:
        return str(self._db_sig())

    def sig_recall(self) -> str:
        parts = []
        if self.retrieval_dir.is_dir():
            for p in sorted(self.retrieval_dir.glob("*.jsonl")):
                parts.append((p.name, *self._stat_sig(p)))
        return str(parts)

    def sig_review(self) -> str:
        parts = []
        if self.judge_dir.is_dir():
            for p in sorted(self.judge_dir.glob("*.json")):
                parts.append((p.name, *self._stat_sig(p)))
        return str((parts, self._db_sig()))

    def sig_memory(self) -> str:
        # 倒计时会随时间走：库没变也要按分钟变一次，前端才会重算"距下次裁定"
        minute = int(time.time() // 60)
        return str((self._db_sig(), minute))

    def signatures(self) -> dict[str, str]:
        return {
            "starmap": self.sig_starmap(),
            "recall": self.sig_recall(),
            "review": self.sig_review(),
            "memory": self.sig_memory(),
        }

    # ---- session 信息 ----

    def session(self) -> dict[str, Any]:
        from ..starmap import inspect_schema

        conn = self.connect()
        try:
            schema = inspect_schema(conn)
            try:
                count = sum(
                    conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
                    for t in ("knowledge", "episodes")
                )
            except sqlite3.Error:
                count = 0
        finally:
            conn.close()
        return {
            "ok": True,
            "db": str(self.db),
            "dir": str(self.library_dir),
            "schema_version": schema["schema_version"],
            "embedding_model": schema.get("embedding_model"),
            "node_count": count,
            "has_retrieval": self.has_retrieval(),
            "has_judge": self.has_judge(),
            "levels": self.cfg.forget_levels,
            "cfg_error": self.cfg_error,
        }
