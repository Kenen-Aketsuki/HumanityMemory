"""检索测试：模拟 recall 的检索与打分，但**不产生任何写入**。

设计文档 §2.1 / §7。地基：
* `store.search()` 是纯只读检索（不强化、不写 JSONL）—— 直接复用，不重写取数逻辑；
* 假想 delta 用纯函数 `lifecycle.activity_contribution()` 算，不 UPDATE、不落库；
* 绝不调 `recall()` / `_reinforce()` / `deep_recall()`（后两者会写库）；
* 构造 store 时绕开 `open_db()`（它会 PRAGMA/ensure_schema/set_meta 写库），改用
  只读连接手动装配 —— `__new__` + 只填 search 需要的字段。

任何一步出现写库，都是 bug：tests/test_webtool.py 有只读断言守着。
"""

from __future__ import annotations

import time
from typing import Any

from ..confidence import ConfidenceCurve
from ..db import load_vec_extension
from ..embeddings import EmbeddingProvider
from ..lifecycle import LevelTable, activity_contribution
from ..store import MemoryStore
from .datasource import WebSource

_NOTE = "仅模拟：未写活跃度、未写检索日志、未触发复盘闸门（分析用途，不计入活跃度）"
_NOTE_DEFAULT = _NOTE + " 默认流程：先严格后宽松、先记忆后遗忘；遗忘层只模拟命中，不复活。"
_NOTE_LIMITED = _NOTE + " 已限定检索条件/范围：找不到就显示空，不退回其它方法或区域。"

# 检索条件（怎么找）与检索范围（去哪找）是两个独立维度，和 recall 记录里的
# mode / deep 一一对应。前端选项值统一收在映射里，中文标签也能直接传。
_CONDITIONS = {
    "default": "default", "默认": "default",
    "strict": "strict", "仅严格": "strict",
    "broad": "broad", "仅宽松": "broad",
}
_SCOPES = {
    "default": "default", "默认": "default",
    "memory": "memory", "仅记忆": "memory",
    "forgotten": "forgotten", "仅遗忘": "forgotten",
}


def _normalize_choice(value: Any, choices: dict[str, str], field: str, default: str) -> str:
    if value in (None, ""):
        return default
    raw = str(value).strip()
    picked = choices.get(raw.lower()) or choices.get(raw)
    if picked is None:
        raise ValueError(f"{field} 只能是 {' / '.join(sorted(set(choices.values())))}，收到 {value!r}")
    return picked


def _search_layer(
    store: MemoryStore,
    query: str,
    *,
    kinds: Any,
    limit: int,
    condition: str,
    level_filter: str,
) -> list:
    """单层检索：严格 / 宽松 / 默认（严格不够再宽泛兜底）。

    显式传 `min_score` 会让 `store.search()` 关掉兜底 —— 这就是「仅严格」。
    「仅宽松」直接调宽泛层，跳过严格层；两者都不会跨层补结果。
    """
    if condition == "strict":
        return store.search(
            query, kinds=kinds, scope="all", limit=limit,
            min_score=store.cfg.min_retrieval, level_filter=level_filter,
        )
    if condition == "broad":
        return store._broad_search(
            query, kinds=kinds, scope="all", include_invalid=False,
            include_archived=False, limit=limit, level_filter=level_filter,
        )
    return store.search(query, kinds=kinds, scope="all", limit=limit, level_filter=level_filter)


def _readonly_store(source: WebSource) -> MemoryStore:
    """用只读连接装配一个只够跑 search() 的 MemoryStore，绕过 open_db 的写副作用。"""
    store = MemoryStore.__new__(MemoryStore)
    store.cfg = source.cfg
    store.layout = source.layout
    store.embedder = EmbeddingProvider(source.cfg)
    store.level_table = LevelTable.from_config(source.cfg)
    store.confidence_curve = ConfidenceCurve.from_config(source.cfg)
    store.global_conn = None
    conn = source.connect()  # mode=ro
    # 关键：vec0 虚表扩展平时由 open_db() 加载；我们用 __new__ 绕开了 open_db，
    # 必须在这里手动补上，否则读 episodes_vec / knowledge_vec 会报 "no such module: vec0"，
    # 纯向量命中（长查询常见）会全部丢失 —— 表现就是"模拟搜索查不到，recall 历史却有"。
    # load_vec_extension 只加载扩展、不写库，和只读承诺不冲突。
    store.vec_source = load_vec_extension(conn, source.cfg)
    store.workspace_conn = conn
    return store


