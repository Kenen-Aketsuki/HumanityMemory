"""静默冻结：长期没跑裁定时，不按真实流逝折算记忆寿命。

守四件事：

1. **间隔上限**：相隔 90 天只按 `max_gap_days` 计，超出部分从周期起点里扣掉；
2. **只降一级**：冻结之后该降的降一级，不会因为「欠了很久」就往下连跳；
3. **不误伤新条目**：上次计算之后才重置的起点不平移，否则会被推到未来、永远不到期；
4. **干跑一致**：预览不写库，但判定结果必须与实跑相同。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
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


def _force(store: MemoryStore, *, table: str, item_id: str, **columns) -> None:
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
    """把「上次计算时刻」摆到指定位置。"""
    with tx(store.workspace_conn):
        store.workspace_conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_judge_at', ?)",
            (str(moment),),
        )


def _clock(store: MemoryStore) -> int | None:
    row = store.workspace_conn.execute(
        "SELECT value FROM meta WHERE key = 'last_judge_at'"
    ).fetchone()
    return int(row["value"]) if row else None


# ---------------------------------------------------------------- 冻结生效


def test_freeze_protects_l3_and_above(env) -> None:
    """上限 2 天 = 「L3 及以上保住，L2 及以下照常掉」。

    L3 周期 3 天（2 < 3 → 静默后仍不到期）；L2 周期 2 天（2 ≤ 2 → 照常降）。
    这条边界就是取值 2 的全部理由，改上限就会把它挪走。
    """
    cfg, store = env
    now = 3_000_000_000_000
    keep = store.write_memory("3 级该保住", importance=0.9)
    drop = store.write_memory("2 级该掉下去", importance=0.9)
    for item, level in ((keep, 3), (drop, 2)):
        _force(
            store,
            table="knowledge",
            item_id=item.id,
            forget_level=level,
            activity=0.0,
            period_start=now - int(90 * DAY_MS),
        )
    _set_clock(store, now - int(90 * DAY_MS))

    report = store.judge_due(now=now)

    assert report["frozen_days"] == pytest.approx(90 - cfg.forget_max_gap_days, abs=0.1)
    assert _row(store, table="knowledge", item_id=keep.id)["forget_level"] == 3, (
        "L3 周期 3 天 > 上限 2 天，静默后必须保住"
    )
    assert _row(store, table="knowledge", item_id=drop.id)["forget_level"] == 1, (
        "L2 周期 2 天 ≤ 上限，静默后照常降一级"
    )


def test_freeze_never_drops_more_than_one_level(env) -> None:
    """静默一年也只动一级 —— 冻结挡住的正是「一算全忘」。"""
    _, store = env
    now = 4_000_000_000_000
    item = store.write_memory("停摆一年的工程里的知识", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=2,
        activity=0.0,
        period_start=now - int(365 * DAY_MS),
    )
    _set_clock(store, now - int(365 * DAY_MS))

    store.judge_due(now=now)

    assert _row(store, table="knowledge", item_id=item.id)["forget_level"] == 1, (
        "365 天被压成 2 天，2 级（2 天周期）到期降 1 —— 只降一级，不是连降"
    )


def test_freeze_saves_memory_that_outlived_its_period(env) -> None:
    """静默 400 天：9 级周期 365 天，不冻结就会被判到期，冻结后保住。

    这是冻结最核心的场景 —— 短时的「降一级」其实两种做法结果一样，
    真正的分水岭是「静默时长超过周期」时，记忆会不会被时间本身拖垮。
    """
    _, store = env
    now = 3_000_000_000_000
    item = store.write_memory("沉淀下来的经验", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=9,
        activity=0.0,
        period_start=now - int(400 * DAY_MS),
    )
    _set_clock(store, now - int(400 * DAY_MS))

    report = store.judge_due(now=now)

    assert report["due"] == 0, "400 天被压成 7 天，9 级的 365 天周期还没走完"
    assert _row(store, table="knowledge", item_id=item.id)["forget_level"] == 9


# ---------------------------------------------------------------- 边界


def test_no_freeze_when_gap_within_cap(env) -> None:
    """间隔在上限内就照常算，不许凭空冻结。"""
    cfg, store = env
    now = 3_000_000_000_000
    gap_days = cfg.forget_max_gap_days - 1
    item = store.write_memory("三天前算过", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=1,
        activity=0.0,
        period_start=now - int(gap_days * DAY_MS),
    )
    _set_clock(store, now - int(gap_days * DAY_MS))

    report = store.judge_due(now=now)

    assert report["frozen_days"] == 0
    assert _row(store, table="knowledge", item_id=item.id)["forget_level"] == 0, (
        "1 级 + 6 天冷落 → 该掉进遗忘层"
    )


def test_freeze_leaves_newer_anchors_alone(env) -> None:
    """上次计算之后才重置的起点不平移，否则会被推到未来、永远不到期。"""
    _, store = env
    now = 3_000_000_000_000
    baseline = now - int(90 * DAY_MS)

    old = store.write_memory("上次计算前就在的老条目", importance=0.9)
    fresh = store.write_memory("上次计算之后才写的", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=old.id,
        forget_level=9,
        activity=0.0,
        period_start=baseline,
    )
    _force(
        store,
        table="knowledge",
        item_id=fresh.id,
        forget_level=9,
        activity=0.0,
        period_start=now - int(DAY_MS),
    )
    _set_clock(store, baseline)

    store.judge_due(now=now)

    cap_ms = int(store.cfg.forget_max_gap_days * DAY_MS)
    assert _row(store, table="knowledge", item_id=old.id)["period_start"] == now - cap_ms
    assert _row(store, table="knowledge", item_id=fresh.id)["period_start"] == now - int(
        DAY_MS
    ), "晚于基线的起点本来就没欠账，不该跟着挪"


def test_first_run_uses_oldest_anchor_as_clock(env) -> None:
    """没有基线时拿最老的周期起点当基线：首跑也不会一次全判到期。"""
    _, store = env
    now = 3_000_000_000_000
    item = store.write_memory("从没算过的老库", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=7,
        activity=0.0,
        period_start=now - int(120 * DAY_MS),
    )

    assert _clock(store) is None
    report = store.judge_due(now=now)

    assert report["frozen_days"] > 100, "首次也要按上限折算，而不是把 120 天照单全收"
    assert report["due"] == 0, "120 天被压到 7 天，7 级（周期 120 天）还没到期"
    assert _clock(store) == now, "算完要把基线推到当下"


def test_clock_is_recorded_even_when_nothing_is_due(env) -> None:
    """没到期也要记基线，否则下次会把同一段静默重复冻结，越滚越偏。"""
    _, store = env
    now = 3_000_000_000_000
    item = store.write_memory("刚刚算过的条目", importance=0.9)
    _force(store, table="knowledge", item_id=item.id, period_start=now)

    report = store.judge_due(now=now)

    assert report["due"] == 0, "周期起点就是 now，L5 还没走完 7 天"
    assert _clock(store) == now


def test_zero_cap_disables_freezing(env) -> None:
    """max_gap_days = 0 → 关闭冻结。同一个场景会降级，反证保护来自冻结。"""
    cfg, store = env
    cfg.settings["forget"]["max_gap_days"] = 0
    now = 3_000_000_000_000
    item = store.write_memory("静默四百天", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=9,
        activity=0.0,
        period_start=now - int(400 * DAY_MS),
    )
    _set_clock(store, now - int(400 * DAY_MS))

    report = store.judge_due(now=now)

    assert report["frozen_days"] == 0
    assert report["due"] == 1
    assert _row(store, table="knowledge", item_id=item.id)["forget_level"] == 8


# ---------------------------------------------------------------- 干跑一致


def test_dry_run_preview_matches_actual(env) -> None:
    """预览不写库，但判定结果必须与实跑一致。"""
    _, store = env
    now = 3_000_000_000_000
    item = store.write_memory("静默很久", importance=0.9)
    _force(
        store,
        table="knowledge",
        item_id=item.id,
        forget_level=2,
        activity=0.0,
        period_start=now - int(90 * DAY_MS),
    )
    _set_clock(store, now - int(90 * DAY_MS))

    preview = store.judge_due(now=now, dry_run=True)
    assert preview["frozen_days"] > 0
    assert preview["due"] == 1
    assert preview["settled"] == 0
    assert _row(store, table="knowledge", item_id=item.id)["forget_level"] == 2, (
        "干跑不能写库"
    )
    assert _row(store, table="knowledge", item_id=item.id)["period_start"] == now - int(
        90 * DAY_MS
    ), "干跑也不该把冻结落进 period_start"

    actual = store.judge_due(now=now)
    assert actual["due"] == preview["due"]
    assert actual["items"][0]["to"] == preview["items"][0]["to"]
