"""服务端注册表：`~/.humanity-memory/registry.json`（准入层的唯一真相）。

它回答两个问题，且只回答这两个：

* **这把钥匙是谁的** —— `keys[sha256(salt+key)]` → workspace / library / access
* **这个库的文件在哪** —— `libraries[label].path`

为什么不是一个索引表
--------------------
令牌就在项目里（`.ai_memory/token.json`），身份由 cwd 决定，所以不需要
"路径 → 文件名"的反查，也就不需要 md5 文件名那套东西（A2 已整体删除）。
registry 只做 `key → 归属` 的真相，不做寻址。

为什么只存哈希
--------------
registry 一旦和令牌文件一起被看到就等于没有门槛，所以存的永远是
`sha256(salt + key)`：钥匙本身只在 `<root>/.ai_memory/token.json` 里。
**查表即比对** —— 用哈希当 dict 的键，不存在"先取出明文再字符串比较"这一步。

写并发
------
R-5 的形态是"两个窗口同时绑定，后写的把前一条吊销记录冲掉"。所以：

1. `O_EXCL` 锁文件（`registry.json.lock`）串行化所有写；
2. **锁内才读**，读改写一起完成 —— 不是"先 load 再 lock 再 save"，
   那样两个进程会各自基于旧快照覆盖对方；
3. 落盘走临时文件 + `os.replace`，读者永远看不到半个 JSON。

所有写操作必须走 `with edit() as reg:`。单独调 `Registry.save()` 只在
`edit()` 内部有意义，绕过它就会重现 R-5。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .paths import global_home
from .token import generate_key, normalize_path, same_path

REGISTRY_NAME = "registry.json"
REGISTRY_VERSION = 2
ACCESS_VALUES = ("rw", "ro")

LOCK_TIMEOUT = 5.0
LOCK_STALE = 120.0
_POLL = 0.05


# ---------------------------------------------------------------- 错误


class RegistryError(ValueError):
    """`code` 与 docs/workspace-binding-and-token.md §6.2 对齐。"""

    code = "registry_bad"

    def __init__(self, message: str, *, path: Path | str | None = None) -> None:
        super().__init__(message)
        self.path = Path(path) if path is not None else None


class RegistryBad(RegistryError):
    """registry.json 坏了 / 版本不认识 / 缺 salt。"""

    code = "registry_bad"


class RegistryBusy(RegistryError):
    """锁等超时。不是数据问题，是"另一个进程还在写"。"""

    code = "registry_busy"


class RegistryConflict(RegistryError):
    """标签撞名。属于运维错误，不在准入契约里（准入看不到这一步）。"""

    code = "library_conflict"


class KeyUnknown(RegistryError):
    code = "key_unknown"


class KeyRevoked(RegistryError):
    code = "key_revoked"


class KeyWorkspaceMismatch(RegistryError):
    code = "key_workspace_mismatch"


class KeyLibraryMismatch(RegistryError):
    """令牌声明的 library 与 registry 记的不一致 —— 令牌被手改过。

    `library` 只是**断言**：真正决定打开哪个库的是 registry。
    所以这条检查的意义不是"拦坏人"，而是"手改过的令牌不许含糊通过"。
    """

    code = "key_library_mismatch"


_UNSET = object()


# ---------------------------------------------------------------- 记录


@dataclass(slots=True)
class Library:
    """一个库：自有库 path = `<root>/.ai_memory`，组库在 `libraries/<label>`。"""

    path: Path
    created: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"path": str(self.path), "created": self.created}


@dataclass(slots=True)
class KeyRecord:
    """一把钥匙的归属。`library is None` = 该工作区已选择不接入（declined）。"""

    hash: str
    workspace: Path
    library: str | None
    access: str = "rw"
    created: str = ""
    revoked: str | None = None

    @property
    def active(self) -> bool:
        return not self.revoked

    def to_json(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "library": self.library,
            "access": self.access,
            "created": self.created,
            "revoked": self.revoked,
        }


def _today() -> str:
    return date.today().isoformat()


def registry_path(path: Path | str | None = None) -> Path:
    """`HM_HOME` 每调用一次就重算 —— registry 必须跟着 `global_home()` 走（C8）。

    写成模块级常量的话，测试里改 `HM_HOME` 会静默写到真实用户目录。
    """
    if path is not None:
        return Path(os.path.expandvars(str(path))).expanduser()
    return global_home() / REGISTRY_NAME


# ---------------------------------------------------------------- 注册表


@dataclass(slots=True)
class Registry:
    path: Path
    salt: str
    version: int = REGISTRY_VERSION
    libraries: dict[str, Library] = field(default_factory=dict)
    keys: dict[str, KeyRecord] = field(default_factory=dict)
    writable: bool = False

    # ---- 读 ----
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Registry":
        file = registry_path(path)
        if not file.is_file():
            # 没注册表 ≠ 坏注册表：新机器第一次跑就是这个状态。
            # salt 现生成，但只有 edit() 落盘时才会被固定下来。
            return cls(path=file, salt=secrets.token_hex(16))

        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RegistryBad(
                f"registry 不是合法 JSON：{file}（{exc}）\n"
                "它只是钥匙归属的索引，损坏时删掉重跑 `bind adopt` 即可，库文件不受影响。",
                path=file,
            ) from exc
        if not isinstance(data, dict):
            raise RegistryBad(f"registry 内容必须是一个 JSON 对象：{file}", path=file)

        raw_version = data.get("version", REGISTRY_VERSION)
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise RegistryBad(f"registry 的 version 不是整数（{raw_version!r}）：{file}", path=file) from exc
        if version != REGISTRY_VERSION:
            raise RegistryBad(
                f"registry 版本 v{version} 与本程序支持的 v{REGISTRY_VERSION} 不一致：{file}\n"
                "不猜、不迁移 —— 由升级器显式处理，避免把钥匙归属写坏。",
                path=file,
            )

        salt = data.get("salt")
        if not isinstance(salt, str) or len(salt) < 16:
            raise RegistryBad(f"registry 缺少 salt（哈希无法复核）：{file}", path=file)

        libraries: dict[str, Library] = {}
        for label, raw in (data.get("libraries") or {}).items():
            if not isinstance(raw, dict) or not raw.get("path"):
                raise RegistryBad(f"libraries.{label} 缺少 path：{file}", path=file)
            libraries[str(label)] = Library(
                path=Path(os.path.expandvars(str(raw["path"]))).expanduser(),
                created=str(raw.get("created") or ""),
            )

        keys: dict[str, KeyRecord] = {}
        for digest, raw in (data.get("keys") or {}).items():
            if not isinstance(raw, dict):
                raise RegistryBad(f"keys.{digest} 必须是对象：{file}", path=file)
            workspace = raw.get("workspace")
            if not workspace:
                raise RegistryBad(f"keys.{digest} 缺少 workspace：{file}", path=file)
            access = str(raw.get("access") or "rw")
            if access not in ACCESS_VALUES:
                raise RegistryBad(f"keys.{digest} 的 access 不认识（{access}）：{file}", path=file)
            library = raw.get("library")
            keys[str(digest)] = KeyRecord(
                hash=str(digest),
                workspace=Path(os.path.expandvars(str(workspace))).expanduser(),
                library=str(library) if library else None,
                access=access,
                created=str(raw.get("created") or ""),
                revoked=str(raw["revoked"]) if raw.get("revoked") else None,
            )

        return cls(path=file, salt=salt, version=version, libraries=libraries, keys=keys)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "salt": self.salt,
            "libraries": {label: lib.to_json() for label, lib in sorted(self.libraries.items())},
            # 键就是哈希：明文 key 从不落这里（`hash` 不再重复存一份）
            "keys": {digest: rec.to_json() for digest, rec in sorted(self.keys.items())},
        }

    def save(self) -> Path:
        """原子落盘。**只该由 `edit()` 调用** —— 它假设调用方已持锁。"""
        if not self.writable:  # pragma: no cover - 防手滑，正常路径不会走到
            raise RegistryBad("Registry.save() 必须在 edit() 里调用，否则会丢并发写")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # ---- 哈希与校验 ----
    def hash_key(self, key: str) -> str:
        return hashlib.sha256((self.salt + key).encode("utf-8")).hexdigest()

    def record_for(self, key: str) -> KeyRecord | None:
        """只查表，不做策略判断。"""
        return self.keys.get(self.hash_key(key))

    def check_key(
        self,
        key: str,
        workspace: Path | str | None = None,
        library: str | None | object = _UNSET,
    ) -> KeyRecord:
        """§5.5 第 5-6 步。失败点决定错误码，调用方原样塞进拒绝契约。

        `library` 传令牌里声明的值（含 `None` = 已拒绝）；不传则不比对
        （迁移 / 运维工具用）。比对是**严格相等**：令牌说 null 而 registry
        说 alpha，也算被改过 —— 否则手改一个 null 就能绕过整个准入。
        """
        record = self.record_for(key)
        if record is None:
            raise KeyUnknown(
                f"registry 里没有这把钥匙（{_mask(key)}）。\n"
                "令牌是从别处复制来的，或换过 registry / HM_HOME —— 用 `bind adopt` 重发一份。"
            )
        if not record.active:
            raise KeyRevoked(
                f"这把钥匙已于 {record.revoked} 被撤销（{_mask(key)}）。\n"
                "撤销是永久的（不删记录、不改哈希），重发新令牌即可继续。"
            )
        if workspace is not None and not same_path(record.workspace, workspace):
            raise KeyWorkspaceMismatch(
                f"registry 记录的归属与令牌不一致：registry={record.workspace}，令牌={workspace}。\n"
                "令牌被手改过。重发令牌（`bind adopt`），不要手改 JSON。"
            )
        if library is not _UNSET:
            claimed = library or None  # type: ignore[assignment]
            if claimed != record.library:
                raise KeyLibraryMismatch(
                    f"令牌声明 library={claimed!r}，registry 记的是 {record.library!r}。\n"
                    "归属由服务端 registry 定，令牌里的 library 只是断言；不一致说明令牌被改过。\n"
                    "重发令牌（`bind adopt`）。"
                )
        return record

    # ---- 库 ----
    def library(self, label: str) -> Library | None:
        return self.libraries.get(label)

    def library_path(self, label: str) -> Path | None:
        lib = self.libraries.get(label)
        return lib.path if lib else None

    def set_library(self, label: str, path: Path | str, *, replace: bool = False) -> Library:
        """登记库。同名不同路径默认拒绝 —— 两个项目静默共用一个标签
        比报错难查得多（A、B 共库必须是显式 bind existing 的结果）。"""
        target = Path(os.path.expandvars(str(path))).expanduser()
        existing = self.libraries.get(label)
        if existing is not None:
            if same_path(existing.path, target):
                return existing
            if not replace:
                raise RegistryConflict(
                    f"库标签 `{label}` 已占用：{existing.path}\n"
                    f"要改指向传 replace=True（bind move 走这条），或换个标签。"
                )
        library = Library(path=target, created=_today())
        self.libraries[label] = library
        return library

    def unique_label(self, base: str) -> str:
        """`alpha` 被占了就 `alpha-2`。adopt 时目录名撞车很常见（两个 `demo`）。"""
        stem = base.strip() or "library"
        if stem not in self.libraries:
            return stem
        n = 2
        while f"{stem}-{n}" in self.libraries:
            n += 1
        return f"{stem}-{n}"

    # ---- 钥匙 ----
    def issue_key(
        self,
        workspace: Path | str,
        library: str | None,
        *,
        access: str = "rw",
    ) -> str:
        """签发一把新钥匙，返回**明文**（只在这一次出现，之后只剩哈希）。

        `library=None` 表示"已拒绝接入"（四态里的 declined），不是错误状态：
        用户明确说过不想接入，也要有东西记住这个决定（§6.3）。
        """
        if access not in ACCESS_VALUES:
            raise RegistryBad(f"access 只能是 {ACCESS_VALUES}，收到 {access!r}")
        if library is not None and library not in self.libraries:
            raise RegistryConflict(
                f"库 `{library}` 还没登记，不能发钥匙。先 set_library()。"
            )
        key = generate_key()
        digest = self.hash_key(key)
        self.keys[digest] = KeyRecord(
            hash=digest,
            workspace=Path(os.path.expandvars(str(workspace))).expanduser(),
            library=library,
            access=access,
            created=_today(),
        )
        return key

    def revoke(self, key: str) -> bool:
        """撤销（软删）。已经撤销过的返回 False —— 重跑迁移不该报错。"""
        record = self.record_for(key)
        if record is None:
            raise KeyUnknown(f"registry 里没有这把钥匙，无从撤销（{_mask(key)}）")
        if record.revoked:
            return False
        record.revoked = _today()
        return True

    def revoke_where(
        self, *, workspace: Path | str | None = None, library: str | None = None
    ) -> list[str]:
        """按归属批量撤销（运维用：手头没有明文 key，只有项目路径）。

        返回被撤销的哈希列表。**不动库文件** —— 撤销只影响"能不能再接入"，
        已经写进去的记忆不受影响（R-6：解绑是单向门，别假装它是回滚）。
        """
        hit: list[str] = []
        for digest, record in self.keys.items():
            if not record.active:
                continue
            if workspace is not None and not same_path(record.workspace, workspace):
                continue
            if library is not None and record.library != library:
                continue
            record.revoked = _today()
            hit.append(digest)
        return hit

    def active_keys(self) -> list[KeyRecord]:
        return [rec for rec in self.keys.values() if rec.active]

    def keys_for(self, workspace: Path | str) -> list[KeyRecord]:
        target = normalize_path(workspace)
        return [rec for rec in self.keys.values() if normalize_path(rec.workspace) == target]


# ---------------------------------------------------------------- 锁与写


def _mask(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "set"


def _is_stale(lock: Path, stale_after: float) -> bool:
    try:
        return (time.time() - lock.stat().st_mtime) > stale_after
    except OSError:  # 刚好被别人清掉了
        return False


@contextmanager
def _locked(path: Path, *, timeout: float = LOCK_TIMEOUT, stale_after: float = LOCK_STALE) -> Iterator[None]:
    """`O_EXCL` 锁。拿不到就等，等超时报 RegistryBusy（不静默继续写）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_stale(lock, stale_after):
                # 上一个进程崩在锁里。留着它等于永久锁死，只能按时间判死。
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise RegistryBusy(
                    f"registry 被另一个进程占用超过 {timeout:g}s：{lock}\n"
                    "通常是另一个窗口正在绑定；等它结束，或确认无进程在写后删掉这个 .lock。"
                )
            time.sleep(_POLL)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover
            pass
        try:
            lock.unlink()
        except OSError:  # pragma: no cover
            pass


@contextmanager
def edit(
    path: Path | str | None = None, *, timeout: float = LOCK_TIMEOUT
) -> Iterator[Registry]:
    """所有写操作的唯一入口：**锁内读 → 改 → 原子落盘**。

    并发绑定时每个进程都在锁内重新读一遍，所以不会出现"后写的覆盖前一条"（R-5）。
    块内抛异常则不落盘。
    """
    file = registry_path(path)
    with _locked(file, timeout=timeout):
        registry = Registry.load(file)
        registry.writable = True
        yield registry
        registry.save()


def read(path: Path | str | None = None) -> Registry:
    """只读快照。`os.replace` 保证读到的永远是完整文件，不需要持锁。"""
    return Registry.load(path)
