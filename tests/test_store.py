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
    fact = store.write_fact("项目使用 Python 3.13", tags=["环境"], importance=0.9)
    assert fact.id.startswith("f_")
    assert fact.live
    assert fact.scope == "workspace"
    assert fact.importance == pytest.approx(0.9)
    assert store.get_fact(fact.id).content == "项目使用 Python 3.13"


def test_fact_rejects_empty_and_too_long(env) -> None:
    _, _, store = env
    with pytest.raises(ValueError):
        store.write_fact("   ")
    with pytest.raises(ValueError):
        store.write_fact("很长" * 300)


def test_fact_clamps_importance(env) -> None:
    _, _, store = env
    assert store.write_fact("高", importance=9).importance == 1.0
    assert store.write_fact("低", importance=-1).importance == 0.0


def test_get_missing_fact_raises(env) -> None:
    _, _, store = env
    with pytest.raises(NotFound):
        store.get_fact("f_nope")


def test_global_scope_goes_to_global_db(env) -> None:
    _, layout, store = env
    fact = store.write_fact("跨项目事实", scope="global")
    assert fact.db == "global"
    assert layout.global_db.exists()
    assert store.get_fact(fact.id).scope == "global"


def test_invalidate_is_one_way(env) -> None:
    _, _, store = env
    fact = store.write_fact("会过时的事实")
    store.invalidate_fact(fact.id)
    assert not store.get_fact(fact.id).live

    with pytest.raises(sqlite3.IntegrityError):
        fs = store.workspace_conn
        with tx(fs):
            fs.execute("UPDATE facts SET invalid_at = NULL WHERE id = ?", (fact.id,))


def test_fact_content_is_immutable(env) -> None:
    _, _, store = env
    fact = store.write_fact("原始正文")
    fs = store.workspace_conn
    with pytest.raises(sqlite3.IntegrityError):
        with tx(fs):
            fs.execute("UPDATE facts SET content = '改写' WHERE id = ?", (fact.id,))


def test_promote_fact(env) -> None:
    _, _, store = env
    local = store.write_fact("值得跨项目复用的约定")
    old, new = store.promote_fact(local.id)
    assert new.scope == "global"
    assert not store.get_fact(local.id).live
    assert store.get_fact(local.id).invalidated_by == new.id


def test_promote_global_twice_rejected(env) -> None:
    _, _, store = env
    g = store.write_fact("已是全局", scope="global")
    with pytest.raises(ValueError):
        store.promote_fact(g.id)


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
        assert store.write_fact("全局可用", scope="global").scope == "global"
    finally:
        store.close()


# ---------------------------------------------------------------- 检索


def test_search_hits_fact_and_log(env) -> None:
    _, _, store = env
    store.write_fact("设施存档用总文件加模块分文件", tags=["存档"])
    store.append_log("今天重构了存档模块的划分方式")
    hits = store.search("存档怎么组织")
    assert hits
    kinds = {h.kind for h in hits}
    assert "fact" in kinds


def test_cross_db_search(env) -> None:
    _, _, store = env
    store.write_fact("项目级事实：向量用 bge-m3")
    store.write_fact("用户是 Unity 客户端工程师", scope="global")

    local_hits = store.search("bge-m3", limit=5)
    assert any(h.item.db == "workspace" for h in local_hits)

    global_hits = store.search("工程师", limit=5)
    assert any(h.item.db == "global" for h in global_hits)


def test_workspace_isolation(env, tmp_path: Path) -> None:
    cfg, _, store = env
    store.write_fact("只属于本项目的秘密约定")
    store.write_fact("跨项目偏好", scope="global")
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
    old = store.write_fact("旧结论：用 UGUI")
    new = store.write_fact("新结论：用 UI Toolkit")
    store.invalidate_fact(old.id, invalidated_by=new.id)
    assert all(h.item.id != old.id for h in store.search("结论 UI"))
    assert any(
        h.item.id == old.id for h in store.search("结论 UI", include_invalid=True)
    )


