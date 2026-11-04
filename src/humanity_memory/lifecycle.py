"""记忆的生命周期：时间衰减、访问强化、综合打分。

设计取舍
--------
库里 `memories.strength` 存的是**最后一次被触碰时的强度**，不是当前强度。
真实强度必须实时算：它随「距离上次访问的时间」按半衰期指数衰减。
这样写入/强化只需要一次 UPDATE，读取时零写放大。
"""

from __future__ import annotations

import math

DAY_MS = 86_400_000.0


def decay(strength: float, elapsed_ms: float, halflife_days: float) -> float:
    """指数衰减：经过 halflife_days 后强度减半。"""
    if strength <= 0 or halflife_days <= 0:
        return 0.0
    days = max(0.0, elapsed_ms) / DAY_MS
    return strength * math.pow(0.5, days / halflife_days)


def current_strength(
    strength: float,
    last_touch_ms: int | None,
    now_ms: int,
    halflife_days: float,
) -> float:
    if last_touch_ms is None:
        return strength
    return decay(strength, now_ms - last_touch_ms, halflife_days)


def reinforce(strength: float, boost: float = 0.20) -> float:
    """强化向 1.0 渐近：访问越频繁，单次提升越小，避免直接刷满。"""
    boost = min(max(boost, 0.0), 1.0)
    return min(1.0, strength + (1.0 - strength) * boost)


def similarity_from_cosine_distance(distance: float) -> float:
    """vec0 cosine 度量返回 1 - cos，范围 [0, 2]，映射到 [0, 1]。"""
    return max(0.0, min(1.0, 1.0 - distance / 2.0))


def combine_score(
    *,
    retrieval: float,
    importance: float,
    strength: float,
    w_retrieval: float,
    w_importance: float,
    w_strength: float,
) -> float:
    total = w_retrieval + w_importance + w_strength
    if total <= 0:
        return 0.0
    return (
        w_retrieval * retrieval + w_importance * importance + w_strength * strength
    ) / total


def usage_factor(access_count: int, cap: int = 20) -> float:
    """「用进废退」的进。

    从未被召回也有 0.5 的底分 —— 新写入的内容不该因为"还没被用过"而被惩罚；
    之后随召回次数对数增长，很快饱和到 1.0。
    """
    if access_count <= 0:
        return 0.5
    return 0.5 + 0.5 * min(1.0, math.log1p(access_count) / math.log1p(cap))


def recency_factor(age_ms: float, halflife_days: float) -> float:
    return decay(1.0, age_ms, halflife_days)


def lifecycle_strength(
    created_ms: int,
    access_count: int,
    now_ms: int,
    halflife_days: float,
    *,
    recency_weight: float = 0.55,
) -> float:
    """综合新鲜度与使用频次，得到一个 0~1 的强度。

    事实的 halflife 极长，recency 近乎恒定，强度几乎只由使用频次决定 ——
    这正是「事实不遗忘、但常用的更容易浮上来」的期望行为。
    """
    recency = recency_factor(max(0, now_ms - created_ms), halflife_days)
    usage = usage_factor(access_count)
    return recency_weight * recency + (1.0 - recency_weight) * usage
