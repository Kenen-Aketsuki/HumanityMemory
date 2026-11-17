"""生命周期数学的正确性。

只测纯函数：打分、强化、相似度映射、归档判据、等级倍率。
`decay` / `lifecycle_strength` / `usage_factor` 已随旧强度方案删除 ——
排序的第三项现在是遗忘等级，见 `test_rank_by_level.py` 的端到端排序验证。
"""

from __future__ import annotations

import pytest

from humanity_memory.lifecycle import (
    DAY_MS,
    archive_decision,
    combine_score,
    level_factor,
    reinforce,
    similarity_from_cosine_distance,
)


# ---------------------------------------------------------------- 综合打分


def test_combine_score_weighted() -> None:
    score = combine_score(
        retrieval=1.0,
        importance=0.0,
        level=0.0,
        w_retrieval=0.7,
        w_importance=0.15,
        w_level=0.15,
    )
    assert score == pytest.approx(0.7)

    # 权重和不为 1 时也会归一化
    normalised = combine_score(
        retrieval=1.0,
        importance=1.0,
        level=1.0,
        w_retrieval=2.0,
        w_importance=2.0,
        w_level=2.0,
    )
    assert normalised == pytest.approx(1.0)


def test_combine_score_zero_weights() -> None:
    assert combine_score(
        retrieval=1.0, importance=1.0, level=1.0,
        w_retrieval=0.0, w_importance=0.0, w_level=0.0,
    ) == 0.0


def test_combine_score_level_term_dominates_when_asked() -> None:
    """等级项确实是独立的一项：它掉到 0 就该把总分拉低。"""
    high = combine_score(
        retrieval=0.8, importance=0.5, level=1.0,
        w_retrieval=0.5, w_importance=0.1, w_level=0.4,
    )
    low = combine_score(
        retrieval=0.8, importance=0.5, level=0.0,
        w_retrieval=0.5, w_importance=0.1, w_level=0.4,
    )
    assert low < high
    assert high - low == pytest.approx(0.4 * 1.0 / 1.0)


# ---------------------------------------------------------------- 等级倍率


def test_level_factor_formula() -> None:
    """`(level / max_level) ^ power`，0 级恒为 0。"""
    assert level_factor(5, 5, 1.5) == pytest.approx(1.0)
    assert level_factor(4, 5, 1.5) == pytest.approx((4 / 5) ** 1.5)
    assert level_factor(1, 5, 1.5) == pytest.approx((1 / 5) ** 1.5)
    assert level_factor(0, 5, 1.5) == 0.0, "0 级是遗忘层，倍率为 0 是「漏进来也排最后」的兜底"


def test_level_factor_monotonic() -> None:
    values = [level_factor(lv, 5, 1.5) for lv in range(0, 6)]
    assert values == sorted(values)


def test_level_factor_overrides_win() -> None:
    assert level_factor(3, 5, 1.5, overrides={3: 0.99}) == pytest.approx(0.99)
    assert level_factor(4, 5, 1.5, overrides={3: 0.99}) == pytest.approx((4 / 5) ** 1.5)


def test_level_factor_degenerate_inputs() -> None:
    assert level_factor(3, 0, 1.5) == 0.0, "没有等级表就没有倍率，不能除零"
    assert level_factor(99, 5, 1.5) == pytest.approx(1.0), "越界夹到顶档"


# ---------------------------------------------------------------- 强化与相似度


def test_reinforce_asymptotic() -> None:
    # 永远不会超过 1.0
    value = 0.0
    for _ in range(50):
        value = reinforce(value, 0.2)
    assert value < 1.0
    assert value > 0.99


def test_reinforce_from_high_base() -> None:
    assert reinforce(0.9, 0.2) > 0.9
    assert reinforce(1.0, 0.2) == 1.0


def test_similarity_mapping() -> None:
    # cosine 距离 = 1 - cos，区间 [0, 2]
    assert similarity_from_cosine_distance(0.0) == pytest.approx(1.0)
    assert similarity_from_cosine_distance(1.0) == pytest.approx(0.5)
    assert similarity_from_cosine_distance(2.0) == pytest.approx(0.0)
    # 越界钳制
    assert similarity_from_cosine_distance(-1.0) == 1.0
    assert similarity_from_cosine_distance(5.0) == 0.0


# ---------------------------------------------------------------- 归档判据


def test_archive_decision_young_item_kept() -> None:
    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 10 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is False
    assert "未到" in reason


def test_archive_decision_old_unused_archived() -> None:
    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 700 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is True
    assert "700 天" in reason


def test_archive_decision_frequently_used_kept() -> None:
    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 700 * DAY_MS), int(now - 700 * DAY_MS), 5, int(now), 540, 1)
    assert should is False


def test_archive_decision_uses_last_access_not_created() -> None:
    """刚被用过就应该续命，即使创建得很早。"""
    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 900 * DAY_MS), int(now - 5 * DAY_MS), 0, int(now), 540, 1)
    assert should is False
