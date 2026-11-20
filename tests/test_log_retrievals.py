"""log 检索条件与复盘继承（schema v5）。

守三件事：
1. log 只记「被什么查询命中过」，不记独立的 activity 字段；
2. 复盘时来源由向量自动推断、活跃度由派生 + 重放取大算出，模型零参与；
3. log 全程不进等级体系。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.store import merge_retrieval

UNIT: list[float] = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def vec_for_sim(sim: float) -> list[float]:
    """造一个与 UNIT 余弦映射后相似度恰为 `sim` 的向量。"""
    cos = 2 * sim - 1
    return [cos, (max(0.0, 1 - cos * cos)) ** 0.5] + [0.0] * 6


class FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = dict(mapping or {})

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "fake"

    def close(self) -> None:
        return None

    def try_embed(self, texts):
        return [list(self.mapping.get(t, UNIT)) for t in texts]


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    store = MemoryStore(cfg, resolve_layout(proj, home=cfg.home))
    store.embedder = FakeEmbedder()
    yield cfg, store
    store.close()


def _inject(log_id: str, store: MemoryStore, *, heat: float, q: str, sim: float = 0.9) -> None:
    """直接给日志灌检索条件，模拟"它今天被这个查询热召回过"。"""
    store.workspace_conn.execute(
        "UPDATE logs SET retrievals = ? WHERE id = ?",
        (
            json.dumps(
                [{"q": q, "n": 1, "sim": sim, "heat": heat, "at": 1000}],
                ensure_ascii=False,
            ),
            log_id,
        ),
    )


def _activity_of(store: MemoryStore, table: str, item_id: str) -> float:
    row = store.workspace_conn.execute(
        f"SELECT activity FROM {table} WHERE id = ?", (item_id,)
    ).fetchone()
    return float(row["activity"])


# ---------------------------------------------------------------- 记录


def test_recall_records_query_into_log_retrievals(env) -> None:
    _, store = env
    q = "引导不触发 排查"
    store.embedder = FakeEmbedder({q: UNIT})
    log = store.append_log("引导不触发 排查：三处检查", importance=0.5)

    hits = store.recall(q, kinds=["log"])
    assert any(h.item.id == log.id for h in hits)
    first = store.get_log(log.id).retrievals
    assert len(first) == 1
    assert first[0]["q"] == q
    assert first[0]["n"] == 1
    assert first[0]["heat"] > 0

    # 同 query 再命中：只累加，不新增项
    store.recall(q, kinds=["log"])
    again = store.get_log(log.id).retrievals
    assert len(again) == 1
    assert again[0]["n"] == 2
    assert again[0]["heat"] > first[0]["heat"]


def test_retrievals_cap_evicts_oldest(env) -> None:
    raw = "[]"
    for i in range(33):
        raw = merge_retrieval(raw, f"查询{i}", sim=0.9, heat=0.1, now=1000 + i, cap=32)
    items = json.loads(raw)
    assert len(items) == 32
    assert items[0]["q"] == "查询1", "最旧的被淘汰"
    assert items[-1]["q"] == "查询32"


# ---------------------------------------------------------------- 重放 / 派生


def test_replay_rewards_matching_query(env) -> None:
    _, store = env
    q = "引导不触发 排查"
    content = "引导不触发时先看配置、再看资源、最后看缓存"
    store.embedder = FakeEmbedder({q: UNIT, content: UNIT})
    log = SimpleNamespace(
        retrievals=[{"q": q, "n": 1, "sim": 0.9, "heat": 0.5, "at": 1}]
    )

    score, detail = store.replay_queries([log], content, UNIT)
    assert score > 0
    assert detail["query"] == q
    assert detail["old_sim"] == pytest.approx(0.9)


def test_replay_degrades_when_no_retrievals(env) -> None:
    _, store = env
    log = SimpleNamespace(retrievals=[])
    assert store.replay_queries([log], "任意内容", UNIT) == (0.0, None)


def test_inherit_takes_max_of_num_and_replay(env, monkeypatch) -> None:
    _, store = env
    log = SimpleNamespace(
        id="l_a", retrievals=[{"q": "a", "n": 1, "sim": 0.9, "heat": 0.8, "at": 1}]
    )
    monkeypatch.setattr(store, "infer_source_logs", lambda *a, **k: ([log], True))

    monkeypatch.setattr(store, "replay_queries", lambda *a, **k: (0.2, None))
    low = store._inherited_activity(None, [log], "内容", UNIT)
    assert low["num"] == pytest.approx(0.8)
    assert low["total"] == pytest.approx(0.8)

    monkeypatch.setattr(store, "replay_queries", lambda *a, **k: (0.95, None))
    high = store._inherited_activity(None, [log], "内容", UNIT)
    assert high["total"] == pytest.approx(0.95), "重放分更高时取重放"


def test_consolidate_merge_uses_sum_of_heat_capped(env, monkeypatch) -> None:
    _, store = env
    logs = [
        SimpleNamespace(
            id=f"l{i}", retrievals=[{"q": "a", "n": 1, "sim": 0.9, "heat": 0.5, "at": 1}]
        )
        for i in range(3)
    ]
    monkeypatch.setattr(store, "infer_source_logs", lambda *a, **k: (logs, True))
    monkeypatch.setattr(store, "replay_queries", lambda *a, **k: (0.0, None))
    result = store._inherited_activity(None, logs, "内容", UNIT)
    assert result["num"] == pytest.approx(1.0), "Σheat=1.5 封顶到 inherit_cap=1.0"


# ---------------------------------------------------------------- 来源自动映射


def test_source_auto_inferred_from_vec(env) -> None:
    _, store = env
    store.embedder = FakeEmbedder(
        {"日志A": UNIT, "日志B": vec_for_sim(0.2), "新结论": UNIT}
    )
    a = store.append_log("日志A")
    b = store.append_log("日志B")
    pending = store.list_logs()

    matched, auto = store.infer_source_logs(store.workspace_conn, pending, UNIT)
    assert auto is True
    assert [x.id for x in matched] == [a.id], "低于 source_match_floor 的日志被滤掉"

    fallback, auto = store.infer_source_logs(store.workspace_conn, pending, None)
    assert auto is False
    assert {x.id for x in fallback} == {a.id, b.id}, "向量不可用回落全量"


# ---------------------------------------------------------------- 三条合并路径


def test_write_paths_accept_inherited_activity(env) -> None:
    _, store = env
    x = store.write_experience("经验A", activity=0.6)
    e = store.write_episode("经历A", activity=0.4)
    assert _activity_of(store, "knowledge", x.id) == pytest.approx(0.6)
    assert _activity_of(store, "episodes", e.id) == pytest.approx(0.4)


def test_consolidate_dedup_uses_max_of_bonus_and_inherited(env) -> None:
    _, store = env
    store.embedder = FakeEmbedder(
        {"旧结论": UNIT, "换句话的旧结论": vec_for_sim(0.93)}
    )
    old = store.write_memory("旧结论", importance=0.8)
    log = store.append_log("某天的事")
    _inject(log.id, store, heat=0.8, q="热查询")

    store.consolidate_apply(experience=[{"content": "换句话的旧结论"}], purge=False)
    assert _activity_of(store, "knowledge", old.id) == pytest.approx(0.8), (
        "max(dup_bonus=0.30, 继承=0.80)，不是固定 0.30"
    )


def test_consolidate_exact_duplicate_still_reinforces(env) -> None:
    _, store = env
    old = store.write_memory("已有结论")
    log = store.append_log("某天的事")
    _inject(log.id, store, heat=0.7, q="热查询")

    report = store.consolidate_apply(experience=[{"content": "已有结论"}], purge=False)
    assert any(s["reason"] == "duplicate" for s in report["skipped_experience"])
    assert _activity_of(store, "knowledge", old.id) == pytest.approx(0.7)


def test_replay_mismatch_is_observation_only(env) -> None:
    _, store = env
    q = "完全不相关的查询"
    store.embedder = FakeEmbedder({q: vec_for_sim(0.1), "新结论": UNIT})
    log = store.append_log("某天的事")
    _inject(log.id, store, heat=0.8, q=q, sim=0.9)

    report = store.consolidate_apply(experience=[{"content": "新结论"}], purge=False)
    assert any("接不住" in w for w in report["warnings"]), "失配是观测信号"
    created = report["created_experience"][0]
    assert created["activity"] > 0, "告警不影响写入，继承值已取 max 保底"


# ---------------------------------------------------------------- 边界


def test_judge_ignores_logs(env) -> None:
    _, store = env
    log = store.append_log("一条很热的日志")
    _inject(log.id, store, heat=1.0, q="热查询")

    report = store.judge_due(dry_run=True)
    assert all(item["id"] != log.id for item in report["items"])
    assert report["due"] == 0, "log 没有 forget_level，不进等级体系"


def test_log_has_no_activity_column(env) -> None:
    _, store = env
    cols = {
        r[1] for r in store.workspace_conn.execute("PRAGMA table_info(logs)")
    }
    assert "retrievals" in cols
    assert "activity" not in cols
