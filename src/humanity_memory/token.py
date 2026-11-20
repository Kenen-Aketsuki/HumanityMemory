"""令牌文件（v2）：只承载「工作区身份」，不承载凭据，也不指向库。

v1 → v2 换掉的三件事（docs/workspace-binding-and-token.md §0 / §5.3）
---------------------------------------------------------------------
1. **落点**：从 `~/.humanity-memory/active.token.json`（全局一条「当前活跃」）
   改为 `<workspace_root>/.ai_memory/token.json` —— 令牌跟着项目走。
   身份回到 cwd，令牌只是「这个工作区接入过」的证明。
2. **字段**：`key` / `workspace` / `library` / `label`。`embedding` 与
   `apply_embedding_env()` 彻底拆除（凭据只走 `.env`）；`rules` 也不再写，
   信条固定从项目里探测（`<root>/.workbuddy/memory/信条.md` 或 `CORE_RULES.md`）。

`library` 是**断言**，不是寻址
------------------------------
`library: "island"` 表示"这个工作区的用户决定用 island 这个库"，
`library: null` 表示"明确决定不接入"（四态里的「已拒绝」，§5.2）——
拒绝也是要记住的决定，否则三个月后没人知道这里为什么记忆全空（§6.3）。

但**归属的真相永远在服务端 registry**：令牌里的 library 只用来跟 registry 的
记录比对（§5.5 第 6 步），不一致就说明令牌被手改过，直接拒绝。所以把令牌
改成 `"library": "别人的库"` 不会拿到别人的库（R7）——最多把自己弄坏。
3. **库路径不进令牌**：访问哪个库由服务端 registry 决定（`keys.py`），
   改令牌指不到别人的库。

v1 只识别不降级
---------------
读到 `version < 2` 直接抛 `TokenV1`（`code = "token_v1"`），不做静默兼容：
拿不准就不放行，迁移由 `bind adopt` 重签一份 v2 令牌。错误码与 §6.2 对齐，
上层（`mcp_server` / `cli`）把它原样塞进拒绝契约，不在本模块拼文案。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import WORKSPACE_DIR

TOKEN_NAME = "token.json"
TOKEN_VERSION = 2
KEY_PREFIX = "hmk_"


class TokenError(ValueError):
    """令牌不可用。

    `code` 不是装饰：拒绝契约（§6.1）里的 `code` 字段直接取它，
    所以子类只改这个类属性，不再另编一套错误字符串。
    """

    code = "token_bad"

    def __init__(self, message: str, *, path: Path | str | None = None) -> None:
        super().__init__(message)
        self.path = Path(path) if path is not None else None


class TokenMissing(TokenError):
    """令牌文件不存在 —— 四态里的「未决定」。"""

    code = "no_token"


class TokenBad(TokenError):
    """JSON 坏了、缺字段、key 形状不对、版本比本程序还新。"""

    code = "token_bad"


class TokenV1(TokenError):
    """v1 令牌。只识别，不降级。"""

    code = "token_v1"


class TokenWorkspaceMismatch(TokenError):
    """令牌里的 workspace 不是本次解析出的工作区（复制 / 搬家 / 手改）。"""

    code = "token_workspace_mismatch"


# ---------------------------------------------------------------- 路径与比较


def normalize_path(path: Path | str) -> str:
    """把路径变成「可比较的形状」：展开变量 → 解析软链 → 统一大小写。

    身份比较只认这个函数。Windows 上 `d:\\` 与 `D:\\` 是同一个目录，
    `normcase` 抹掉这层差异；`resolve` 抹掉 junction 与 8.3 短名；
    `abspath` 收掉 `..`。三者缺一，同一项目就会算出两个身份（见附录 B）。
    """
    raw = os.path.expandvars(str(path))
    candidate = Path(raw).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:  # pragma: no cover - 盘符未挂载 / 权限不足
        candidate = Path(os.path.abspath(candidate))
    return os.path.normcase(str(candidate))


def same_path(left: Path | str, right: Path | str) -> bool:
    return normalize_path(left) == normalize_path(right)


def token_path(workspace_root: Path | str) -> Path:
    """`<root>/.ai_memory/token.json`。

    直接传 `.ai_memory` 目录也认 —— 调用方常常从 `find_workspace_dir()`
    拿到的是 `.ai_memory` 而不是它的父目录，不该为此在各处补分支。
    """
    root = Path(os.path.expandvars(str(workspace_root))).expanduser()
    return (root if root.name == WORKSPACE_DIR else root / WORKSPACE_DIR) / TOKEN_NAME


def generate_key() -> str:
    """签发一个新 key：`hmk_` + 32 位十六进制。

    这里只管**格式**。登记、哈希入 registry、撤销是 `keys.py` 的事
    （T2）；把格式留在本模块，是因为令牌的线上格式由这里定义，
    `keys.py` 是它的上层，不该各写一份前缀。
    """
    return KEY_PREFIX + secrets.token_hex(16)


# ---------------------------------------------------------------- 令牌


@dataclass(slots=True)
class Token:
    """一个工作区的接入证明。不含凭据、不含库路径。

    `library is None` 是**已拒绝**（declined），不是"还没决定"——
    还没决定的表现是令牌文件根本不存在（四态，§5.2）。
    """

    path: Path
    key: str
    workspace: Path
    library: str | None
    label: str | None = None
    version: int = TOKEN_VERSION

    @property
    def declined(self) -> bool:
        return self.library is None

    @property
    def display(self) -> str:
        return self.label or self.workspace.name

    def masked(self) -> dict[str, Any]:
        """脱敏视图，供 CLI / 工具展示。key 永远不整条打印。"""
        key = self.key or ""
        shown = f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "set"
        return {
            "path": str(self.path),
            "workspace": str(self.workspace),
            "library": self.library,
            "label": self.display,
            "version": self.version,
            "key": shown,
        }

    def to_payload(self) -> dict[str, Any]:
        """落盘内容。字段是白名单，不是「把对象 dict 出去」——
        免得哪天给 dataclass 加个字段就顺手写进了令牌。"""
        return {
            "version": TOKEN_VERSION,
            "key": self.key,
            "workspace": str(self.workspace),
            "library": self.library,
            "label": self.label or self.workspace.name,
        }

    def save(self) -> Path:
        """原子落盘：临时文件 + `os.replace`。

        令牌写坏等于工作区失联，所以宁可多一次 rename 也不直接覆写。
        """
        payload = self.to_payload()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    def same_workspace(self, root: Path | str) -> bool:
        return same_path(self.workspace, root)

    def assert_workspace(self, root: Path | str) -> None:
        """§5.5 第 4 步。失败给的是「怎么修」，不是「哪里不对」。"""
        if self.same_workspace(root):
            return
        exists = self.workspace.is_dir()
        if exists:
            hint = "该令牌属于另一个工作区（复制过工程，或这里不是它的项目根）。"
        else:
            hint = (
                f"令牌指向的目录已不存在（{self.workspace}）——"
                "项目搬过家，用 `bind move --from <旧> --to <新>` 改，不要手改令牌。"
            )
        raise TokenWorkspaceMismatch(
            f"令牌 {self.path} 的 workspace 与本工作区不一致："
            f"令牌={self.workspace}，本次={Path(root)}。{hint}",
            path=self.path,
        )

    # ---- 读 ----
    @classmethod
    def load(cls, path: Path | str) -> "Token":
        """读令牌。**不检查 workspace 目录是否存在** —— 搬家后的诊断
        （`bind move`）必须先能读到这份令牌，才谈得上修它。"""
        file = Path(os.path.expandvars(str(path))).expanduser()
        if not file.is_file():
            raise TokenMissing(
                f"令牌文件不存在：{file}\n"
                "这个工作区尚未接入记忆库。四个选择：沿用本目录已有的库（bind adopt）/"
                "绑到已有库（bind existing）/ 新建库（bind new）/ 本次不接入（bind decline）。",
                path=file,
            )
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TokenBad(f"令牌文件不是合法 JSON：{file}（{exc}）", path=file) from exc
        if not isinstance(data, dict):
            raise TokenBad(f"令牌内容必须是一个 JSON 对象：{file}", path=file)

        version = _read_version(data, file)
        if version < TOKEN_VERSION:
            raise TokenV1(
                f"这是 v{version} 令牌：{file}\n"
                "v2 不再接受它的字段（embedding 凭据已拆除、库归属改由服务端 registry 决定）。\n"
                "迁移：`humanity-memory bind adopt` 会按现有库重签一份 v2 令牌，凭据请放 .env。",
                path=file,
            )
        if version > TOKEN_VERSION:
            raise TokenBad(
                f"令牌版本 v{version} 高于本程序支持的 v{TOKEN_VERSION}：{file}\n"
                "升级 humanity-memory，别用旧程序读新令牌。",
                path=file,
            )

        key = data.get("key")
        if not isinstance(key, str) or not key.strip():
            raise TokenBad(f"令牌缺少 key 字段（或不是字符串）：{file}", path=file)
        key = key.strip()
        if not key.startswith(KEY_PREFIX):
            raise TokenBad(
                f"令牌的 key 形状不对（应以 {KEY_PREFIX} 开头）：{file}\n"
                "token 被手改过就重签一份（bind adopt）。",
                path=file,
            )

        raw_ws = data.get("workspace")
        if not raw_ws:
            raise TokenBad(f"令牌缺少 workspace 字段：{file}", path=file)
        workspace = Path(os.path.expandvars(str(raw_ws))).expanduser()
        if not workspace.is_absolute():
            # 容忍手写相对路径：相对令牌所在目录，而不是相对 cwd
            workspace = (file.parent / workspace).resolve()
        else:
            workspace = Path(os.path.abspath(workspace))

        # `library` 必须显式出现：缺失不能猜成 null —— null 是"已拒绝"，
        # 猜错就等于替用户做了一个"以后别再问"的决定。
        if "library" not in data:
            raise TokenBad(
                f"令牌缺少 library 字段：{file}\n"
                "v2 令牌必须显式声明绑定目标（字符串）或拒绝接入（null）。",
                path=file,
            )
        raw_lib = data["library"]
        if raw_lib is None:
            library = None
        elif isinstance(raw_lib, str):
            library = raw_lib.strip() or None
        else:
            raise TokenBad(f"令牌的 library 必须是字符串或 null：{file}", path=file)

        label = data.get("label")
        return cls(
            path=file.resolve(),
            key=key,
            workspace=workspace,
            library=library,
            label=str(label) if label else None,
            version=version,
        )


def _read_version(data: dict[str, Any], file: Path) -> int:
    raw = data.get("version", 1)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TokenBad(f"令牌的 version 不是整数（{raw!r}）：{file}", path=file) from exc


# ---------------------------------------------------------------- 构造


def build_token(
    path: Path | str,
    *,
    workspace: Path | str,
    library: str | None,
    key: str | None = None,
    label: str | None = None,
) -> Token:
    """构造一个 v2 令牌（不落盘）。`key` 不传就现签一个。

    `library` 是必填的 —— 连 `None` 都要写出来（"明确拒绝"），
    不给默认值是为了让每个签发点都回答一次"这个工作区归哪个库"。

    只校验「这个工作区存在」。库是否存在、registry 里有没有这个 key，
    都不归本模块判断 —— 那是 §5.5 第 5-7 步的事。
    """
    ws = Path(os.path.expandvars(str(workspace))).expanduser()
    if not ws.is_absolute():
        ws = Path(os.path.abspath(ws))
    if not ws.is_dir():
        raise TokenBad(f"workspace 不是存在的目录：{ws}")
    return Token(
        path=Path(os.path.expandvars(str(path))).expanduser().resolve(),
        key=(key or "").strip() or generate_key(),
        workspace=ws,
        library=(library.strip() or None) if library else None,
        label=label,
        version=TOKEN_VERSION,
    )


def resolve_token_path(value: str | None) -> Path | None:
    """把命令行传入的 `--token` 解析成实际路径。

    v2 里 `--token` 已不是身份来源（A2），只是「令牌路径覆盖」；
    仍支持传目录，自动补 `token.json`。
    """
    if not value:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if path.is_dir() or path.name == WORKSPACE_DIR:
        path = path / TOKEN_NAME
    return path
