"""外部可视化与分析工具（本地 HTTP 后端 + 单页前端）。

定位：**观测台**，只读。所有端点现读现算，不写库、不写活跃度、不写检索日志。
设计文档见 docs/external-visualization-tool.md。
"""

from .server import serve

__all__ = ["serve"]
