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


def test_write_and_get_experience(env) -> None:
    _, _, store = env
    fact = store.write_memory("项目使用 Python 3.13", tags=["环境"], importance=0.9)
    assert fact.id.startswith("x_")
    assert fact.live
    assert fact.scope == "workspace"
    assert fact.importance == pytest.approx(0.9)
    assert store.get_experience(fact.id).content == "项目使用 Python 3.13"


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


def test_get_missing_experience_raises(env) -> None:
    _, _, store = env
    with pytest.raises(NotFound):
        store.get_experience("f_nope")


def test_global_scope_is_closed(env) -> None:
    """§8.2：不再有 AI 自动写入的全局库，写 global 要明确报错而不是静默落项目库。"""
    _, layout, store = env
    with pytest.raises(ScopeError, match="全局库已关闭"):
        store.write_memory("跨项目事实", scope="global")
    assert not layout.global_db.exists(), "连一个空库都不该被建出来"


def test_invalidate_is_one_way(env) -> None:
    _, _, store = env
    fact = store.write_memory("会过时的事实")
    store.invalidate(fact.id)
    assert not store.get_experience(fact.id).live

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


def test_promote_is_retired(env) -> None:
    """`memory_promote` 已下线：没有全局库可去，且不该顺手把原条失效掉。"""
    _, _, store = env
    local = store.write_memory("值得跨项目复用的约定")

    with pytest.raises(ValueError, match="已下线"):
        store.promote(local.id)

    kept = store.get_experience(local.id)
    assert kept.live, "提升失败不能留下一个被失效却没有替代品的条目"
    assert kept.scope == "workspace"


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
        # 全局库已关闭，没有工作区就是彻底写不进去
        with pytest.raises(ScopeError):
            store.write_memory("全局可用", scope="global")
    finally:
        store.close()


# ---------------------------------------------------------------- 检索


def test_search_hits_experience_and_log(env) -> None:
    _, _, store = env
    store.write_memory("设施存档用总文件加模块分文件", tags=["存档"])
    store.append_log("今天重构了存档模块的划分方式")
    # 假 embedding 的相似度过不了真实门槛，这里只验召回面，显式关掉门槛
    hits = store.search("存档怎么组织", min_score=0.0)
    assert hits
    kinds = {h.kind for h in hits}
    assert "experience" in kinds


def test_search_only_covers_workspace(env) -> None:
    """只有项目库了，检索结果里不该再出现 global 来源。"""
    _, _, store = env
    store.write_memory("项目级事实：向量用 bge-m3")

    hits = store.search("bge-m3", limit=5)
    assert hits
    assert all(h.item.db == "workspace" for h in hits)


def test_workspace_isolation(env, tmp_path: Path) -> None:
    cfg, _, store = env
    store.write_memory("只属于本项目的秘密约定")
    store.close()

    other = tmp_path / "other"
    other.mkdir()
    cfg2 = Config.load({"HM_HOME": str(cfg.home), "HM_EMBED_DIM": "8"})
    layout2 = resolve_layout(other, home=cfg2.home)
    store2 = MemoryStore(cfg2, layout2)
    try:
        hits = store2.search("秘密约定", limit=10)
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
    assert store.get_experience(fact.id).access_count == 3


def test_search_does_not_reinforce(env) -> None:
    _, _, store = env
    fact = store.write_memory("只查不改")
    store.search("只查不改")
    assert store.get_experience(fact.id).access_count == 0


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
        experience=[{"content": "trigram 分词器需要 SQLite 3.34 以上", "tags": ["sqlite"]}],
        episodes=["今天排查了索引失效，根因是分词器版本过低。"],
        summary="索引问题排查",
        purge=True,
    )
    assert result["log_count"] == 2
    assert len(result["created_experience"]) == 1
    assert len(result["created_episodes"]) == 1
    assert result["purged"] == 2

    # 原始日志已物理清空
    assert store.list_logs(include_consolidated=True) == []
    # 事实与经历保留
    assert store.list_experience(scope="workspace")[0].content.startswith("trigram")
    assert store.list_episodes()[0].content.startswith("今天排查")


