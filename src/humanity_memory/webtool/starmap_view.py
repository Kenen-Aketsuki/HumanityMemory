"""星图视图：复用 starmap.build_graph_payload，输出 graph JSON。

不写文件、不渲染 HTML（渲染交给前端 canvas）。与 CLI 的 `starmap` 子命令共用同一取数函数
（`starmap.build_graph_payload`）；老的 `starmap.bat` 入口已于 2026-11-25 删除，改由本工具内嵌。
设计文档 §5。
"""

from __future__ import annotations

from typing import Any

from ..starmap import STARMAP_DEFAULTS, build_graph_payload
from .datasource import WebSource


def get(source: WebSource) -> dict[str, Any]:
    graph, _extras = build_graph_payload(source.db, STARMAP_DEFAULTS)
    return {"ok": True, "graph": graph}
