"""遗忘等级：周期裁定、滞回、活跃度（TODO 4 / §2.1 / §2.3 / §5.1）。

这组测试守四件事：

1. **滞回**：升级线与降级线之间是死区，落进去不动作；
2. **每周期最多动一级**：升要连续达标，降也要连续冷落；
3. **维持也要回写**：活跃度清零 + 周期起点刷新，否则死区条目天天「到期」；
4. **0 级不参与裁定**：它只能复活或删除，不能被周期推来推去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.lifecycle import (
    DAY_MS,
    PERIOD_EPISODE,
    PERIOD_EXPERIENCE,
    LevelTable,
    activity_contribution,
    initial_level,
    level_factor,
)

from test_store import tx


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    ws = proj / ".ai_memory"
    ws.mkdir(parents=True)

    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    yield cfg, store
    store.close()


def _force(store: MemoryStore, *, table: str, item_id: str, **columns) -> None:
    """直接改列，把一条记忆摆到「该裁定」的姿势。"""
    sets = ", ".join(f"{key} = ?" for key in columns)
    with tx(store.workspace_conn):
        store.workspace_conn.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?", (*columns.values(), item_id)
        )


def _row(store: MemoryStore, *, table: str, item_id: str):
    return store.workspace_conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (item_id,)
    ).fetchone()


def _set_clock(store: MemoryStore, moment: int) -> None:
    """把「上次计算时刻」摆到指定位置。

    间隔落在冻结上限（max_gap_days）之内就不会触发静默冻结 ——
    这些用例测的是到期裁定，不该被「静默很久」的语义顺手改写。
    """
    with tx(store.workspace_conn):
        store.workspace_conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_judge_at', ?)",
            (str(moment),),
        )


# ---------------------------------------------------------------- 纯判定


def test_decide_three_bands(env) -> None:
    """三组验收：3 级 → 升 / 死区 / 降。

    按出厂等级表，3 级的死区是 [down=2.8, up=5.0)：
    6 → 升 4；4 → 维持 3；2 → 降 2（2 < 2.8）。
    """
    cfg, _ = env
    table = LevelTable.from_config(cfg)

    up = table.decide(3, 6.0, PERIOD_EXPERIENCE)
    assert (up.level, up.action) == (4, "up")

    hold = table.decide(3, 4.0, PERIOD_EXPERIENCE)
    assert (hold.level, hold.action) == (3, "hold")
    assert "死区" in hold.reason

    down = table.decide(3, 2.0, PERIOD_EXPERIENCE)
    assert (down.level, down.action) == (2, "down")


def test_decide_never_moves_two_levels(env) -> None:
    cfg, _ = env
    table = LevelTable.from_config(cfg)
    # 活跃度爆表也只升一级；冷到 0 也只降一级
    assert table.decide(1, 10_000.0).level == 2
    assert table.decide(4, -10.0).level == 3


def test_top_level_cannot_rise(env) -> None:
    cfg, _ = env
    table = LevelTable.from_config(cfg)
    decision = table.decide(table.max_level, 9_999.0)
    assert decision.level == table.max_level
    assert decision.action == "hold"


def test_level_zero_is_never_judged(env) -> None:
    cfg, _ = env
    table = LevelTable.from_config(cfg)
    assert table.decide(0, 9_999.0).action == "hold"
    assert table.decide(0, 0.0).action == "hold"
    assert table.is_due(0, 0, 10**13, PERIOD_EXPERIENCE) is False


def test_is_due_uses_per_dimension_period(env) -> None:
    """陈述与经历各有各的周期：8 级是 240 天 / 120 天。"""
    cfg, _ = env
    table = LevelTable.from_config(cfg)
    now = 2_000_000_000_000
    start = now - int(121 * DAY_MS)

    assert table.is_due(8, start, now, PERIOD_EPISODE) is True
    assert table.is_due(8, start, now, PERIOD_EXPERIENCE) is False
    assert table.is_due(8, None, now, PERIOD_EPISODE) is False, "没有起点就不该判到期"


def test_initial_level_and_level_factor(env) -> None:
    cfg, _ = env
    bands = cfg.initial_bands
    assert initial_level(0.9, bands) == 4
    assert initial_level(0.4, bands) == 3
    assert initial_level(0.1, bands) == 2
    # 录入可达的顶档就是 bands 的最高档：L5 及以上只能靠活跃度挣
    assert initial_level(1.0, bands) == 4
    assert initial_level(1.0, bands) < cfg.max_level

    top = cfg.max_level
    power = cfg.level_factor_power
    assert level_factor(top, top, power) == pytest.approx(1.0)
    assert level_factor(3, top, power) == pytest.approx((3 / top) ** power)
    assert level_factor(0, top, power) == 0.0
    assert level_factor(2, top, power, overrides={2: 0.9}) == pytest.approx(0.9)


def test_level_factor_keeps_long_term_entry_comparable(env) -> None:
    """档数从 5 扩到 10 后，长期记忆起点（6 级）仍占旧 4 级那份分量。

    旧表：(4/5)^1.5 ≈ 0.716。新表若沿用 1.5，(6/10)^1.5 ≈ 0.465 —— 整体降权，
    新写的重要经验在排序里会被老条目压住。power 就是为这件事重标的。
    """
    cfg, _ = env
    old_mid = (4 / 5) ** 1.5
    new_entry = level_factor(6, cfg.max_level, cfg.level_factor_power)
    assert new_entry == pytest.approx(old_mid, abs=0.02)


def test_activity_contribution(env) -> None:
    cfg, _ = env
    floor, power = cfg.activity_sim_floor, cfg.activity_curve_power
    assert activity_contribution(floor, sim_floor=floor, curve_power=power) == 0.0
    assert activity_contribution(0.1, sim_floor=floor, curve_power=power) == 0.0
    # (0.85 - 0.35) ^ 2 = 0.25
    assert activity_contribution(0.85, sim_floor=floor, curve_power=power) == pytest.approx(0.25)


# ---------------------------------------------------------------- 写入时的初始等级


def test_write_sets_initial_level_and_period(env) -> None:
    _, store = env
    high = store.write_memory("重大架构决策：三级深合并配置", importance=0.9)
    mid = store.write_memory("默认落点的知识", importance=0.4)
    low = store.write_memory("顺手改了个 typo", importance=0.1)

    assert (high.forget_level, mid.forget_level, low.forget_level) == (4, 3, 2)
    assert high.period_start is not None and high.period_start == high.created_at

    ep = store.write_episode("一次普通叙述", importance=0.4)
    assert ep.forget_level == 3
    assert ep.period_start == ep.at


# ---------------------------------------------------------------- 到期裁定


def test_judge_upgrades_and_resets_period(env) -> None:
    _, store = env
    item = store.write_memory("被反复检索到的知识", importance=0.4)
    now = 3_000_000_000_000
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=3,
        activity=16.0,
        period_start=now - int(91 * DAY_MS),
    )
    _set_clock(store, now - int(DAY_MS))

    report = store.judge_due(now=now)

    assert report["due"] == 1
    assert report["items"][0]["action"] == "up"
    row = _row(store, table="knowledge", item_id=item.id)
    assert row["forget_level"] == 4
    assert row["activity"] == 0.0, "裁定完必须清零活跃度"
    assert row["period_start"] == now, "周期起点要推到现在，按新等级重新起算"

    events = store.workspace_conn.execute(
        "SELECT event, from_value, to_value FROM memory_events WHERE item_id = ?",
        (item.id,),
    ).fetchall()
    assert len(events) == 1
    assert (events[0]["event"], events[0]["from_value"], events[0]["to_value"]) == (
        "level",
        "3",
        "4",
    )


def test_judge_hold_still_resets_period(env) -> None:
    """死区不动作，但必须清零 + 重置，否则每轮都被重新扫出来。"""
    _, store = env
    item = store.write_memory("活跃度落在死区", importance=0.4)
    now = 3_000_000_000_000
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=3,
        activity=4.0,
        period_start=now - int(4 * DAY_MS),
    )
    _set_clock(store, now - int(DAY_MS))

    report = store.judge_due(now=now)

    assert report["items"][0]["action"] == "hold"
    row = _row(store, table="knowledge", item_id=item.id)
    assert row["forget_level"] == 3
    assert row["activity"] == 0.0
    assert row["period_start"] == now

    # 再跑一次：周期已刷新，不该再被判到期
    assert store.judge_due(now=now + 1000)["due"] == 0


def test_judge_downgrades_to_zero_and_stamps_level0(env) -> None:
    _, store = env
    item = store.write_memory("没人再用的知识", importance=0.4)
    now = 3_000_000_000_000
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=1,
        activity=0.0,
        period_start=now - int(8 * DAY_MS),
    )

    store.judge_due(now=now)

    row = _row(store, table="knowledge", item_id=item.id)
    assert row["forget_level"] == 0
    assert row["level0_since"] == now, "进遗忘层要记时刻，删除判定靠它算停留时长"


def test_judge_skips_not_due(env) -> None:
    _, store = env
    item = store.write_memory("刚写过，还没到期", importance=0.4)
    now = 3_000_000_000_000
    _force(store, table="knowledge", item_id=item.id, period_start=now - int(DAY_MS))

    assert store.judge_due(now=now)["due"] == 0


def test_judge_uses_episode_period_and_dry_run(env) -> None:
    _, store = env
    ep = store.write_episode("三十天前的经历", importance=0.4)
    now = 3_000_000_000_000
    _force(
        store,
        table="episodes",
        item_id=ep.id,
        forget_level=3,
        activity=16.0,
        period_start=now - int(31 * DAY_MS),
    )
    _set_clock(store, now - int(DAY_MS))

    preview = store.judge_due(now=now, dry_run=True)
    assert preview["due"] == 1
    assert preview["settled"] == 0
    assert _row(store, table="episodes", item_id=ep.id)["forget_level"] == 3, "干跑不能写库"

    report = store.judge_due(now=now)
    assert report["settled"] == 1
    row = _row(store, table="episodes", item_id=ep.id)
    assert row["forget_level"] == 4
    assert row["activity"] == 0.0


# ---------------------------------------------------------------- 等级表改档后的存量重算
# 改周期是改「尺子」，不是让老条目变旧。若不重算，旧的 L4 会按新表的 5 天周期
# 连锁降级，几周内集体掉进遗忘层 —— 这组用例守的就是这条迁移路径。


def test_relevel_recomputes_by_importance_and_resets_period(env) -> None:
    """按 importance 重算落档，并把周期账清空、从头走。"""
    _, store = env
    now = 4_000_000_000_000
    high = store.write_memory("重大架构决策", importance=0.9)
    low = store.write_memory("顺手改了个 typo", importance=0.1)
    # 摆成旧表的姿势：两条都停在更高的档，带着旧周期攒下的活跃度
    for item in (high, low):
        _force(
            store,
            table="knowledge",
            item_id=item.id,
            forget_level=7,
            activity=9.0,
            period_start=now - int(150 * DAY_MS),
        )

    preview = store.relevel_by_importance(now=now, dry_run=True)
    assert preview["touched"] == 2
    assert {m["to"] for m in preview["moves"]} == {4, 2}
    assert _row(store, table="knowledge", item_id=high.id)["forget_level"] == 7, "干跑不写库"

    report = store.relevel_by_importance(now=now)
    assert report["changed"] == 2
    row = _row(store, table="knowledge", item_id=high.id)
    assert row["forget_level"] == 4
    assert row["activity"] == 0.0, "旧周期的活跃度不该带进新周期"
    assert row["period_start"] == now, "倒计时要按新表重新起算"
    assert _row(store, table="knowledge", item_id=low.id)["forget_level"] == 2


def test_relevel_skips_forgotten_and_archived(env) -> None:
    """遗忘层走自己的复活通道；已归档是显式选择 —— 都不该被顺手拉回来。"""
    _, store = env
    now = 4_000_000_000_000
    gone = store.write_memory("已经掉进遗忘层的", importance=0.9)
    kept = store.write_memory("已经归档的", importance=0.9)
    _force(store, table="knowledge", item_id=gone.id, forget_level=0, level0_since=now)
    _force(store, table="knowledge", item_id=kept.id, forget_level=3, archived_at=now - 1000)

    report = store.relevel_by_importance(now=now)
    assert report["touched"] == 0
    assert _row(store, table="knowledge", item_id=gone.id)["forget_level"] == 0
    assert _row(store, table="knowledge", item_id=kept.id)["forget_level"] == 3

    pulled = store.relevel_by_importance(now=now, include_level0=True)
    assert pulled["touched"] == 1
    row = _row(store, table="knowledge", item_id=gone.id)
    assert row["forget_level"] == 4, "显式要求时才按 importance 拉回"
    assert row["level0_since"] is None, "离开遗忘层要清掉停留起点"