def test_consolidate_keeps_raw_when_asked(env) -> None:
    _, _, store = env
    store.append_log("保留测试")
    result = store.consolidate_apply(episodes=["压缩"], purge=False)
    assert result["purged"] == 0
    assert len(store.list_logs(include_consolidated=True)) == 1


def test_consolidate_dedupes_experience(env) -> None:
    _, _, store = env
    store.write_memory("已经存在的事实")
    store.append_log("今天没什么新东西")
    result = store.consolidate_apply(
        experience=[{"content": "已经存在的事实"}, {"content": "全新的结论"}],
        episodes=["压缩"],
    )
    assert len(result["created_experience"]) == 1
    assert result["created_experience"][0]["content"] == "全新的结论"


def test_consolidate_no_pending(env) -> None:
    _, _, store = env
    result = store.consolidate_apply(episodes=["空转"])
    assert result["log_count"] == 0
    assert "note" in result


def test_consolidate_invalidates_old_experience(env) -> None:
    _, _, store = env
    old = store.write_memory("旧事实：用 A 方案")
    store.append_log("今天决定改用 B 方案")
    store.consolidate_apply(
        experience=[{"content": "新事实：改用 B 方案", "invalidates": old.id}],
        episodes=["方案切换"],
    )
    assert not store.get_experience(old.id).live


# --------------------------------------------- 复盘：整批拒绝与原子回滚


def _consolidation_counts(store):
    """复盘前后对照计数：证明失败时零写入、原始日志没被动过。"""
    return {
        "logs": len(store.list_logs(include_consolidated=True)),
        "pending": len(store.list_logs(include_consolidated=False)),
        "knowledge": len(store.list_experience(scope="workspace")),
        "episodes": len(store.list_episodes()),
        "digests": store.workspace_conn.execute(
            "SELECT COUNT(*) FROM digests"
        ).fetchone()[0],
    }


def test_consolidate_rejects_unknown_kind_without_any_write(env) -> None:
    """复现 11-08 事故：state 误入 episodes。旧版静默跳过并清空了日志。"""
    _, _, store = env
    store.append_log("事件 A")
    store.append_log("事件 B")
    before = _consolidation_counts(store)

    with pytest.raises(ValueError, match=r"episodes\[0\].*放错了数组"):
        store.consolidate_apply(
            episodes=[{"content": "某现状", "kind": "experience"}], purge=True
        )

    assert _consolidation_counts(store) == before
    assert before["pending"] == 2


def test_consolidate_rejects_oversized_item_before_any_write(env) -> None:
    """超长经验必须在写库之前就被拦下，不能等写入时静默丢。

    episodes 没有长度上限 —— 经历本来就是长文本，压缩才是它的收敛手段；
    经验要能一句话说清，所以只有它卡上限。
    """
    _, _, store = env
    store.append_log("事件 A")
    before = _consolidation_counts(store)

    with pytest.raises(ValueError, match=r"experience\[1\].*上限"):
        store.consolidate_apply(
            experience=[
                {"content": "本来会被写进去的合法结论"},
                {"content": "超长结论" * 200},
            ],
            purge=True,
        )
    assert _consolidation_counts(store) == before


def test_consolidate_rolls_back_on_mid_write_failure(env, monkeypatch) -> None:
    """前置校验覆盖不到的运行时故障：整个提交回滚，日志与 digest 都不动。"""
    _, _, store = env
    store.append_log("事件 A")
    before = _consolidation_counts(store)

    def boom(*args, **kwargs):
        raise ValueError("模拟写入期故障")

    monkeypatch.setattr(store, "write_episode", boom)
    with pytest.raises(ValueError, match=r"episodes\[0\] 写入失败"):
        store.consolidate_apply(
            experience=[{"content": "先写进去又该被回滚的结论"}],
            episodes=["一条叙述"],
            purge=True,
        )
    assert _consolidation_counts(store) == before


def test_consolidate_retry_after_rejection_succeeds(env) -> None:
    """被拒后日志仍可复盘：改对参数原样重跑，不必手工补数据。"""
    _, _, store = env
    store.append_log("事件 A")
    with pytest.raises(ValueError):
        store.consolidate_apply(
            episodes=[{"content": "某现状", "kind": "experience"}], purge=True
        )
    result = store.consolidate_apply(
        experience=[{"content": "某现状", "kind": "experience", "source": "重跑"}],
        episodes=["修好后的叙述"],
        purge=True,
    )
    assert result["purged"] == 1
    assert len(result["created_experience"]) == 1


