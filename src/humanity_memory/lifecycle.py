"""记忆的生命周期：时间衰减、访问强化、综合打分。

设计取舍
--------
库里 `memories.strength` 存的是**最后一次被触碰时的强度**，不是当前强度。
真实强度必须实时算：它随「距离上次访问的时间」按半衰期指数衰减。
这样写入/强化只需要一次 UPDATE，读取时零写放大。
"""

from __future__ import annotations

import math
from typing import Sequence

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
    """判断一条记忆该不该归档（**现状已不走这里，见 state_review_status**）。

    为什么不用 strength 阈值：`usage_factor` 有 0.5 底分（新内容不该被惩罚），
    导致再冷的条目强度也跌不到 0，拿绝对阈值归档会永远不触发。
    改用两个显性条件 —— 「多久没被碰过」+「被访问过几次」，更好解释也好调。

    `access_extends=False` 是 state 从前的用法（被检索到不算续命）。现状改用
    复核梯子与宽限期后由 `state_review_status` 独立判定，这里的开关只为兼容保留。

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


# ---------------------------------------------------------------- 现状的复核
# 现状不该靠「被读到」续命，也不该靠「访问次数」留人 —— 它只有一条命：
# 到点了有人来看一眼。看一眼还在，续一段；发现不对，当场删掉。
#
# 周期不是固定的：刚写下的现状最容易错（写的时候就过时了），所以 1 天就验一次；
# 连续验过几次都没变，才逐步放宽到 7 天。这就是「渐进式周期」。

REVIEW_OK = "ok"
REVIEW_DUE = "due"
REVIEW_OVERDUE = "overdue"


def next_interval(current_days: float, ladder: Sequence[float]) -> float:
    """复核确认「没变」之后升一档；已在顶端就留在顶端。"""
    steps = sorted(float(x) for x in ladder if x > 0) or [max(float(current_days), 1.0)]
    for step in steps:
        if step > current_days:
            return step
    return steps[-1]


def state_grace_days(interval_days: float, cap_days: float) -> float:
    """宽限期 = 一个当前周期，但不超过 cap。单次到期只宽限一次。"""
    return min(max(float(interval_days), 0.0), max(float(cap_days), 0.0))


def state_deadlines(
    created_ms: int,
    verified_ms: int | None,
    interval_days: float,
    grace_cap_days: float,
) -> tuple[int, int]:
    """返回 (该复核的时刻, 宽限结束的时刻)，单位毫秒。

    起点取「创建」与「上次确认」中较晚者 —— 未确认过就从创建时刻起算。
    """
    base = max(int(created_ms), int(verified_ms or 0))
    interval = max(float(interval_days), 0.0)
    due = base + int(interval * DAY_MS)
    expire = due + int(state_grace_days(interval, grace_cap_days) * DAY_MS)
    return due, expire


def state_review_status(
    created_ms: int,
    verified_ms: int | None,
    interval_days: float,
    now_ms: int,
    grace_cap_days: float,
) -> tuple[str, str]:
    """现状的复核状态：ok / due（该复核了）/ overdue（宽限也过了）。

    overdue 的条目在归档扫描时会被归档；due 的只进待复核清单，等有人来看。
    """
    due, expire = state_deadlines(created_ms, verified_ms, interval_days, grace_cap_days)
    if now_ms < due:
        left = (due - now_ms) / DAY_MS
        return REVIEW_OK, f"还有 {left:.1f} 天到期"
    if now_ms < expire:
        left = (expire - now_ms) / DAY_MS
        return REVIEW_DUE, f"已到期，宽限剩 {left:.1f} 天"
    late = (now_ms - expire) / DAY_MS
    return REVIEW_OVERDUE, f"宽限已过 {late:.1f} 天，应归档"


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
