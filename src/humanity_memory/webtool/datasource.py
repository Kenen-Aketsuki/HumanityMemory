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

from .. import bindings, keys
from ..config import Config
from ..lifecycle import LevelTable
from ..reports import judge_dir_name, retrieval_dir_name
from ..token import same_path


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

    def __init__(self, target: str | Path | None, *, label: str | None = None) -> None:
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
        self.label = label or self._guess_label()

    # ---- 标签 ----

    def _guess_label(self) -> str:
        """库标签：**以 registry 为准**，对不上就退回目录名（离线拷来的库没登记）。

        标签只是显示与切换用的名字，不是身份 —— registry 坏了也不该让取数失败。
        """
        try:
            for name, lib in keys.read().libraries.items():
                if same_path(lib.path, self.library_dir):
                    return name
        except Exception:  # noqa: BLE001 - 显示用的名字，不值得挡住只读取数
            pass
        return (
            self.library_dir.parent.name
            if self.library_dir.name == ".ai_memory"
            else self.library_dir.name
        )

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

    def _db_sig(self) -> tuple[int, ...]:
        """库（mtime/size）+ WAL 待落盘字节数。口径 =「数据或待落盘量变了吗」。

        两个**必须排除**的量，它们会随「自己去读」而变，把只读观测变成刷新风暴：

        1. `-shm`：WAL 的共享内存索引。没有别的连接常驻时，只读连接每取一次数
           都要重新登记 read mark，其 mtime 必涨（NTFS 实测每次查询都涨）。
        2. `-wal` 的存在性与 mtime：没有任何连接时，只读连接一关闭就会把空 WAL
           删掉、下次打开又重建，mtime 一直翻。**大小**才是有效信息：缺失与
           0 字节都表示「没有待落盘数据」，等价归一成 0。

        漏不掉真实写入：提交必追加 WAL 帧（size 变），checkpoint 折回主库后
        `.db` 的 mtime/size 变；clean close 时的 checkpoint 同样改 `.db`。
        回归见 tests/test_webtool.py::test_signature_stable_across_reads。
        """
        sig = self._stat_sig(self.db)
        sig += (self._wal_size(),)
        return sig

    def _wal_size(self) -> int:
        """WAL 待落盘字节数；文件不存在（或没有连接、已被清理）视为 0。"""
        try:
            return Path(str(self.db) + "-wal").stat().st_size
        except OSError:
            return 0

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
            "library": self.label,
            "schema_version": schema["schema_version"],
            "embedding_model": schema.get("embedding_model"),
            "node_count": count,
            "has_retrieval": self.has_retrieval(),
            "has_judge": self.has_judge(),
            "levels": self.cfg.forget_levels,
            "cfg_error": self.cfg_error,
        }