def test_consolidate_receipt_is_auditable(env) -> None:
    """回执必须能对照「请求几条 / 真写几条」，不能只看 ok=true。"""
    _, _, store = env
    store.append_log("事件 A")
    store.append_log("事件 B")
    result = store.consolidate_apply(
        experience=[
            {"content": "结论一"},
            {"content": "结论二", "kind": "experience", "source": "测试"},
        ],
        episodes=[{"content": "叙述一", "kind": "episode"}],
        purge=True,
    )
    assert result["requested_experience"] == 2 == len(result["created_experience"])
    assert result["requested_episodes"] == 1 == len(result["created_episodes"])
    assert result["skipped_experience"] == []
    assert result["warnings"] == []
    assert result["purged"] == 2


def test_consolidate_warns_on_zero_output(env) -> None:
    """漏传数组要在回执里显式提示。

    `summary` 只进 digest —— 它不再被兜底成一条经历：
    「总结」和「发生过的事」是两类东西，兜底会把复盘自己写的话当成经历留在库里。
    """
    _, _, store = env
    store.append_log("事件 A")
    zero = store.consolidate_apply(purge=False)
    assert any("零产出" in w for w in zero["warnings"])

    store.append_log("事件 B")
    result = store.consolidate_apply(summary="只有一句话总结", purge=False)
    assert result["created_episodes"] == []
    row = store.workspace_conn.execute(
        "SELECT summary FROM digests ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row["summary"] == "只有一句话总结"


def test_consolidate_default_day_follows_preview(env) -> None:
    """day 不传时与 preview 对齐，补历史不会把经历 / digest 记到今天。"""
    from humanity_memory.lifecycle import DAY_MS
    from humanity_memory.models import now_ms

    _, _, store = env
    store.append_log("昨天的日志", at=int(now_ms() - DAY_MS))
    preview = store.consolidate_preview()
    assert preview["log_count"] == 1

    result = store.consolidate_apply(episodes=["昨天的复盘"], purge=True)
    assert result["day"] == preview["day"]
    assert store.list_episodes()[0].day == preview["day"]


# ---------------------------------------------------------------- 统计


def test_stats_shape(env) -> None:
    _, _, store = env
    store.write_memory("一条项目事实")
    store.append_log("一条日志")
    stats = store.stats()
    assert set(stats["dbs"]) == {"workspace"}, "只剩项目库"
    assert stats["dbs"]["workspace"]["experience_live"] == 1
    assert stats["dbs"]["workspace"]["logs_pending"] == 1
    assert "global_experience" not in stats, "全局库关闭后这个统计没有意义了"




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
    assert store.get_experience(fact.id).archived
    assert store.get_experience(fact.id).content == "很久以前的老结论"

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
    assert not store.get_experience(fact.id).archived


def test_unarchive_restores(env) -> None:
    _, _, store = env
    from humanity_memory.models import now_ms

    day = 86_400_000
    fact = store.write_memory("临时归档的结论")
    store.workspace_conn.execute(
        "UPDATE knowledge SET created_at = ? WHERE id = ?", (now_ms() - 1200 * day, fact.id)
    )
    store.archive_pass(dry_run=False)
    assert store.get_experience(fact.id).archived

    store.unarchive(fact.id)
    assert not store.get_experience(fact.id).archived


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
        store.get_experience(fact.id)


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
    assert store.get_experience(fact.id) is not None


def test_initial_level_follows_importance(env) -> None:
    """初始粒度由重要性决定：重要的事记得细，琐事一开始就模糊。"""
    _, _, store = env
    ws = store.workspace_conn
    now = 1_700_000_000_000

    trivial = store._insert_episode("昨天吃了牛肉面", now, "2026-11-04", "d", importance=0.2)
    normal = store._insert_episode("常规任务：修了个拼写", now, "2026-11-04", "d", importance=0.6)
    heavy = store._insert_episode("重大失误，用户明确不满", now, "2026-11-04", "d", importance=0.95)

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
