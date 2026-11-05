"""三层记忆的存储行为：信条 / 事实 / 日志 / 检索 / 复盘 / 硬约束。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, NotFound, ScopeError, resolve_layout
from humanity_memory import rules


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


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
    yield cfg, layout, store
    store.close()


# ---------------------------------------------------------------- 事实


def test_write_and_get_fact(env) -> None:
    _, _, store = env
    fact = store.write_memory("项目使用 Python 3.13", tags=["环境"], importance=0.9)
    assert fact.id.startswith("k_")
    assert fact.live
    assert fact.scope == "workspace"
    assert fact.importance == pytest.approx(0.9)
    assert store.get_knowledge(fact.id).content == "项目使用 Python 3.13"


def test_fact_rejects_empty_and_too_long(env) -> None:
    _, _, store = env
    with pytest.raises(ValueError):
        store.write_memory("   ")
    with pytest.raises(ValueError):
        store.write_memory("很长" * 300)


def test_fact_clamps_importance(env) -> None:
    _, _, store = env
    assert store.write_memory("高", importance=9).importance == 1.0
    assert store.write_memory("低", importance=-1).importance == 0.0


def test_get_missing_fact_raises(env) -> None:
    _, _, store = env
    with pytest.raises(NotFound):
        store.get_knowledge("f_nope")


def test_global_scope_goes_to_global_db(env) -> None:
    _, layout, store = env
    fact = store.write_memory("跨项目事实", scope="global")
    assert fact.db == "global"
    assert layout.global_db.exists()
    assert store.get_knowledge(fact.id).scope == "global"


def test_invalidate_is_one_way(env) -> None:
    _, _, store = env
    fact = store.write_memory("会过时的事实")
    store.invalidate(fact.id)
    assert not store.get_knowledge(fact.id).live

    with pytest.raises(sqlite3.IntegrityError):
        fs = store.workspace_conn
        with tx(fs):
            fs.execute("UPDATE knowledge SET invalid_at = NULL WHERE id = ?", (fact.id,))


def test_fact_content_is_immutable(env) -> None:
    _, _, store = env
    fact = store.write_memory("原始正文")
    fs = store.workspace_conn
    with pytest.raises(sqlite3.IntegrityError):
        with tx(fs):
            fs.execute("UPDATE knowledge SET content = '改写' WHERE id = ?", (fact.id,))


def test_promote_fact(env) -> None:
    _, _, store = env
    local = store.write_memory("值得跨项目复用的约定")
    old, new = store.promote(local.id)
    assert new.scope == "global"
    assert not store.get_knowledge(local.id).live
    assert store.get_knowledge(local.id).invalidated_by == new.id


def test_promote_global_twice_rejected(env) -> None:
    _, _, store = env
    g = store.write_memory("已是全局", scope="global")
    with pytest.raises(ValueError):
        store.promote(g.id)


# ---------------------------------------------------------------- 日志


def test_append_log_only(env) -> None:
    _, _, store = env
    entry = store.append_log("今天修了一个 bug", tags=["排查"])
    assert entry.id.startswith("l_")
    assert not entry.consolidated
    assert entry.day
    assert store.get_log(entry.id).content == "今天修了一个 bug"


def test_log_content_immutable(env) -> None:
    _, _, store = env
    entry = store.append_log("原始记录")
    ws = store.workspace_conn
    with pytest.raises(sqlite3.IntegrityError):
        with tx(ws):
            ws.execute("UPDATE logs SET content = '篡改' WHERE id = ?", (entry.id,))
    with pytest.raises(sqlite3.IntegrityError):
        with tx(ws):
            ws.execute("UPDATE logs SET at = 0 WHERE id = ?", (entry.id,))


def test_unconsolidated_log_cannot_be_deleted(env) -> None:
    _, _, store = env
    entry = store.append_log("未复盘的记录")
    ws = store.workspace_conn
    with pytest.raises(sqlite3.IntegrityError):
        with tx(ws):
            ws.execute("DELETE FROM logs WHERE id = ?", (entry.id,))


def test_consolidated_flag_is_the_only_allowed_update(env) -> None:
    _, _, store = env
    entry = store.append_log("待标记")
    ws = store.workspace_conn
    with tx(ws):
        ws.execute("UPDATE logs SET consolidated = 1 WHERE id = ?", (entry.id,))
    assert store.get_log(entry.id).consolidated


def test_log_requires_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    no_ws = tmp_path / "lonely"
    no_ws.mkdir()
    layout = resolve_layout(no_ws, create=False, home=cfg.home)
    assert layout.workspace_dir is None
    store = MemoryStore(cfg, layout)
    try:
        with pytest.raises(ScopeError):
            store.append_log("没有工作区")
        # 全局事实仍然可写
        assert store.write_memory("全局可用", scope="global").scope == "global"
    finally:
        store.close()


# ---------------------------------------------------------------- 检索


def test_search_hits_fact_and_log(env) -> None:
    _, _, store = env
    store.write_memory("设施存档用总文件加模块分文件", tags=["存档"])
    store.append_log("今天重构了存档模块的划分方式")
    hits = store.search("存档怎么组织")
    assert hits
    kinds = {h.kind for h in hits}
    assert "fact" in kinds


def test_cross_db_search(env) -> None:
    _, _, store = env
    store.write_memory("项目级事实：向量用 bge-m3")
    store.write_memory("用户是 Unity 客户端工程师", scope="global")

    local_hits = store.search("bge-m3", limit=5)
    assert any(h.item.db == "workspace" for h in local_hits)

    global_hits = store.search("工程师", limit=5)
    assert any(h.item.db == "global" for h in global_hits)


def test_workspace_isolation(env, tmp_path: Path) -> None:
    cfg, _, store = env
    store.write_memory("只属于本项目的秘密约定")
    store.write_memory("跨项目偏好", scope="global")
    store.close()

    other = tmp_path / "other"
    other.mkdir()
    cfg2 = Config.load({"HM_HOME": str(cfg.home), "HM_EMBED_DIM": "8"})
    layout2 = resolve_layout(other, home=cfg2.home)
    store2 = MemoryStore(cfg2, layout2)
    try:
        hits = store2.search("秘密约定 跨项目偏好", limit=10)
        contents = [h.item.content for h in hits]
        assert all("本项目的秘密约定" not in c for c in contents)
    finally:
        store2.close()


def test_invalid_facts_hidden_unless_requested(env) -> None:
    _, _, store = env
    old = store.write_memory("旧结论：用 UGUI")
    new = store.write_memory("新结论：用 UI Toolkit")
    store.invalidate(old.id, invalidated_by=new.id)
    assert all(h.item.id != old.id for h in store.search("结论 UI"))
    assert any(
        h.item.id == old.id for h in store.search("结论 UI", include_invalid=True)
    )


def test_kind_filter(env) -> None:
    _, _, store = env
    store.write_memory("检索过滤测试事实")
    store.append_log("检索过滤测试日志")
    only_logs = store.search("检索过滤测试", kinds=["log"])
    assert only_logs and all(h.kind == "log" for h in only_logs)


def test_recall_reinforces(env) -> None:
    _, _, store = env
    fact = store.write_memory("会被反复召回的事实")
    for _ in range(3):
        store.recall("反复召回")
    assert store.get_knowledge(fact.id).access_count == 3


def test_search_does_not_reinforce(env) -> None:
    _, _, store = env
    fact = store.write_memory("只查不改")
    store.search("只查不改")
    assert store.get_knowledge(fact.id).access_count == 0


def test_empty_query_returns_nothing(env) -> None:
    _, _, store = env
    store.write_memory("随便一条")
    assert store.search("   ") == []


# ---------------------------------------------------------------- 复盘


def test_consolidate_full_flow(env) -> None:
    _, _, store = env
    for text in ["事件一：排查索引失效", "事件二：确认是分词器版本问题"]:
        store.append_log(text)

    preview = store.consolidate_preview()
    assert preview["log_count"] == 2

    result = store.consolidate_apply(
        knowledge=[{"content": "trigram 分词器需要 SQLite 3.34 以上", "tags": ["sqlite"]}],
        episodes=["今天排查了索引失效，根因是分词器版本过低。"],
        summary="索引问题排查",
        purge=True,
    )
    assert result["log_count"] == 2
    assert len(result["created_knowledge"]) == 1
    assert len(result["created_episodes"]) == 1
    assert result["purged"] == 2

    # 原始日志已物理清空
    assert store.list_logs(include_consolidated=True) == []
    # 事实与经历保留
    assert store.list_knowledge(scope="workspace")[0].content.startswith("trigram")
    assert store.list_episodes()[0].content.startswith("今天排查")


def test_consolidate_keeps_raw_when_asked(env) -> None:
    _, _, store = env
    store.append_log("保留测试")
    result = store.consolidate_apply(episodes=["压缩"], purge=False)
    assert result["purged"] == 0
    assert len(store.list_logs(include_consolidated=True)) == 1


def test_consolidate_dedupes_facts(env) -> None:
    _, _, store = env
    store.write_memory("已经存在的事实")
    store.append_log("今天没什么新东西")
    result = store.consolidate_apply(
        knowledge=[{"content": "已经存在的事实"}, {"content": "全新的结论"}],
        episodes=["压缩"],
    )
    assert len(result["created_knowledge"]) == 1
    assert result["created_knowledge"][0]["content"] == "全新的结论"


def test_consolidate_no_pending(env) -> None:
    _, _, store = env
    result = store.consolidate_apply(episodes=["空转"])
    assert result["log_count"] == 0
    assert "note" in result


def test_consolidate_invalidates_old_fact(env) -> None:
    _, _, store = env
    old = store.write_memory("旧事实：用 A 方案")
    store.append_log("今天决定改用 B 方案")
    store.consolidate_apply(
        knowledge=[{"content": "新事实：改用 B 方案", "invalidates": old.id}],
        episodes=["方案切换"],
    )
    assert not store.get_knowledge(old.id).live


# ---------------------------------------------------------------- 统计


def test_stats_shape(env) -> None:
    _, _, store = env
    store.write_memory("一条项目事实")
    store.write_memory("一条全局事实", scope="global")
    store.append_log("一条日志")
    stats = store.stats()
    assert stats["dbs"]["workspace"]["knowledge_live"] == 1
    assert stats["dbs"]["global"]["knowledge_live"] == 1
    assert stats["dbs"]["workspace"]["logs_pending"] == 1
    assert stats["global_knowledge"] == 1
    assert stats["global_knowledge_warn"] is False


def test_global_facts_warning(env) -> None:
    cfg, _, store = env
    for i in range(cfg.global_facts_warn + 1):
        store.write_memory(f"全局事实 {i}", scope="global")
    stats = store.stats()
    assert stats["global_knowledge_warn"] is True
    assert "CORE_RULES" in stats["hint"]


# ---------------------------------------------------------------- 信条


def test_rules_template_and_append(env) -> None:
    _, layout, _ = env
    text = rules.load(layout.global_rules)
    assert "绝对信条" in text

    rules.append(layout.global_rules, "禁止删除用户数据")
    assert "禁止删除用户数据" in layout.global_rules.read_text(encoding="utf-8")


def test_rules_append_is_idempotent(env) -> None:
    _, layout, _ = env
    rules.append(layout.global_rules, "同一条规则")
    before = layout.global_rules.read_text(encoding="utf-8").count("同一条规则")
    rules.append(layout.global_rules, "同一条规则")
    after = layout.global_rules.read_text(encoding="utf-8").count("同一条规则")
    assert before == after


def test_rules_append_into_section(env) -> None:
    _, layout, _ = env
    rules.append(layout.global_rules, "分节规则", section="禁止事项")
    parsed = rules.sections(layout.global_rules.read_text(encoding="utf-8"))
    assert any("分节规则" in item for item in parsed.get("禁止事项", []))


def test_rules_render_is_stable(env) -> None:
    """同样内容必须渲染出逐字节相同的字符串，否则 KV Cache 会失效。"""
    _, layout, _ = env
    first = rules.render_instructions(
        global_rules=layout.global_rules, workspace_rules=layout.workspace_rules
    )
    second = rules.render_instructions(
        global_rules=layout.global_rules, workspace_rules=layout.workspace_rules
    )
    assert first == second
    assert "绝对信条" in first


def test_rules_empty_append_rejected(env) -> None:
    _, layout, _ = env
    with pytest.raises(rules.RulesError):
        rules.append(layout.global_rules, "   ")


# ---------------------------------------------------------------- 生命维持


def test_archive_hides_but_keeps_content(env) -> None:
    _, _, store = env
    import time

    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("很久以前的老结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ?, last_accessed_at = NULL, access_count = 0 WHERE id = ?",
        (now_ms() - 1200 * day, fact.id),
    )

    report = store.archive_pass(dry_run=True)
    assert any(x["id"] == fact.id for x in report["archived"])

    store.archive_pass(dry_run=False)
    assert store.get_knowledge(fact.id).archived
    assert store.get_knowledge(fact.id).content == "很久以前的老结论"

    assert all(h.item.id != fact.id for h in store.search("老结论"))
    assert any(h.item.id == fact.id for h in store.search("老结论", include_archived=True))


def test_archive_keeps_frequently_used(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("老但常被引用")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ?, last_accessed_at = ?, access_count = 9 WHERE id = ?",
        (now_ms() - 1200 * day, now_ms() - 1200 * day, fact.id),
    )
    store.archive_pass(dry_run=False)
    assert not store.get_knowledge(fact.id).archived


def test_unarchive_restores(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("临时归档的结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    assert store.get_knowledge(fact.id).archived

    store.unarchive(fact.id)
    assert not store.get_knowledge(fact.id).archived


def test_purge_refuses_unarchived(env) -> None:
    _, _, store = env
    fact = store.write_memory("还在用的结论")
    with pytest.raises(ValueError):
        store.purge_items([fact.id])


def test_purge_removes_archived(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("要清理的旧结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    result = store.purge_items([fact.id])
    assert result["count"] == 1
    with pytest.raises(NotFound):
        store.get_knowledge(fact.id)


def test_purge_candidates_only_reports(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("候选测试")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    cands = store.purge_candidates(after_days=0)
    assert any(c["id"] == fact.id for c in cands)
    # 报告不等于删除
    assert store.get_knowledge(fact.id) is not None


def test_compress_keeps_originals_archived(env) -> None:
    _, _, store = env
    ws = store.workspace_conn
    now = 1_700_000_000_000
    e1 = store._insert_episode(ws, "workspace", "经历一：排查索引", now, "2024-11-01", "d1")
    e2 = store._insert_episode(ws, "workspace", "经历二：确认版本", now, "2024-11-01", "d1")

    preview = store.compress_candidates()
    assert preview["total"] == 1
    assert preview["groups"][0]["count"] == 2

    result = store.compress_apply([e1.id, e2.id], "合并后的叙述", day="2024-11-01")
    assert result["merged_into"]["content"] == "合并后的叙述"
    assert len(result["compressed"]) == 2

    rows = ws.execute("SELECT id FROM episodes WHERE archived_at IS NOT NULL").fetchall()
    archived = {r["id"] for r in rows}
    assert {e1.id, e2.id} <= archived


# ---------------------------------------------------------------- 生命维持


def test_archive_hides_but_keeps_content(env) -> None:
    _, _, store = env
    import time

    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("很久以前的老结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ?, last_accessed_at = NULL, access_count = 0 WHERE id = ?",
        (now_ms() - 1200 * day, fact.id),
    )

    report = store.archive_pass(dry_run=True)
    assert any(x["id"] == fact.id for x in report["archived"])

    store.archive_pass(dry_run=False)
    assert store.get_knowledge(fact.id).archived
    assert store.get_knowledge(fact.id).content == "很久以前的老结论"

    assert all(h.item.id != fact.id for h in store.search("老结论"))
    assert any(h.item.id == fact.id for h in store.search("老结论", include_archived=True))


def test_archive_keeps_frequently_used(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("老但常被引用")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ?, last_accessed_at = ?, access_count = 9 WHERE id = ?",
        (now_ms() - 1200 * day, now_ms() - 1200 * day, fact.id),
    )
    store.archive_pass(dry_run=False)
    assert not store.get_knowledge(fact.id).archived


def test_unarchive_restores(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("临时归档的结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    assert store.get_knowledge(fact.id).archived

    store.unarchive(fact.id)
    assert not store.get_knowledge(fact.id).archived


def test_purge_refuses_unarchived(env) -> None:
    _, _, store = env
    fact = store.write_memory("还在用的结论")
    with pytest.raises(ValueError):
        store.purge_items([fact.id])


def test_purge_removes_archived(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("要清理的旧结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    result = store.purge_items([fact.id])
    assert result["count"] == 1
    with pytest.raises(NotFound):
        store.get_knowledge(fact.id)


def test_purge_candidates_only_reports(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("候选测试")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    cands = store.purge_candidates(after_days=0)
    assert any(c["id"] == fact.id for c in cands)
    # 报告不等于删除
    assert store.get_knowledge(fact.id) is not None


def test_compress_keeps_originals_archived(env) -> None:
    _, _, store = env
    ws = store.workspace_conn
    now = 1_700_000_000_000
    e1 = store._insert_episode(ws, "workspace", "经历一：排查索引", now, "2024-11-01", "d1")
    e2 = store._insert_episode(ws, "workspace", "经历二：确认版本", now, "2024-11-01", "d1")

    preview = store.compress_candidates()
    assert preview["total"] == 1
    assert preview["groups"][0]["count"] == 2

    result = store.compress_apply([e1.id, e2.id], "合并后的叙述", day="2024-11-01")
    assert result["merged_into"]["content"] == "合并后的叙述"
    assert len(result["compressed"]) == 2

    rows = ws.execute("SELECT id FROM episodes WHERE archived_at IS NOT NULL").fetchall()
    archived = {r["id"] for r in rows}
    assert {e1.id, e2.id} <= archived


# ---------------------------------------------------------------- 渐进压缩


def test_recompress_preview_by_age(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    ws = store.workspace_conn
    day = 86_400_000
    now = now_ms()

    old = store._insert_episode(ws, "workspace", "40 天前的完整叙述", now, "2026-09-25", "d1", 0)
    fresh = store._insert_episode(ws, "workspace", "5 天前的完整叙述", now, "2026-10-31", "d1", 0)
    older = store._insert_episode(ws, "workspace", "200 天前的去冗余版", now, "2026-04-19", "d2", 1)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 40 * day, old.id))
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 200 * day, older.id))

    preview = store.recompress_preview()
    ids = {it["id"] for g in preview["groups"] for it in g["items"]}
    assert old.id in ids
    assert older.id in ids
    assert fresh.id not in ids

    levels = {g["target_level"]: {it["id"] for it in g["items"]} for g in preview["groups"]}
    assert old.id in levels.get(1, set())
    assert older.id in levels.get(2, set())


def test_recompress_apply_updates_level_and_content(env) -> None:
    _, _, store = env
    ws = store.workspace_conn
    ep = store._insert_episode(ws, "workspace", "很长的原始叙述", 1_700_000_000_000, "2024-11-01", "d1", 0)

    result = store.recompress_apply([{"id": ep.id, "content": "压缩后的一句话"}], level=2)
    assert result["count"] == 1

    row = ws.execute(
        "SELECT content, compression_level FROM episodes WHERE id = ?", (ep.id,)
    ).fetchone()
    assert row["content"] == "压缩后的一句话"
    assert row["compression_level"] == 2


def test_recompress_rejects_bad_level(env) -> None:
    _, _, store = env
    with pytest.raises(ValueError):
        store.recompress_apply([{"id": "e_x", "content": "x"}], level=0)


def test_recompress_skips_unknown_id(env) -> None:
    _, _, store = env
    result = store.recompress_apply([{"id": "e_nope", "content": "x"}], level=1)
    assert result["count"] == 0
    assert result["skipped"] == ["e_nope"]


def test_initial_level_follows_importance(env) -> None:
    """初始粒度由重要性决定：重要的事记得细，琐事一开始就模糊。"""
    _, _, store = env
    ws = store.workspace_conn
    now = 1_700_000_000_000

    trivial = store._insert_episode(ws, "workspace", "昨天吃了牛肉面", now, "2026-11-04", "d", importance=0.2)
    normal = store._insert_episode(ws, "workspace", "常规任务：修了个拼写", now, "2026-11-04", "d", importance=0.6)
    heavy = store._insert_episode(ws, "workspace", "重大失误，用户明确不满", now, "2026-11-04", "d", importance=0.95)

    assert trivial.compression_level == 2
    assert normal.compression_level == 1
    assert heavy.compression_level == 0


def test_consolidate_accepts_importance_per_episode(env) -> None:
    _, _, store = env
    store.append_log("今天被用户批评了一顿，还顺手改了个小 bug")
    store.consolidate_apply(
        episodes=[
            {"content": "和用户沟通时过于自作主张，被明确指出应该先给方案", "importance": 0.9},
            {"content": "修了个 typo", "importance": 0.2},
        ],
        purge=True,
    )
    eps = {e.content: e for e in store.list_episodes()}
    assert eps["和用户沟通时过于自作主张，被明确指出应该先给方案"].compression_level == 0
    assert eps["修了个 typo"].compression_level == 2


def test_consolidate_still_accepts_plain_strings(env) -> None:
    _, _, store = env
    store.append_log("普通的一天")
    store.consolidate_apply(episodes=["纯字符串形式的经历"], purge=True)
    eps = store.list_episodes()
    assert eps and eps[0].content == "纯字符串形式的经历"
    # 默认 importance 0.4 → level 2
    assert eps[0].compression_level == 2


def test_recompress_threshold_scales_with_importance(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    ws = store.workspace_conn
    day = 86_400_000
    now = now_ms()

    # 重要（0.95）：门槛 30 * 1.55 = 46.5 天，40 天还不够
    heavy = store._insert_episode(ws, "workspace", "重大经历", now, "2026-09-25", "d", 0, importance=0.95)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 40 * day, heavy.id))

    # 次要（0.1）：门槛 30 * 0.7 = 21 天，25 天就够
    light = store._insert_episode(ws, "workspace", "小事", now, "2026-10-10", "d", 0, importance=0.1)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 25 * day, light.id))

    preview = store.recompress_preview()
    ids = {it["id"] for g in preview["groups"] for it in g["items"]}
    assert light.id in ids
    assert heavy.id not in ids


# ---------------------------------------------------------------- 渐进压缩


def test_recompress_preview_by_age(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    ws = store.workspace_conn
    day = 86_400_000
    now = now_ms()

    old = store._insert_episode(ws, "workspace", "40 天前的完整叙述", now, "2026-09-25", "d1", 0)
    fresh = store._insert_episode(ws, "workspace", "5 天前的完整叙述", now, "2026-10-31", "d1", 0)
    older = store._insert_episode(ws, "workspace", "200 天前的去冗余版", now, "2026-04-19", "d2", 1)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 40 * day, old.id))
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 200 * day, older.id))

    preview = store.recompress_preview()
    ids = {it["id"] for g in preview["groups"] for it in g["items"]}
    assert old.id in ids
    assert older.id in ids
    assert fresh.id not in ids

    levels = {g["target_level"]: {it["id"] for it in g["items"]} for g in preview["groups"]}
    assert old.id in levels.get(1, set())
    assert older.id in levels.get(2, set())


def test_recompress_apply_updates_level_and_content(env) -> None:
    _, _, store = env
    ws = store.workspace_conn
    ep = store._insert_episode(ws, "workspace", "很长的原始叙述", 1_700_000_000_000, "2024-11-01", "d1", 0)

    result = store.recompress_apply([{"id": ep.id, "content": "压缩后的一句话"}], level=2)
    assert result["count"] == 1

    row = ws.execute(
        "SELECT content, compression_level FROM episodes WHERE id = ?", (ep.id,)
    ).fetchone()
    assert row["content"] == "压缩后的一句话"
    assert row["compression_level"] == 2


def test_recompress_rejects_bad_level(env) -> None:
    _, _, store = env
    with pytest.raises(ValueError):
        store.recompress_apply([{"id": "e_x", "content": "x"}], level=0)


def test_recompress_skips_unknown_id(env) -> None:
    _, _, store = env
    result = store.recompress_apply([{"id": "e_nope", "content": "x"}], level=1)
    assert result["count"] == 0
    assert result["skipped"] == ["e_nope"]


def test_initial_level_follows_importance(env) -> None:
    """初始粒度由重要性决定：重要的事记得细，琐事一开始就模糊。"""
    _, _, store = env
    ws = store.workspace_conn
    now = 1_700_000_000_000

    trivial = store._insert_episode(ws, "workspace", "昨天吃了牛肉面", now, "2026-11-04", "d", importance=0.2)
    normal = store._insert_episode(ws, "workspace", "常规任务：修了个拼写", now, "2026-11-04", "d", importance=0.6)
    heavy = store._insert_episode(ws, "workspace", "重大失误，用户明确不满", now, "2026-11-04", "d", importance=0.95)

    assert trivial.compression_level == 2
    assert normal.compression_level == 1
    assert heavy.compression_level == 0


def test_consolidate_accepts_importance_per_episode(env) -> None:
    _, _, store = env
    store.append_log("今天被用户批评了一顿，还顺手改了个小 bug")
    store.consolidate_apply(
        episodes=[
            {"content": "和用户沟通时过于自作主张，被明确指出应该先给方案", "importance": 0.9},
            {"content": "修了个 typo", "importance": 0.2},
        ],
        purge=True,
    )
    eps = {e.content: e for e in store.list_episodes()}
    assert eps["和用户沟通时过于自作主张，被明确指出应该先给方案"].compression_level == 0
    assert eps["修了个 typo"].compression_level == 2


def test_consolidate_still_accepts_plain_strings(env) -> None:
    _, _, store = env
    store.append_log("普通的一天")
    store.consolidate_apply(episodes=["纯字符串形式的经历"], purge=True)
    eps = store.list_episodes()
    assert eps and eps[0].content == "纯字符串形式的经历"
    # 默认 importance 0.4 → level 2
    assert eps[0].compression_level == 2


def test_recompress_threshold_scales_with_importance(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    ws = store.workspace_conn
    day = 86_400_000
    now = now_ms()

    # 重要（0.95）：门槛 30 * 1.55 = 46.5 天，40 天还不够
    heavy = store._insert_episode(ws, "workspace", "重大经历", now, "2026-09-25", "d", 0, importance=0.95)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 40 * day, heavy.id))

    # 次要（0.1）：门槛 30 * 0.7 = 21 天，25 天就够
    light = store._insert_episode(ws, "workspace", "小事", now, "2026-10-10", "d", 0, importance=0.1)
    ws.execute("UPDATE episodes SET created_at = ? WHERE id = ?", (now - 25 * day, light.id))

    preview = store.recompress_preview()
    ids = {it["id"] for g in preview["groups"] for it in g["items"]}
    assert light.id in ids
    assert heavy.id not in ids
