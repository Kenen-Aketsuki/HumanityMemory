"""星图辉光颜色 = 活跃度 ÷ 本档阈值（2026-11-17 第二种口径）。

第一种口径（秩归一，见 `test_starmap_activity.py`）让颜色沿节点均匀铺开，
但颜色只说明「你排第几」。这一种回到绝对尺度：

    act01 = activity ÷ 本档的 up_threshold

``1.0`` 恰好是升档线 —— 下一次周期裁定就该升了；``0`` 是本周期没被检索到。
分母必须来自配置的 ``[[forget.levels]]``，所以本文件守的是「阈值怎么取、
取不到怎么办、跨档为什么不能共用一个分母」这几件事。
"""

from __future__ import annotations

import pytest

from humanity_memory import starmap

# 摘了默认配置里关键的几档：L3 升档线 5.0、L5 升档线 9.5、L10 顶档只有降档线 27.2
LEVELS = [
    {"level": 10, "down_threshold": 27.2},
    {"level": 9, "up_threshold": 34.0, "down_threshold": 20.0},
    {"level": 5, "up_threshold": 9.5, "down_threshold": 5.6},
    {"level": 3, "up_threshold": 5.0, "down_threshold": 2.8},
    {"level": 1, "up_threshold": 2.0, "down_threshold": 0.5},
]


def _node(level: int | None = 5, activity: float = 0.0, kind: str = "experience", **kw) -> dict:
    node = {
        "id": kw.pop("id", "x"),
        "kind": kind,
        "activity": activity,
        "access_count": 0,
        "last_accessed_at": None,
        "at": None,
        "forget_level": 5 if level is None else level,
        "has_level": level is not None,
    }
    node.update(kw)
    return node


# ---------------------------------------------------------------- 比例本身


def test_ratio_is_activity_over_own_level_gate() -> None:
    """半程 → 0.5，正好压线 → 1.0，超额夹到 1.0。"""
    nodes = [
        _node(id="half", activity=4.75),   # L5 门槛 9.5
        _node(id="edge", activity=9.5),
        _node(id="over", activity=40.0),
    ]
    starmap.assign_activity_ratio(nodes, LEVELS)

    by_id = {n["id"]: n for n in nodes}
    assert by_id["half"]["act01"] == pytest.approx(0.5)
    assert by_id["edge"]["act01"] == pytest.approx(1.0)
    assert by_id["over"]["act01"] == pytest.approx(1.0), "超过升档线就夹住，不再往上跑"
    assert by_id["half"]["gate"] == 9.5


def test_each_level_uses_its_own_gate_not_a_global_one() -> None:
    """同样的活跃度，低档更红 —— 门槛低，攒到同样分数更接近晋升。

    这是 ratio 口径区别于秩归一的地方：它不做跨节点比较，颜色只跟自己的档位有关。
    """
    low = _node(id="l3", level=3, activity=2.5)   # 门槛 5.0
    high = _node(id="l5", level=5, activity=2.5)  # 门槛 9.5
    starmap.assign_activity_ratio([low, high], LEVELS)

    assert low["act01"] == pytest.approx(0.5)
    assert high["act01"] == pytest.approx(2.5 / 9.5, abs=1e-4)
    assert low["act01"] > high["act01"]


def test_top_level_falls_back_to_its_down_threshold() -> None:
    """顶档没有 up_threshold（升无可升），用降档线兜底，含义退化成「还稳不稳」。"""
    node = _node(id="l10", level=10, activity=13.6)  # 降档线 27.2 的一半
    starmap.assign_activity_ratio([node], LEVELS)

    assert node["gate"] == 27.2
    assert node["act01"] == pytest.approx(0.5)


def test_levelless_non_log_nodes_use_the_loosest_gate() -> None:
    """没有等级但确实有 activity 的条目（配置外的残留数据）用全表最松门槛。

    日志不在这条里 —— 它压根没有 activity 列，已被 `_scored_nodes` 排除，
    见 `test_logs_are_not_scored_at_all`。
    """
    node = _node(id="stray", level=None, activity=1.0, kind="experience")
    starmap.assign_activity_ratio([node], LEVELS)

    assert node["gate"] == 2.0, "最松门槛 = 表里最小的 up_threshold（L1 的 2.0）"
    assert node["act01"] == pytest.approx(0.5)


