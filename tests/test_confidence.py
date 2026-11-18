"""可信度：证实的加分曲线、证伪的扣分、证据级别（TODO 5 / §2.2）。

三条硬规矩：
1. 加分封顶 `1 - c`、扣分封顶 `c`，不出 0~1 的范围；
2. L3（仅文本共现）**恒为 0 分** —— 只记一行事件，不改值；
3. 经历没有独立可信度：它是 `forget_level` 的派生值，调它就是调错了对象。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.confidence import ConfidenceCurve
from humanity_memory.lifecycle import DAY_MS

from test_store import tx


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)

    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    yield cfg, store
    store.close()


def _events(store: MemoryStore, item_id: str) -> list:
    return store.workspace_conn.execute(
        "SELECT event, from_value, to_value, delta FROM memory_events"
        " WHERE item_id = ? ORDER BY rowid",
        (item_id,),
    ).fetchall()


# ---------------------------------------------------------------- 纯曲线


def test_gain_curve_matches_design_table(env) -> None:
    """§2.2 的表：1 天 +0.05、7 天封顶。"""
    cfg, _ = env
    curve = ConfidenceCurve.from_config(cfg)

    assert curve.gain(0.3, 1, "L1") == pytest.approx(0.05)
    assert curve.gain(0.3, 3, "L1") == pytest.approx(0.05 * 3**curve.gain_alpha, rel=1e-6)
    # 7 天算出来是 0.70，但只剩 0.70 的空间 → 刚好到 1.0
    assert curve.gain(0.3, 7, "L1") == pytest.approx(0.70, abs=0.01)
    assert 0.3 + curve.gain(0.3, 7, "L1") <= 1.0
    # 已经满分就没有空间了
    assert curve.gain(1.0, 30, "L1") == 0.0


def test_evidence_levels_weight(env) -> None:
    """L2 半权，L3 为零且不开放配置。"""
    cfg, _ = env
    curve = ConfidenceCurve.from_config(cfg)
    assert curve.weight("L1") == 1.0
    assert curve.weight("L2") == 0.5
    assert curve.weight("L3") == 0.0
    assert curve.weight("l2") == 0.5, "级别大小写不敏感"


def test_loss_is_symmetric_and_floored(env) -> None:
    cfg, _ = env
    curve = ConfidenceCurve.from_config(cfg)
    # 同样的间隔，扣分系数是加分的三倍
    assert curve.loss(0.3, 1) == pytest.approx(-0.15)
    assert curve.apply(0.3, ok=False, interval_days=1) == pytest.approx((0.15, -0.15))
    # 扣到 0 为止
    assert curve.apply(0.05, ok=False, interval_days=100)[0] == 0.0


def test_two_thresholds(env) -> None:
    cfg, _ = env
    curve = ConfidenceCurve.from_config(cfg)
    assert curve.needs_verify(0.49) is True
    assert curve.needs_verify(0.5) is False
    assert curve.is_fact(0.84) is False
    assert curve.is_fact(0.85) is True


# ---------------------------------------------------------------- 落盘


def test_verify_raises_confidence_and_stamps_time(env) -> None:
    _, store = env
    item = store.write_memory("SQLite 的 WAL 允许读写并发", importance=0.8)
    assert item.confidence == pytest.approx(0.3)

    now = item.created_at + int(DAY_MS)
    result = store.adjust_confidence(item.id, ok=True, level="L1", now=now)

    assert result["from"] == pytest.approx(0.3)
    assert result["to"] == pytest.approx(0.35)
    assert result["delta"] == pytest.approx(0.05)
    assert result["counted"] is True

    row = store.workspace_conn.execute(
        "SELECT confidence, verified_at FROM knowledge WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["confidence"] == pytest.approx(0.35)
    assert row["verified_at"] == now, "证实要记下「什么时候验的」，间隔靠它算"

    events = _events(store, item.id)
    assert len(events) == 1
    assert events[0]["event"] == "verify"
    assert events[0]["from_value"] == "0.300"
    assert events[0]["to_value"] == "0.350"


def test_verify_caps_at_one(env) -> None:
    _, store = env
    item = store.write_memory("长期稳定的结论", importance=0.8)
    first = item.created_at + int(DAY_MS)
    store.adjust_confidence(item.id, ok=True, level="L1", now=first)

    # 再过 7 天：从 0.35 起，7 天给 +0.70，但只剩 0.65 → 封顶 1.0
    second = first + int(7 * DAY_MS)
    result = store.adjust_confidence(item.id, ok=True, level="L1", now=second)
    assert result["to"] == 1.0
    assert result["is_fact"] is True


def test_l3_records_event_without_changing_value(env) -> None:
    """TODO 5 的第三条验收：L3 的分值只记录、不变。"""
    _, store = env
    item = store.write_memory("只是日志里提到过的一句", importance=0.8)

    now = item.created_at + int(DAY_MS)
    result = store.adjust_confidence(item.id, ok=True, level="L3", now=now)

    assert result["counted"] is False
    assert result["delta"] == 0.0
    assert result["from"] == result["to"] == pytest.approx(0.3)

    events = _events(store, item.id)
    assert len(events) == 1, "不计分也要留痕：'看过但不算证据'本身是信息"
    assert events[0]["event"] == "verify"
    assert events[0]["delta"] == 0.0


def test_contradict_lowers_and_keeps_verified_at(env) -> None:
    _, store = env
    item = store.write_memory("会被推翻的结论", importance=0.8)
    now = item.created_at + int(DAY_MS)

    result = store.adjust_confidence(item.id, ok=False, level="L1", now=now)

    assert result["event"] == "contradict"
    assert result["to"] == pytest.approx(0.15)
    row = store.workspace_conn.execute(
        "SELECT confidence, verified_at FROM knowledge WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["confidence"] == pytest.approx(0.15)
    assert row["verified_at"] is None, "被推翻过就不存在「刚验过」"


def test_episode_confidence_is_derived_not_stored(env) -> None:
    _, store = env
    ep = store.write_episode("一次经历没有独立可信度", importance=0.5)
    with pytest.raises(ValueError) as excinfo:
        store.adjust_confidence(ep.id, ok=True, level="L1")
    assert "派生" in str(excinfo.value)


# ---------------------------------------------------------------- memory_verify 的语义


def test_verify_knowledge_confirms(env) -> None:
    _, store = env
    item = store.write_memory("一条知识", importance=0.8)
    now = item.created_at + int(DAY_MS)

    result = store.verify(item.id, ok=True, level="L1", now=now)

    assert result["action"] == "confirmed"
    assert result["to"] == pytest.approx(0.35)
    assert "deleted" not in result


def test_verify_false_deducts_without_deleting(env) -> None:
    """证伪不删条目。

    经验的价值在于「它曾经是个结论」，被推翻本身是知识；删掉之后
    三个月后回头只会看到"这条从没存在过"，而不是"这条错了"。
    """
    _, store = env
    item = store.write_memory("已经过期的结论", importance=0.85)
    # 隔一天再证伪：扣分是按间隔算的，同刻验证的间隔为 0 → 扣 0 分
    now = item.created_at + int(DAY_MS)

    result = store.verify(item.id, ok=False, level="L1", now=now)

    assert result["action"] == "contradicted"
    assert "deleted" not in result
    row = store.workspace_conn.execute(
        "SELECT confidence, invalid_at FROM knowledge WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["confidence"] < 0.3, "被推翻要扣分"
    assert row["invalid_at"] is None, "扣分不等于失效：失效由 invalidate 显式完成"

    events = _events(store, item.id)
    assert len(events) == 1
    assert events[0]["event"] == "contradict"


def test_low_confidence_is_marked_on_read(env) -> None:
    """低于 verify_on_read 的经验，检索命中要带「先验证再用」标记。

    不标的话，一条 0.3 的猜测和一条 0.9 的机制在上下文里长得一模一样，
    读的人没有理由区别对待它们。
    """
    cfg, store = env
    floor = cfg.confidence_settings["verify_on_read"]

    shaky = store.write_memory("大概是这么回事", confidence=max(0.0, floor - 0.2))
    solid = store.write_memory("这条我验过", confidence=min(1.0, floor + 0.2))

    hits = {h.item.id: h for h in store.search("这么回事 我验过", limit=10)}
    assert hits[shaky.id].needs_verify is True
    assert hits[solid.id].needs_verify is False

    assert "待验证" in hits[shaky.id].to_line()
    assert "待验证" not in hits[solid.id].to_line()
    assert hits[shaky.id].to_dict().get("needs_verify") is True
    assert "needs_verify" not in hits[solid.id].to_dict()


def test_episode_never_marked_needs_verify(env) -> None:
    """经历没有可信度，也就不该有「待验证」——真假由事实决定，不由分数决定。"""
    _, store = env
    ep = store.write_episode("今天修了个 bug", importance=0.4)

    hits = store.search("修了个 bug", kinds=["episode"], limit=5)
    assert hits, "检索没命中，测试前提不成立"
    assert all(h.needs_verify is False for h in hits)
    assert all("待验证" not in h.to_line() for h in hits)


def test_low_confidence_is_marked_on_read(env) -> None:
    """低于 verify_on_read 的经验，检索命中要带「先验证再用」标记。

    不标的话，一条 0.3 的猜测和一条 0.9 的机制在上下文里长得一模一样，
    读的人没有理由区别对待它们。
    """
    cfg, store = env
    floor = cfg.confidence_settings["verify_on_read"]

    shaky = store.write_memory("大概是这么回事", confidence=max(0.0, floor - 0.2))
    solid = store.write_memory("这条我验过", confidence=min(1.0, floor + 0.2))

    hits = {h.item.id: h for h in store.search("这么回事 我验过", limit=10)}
    assert hits[shaky.id].needs_verify is True
    assert hits[solid.id].needs_verify is False

    assert "待验证" in hits[shaky.id].to_line()
    assert "待验证" not in hits[solid.id].to_line()
    assert hits[shaky.id].to_dict().get("needs_verify") is True
    assert "needs_verify" not in hits[solid.id].to_dict()


def test_episode_never_marked_needs_verify(env) -> None:
    """经历没有可信度，也就不该有「待验证」——真假由事实决定，不由分数决定。"""
    _, store = env
    ep = store.write_episode("今天修了个 bug", importance=0.4)

    hits = store.search("修了个 bug", kinds=["episode"], limit=5)
    assert hits, "检索没命中，测试前提不成立"
    assert all(h.needs_verify is False for h in hits)
    assert all("待验证" not in h.to_line() for h in hits)
