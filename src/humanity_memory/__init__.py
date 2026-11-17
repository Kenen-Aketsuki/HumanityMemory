"""HumanityMemory —— 基于 SQLite + sqlite-vec 的 AI 记忆库。

对外提供两种形态：
- MCP server：`python -m humanity_memory.mcp_server`
- CLI：`humanity-memory <command>`

记忆只有两类：经验（Experience）与经历（Episode）。
"""

from __future__ import annotations

from .config import Config
from .models import Episode, Experience, Hit, LogEntry
from .paths import Layout, resolve_layout
from .store import MemoryStore, NotFound, ScopeError

__version__ = "0.4.0"

__all__ = [
    "Config",
    "Episode",
    "Experience",
    "Hit",
    "Layout",
    "LogEntry",
    "MemoryStore",
    "NotFound",
    "ScopeError",
    "resolve_layout",
    "__version__",
]
