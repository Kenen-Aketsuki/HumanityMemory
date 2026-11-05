"""生命周期数学的正确性。"""

from __future__ import annotations

import math

import pytest

from humanity_memory.lifecycle import (
    DAY_MS,
    combine_score,
    current_strength,
    decay,
    reinforce,
    similarity_from_cosine_distance,
)


def test_decay_half_life() -> None:
    # 经过一个半衰期，强度正好减半
    assert decay(1.0, 14 * DAY_MS, 14) == pytest.approx(0.5, rel=1e-6)
    assert decay(1.0, 28 * DAY_MS, 14) == pytest.approx(0.25, rel=1e-6)


def test_decay_zero_and_monotonic() -> None:
    assert decay(0.8, 0, 14) == pytest.approx(0.8)
    values = [decay(1.0, d * DAY_MS, 14) for d in (0, 7, 14, 30, 90)]
    assert values == sorted(values, reverse=True)


def test_decay_negative_elapsed_clamped() -> None:
    # 时钟回拨不应让强度变大
    assert decay(0.5, -1000, 14) == pytest.approx(0.5)


def test_decay_invalid_halflife() -> None:
    assert decay(1.0, 1000, 0) == 0.0


def test_current_strength_without_touch() -> None:
    assert current_strength(0.7, None, 12345, 30) == pytest.approx(0.7)


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


def test_combine_score_weighted() -> None:
    score = combine_score(
        retrieval=1.0,
        importance=0.0,
        strength=0.0,
        w_retrieval=0.7,
        w_importance=0.15,
        w_strength=0.15,
    )
    assert score == pytest.approx(0.7)

    # 权重和不为 1 时也会归一化
    normalised = combine_score(
        retrieval=1.0,
        importance=1.0,
        strength=1.0,
        w_retrieval=2.0,
        w_importance=2.0,
        w_strength=2.0,
    )
    assert normalised == pytest.approx(1.0)


def test_combine_score_zero_weights() -> None:
    assert combine_score(
        retrieval=1.0, importance=1.0, strength=1.0,
        w_retrieval=0.0, w_importance=0.0, w_strength=0.0,
    ) == 0.0


# ---------------------------------------------------------------- 用进废退


def test_usage_factor_grows_and_saturates() -> None:
    from humanity_memory.lifecycle import usage_factor

    assert usage_factor(0) == pytest.approx(0.5)
    assert usage_factor(0) > 0
    values = [usage_factor(n) for n in (1, 2, 5, 10, 20, 100)]
    assert values == sorted(values)
    assert values[-2] <= 1.0
    assert usage_factor(20) == pytest.approx(1.0)
    assert usage_factor(100) == pytest.approx(1.0)


def test_lifecycle_strength_fact_barely_decays() -> None:
    """事实半衰期极长：一年后新鲜度仍有 93%，强度几乎不掉。"""
    from humanity_memory.lifecycle import lifecycle_strength

    now = 1_000 * DAY_MS
    created = now - 365 * DAY_MS
    fact_strength = lifecycle_strength(int(created), 0, int(now), 3650)
    assert fact_strength > 0.7

    log_strength = lifecycle_strength(int(created), 0, int(now), 14)
    assert log_strength < 0.3
    assert fact_strength > log_strength * 2


def test_lifecycle_strength_rewards_usage() -> None:
    from humanity_memory.lifecycle import lifecycle_strength

    now = 10_000 * DAY_MS
    created = now - 90 * DAY_MS
    idle = lifecycle_strength(int(created), 0, int(now), 180)
    used = lifecycle_strength(int(created), 20, int(now), 180)
    assert used > idle


# ---------------------------------------------------------------- 归档判据


def test_archive_decision_young_item_kept() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 10 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is False
    assert "未到" in reason


def test_archive_decision_old_unused_archived() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 700 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is True
    assert "700 天" in reason


def test_archive_decision_frequently_used_kept() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 700 * DAY_MS), int(now - 700 * DAY_MS), 5, int(now), 540, 1)
    assert should is False


def test_archive_decision_uses_last_access_not_created() -> None:
    """刚被用过就应该续命，即使创建得很早。"""
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 900 * DAY_MS), int(now - 5 * DAY_MS), 0, int(now), 540, 1)
    assert should is False


# ---------------------------------------------------------------- 归档判据


def test_archive_decision_young_item_kept() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 10 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is False
    assert "未到" in reason


def test_archive_decision_old_unused_archived() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, reason = archive_decision(int(now - 700 * DAY_MS), None, 0, int(now), 540, 1)
    assert should is True
    assert "700 天" in reason


def test_archive_decision_frequently_used_kept() -> None:
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 700 * DAY_MS), int(now - 700 * DAY_MS), 5, int(now), 540, 1)
    assert should is False


def test_archive_decision_uses_last_access_not_created() -> None:
    """刚被用过就应该续命，即使创建得很早。"""
    from humanity_memory.lifecycle import archive_decision

    now = 1000 * DAY_MS
    should, _ = archive_decision(int(now - 900 * DAY_MS), int(now - 5 * DAY_MS), 0, int(now), 540, 1)
    assert should is False