def post(source: WebSource, body: dict[str, Any]) -> dict[str, Any]:
    query = str(body.get("query") or "").strip()
    if not query:
        return {"ok": False, "code": "empty_query", "message": "查询不能为空"}
    try:
        limit = max(1, min(50, int(body.get("limit") or 8)))
    except (TypeError, ValueError):
        limit = 8
    kinds = body.get("kinds") or None  # None / str / list
    try:
        # condition = 怎么找（严格/宽松），scope = 去哪找（记忆/遗忘）
        condition = _normalize_choice(
            body.get("condition", body.get("method")), _CONDITIONS, "检索条件", "default"
        )
        scope = _normalize_choice(body.get("scope"), _SCOPES, "检索范围", "default")
    except ValueError as exc:
        return {"ok": False, "code": "bad_param", "message": str(exc)}

    started = time.perf_counter()
    store = _readonly_store(source)
    live_hits: list = []
    forgotten_hits: list = []
    try:
        if scope != "forgotten":
            live_hits = _search_layer(
                store, query, kinds=kinds, limit=limit,
                condition=condition, level_filter="live",
            )
        # 默认范围按 recall 的判据：常规命中相似度低于 deep_recall_floor 才翻遗忘层；
        # 限定「仅遗忘」则直接翻。这里只调 search(level_filter=forgotten)，
        # 绝不调 deep_recall()/ _revive_hit()（那会写复活计数）。
        if scope == "forgotten" or (scope == "default" and store._is_miss(live_hits)):
            forgotten_hits = _search_layer(
                store, query, kinds=kinds, limit=limit,
                condition=condition, level_filter="forgotten",
            )
            for hit in forgotten_hits:
                hit.forgotten = True
    finally:
        # 只关连接，不调 store.close()（后者会碰 embedder，但这里没跑异步任务）
        if store.workspace_conn is not None:
            store.workspace_conn.close()
    took_ms = (time.perf_counter() - started) * 1000.0
    hits = live_hits + forgotten_hits

    floor = source.cfg.activity_sim_floor
    power = source.cfg.activity_curve_power
    rows: list[dict[str, Any]] = []
    for rank, h in enumerate(hits, 1):
        item = h.item
        fused = MemoryStore._fuse(h.vector_similarity, h.lexical_score)
        delta = activity_contribution(fused, sim_floor=floor, curve_power=power)
        current = float(getattr(item, "activity", 0.0) or 0.0)
        rows.append(
            {
                "rank": rank,
                "id": str(item.id),
                "kind": getattr(item, "kind", "unknown"),
                "level": getattr(item, "forget_level", None),
                # 前端悬浮窗要显示条目内容；截 500 字够看全文大意，不撑爆响应
                "content": str(getattr(item, "content", "") or "")[:500],
                "score": round(float(h.score), 4),
                "vec_sim": None if h.vector_similarity is None else round(float(h.vector_similarity), 4),
                "lex": None if h.lexical_score is None else round(float(h.lexical_score), 4),
                "activity": round(current, 4),
                "simulated_delta": round(delta, 4),
                "activity_after": round(current + delta, 4),
                "needs_verify": bool(getattr(h, "needs_verify", False)),
                "fallback": bool(getattr(h, "fallback", False)),
                "forgotten": bool(getattr(h, "forgotten", False)),
            }
        )
    limited = condition != "default" or scope != "default"
    return {
        "ok": True,
        "query": query,
        "limit": limit,
        "condition": condition,
        "scope": scope,
        "hits": rows,
        # 与 recall 记录 / mcp_server 同语义：任一条来自宽泛层即 fallback。
        # 各 hit 也带 fallback / forgotten 标志，前端可逐条标注。
        "mode": "fallback" if any(h.fallback for h in hits) else "strict",
        "deep": bool(forgotten_hits),
        "took_ms": round(took_ms, 1),
        "note": _NOTE_LIMITED if limited else _NOTE_DEFAULT,
    }