def test_logs_are_not_scored_at_all() -> None:
    """原始日志没有 activity 列 —— 它不参与色标，也不占秩归一的分母。

    两种口径都排除：给它算比例只会得到恒 0 的假值，而 0 在这里的含义是
    「本周期没被检索」，对日志是句假话。它画固定灰。
    """
    log = _node(id="log", level=None, activity=0.0, kind="log")
    real = _node(id="real", level=5, activity=9.5)

    ratio_stats = starmap.assign_activity_ratio([log, real], LEVELS)
    assert ratio_stats["scored"] == 1, "日志不算进统计"
    assert log["gate"] == 0.0, "日志连分母都不该有"

    # 秩归一里日志同样不占位次：两个真实节点就是 0 和 1，不会因为多了条日志变成 0/0.5/1
    a = _node(id="a", level=5, activity=1.0)
    b = _node(id="b", level=5, activity=5.0)
    rank_stats = starmap.assign_activity([log, a, b], LEVELS, mode="rank")
    assert rank_stats["scored"] == 2
    assert sorted([a["act01"], b["act01"]]) == [0.0, 1.0]


def test_unknown_level_falls_back_instead_of_crashing() -> None:
    """库里残留了配置表里没有的档位（改过等级表的老数据）→ 用最松门槛，不抛。"""
    node = _node(id="stray", level=7, activity=1.0)
    starmap.assign_activity_ratio([node], LEVELS)

    assert node["gate"] == 2.0
    assert node["act01"] == pytest.approx(0.5)


def test_zero_activity_is_zero_and_counted() -> None:
    """本周期没被检索过就是 0（青），并且把数量统计出来 —— 面板要能说清「整片青」的原因。"""
    nodes = [_node(id=f"z{i}") for i in range(4)] + [_node(id="hot", activity=9.5)]
    stats = starmap.assign_activity_ratio(nodes, LEVELS)

    assert {n["act01"] for n in nodes if n["id"].startswith("z")} == {0.0}
    assert stats["zero"] == 4
    assert stats["scored"] == 5
    assert stats["mode"] == "ratio"
    assert stats["max"] == 1.0


def test_digest_is_excluded_from_ratio() -> None:
    """复盘恒星没有等级也没有 activity，画固定金黄，不占统计分母。"""
    digest = _node(id="dig", kind="digest", level=None)
    stats = starmap.assign_activity_ratio([digest, _node(id="a", activity=9.5)], LEVELS)

    assert digest["act01"] == 1.0
    assert stats["scored"] == 1


# ---------------------------------------------------------------- 退化与分派


def test_no_threshold_anywhere_is_reported_unusable() -> None:
    """等级表只有档位没有阈值（配置读不到、退回库内档位时就是这样）→ 不硬算。"""
    stats = starmap.assign_activity_ratio([_node(activity=1.0)], [{"level": 5}, {"level": 0}])
    assert stats["usable"] is False


def test_assign_activity_falls_back_to_rank_and_says_so() -> None:
    """算不出分母时自动退回秩归一，并把原因挂出来 —— 不能悄悄换口径。"""
    nodes = [_node(id="a", activity=1.0), _node(id="b", activity=5.0)]
    stats = starmap.assign_activity(nodes, [{"level": 5}], mode="ratio")

    assert stats["mode"] == "rank"
    assert "退回秩归一" in stats["fallback"]
    assert {n["act01"] for n in nodes} == {0.0, 1.0}


def test_rank_mode_is_still_available() -> None:
    """--activity-mode rank 走老口径，行为不该被这次改动影响。"""
    nodes = [_node(id=f"n{i}", activity=float(i), at=None) for i in range(1, 4)]
    stats = starmap.assign_activity(nodes, LEVELS, mode="rank")

    assert stats["mode"] == "rank"
    assert sorted(n["act01"] for n in nodes) == pytest.approx([0.0, 0.5, 1.0])


# ---------------------------------------------------------------- 配置接线


def test_gates_read_from_slim_level() -> None:
    """阈值必须从配置一路带出来：_slim_level 丢字段的话整条链就断了。"""
    slim = starmap._slim_level(
        {"level": 5, "period_experience_days": 7, "up_threshold": 9.5, "down_threshold": 5.6}
    )
    assert slim["up_threshold"] == 9.5
    assert slim["down_threshold"] == 5.6
    assert starmap.level_gates([slim, {"level": 9, "up_threshold": 34.0}]) == {5: 9.5, 9: 34.0}


def test_bad_threshold_values_are_ignored() -> None:
    """配置里给个字符串/负数不能把分母搞成 0 → 除零。"""
    assert starmap.level_gates([{"level": 5, "up_threshold": "oops"}]) == {}
    assert starmap.level_gates([{"level": 5, "up_threshold": 0}]) == {}


# ---------------------------------------------------------------- 前端


def test_frontend_shows_the_gate_in_tooltip_and_panel() -> None:
    """悬停要给分母：单看 0.09 分不清「懒得动」还是「门槛高」。"""
    from pathlib import Path

    src = Path(starmap.__file__).read_text(encoding="utf-8")
    assert "÷ 阈值 <b>" in src, "tooltip 要写出分母"
    assert "id=\"actGate\"" in src, "侧栏要有一行分母"
    assert "1.0 = 升档线" in src, "图例要说明 1.0 的含义"