def test_kind_filter(env) -> None:
    _, _, store = env
    store.write_fact("检索过滤测试事实")
    store.append_log("检索过滤测试日志")
    only_logs = store.search("检索过滤测试", kinds=["log"])
    assert only_logs and all(h.kind == "log" for h in only_logs)


def test_recall_reinforces(env) -> None:
    _, _, store = env
    fact = store.write_fact("会被反复召回的事实")
    for _ in range(3):
        store.recall("反复召回")
    assert store.get_fact(fact.id).access_count == 3


def test_search_does_not_reinforce(env) -> None:
    _, _, store = env
    fact = store.write_fact("只查不改")
    store.search("只查不改")
    assert store.get_fact(fact.id).access_count == 0


def test_empty_query_returns_nothing(env) -> None:
    _, _, store = env
    store.write_fact("随便一条")
    assert store.search("   ") == []


# ---------------------------------------------------------------- 复盘


def test_consolidate_full_flow(env) -> None:
    _, _, store = env
    for text in ["事件一：排查索引失效", "事件二：确认是分词器版本问题"]:
        store.append_log(text)

    preview = store.consolidate_preview()
    assert preview["log_count"] == 2

    result = store.consolidate_apply(
        facts=[{"content": "trigram 分词器需要 SQLite 3.34 以上", "tags": ["sqlite"]}],
        episodes=["今天排查了索引失效，根因是分词器版本过低。"],
        summary="索引问题排查",
        purge=True,
    )
    assert result["log_count"] == 2
    assert len(result["created_facts"]) == 1
    assert len(result["created_episodes"]) == 1
    assert result["purged"] == 2

    # 原始日志已物理清空
    assert store.list_logs(include_consolidated=True) == []
    # 事实与经历保留
    assert store.list_facts(scope="workspace")[0].content.startswith("trigram")
    assert store.list_episodes()[0].content.startswith("今天排查")


def test_consolidate_keeps_raw_when_asked(env) -> None:
    _, _, store = env
    store.append_log("保留测试")
    result = store.consolidate_apply(episodes=["压缩"], purge=False)
    assert result["purged"] == 0
    assert len(store.list_logs(include_consolidated=True)) == 1


def test_consolidate_dedupes_facts(env) -> None:
    _, _, store = env
    store.write_fact("已经存在的事实")
    store.append_log("今天没什么新东西")
    result = store.consolidate_apply(
        facts=[{"content": "已经存在的事实"}, {"content": "全新的结论"}],
        episodes=["压缩"],
    )
    assert len(result["created_facts"]) == 1
    assert result["created_facts"][0]["content"] == "全新的结论"


def test_consolidate_no_pending(env) -> None:
    _, _, store = env
    result = store.consolidate_apply(episodes=["空转"])
    assert result["log_count"] == 0
    assert "note" in result


def test_consolidate_invalidates_old_fact(env) -> None:
    _, _, store = env
    old = store.write_fact("旧事实：用 A 方案")
    store.append_log("今天决定改用 B 方案")
    store.consolidate_apply(
        facts=[{"content": "新事实：改用 B 方案", "invalidates": old.id}],
        episodes=["方案切换"],
    )
    assert not store.get_fact(old.id).live


# ---------------------------------------------------------------- 统计


def test_stats_shape(env) -> None:
    _, _, store = env
    store.write_fact("一条项目事实")
    store.write_fact("一条全局事实", scope="global")
    store.append_log("一条日志")
    stats = store.stats()
    assert stats["dbs"]["workspace"]["facts_live"] == 1
    assert stats["dbs"]["global"]["facts_live"] == 1
    assert stats["dbs"]["workspace"]["logs_pending"] == 1
    assert stats["global_facts"] == 1
    assert stats["global_facts_warn"] is False


def test_global_facts_warning(env) -> None:
    cfg, _, store = env
    for i in range(cfg.global_facts_warn + 1):
        store.write_fact(f"全局事实 {i}", scope="global")
    stats = store.stats()
    assert stats["global_facts_warn"] is True
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
