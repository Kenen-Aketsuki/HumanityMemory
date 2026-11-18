"""深度回忆与复活（TODO 9 / §3.2）。

遗忘层是「0 级」：正文一个字不动，但不进常规检索 —— 它与归档同义，
只是可以被主动翻出来。这条边界的三个后果都得钉住：

1. 常规检索**看不到** 0 级；
2. 常规捞不到（最高相似度 < `deep_recall_floor`）时才转深度回忆；
3. 经历被深度回忆命中 `revive_hits` 次 → 复活到 1 级，不是 3 级。

第 3 条的方向是有意的：想起一次不代表记住了，回 3 级等于一次免费续命，
遗忘层会变回收站。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.lifecycle import DAY_MS
from humanity_memory.models import now_ms
from humanity_memory.models import now_ms


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


def _forget(store: MemoryStore, item_id: str, *, since: int | None = None) -> None:
    """把一条记忆推进遗忘层：0 级 + 记下进入时刻（默认刚刚）。"""
    store.workspace_conn.execute(
        "UPDATE episodes SET forget_level = 0, level0_since = ? WHERE id = ?",
        (since if since is not None else now_ms(), item_id),
    )


def _events(store: MemoryStore, item_id: str) -> list:
    return store.workspace_conn.execute(
        "SELECT event, from_value, to_value FROM memory_events"
        " WHERE item_id = ? ORDER BY rowid",
        (item_id,),
    ).fetchall()


# ---------------------------------------------------------------- 可见性


def test_forgotten_episode_is_invisible_to_normal_search(env) -> None:
    """0 级不进常规检索 —— 遗忘层与归档同义（§3.1）。"""
    _, store = env
    item = store.write_episode("深海打捞：一次彻底失联的排查", importance=0.8)

    assert store.search("深海打捞") != [], "还在常规视野时应该查得到"

    _forget(store, item.id)
    assert store.search("深海打捞") == [], "落到 0 级后就该从常规检索里消失"


def test_logs_are_never_filtered_by_level(env) -> None:
    """日志没有等级概念，不该被遗忘层过滤误伤。"""
    _, store = env
    store.append_log("一条普通日志")

    assert store.search("普通日志", kinds=["log"]) != []


def test_deep_recall_only_sees_level_zero(env) -> None:
    _, store = env
    live = store.write_episode("活着的经历：网关超时排查", importance=0.8)
    dead = store.write_episode("遗忘的经历：网关超时排查", importance=0.8)
    _forget(store, dead.id)

    result = store.deep_recall("网关超时排查")
    ids = {h.item.id for h in result["hits"]}

    assert ids == {dead.id}
    assert live.id not in ids
    assert all(h.forgotten for h in result["hits"])
    assert "已遗忘" in result["hits"][0].to_line()


# ---------------------------------------------------------------- 触发线


def test_recall_falls_back_to_deep_when_nothing_matches(env) -> None:
    """常规捞不到（相似度低于 deep_recall_floor）才翻遗忘层。"""
    _, store = env
    dead = store.write_episode("旧事：关于数据库连接池耗尽", importance=0.8)
    _forget(store, dead.id)

    hits = store.recall("连接池")
    assert any(h.item.id == dead.id and h.forgotten for h in hits)


def test_recall_skips_deep_when_normal_hits_are_good(env) -> None:
    """常规命中够相关时不翻遗忘层 —— 否则每次召回都白扫一遍。"""
    _, store = env
    store.write_episode("数据库连接池耗尽的排查记录", importance=0.8)
    dead = store.write_episode("另一件已遗忘的事", importance=0.8)
    _forget(store, dead.id)

    hits = store.recall("数据库连接池耗尽")
    assert hits
    assert all(not h.forgotten for h in hits)


def test_deep_recall_respects_relevance_floor(env) -> None:
    """遗忘层里也得够相关才算「想起」，否则一点词法重叠就能刷复活次数。"""
    _, store = env
    dead = store.write_episode("完全无关的一段旧事", importance=0.8)
    _forget(store, dead.id)

    result = store.deep_recall("量子色动力学与渐近自由")
    assert result["count"] == 0
    assert result["revived"] == []
    assert _events(store, dead.id) == []


# ---------------------------------------------------------------- 复活


def test_first_hit_counts_but_does_not_revive(env) -> None:
    cfg, store = env
    item = store.write_episode("要被想起来两次的事", importance=0.8)
    _forget(store, item.id)

    result = store.deep_recall("要被想起来两次")
    assert result["revived"] == []

    row = store.workspace_conn.execute(
        "SELECT forget_level, revive_hits FROM episodes WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["revive_hits"] == 1
    assert row["forget_level"] == 0, "想起一次不代表记住了"

    events = _events(store, item.id)
    assert events[-1]["event"] == "deep_hit"


def test_second_hit_revives_to_level_one(env) -> None:
    cfg, store = env
    item = store.write_episode("要被想起来两次的事", importance=0.8)
    _forget(store, item.id)

    first = store.deep_recall("要被想起来两次")
    assert first["revived"] == []
    # 第一次命中后它还留在遗忘层，所以第二次仍能捞到
    second = store.deep_recall("要被想起来两次")

    assert second["revived"] == [item.id]
    assert second["hits"][0].revived is True

    row = store.workspace_conn.execute(
        "SELECT forget_level, revive_hits, activity, period_start, level0_since"
        " FROM episodes WHERE id = ?",
        (item.id,),
    ).fetchone()
    assert row["forget_level"] == cfg.revive_to_level == 1, "复活回 1 级，不回 3 级"
    assert row["revive_hits"] == 0
    assert row["activity"] == 0
    assert row["period_start"] is not None
    assert row["level0_since"] is None, "升出 0 级要把停留计时清掉"

    assert any(e["event"] == "revive" for e in _events(store, item.id))
    # 复活后立刻回到常规视野
    assert store.search("要被想起来两次")


def test_revived_item_leaves_forget_layer(env) -> None:
    _, store = env
    item = store.write_episode("复活后不该再出现在遗忘层", importance=0.8)
    _forget(store, item.id)

    store.deep_recall("复活后不该再出现")
    store.deep_recall("复活后不该再出现")

    assert store.deep_recall("复活后不该再出现")["count"] == 0


def test_experience_has_no_revive_counter(env) -> None:
    """经验没有 revive_hits 列（§7.1 只给了 episodes），所以只标记不复活。

    这里如实断言 schema 的不对称 —— 假装支持会在某天变成"复活了但没记次"的悬案。
    """
    _, store = env
    exp = store.write_memory("一条落到遗忘层的经验", importance=0.7)
    store.workspace_conn.execute(
        "UPDATE knowledge SET forget_level = 0, level0_since = ? WHERE id = ?",
        (now_ms(), exp.id),
    )

    result = store.deep_recall("落到遗忘层的经验")
    assert result["count"] == 1
    assert result["hits"][0].forgotten is True
    assert result["revived"] == []

    row = store.workspace_conn.execute(
        "SELECT forget_level FROM knowledge WHERE id = ?", (exp.id,)
    ).fetchone()
    assert row["forget_level"] == 0


# ---------------------------------------------------------------- 0 级清理


def test_level0_goes_stale_and_becomes_purge_candidate(env) -> None:
    cfg, store = env
    item = store.write_episode("在遗忘层待了很久的事", importance=0.8)
    stale = now_ms() - int(cfg.level0_purge_days * DAY_MS) - 2 * DAY_MS
    _forget(store, item.id, since=stale)

    cands = store.purge_candidates()
    level0 = [c for c in cands if c["reason"] == "level0"]
    assert [c["id"] for c in level0] == [item.id]


def test_fresh_level0_is_not_a_candidate(env) -> None:
    _, store = env
    item = store.write_episode("刚掉进遗忘层的事", importance=0.8)
    _forget(store, item.id)

    assert [c for c in store.purge_candidates() if c["reason"] == "level0"] == []


def test_stale_level0_can_be_purged(env) -> None:
    """遗忘层超期的条目允许物理删 —— 它已经不在常规视野里了。"""
    cfg, store = env
    item = store.write_episode("该被清掉的事", importance=0.8)
    stale = now_ms() - int(cfg.level0_purge_days * DAY_MS) - 2 * DAY_MS
    _forget(store, item.id, since=stale)

    result = store.purge_items([item.id])
    assert result["count"] == 1
    with pytest.raises(Exception):
        store.get(item.id)


def test_live_item_still_refuses_purge(env) -> None:
    """常规视野里的条目依旧不能直接删 —— 这条闸门没被新分支拆掉。"""
    _, store = env
    item = store.write_episode("还好好的经历", importance=0.8)

    with pytest.raises(ValueError, match="拒绝直接删除"):
        store.purge_items([item.id])
