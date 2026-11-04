"""令牌文件：把「工作区绑定」与「凭据」从 mcp.json 里解耦。

为什么需要它
------------
`mcp.json` 常常被同步、备份、甚至发给别人。把 API key 和项目路径写在里面，
一旦分享就是双重泄露（凭据 + 目录结构）。

令牌把这两样收进一个文件：

    mcp.json  →  { "args": ["--token", "D:/proj/.ai_memory/memory.token.json"] }

令牌里放 workspace / rules / embedding 三组信息。改配置只需要改令牌，
不用动客户端配置；令牌不进版本库，凭据也就不会跟着 mcp.json 传播。

两种用法并存
------------
1. 每项目一个令牌（推荐）：令牌放项目里，跟着项目走。
2. 全局一个「当前活跃」令牌：切项目时重写它，mcp.json 只留一条。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKEN_NAME = "memory.token.json"
DEFAULT_TOKEN = Path.home() / ".humanity-memory" / "active.token.json"


class TokenError(ValueError):
    pass


@dataclass(slots=True)
class Token:
    path: Path
    workspace: Path
    rules: Path | None = None
    label: str | None = None
    embedding: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    @property
    def display(self) -> str:
        return self.label or self.workspace.name

    def masked(self) -> dict[str, Any]:
        """脱敏视图，供 CLI / 工具展示。"""
        emb = dict(self.embedding)
        key = str(emb.get("api_key") or "")
        if key:
            emb["api_key"] = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"
        return {
            "path": str(self.path),
            "workspace": str(self.workspace),
            "rules": str(self.rules) if self.rules else None,
            "label": self.label,
            "version": self.version,
            "embedding": emb,
        }

    # ---- 读写 ----
    @classmethod
    def load(cls, path: Path | str) -> "Token":
        file = Path(path).expanduser()
        if not file.is_file():
            raise TokenError(f"令牌文件不存在：{file}")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TokenError(f"令牌文件不是合法 JSON：{file}（{exc}）") from exc
        if not isinstance(data, dict):
            raise TokenError(f"令牌文件内容必须是对象：{file}")

        raw_ws = data.get("workspace")
        if not raw_ws:
            raise TokenError(f"令牌缺少 workspace 字段：{file}")
        workspace = Path(os.path.expandvars(str(raw_ws))).expanduser()
        if not workspace.is_absolute():
            # 允许相对路径：相对于令牌文件所在目录
            workspace = (file.parent / workspace).resolve()
        if not workspace.is_dir():
            raise TokenError(
                f"令牌里的 workspace 不是一个存在的目录：{workspace}\n"
                "如果项目挪了位置，用 `humanity-memory workspace use <新路径>` 重写令牌。"
            )

        rules_raw = data.get("rules")
        rules = None
        if rules_raw:
            rules = Path(os.path.expandvars(str(rules_raw))).expanduser()
            if not rules.is_absolute():
                rules = (file.parent / rules).resolve()

        embedding = data.get("embedding") or {}
        if not isinstance(embedding, dict):
            raise TokenError(f"令牌的 embedding 字段必须是对象：{file}")

        return cls(
            path=file.resolve(),
            workspace=workspace.resolve(),
            rules=rules,
            label=data.get("label"),
            embedding=embedding,
            version=int(data.get("version", 1)),
        )

    def save(self) -> Path:
        payload = {
            "version": self.version,
            "label": self.label or self.workspace.name,
            "workspace": str(self.workspace),
            "rules": str(self.rules) if self.rules else None,
            "embedding": self.embedding,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.path

    def apply_embedding_env(self) -> list[str]:
        """把令牌里的 embedding 配置注入进程环境，返回被设置的变量名。

        令牌优先级高于 .env —— 一个令牌就应该完整描述一个工作区。
        """
        mapping = {
            "base_url": "HM_EMBED_BASE_URL",
            "api_key": "HM_EMBED_API_KEY",
            "model": "HM_EMBED_MODEL",
            "dim": "HM_EMBED_DIM",
            "batch": "HM_EMBED_BATCH",
            "timeout": "HM_EMBED_TIMEOUT",
        }
        applied: list[str] = []
        for key, env_name in mapping.items():
            value = self.embedding.get(key)
            if value in (None, ""):
                continue
            os.environ[env_name] = str(value)
            applied.append(env_name)
        return applied


def build_token(
    path: Path | str,
    *,
    workspace: Path | str,
    rules: Path | str | None = None,
    label: str | None = None,
    embedding: dict[str, Any] | None = None,
) -> Token:
    """构造并校验一个新令牌（不落盘）。"""
    ws = Path(os.path.expandvars(str(workspace))).expanduser().resolve()
    if not ws.is_dir():
        raise TokenError(f"workspace 不是存在的目录：{ws}")
    rp = None
    if rules:
        rp = Path(os.path.expandvars(str(rules))).expanduser()
        if not rp.is_absolute():
            rp = (ws / rp).resolve()
        if not rp.is_file():
            raise TokenError(f"rules 文件不存在：{rp}")
    return Token(
        path=Path(path).expanduser().resolve(),
        workspace=ws,
        rules=rp,
        label=label,
        embedding=embedding or {},
    )


def resolve_token_path(value: str | None) -> Path | None:
    """把命令行传入的 --token 解析成实际路径。

    支持传入文件本身，也支持传入目录（自动补默认文件名）。
    """
    if not value:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if path.is_dir():
        path = path / TOKEN_NAME
    return path
