"""HumanityMemory —— 基于 SQLite + sqlite-vec 的 AI 记忆库。

对外提供两种形态：
- MCP server：`python -m humanity_memory.mcp_server`
- CLI：`humanity-memory <command>`
"""

from __future__ import annotations

from .config import Config
from .models import Episode, Fact, Hit, LogEntry
from .paths import Layout, resolve_layout
from .store import MemoryStore, NotFound, ScopeError

__version__ = "0.2.0"

__all__ = [
    "Config",
    "Episode",
    "Fact",
    "Hit",
    "Layout",
    "LogEntry",
    "MemoryStore",
    "NotFound",
    "ScopeError",
    "resolve_layout",
    "__version__",
]
