"""库清单 + 「当前观测库」切换。

观测台原先只能看启动时那一个 target（项目根 / token / 库目录 / db），换个库就得
重开一个进程。这份模块把「有哪些库可看」变成运行时事实，页面右上角下拉即切：

1. **主来源 = registry.json**（`~/.humanity-memory/registry.json`）—— 已登记的库
   才是这套系统认可的库集合，标签也以它为准（与 `bindings` / `keys` 同一份真相）；
2. **兜底 = `<安装根>/libraries/*`**：registry 缺失或损坏时，离线拷来的库也能看；
3. 启动时显式给了 target，则并成一条额外条目（**不覆盖** registry 里的同名库）。

全程只读：只 stat 目录与 `workspace.db`，不打开库、不建表、不写 registry。
切库只换「当前观测对象」，任何视图都不因此写盘（设计文档 §2.4）。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import keys
from ..config import libraries_home
from .datasource import SourceError, WebSource

DB_NAME = "workspace.db"


class LibraryNotFound(Exception):
    """要切的标签不在清单里（多半是 URL / 上次选择记着一个已经被 revoke 的标签）。"""

    def __init__(self, label: str, known: list[str]) -> None:
        self.label = label
        self.known = known
        super().__init__(f"没有叫 `{label}` 的库。现有：{'、'.join(known) or '（空）'}")


@dataclass(slots=True)
class LibraryEntry:
    """一条可观测的候选。`error` 非空表示登记在册但当前打不开（盘没挂上等）。"""

    label: str
    dir: Path
    origin: str = "registry"  # registry | libraries | target
    error: str | None = None

    @property
    def db(self) -> Path:
        return self.dir / DB_NAME

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_json(self) -> dict[str, Any]:
        size: int | None = None
        if self.ok:
            try:
                size = self.db.stat().st_size
            except OSError:  # 刚 stat 过又在两步之间消失：当不可读，不崩
                size = None
        return {
            "label": self.label,
            "dir": str(self.dir),
            "db": str(self.db),
            "ok": self.ok,
            "error": self.error,
            "origin": self.origin,
            "db_size": size,
        }


# ---------------------------------------------------------------- 清单


def _norm(path: Path | str) -> str:
    """路径身份比较键：展开 + 绝对化 + normcase（与 token.normalize_path 同口径）。"""
    p = Path(os.path.expandvars(str(path))).expanduser()
    try:
        p = p.resolve()
    except OSError:  # pragma: no cover - 目录被删了也不该让整张清单失败
        p = Path(os.path.abspath(p))
    return os.path.normcase(str(p))


def _default_label(lib_dir: Path) -> str:
    """没有登记标签时的显示名：`.ai_memory` 退到项目名，其余用目录名。"""
    return lib_dir.parent.name if lib_dir.name == ".ai_memory" else lib_dir.name


def _entry(label: str, lib_dir: Path | str, origin: str) -> LibraryEntry:
    d = Path(os.path.expandvars(str(lib_dir))).expanduser()
    if not d.is_dir():
        return LibraryEntry(label, d, origin, f"库目录不存在：{d}")
    if not (d / DB_NAME).is_file():
        return LibraryEntry(label, d, origin, f"缺少 {DB_NAME}")
    return LibraryEntry(label, d, origin, None)


def discover() -> tuple[list[LibraryEntry], list[str]]:
    """列出所有候选库。返回 `(entries, warnings)`；warnings 只用于展示，不致命。"""
    entries: list[LibraryEntry] = []
    warnings: list[str] = []
    seen: dict[str, LibraryEntry] = {}

    try:
        registry = keys.read()
    except keys.RegistryError as exc:
        warnings.append(f"registry 不可用（{exc}）—— 已退回扫描 libraries 目录")
        registry = None

    if registry is not None:
        for label in sorted(registry.libraries):
            entry = _entry(label, registry.libraries[label].path, "registry")
            seen[_norm(entry.dir)] = entry
            entries.append(entry)

    try:
        home = libraries_home()
    except Exception:  # noqa: BLE001 - 拿不到安装根就只信 registry
        home = None
    if home is not None and home.is_dir():
        for sub in sorted(p for p in home.iterdir() if p.is_dir()):
            key = _norm(sub)
            if key in seen or not (sub / DB_NAME).is_file():
                continue
            entry = _entry(_default_label(sub), sub, "libraries")
            seen[key] = entry
            entries.append(entry)

    return entries, warnings


# ---------------------------------------------------------------- 池


class SourcePool:
    """当前观测的库 + 可切换清单。

    并发：切库在锁内完成，`current` 是原子赋值。请求进来时取一次快照（server 侧），
    所以一次响应里不会出现「前半段读 A 库、后半段读 B 库」。
    """

    def __init__(
        self,
        target: str | Path | None = None,
        *,
        source: WebSource | None = None,
    ) -> None:
        self.target = target
        self._lock = threading.RLock()
        self.entries: list[LibraryEntry] = []
        self.warnings: list[str] = []
        self.current: WebSource | None = None
        self.current_label = ""
        self.entries, self.warnings = discover()

        if source is not None:
            self._adopt(source)
            return
        if target is not None and str(target).strip():
            # 显式 target 解析失败要立刻报（拖拽启动时路径错，静默换库更糟）
            self._adopt(WebSource(target))
            return

        usable = [e for e in self.entries if e.ok]
        if not usable:
            raise SourceError(
                "no_library",
                "没有任何可观测的库：registry 里没有登记，libraries 目录下也没有 workspace.db。",
                checklist=[
                    "① 先接入一个项目：`bind new` / `bind existing` / `bind adopt`",
                    "② 或把库目录 / token.json / workspace.db 拖到 analyze.bat 上直接看",
                ],
            )
        self.switch(label=usable[0].label)

    @classmethod
    def from_source(cls, source: WebSource) -> "SourcePool":
        """单库模式（测试 / 调用方已自行解析 source）。"""
        return cls(source=source)

    # ---- 内部 ----

    def _find(self, label: str) -> LibraryEntry | None:
        for entry in self.entries:
            if entry.label == label:
                return entry
        return None

    def _adopt(self, src: WebSource) -> None:
        """把已解析的 source 立为当前库，并保证它在清单里有一条。"""
        key = _norm(src.library_dir)
        for entry in self.entries:
            if _norm(entry.dir) == key:
                src.label = entry.label  # 标签以 registry 为准，不以目录名为准
                break
        else:
            self.entries.insert(0, LibraryEntry(src.label, src.library_dir, "target"))
        self.current, self.current_label = src, src.label

    # ---- 对外 ----

    @property
    def source(self) -> WebSource:
        src = self.current
        if src is None:  # pragma: no cover - 构造期已保证；防御性
            raise SourceError("no_library", "当前没有选中的库")
        return src

    def state(self) -> dict[str, Any]:
        return {
            "ok": True,
            "current": self.current_label,
            "libraries": [e.to_json() for e in self.entries],
            "warnings": list(self.warnings),
        }

    def listing(self) -> dict[str, Any]:
        """每次下拉打开/页面轮询时重扫一遍：盘刚挂上就立刻可选。"""
        with self._lock:
            self.entries, self.warnings = discover()
            return self.state()

    def switch(self, *, label: str | None = None, target: str | None = None) -> dict[str, Any]:
        """切换当前观测库。失败时**保持原库不变**（先造新对象，再赋值）。"""
        with self._lock:
            if target and str(target).strip():
                self._adopt(WebSource(target))  # 任意路径也能切（手输 / 拖拽）
            else:
                entry = self._find(label or "")
                if entry is None:
                    raise LibraryNotFound(label or "", [e.label for e in self.entries])
                src = WebSource(entry.dir, label=entry.label)
                self.current, self.current_label = src, entry.label
            self.entries, self.warnings = discover()
            return self.state()
