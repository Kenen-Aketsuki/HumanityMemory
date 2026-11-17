"""一键裁定 `judge_due`（TODO 10 / §5.1）。

三个必须钉住的行为：

1. **一次调用跑完全部到期项** —— 项目库与全局库都扫，逐条升降；
2. **维持也要回写** —— 不清零活跃度、不重置周期，这条会永远是「已到期」；
3. **没到期就一个字节都不写** —— 空事务也会拿写锁，定时任务每天白拿一次没意义。

第 3 条得真的验证「没写」，不能只看返回值：所以断言 `period_start` 未变、`memory_events` 无新增。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, ScopeError, resolve_layout
from humanity_memory.lifecycle import DAY_MS


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)

    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    store = MemoryStore(cfg, resolve_layout(proj, home=cfg.home))
    yield cfg, store
    store.close()


def _bands(cfg: Config, level: int) -> dict:
    for item in cfg.forget_levels:
        if int(item["level"]) == level:
            return item
    raise AssertionError(f"配置里没有等级 {level}")


def _stage(
    store: MemoryStore,
    item_id: str,
    *,
    level: int,
    activity: float,
    overdue_days: int = 400,
) -> int:
    """把一条记忆摆成「已到期 + 指定活跃度」，返回判定时刻。"""
    now = 3_000_000_000_000
    store.workspace_conn.execute(
        "UPDATE episodes SET forget_level = ?, activity = ?, period_start = ? WHERE id = ?",
        (level, activity, now - overdue_days * DAY_MS, item_id),
    )
    return now


def _row(store: MemoryStore, item_id: str):
    return store.workspace_conn.execute(
        "SELECT forget_level, activity, period_start FROM episodes WHERE id = ?",
        (item_id,),
    ).fetchone()


def _event_count(store: MemoryStore) -> int:
    return store.workspace_conn.execute("SELECT count(*) FROM memory_events").fetchone()[0]


# ---------------------------------------------------------------- 三种动作


def test_one_call_settles_up_down_and_hold(env) -> None:
    """三条不同等级的到期项：升、降、维持各一条，一次调用全部落盘。"""
    cfg, store = env
    up = store.write_episode("该升的", importance=0.8)
    down = store.write_episode("该降的", importance=0.8)
    hold = store.write_episode("该维持的", importance=0.8)

    band4 = _bands(cfg, 4)
    band3 = _bands(cfg, 3)

    now = _stage(store, up.id, level=4, activity=float(band4["up_threshold"]) + 5)
    _stage(store, down.id, level=4, activity=0.0)
    _stage(
        store,
        hold.id,
        level=3,
        activity=(float(band3["up_threshold"]) + float(band3["down_threshold"])) / 2,
    )
    # 三条都得在同一个判定时刻到期：后面两条的 period_start 按同一 now 摆
    store.workspace_conn.execute(
        "UPDATE episodes SET period_start = ? WHERE id IN (?, ?)",
        (now - 400 * DAY_MS, down.id, hold.id),
    )

    report = store.judge_due(now=now)

    actions = {it["id"]: it["action"] for it in report["items"]}
    assert actions[up.id] == "up"
    assert actions[down.id] == "down"
    assert actions[hold.id] == "hold"
    assert report["due"] == 3
    assert report["settled"] == 3

    assert _row(store, up.id)["forget_level"] == 5
    assert _row(store, down.id)["forget_level"] == 3
    assert _row(store, hold.id)["forget_level"] == 3, "维持不改等级"

    for item in (up, down, hold):
        row = _row(store, item.id)
        assert row["activity"] == 0.0, "裁定完必须清零活跃度"
        assert row["period_start"] == now, "周期起点要推到判定时刻"


def test_hold_is_recorded_so_it_does_not_rescan(env) -> None:
    """维持也算「处理过」：再跑一次不该把它重新扫出来。"""
    cfg, store = env
    item = store.write_episode("会一直维持的", importance=0.8)
    band = _bands(cfg, 4)
    now = _stage(
        store,
        item.id,
        level=4,
        activity=(float(band["up_threshold"]) + float(band["down_threshold"])) / 2,
    )

    assert store.judge_due(now=now)["due"] == 1
    assert store.judge_due(now=now)["due"] == 0, "同一时刻再跑，它已经不在到期集合里"


def test_downgrade_to_zero_stamps_level0_since(env) -> None:
    cfg, store = env
    item = store.write_episode("掉进遗忘层的", importance=0.8)
    store.workspace_conn.execute("UPDATE episodes SET forget_level = 1 WHERE id = ?", (item.id,))
    now = _stage(store, item.id, level=1, activity=0.0)

    store.judge_due(now=now)

    row = store.workspace_conn.execute(
        "SELECT forget_level, level0_since FROM episodes WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["forget_level"] == 0
    assert row["level0_since"] == now


# ---------------------------------------------------------------- 不写库


def test_nothing_due_writes_nothing(env) -> None:
    _, store = env
    item = store.write_episode("刚写进来的", importance=0.8)
    before = _row(store, item.id)
    events_before = _event_count(store)

    report = store.judge_due()

    assert report["due"] == 0
    assert report["settled"] == 0
    assert report["items"] == []

    after = _row(store, item.id)
    assert after["forget_level"] == before["forget_level"]
    assert after["period_start"] == before["period_start"], "没到期就不该动周期起点"
    assert _event_count(store) == events_before, "没到期就不该写审计"


def test_dry_run_reports_without_writing(env) -> None:
    cfg, store = env
    item = store.write_episode("干跑不落盘", importance=0.8)
    band = _bands(cfg, 4)
    now = _stage(store, item.id, level=4, activity=float(band["up_threshold"]) + 5)
    events_before = _event_count(store)

    report = store.judge_due(now=now, dry_run=True)

    assert report["due"] == 1
    assert report["settled"] == 0
    assert report["items"][0]["action"] == "up"
    assert _row(store, item.id)["forget_level"] == 4, "干跑不能真改"
    assert _event_count(store) == events_before


# ---------------------------------------------------------------- 范围


def test_scope_all_only_touches_workspace(env) -> None:
    """`scope="all"` 现在只有一个库可跑 —— 全局库已关闭（§8.2）。"""
    cfg, store = env
    local = store.write_episode("项目里的", importance=0.8)
    band = _bands(cfg, 4)
    now = 3_000_000_000_000
    store.workspace_conn.execute(
        "UPDATE episodes SET forget_level = 4, activity = ?, period_start = ? WHERE id = ?",
        (float(band["up_threshold"]) + 5, now - 400 * DAY_MS, local.id),
    )

    report = store.judge_due(scope="all", now=now)

    assert report["due"] == 1
    assert [d["db"] for d in report["dbs"]] == ["workspace"]


def test_global_scope_is_rejected(env) -> None:
    _, store = env
    with pytest.raises(ScopeError, match="全局库已关闭"):
        store.judge_due(scope="global")
