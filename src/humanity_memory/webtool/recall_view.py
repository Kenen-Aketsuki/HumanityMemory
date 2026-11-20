"""recall 历史视图：复用 reports 四函数 + level_thresholds，输出与 CLI 报告同构的 JSON。

数据来源：工作区 retrieval/*.jsonl（检索记录）+ 库实时 activity + 最新裁定快照的节点阈值。
与 cli.cmd_retrieval_report 塞进 HTML 的 DATA 结构一致（设计文档 §5 / §11.1）。
"""

from __future__ import annotations

from typing import Any

from ..reports import (
    latest_judge_snapshot,
    level_thresholds,
    read_node_activity,
    read_retrievals,
)
from .datasource import WebSource


def get(source: WebSource, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    q = query or {}
    try:
        days = int(q.get("days", [0])[0] or 0)
    except (ValueError, IndexError):
        days = 0

    records = read_retrievals(source.library_dir, source.cfg, days=days)
    snapshot = latest_judge_snapshot(source.library_dir, source.cfg)
    nodes = [
        {
            "level": n.get("level"),
            "activity": n.get("activity"),
            "up": n.get("up_threshold"),
            "down": n.get("down_threshold"),
        }
        for n in (snapshot or {}).get("nodes", [])
    ]
    return {
        "ok": True,
        "records": records,
        "meta": {
            "db": str(source.db),
            "days": days,
            "levels": level_thresholds(source.cfg),
            "nodes": nodes,
            "activity": read_node_activity(source.library_dir),
            "generated": (snapshot or {}).get("generated_at"),
        },
    }
