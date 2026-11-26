"""HumanityMemory —— 基于 SQLite + sqlite-vec 的 AI 记忆库。

对外提供两种形态：
- MCP server：`python -m humanity_memory.mcp_server`
- CLI：`humanity-memory <command>`

记忆只有两类：经验（Experience）与经历（Episode）。
"""

from __future__ import annotations

from pathlib import Path

from .config import NO_LIBRARY, Config
from .models import Episode, Experience, Hit, LogEntry
from .paths import Layout, resolve_layout
from .store import MemoryStore, NotFound, ScopeError

# 版本基线：源码态显示这个值，也是打包时没指定 --target 的兜底。
# 发布版 exe 的真实版本由 tools/build_release.py 在打包时随包写入 version.txt，
# 所以日常滚动发布**不需要改这个文件**，也就不会在 git 里留下版本号 diff。
_BASE_VERSION = "2.0"


def _packaged_version() -> str:
    """读打包时随包附带的 version.txt；源码态没有这个文件，返回空串。"""
    try:
        text = Path(__file__).with_name("version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if _looks_like_version(text) else ""


def _looks_like_version(text: str) -> bool:
    # 防止打包残留/手误把乱七八糟的内容当成版本号显示出去
    head = text.split("+", 1)[0]
    return bool(head) and head[0].isdigit() and all(ch.isalnum() or ch in ".-%" for ch in head)


__version__ = _packaged_version() or _BASE_VERSION

__all__ = [
    "Config",
    "Episode",
    "Experience",
    "Hit",
    "Layout",
    "LogEntry",
    "MemoryStore",
    "NO_LIBRARY",
    "NotFound",
    "ScopeError",
    "resolve_layout",
    "__version__",
]
