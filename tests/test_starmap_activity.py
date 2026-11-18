"""星图辉光颜色 = 活跃度（2026-11-17 改）。

用户定的色标：三个锚点 0 → `#0FE0E5`、0.5 → `#F6DE14`、1 → `#F10000`，
横轴是节点活跃度，且要求「尽可能分布平均」。

「平均」这件事不能靠线性归一：活跃度的量纲是 `excess ** curve_power` 的累加，
一个被反复检索的老条目能到几十，线性归一后其余节点会全挤在青端。
所以这里钉的是**秩归一**（有多少比例的节点比你更不活跃），本文件守四件事：

1. 分值不同的节点，色标值沿节点均匀铺开（不是沿数值）；
2. 同分同色，且最低分恒为 0 —— 大量 0 活跃的条目不能被抬到中间色；
3. 全体同分时的退化行为：全 0 给 0（全冷）、全非 0 给 0.5（无从区分给中性）；
4. 复盘恒星不参与着色（它没有 level 也没有 activity，画固定金黄）。
"""

from __future__ import annotations

import math

import pytest

from humanity_memory import starmap

DAY = starmap.DAY_MS
NOW = 1_800_000_000_000.0  # 固定在某个时刻，避免用例随真实时钟漂移


def _node(kind: str = "experience", **kw) -> dict:
    base = {
        "id": kw.pop("id", "x"),
        "kind": kind,
        "activity": 0.0,
        "access_count": 0,
        "last_accessed_at": None,
        "at": NOW,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 原始分


def test_activity_column_dominates_and_others_add_up() -> None:
    """三个信号是相加的：检索累积 + 取用次数 + 新鲜度。"""
    hot = _node(activity=4.0, access_count=0)
    used = _node(activity=0.0, access_count=9)  # log1p(9) ≈ 2.30
    fresh = _node(activity=0.0, last_accessed_at=NOW)  # 新鲜度 ≈ 1.0
    stale = _node(activity=0.0, last_accessed_at=NOW - 40 * DAY)  # ≈ 0.06

    assert starmap.activity_score(hot, NOW) == pytest.approx(4.0 + math.exp(0))
    assert starmap.activity_score(used, NOW) == pytest.approx(math.log1p(9) + math.exp(0))
    assert starmap.activity_score(fresh, NOW) == pytest.approx(1.0)
    assert starmap.activity_score(stale, NOW) < 0.1
    assert starmap.activity_score(hot, NOW) > starmap.activity_score(used, NOW)


def test_missing_timestamp_does_not_crash() -> None:
    """日志表没有 activity 列，at 也可能缺 —— 不能因此抛异常或给出 nan。"""
    node = _node(kind="log", at=None, last_accessed_at=None)
    score = starmap.activity_score(node, NOW)
    assert score == 0.0
    assert not math.isnan(score)


# ---------------------------------------------------------------- 秩归一


def test_units_spread_evenly_over_nodes_not_over_values() -> None:
    """核心需求：颜色沿节点均匀铺开。

    造一条长尾 —— 9 个节点分值是 1..9，最后一个是 500。
    线性归一的话 500 会吃掉整条色带（其余全在 0.02 以下），
    秩归一必须让 10 个节点的色标值落在 0 / 0.11 / … / 1 这条等距网格上。
    """
    nodes = [_node(id=f"n{i}", activity=float(i)) for i in range(1, 10)]
    nodes.append(_node(id="outlier", activity=500.0))

    starmap.assign_activity_units(nodes, NOW)
    units = sorted(n["act01"] for n in nodes)

    expected = [round(i / 9, 4) for i in range(10)]
    assert units == pytest.approx(expected, abs=1e-4)
    # 离群值只占一个位次，没有把其余节点压到青端
    assert min(units[1:]) >= 0.11


def test_zero_activity_group_stays_cold() -> None:
    """一半节点是 0 活跃：它们必须全是 0（青），不能被「同分占多数」抬到 0.5。"""
    nodes = [_node(id=f"z{i}", activity=0.0, at=NOW - 30 * DAY) for i in range(6)]
    nodes += [_node(id=f"a{i}", activity=float(i)) for i in range(1, 5)]

    starmap.assign_activity_units(nodes, NOW)

    zero_units = {n["act01"] for n in nodes if n["id"].startswith("z")}
    assert zero_units == {0.0}, "整组 0 活跃必须全冷"
    assert max(n["act01"] for n in nodes) == 1.0, "最高分恒为 1（红）"


def test_ties_share_the_same_unit() -> None:
    nodes = [_node(id="a", activity=1.0), _node(id="b", activity=1.0), _node(id="c", activity=3.0)]
    starmap.assign_activity_units(nodes, NOW)
    by_id = {n["id"]: n["act01"] for n in nodes}
    assert by_id["a"] == by_id["b"]
    assert by_id["c"] == 1.0
    assert by_id["a"] == 0.0


def test_flat_distribution_degrades_to_neutral() -> None:
    """无从比较时给中性色，不给「全冷」或「全热」的假信号。

    注意：只要节点有时间戳，总分就永远 > 0（新鲜度那一项），
    所以「全 0 → 全冷」这条分支只在没有时间戳的裸节点上才走得到 ——
    两个都钉住，免得日后有人以为 0.5 是 bug。
    """
    bare = [_node(id=f"b{i}", at=None) for i in range(3)]
    starmap.assign_activity_units(bare, NOW)
    assert {n["act01"] for n in bare} == {0.0}, "彻底没有信号才是全冷"

    cold = [_node(id=f"c{i}", at=NOW - 30 * DAY) for i in range(3)]
    starmap.assign_activity_units(cold, NOW)
    assert {n["act01"] for n in cold} == {0.5}, "同分（都只是老）→ 中性，不假装冷"

    warm = [_node(id=f"w{i}", activity=2.0, at=NOW - 30 * DAY) for i in range(3)]
    starmap.assign_activity_units(warm, NOW)
    assert {n["act01"] for n in warm} == {0.5}


def test_single_node_is_neutral_not_fake_hot() -> None:
    """只有一个节点时没有「比谁更活跃」，给中性 0.5，别画成满红骗人。"""
    node = _node(activity=1.0, last_accessed_at=NOW)
    stats = starmap.assign_activity_units([node], NOW)
    assert node["act01"] == 0.5
    assert stats["scored"] == 1


def test_digest_is_excluded_from_scoring() -> None:
    """复盘恒星没有 activity / forget_level，画固定金黄，不参与秩归一的分布。"""
    digest = _node(kind="digest", id="dig")
    nodes = [digest, _node(id="a", activity=1.0), _node(id="b", activity=5.0)]

    stats = starmap.assign_activity_units(nodes, NOW)

    assert stats["scored"] == 2, "digest 不占分母"
    assert digest["act01"] == 1.0
    assert {n["act01"] for n in nodes if n["kind"] != "digest"} == {0.0, 1.0}


# ---------------------------------------------------------------- 前端取色


def _extract_glow_color() -> str:
    """把模板里的 glowColor 揪出来跑一遍，验证三个锚点落在用户指定的色上。"""
    src = (starmap.__file__).replace("\\", "/")
    from pathlib import Path

    text = Path(src).read_text(encoding="utf-8")
    start = text.index("const GLOW_STOPS=")
    end = text.index("function rnd(i)")
    return text[start:end]


def test_glow_color_hits_the_three_anchors() -> None:
    """0 / 0.5 / 1 必须取到 #0FE0E5 / #F6DE14 / #F10000，且越界要夹住。"""
    snippet = _extract_glow_color()
    for anchor in ("[0,[15,224,229]]", "[.5,[246,222,20]]", "[1,[241,0,0]]"):
        assert anchor in snippet

    assert "t=Math.max(0,Math.min(1,t||0))" in snippet, "越界值必须夹到 0~1"
    # 段内线性插值：两端取到锚点本身，中点落在 1/4 与 3/4 处
    assert "const f=(b[0]===a[0])?0:(t-a[0])/(b[0]-a[0])" in snippet
