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


def archive_decision(
    created_ms: int,
    last_access_ms: int | None,
    access_count: int,
    now_ms: int,
    ttl_days: float,
    max_access: int,
    *,
    verified_ms: int | None = None,
    access_extends: bool = True,
) -> tuple[bool, str]:
    """判断一条记忆该不该归档。

    为什么不用 strength 阈值：`usage_factor` 有 0.5 底分（新内容不该被惩罚），
    导致再冷的条目强度也跌不到 0，拿绝对阈值归档会永远不触发。
    改用两个显性条件 —— 「多久没被碰过」+「被访问过几次」，更好解释也好调。

    `access_extends=False` 用于 state：被检索到**不算续命**，必须有人显式确认
    「我看过了，它还是这样」才刷新 verified_at。否则一条早就过时的
    「Unity 2022.3」会因为反复被查而永远赖在库里 —— 而反复被查，
    恰恰可能是它写得太模糊、或者已经不准的信号。

    归档是**可逆的**：只置 archived_at，默认检索不到，内容一行不动。
    """
    if access_extends:
        touch = last_access_ms if last_access_ms is not None else created_ms
    else:
        touch = max(created_ms, verified_ms or 0)
    age_days = max(0.0, now_ms - touch) / DAY_MS

    if access_extends and access_count > max_access:
        return False, f"访问 {access_count} 次（阈值 {max_access}），保留"
    if age_days < ttl_days:
        return False, f"{age_days:.0f} 天未确认（未到 {ttl_days:.0f} 天）"
    if not access_extends:
        return True, f"{age_days:.0f} 天未被确认，现状可能已过时"
    return True, f"{age_days:.0f} 天未用且仅访问 {access_count} 次"


# ---------------------------------------------------------------- 初始粒度
# 仿生记忆的第一个维度：**重要的事一开始就记得细，琐事一开始就模糊**。
# 不能所有经历都从「完整叙述」起步 —— 那等于把「昨天吃了什么」也录成纪录片。

INITIAL_LEVEL_THRESHOLDS = ((0.75, 0), (0.45, 1), (0.0, 2))


def initial_compression_level(importance: float) -> int:
    """由重要性推导初始压缩级别。

    0.75 以上 → level 0（完整叙述）
    0.45~0.75 → level 1（要点）
    0.45 以下 → level 2（一句话）
    """
    value = max(0.0, min(1.0, float(importance)))
    for floor, level in INITIAL_LEVEL_THRESHOLDS:
        if value >= floor:
            return level
    return 2


def recompress_threshold(base_days: float, importance: float) -> float:
    """压缩门槛按重要性缩放：重要的压得慢，琐碎的压得快。

    importance 0.9 → 1.5 倍门槛；0.2 → 0.8 倍。
    这样「被用户骂了」能比「修了个拼写」多保留一倍的细节时长。
    """
    value = max(0.0, min(1.0, float(importance)))
    return base_days * (0.6 + value)
