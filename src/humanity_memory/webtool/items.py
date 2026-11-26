"""按 id 反查条目正文：给「悬停记忆 ID 弹出内容」的浮层供数。

检索记录（retrieval/*.jsonl）与裁定快照里只有 id、类型、活跃度，没有正文 ——
那是写日志时刻意不落的东西（正文会变，日志只记当时的判定依据）。所以页面上
想把 id 悬停出内容，就必须回库现查一次，这个模块就是那个端点。

与「检索测试」的区别：那边是当场检索拿到的命中，`content` 跟着结果一起返回；
这边的 id 来自历史记录，可能早已被顶替、归档甚至清理，所以查不到要如实返回
（前端显示「库里没有这条记忆」），不能假装有内容。

只读：全部走 ``reports.read_item_contents`` 的 ``mode=ro`` 连接。
"""

from __future__ import annotations

from typing import Any

from ..reports import read_item_contents
from .datasource import WebSource

# 一次请求最多反查多少条：前端浮层一次只 hover 一个 id，这个上限只是防滥用
MAX_IDS = 200


def _parse_ids(raw: str) -> list[str]:
    """逗号分隔（也容忍换行）→ 去重保序的 id 列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in (raw or "").replace("\n", ",").split(","):
        item_id = chunk.strip()
        if item_id and item_id not in seen:
            seen.add(item_id)
            out.append(item_id)
    return out


def get(source: WebSource, raw: str) -> dict[str, Any]:
    """GET /api/item?ids=a,b,c → {ok, items:{id:{content,...}}, ids:[...]}

    ``items`` 里没有的 id 就是查不到（已删除 / 归档在快照里但库里没了）。
    """
    ids = _parse_ids(raw)[:MAX_IDS]
    if not ids:
        return {"ok": True, "items": {}, "ids": [], "missing": []}
    items = read_item_contents(source.db, ids)
    return {
        "ok": True,
        "items": items,
        "ids": ids,
        "missing": [x for x in ids if x not in items],
    }
