"""录入查重（§5.3）：经验写入前向量 top-k，够了就转强化，不够才新建。

查重的判据是**相似度**，不是「措辞是否接近」——所以这里用可控的假 embedder
造出精确的相似度，而不是拿真实模型碰运气。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout

UNIT: list[float] = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def vec_for_sim(sim: float) -> list[float]:
    """造一个与 UNIT 相似度恰为 `sim` 的向量。

    vec0 的 cosine distance 是 `1 - cos`，而 `similarity_from_cosine_distance`
    把它按 `1 - d/2` 映射回 [0,1]，也就是 `sim = (1 + cos) / 2` —— 所以反向解出 cos。
    """
    cos = 2 * sim - 1
    return [cos, (max(0.0, 1 - cos * cos)) ** 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class FakeEmbedder:
    """按文本查表返回定长向量；表里没登记的文本当作「和 UNIT 完全相同」。"""

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
    yield cfg, store
    store.close()


def test_duplicate_is_rejected_and_reinforces_existing(env) -> None:
    """相似度 0.93 ≥ dup_floor(0.90)：不新建，给原条目加活跃度，返回原 id。"""
    cfg, store = env
    store.embedder = FakeEmbedder(
        {"旧结论": UNIT, "换了个说法的同一件事": vec_for_sim(0.93)}
    )

    first = store.write_memory("旧结论", importance=0.8)
    assert first.deduped is False

    again = store.write_memory("换了个说法的同一件事", importance=0.8)

    assert again.id == first.id, "命中的必须是库里的那条"
    assert again.deduped is True
    assert again.dup_similarity == pytest.approx(0.93, abs=1e-4)
    assert store.stats()["dbs"]["workspace"]["experience_live"] == 1, "库里不该多出一条"
    assert again.activity == pytest.approx(first.activity + cfg.dedup_bonus)
    assert again.to_dict()["deduped"] is True


def test_rejection_is_audited(env) -> None:
    """被拦下这件事本身要留痕：三个月后要能回答「那天为什么没写进去」。"""
    _, store = env
    store.embedder = FakeEmbedder({"甲": UNIT, "甲的另一种说法": vec_for_sim(0.95)})

    store.write_memory("甲")
    store.write_memory("甲的另一种说法")

    row = store.workspace_conn.execute(
        "SELECT event, to_value, delta, reason FROM memory_events WHERE event = 'dedup'"
    ).fetchone()
    assert row is not None
    assert row["to_value"] == "reinforced"
    assert row["delta"] == pytest.approx(0.30)
    assert "甲的另一种说法" in row["reason"]


def test_dissimilar_experience_is_written(env) -> None:
    """相似度 0.70 < 0.90：这是另一条结论，正常入库。"""
    _, store = env
    store.embedder = FakeEmbedder({"结论甲": UNIT, "结论乙": vec_for_sim(0.70)})

    first = store.write_memory("结论甲")
    second = store.write_memory("结论乙")

    assert second.deduped is False
    assert second.id != first.id
    assert store.stats()["dbs"]["workspace"]["experience_live"] == 2


def test_invalidated_experience_is_not_a_duplicate(env) -> None:
    """已失效的条目不算重复 —— 新写的很可能正是来取代它的。

    这一条不做，被推翻过的结论会变成写入的永久路障：越像越写不进去。
    """
    _, store = env
    store.embedder = FakeEmbedder({"旧结论": UNIT, "新结论": vec_for_sim(0.97)})

    old = store.write_memory("旧结论")
    store.invalidate(old.id)

    fresh = store.write_memory("新结论")

    assert fresh.deduped is False
    assert fresh.id != old.id
    assert fresh.live


def test_allow_duplicate_forces_write(env) -> None:
    """措辞相似但确实是另一件事时，调用方可以显式跳过查重。"""
    _, store = env
    store.embedder = FakeEmbedder({"甲": UNIT, "甲（另一件事）": vec_for_sim(0.99)})

    first = store.write_memory("甲")
    forced = store.write_memory("甲（另一件事）", allow_duplicate=True)

    assert forced.deduped is False
    assert forced.id != first.id


def test_invalidates_bypasses_dedup(env) -> None:
    """带 invalidates 就不查重，否则旧条被失效、新条又被拦，等于凭空空掉一条。"""
    _, store = env
    store.embedder = FakeEmbedder({"旧结论": UNIT, "新结论": vec_for_sim(0.99)})

    old = store.write_memory("旧结论")
    new = store.write_memory("新结论", invalidates=old.id)

    assert new.deduped is False
    assert new.id != old.id
    assert store.get_experience(old.id).live is False
    assert store.get_experience(new.id).live is True


def test_no_embedding_means_no_dedup(env) -> None:
    """没配 embedding 时查重自动失效，写入必须照常成功。

    查重是优化，一旦它变成写入的前置条件，没网就等于不能用。
    """
    _, store = env
    first = store.write_memory("一模一样的结论")
    second = store.write_memory("一模一样的结论")

    assert second.id != first.id
    assert second.deduped is False


def test_episodes_are_never_deduped(env) -> None:
    """经历不查重：同一天做两次同样的排查，是两件事，不是一条被写了两遍。"""
    _, store = env
    store.embedder = FakeEmbedder({"同样的排查": UNIT})

    a = store.write_episode("同样的排查", importance=0.4)
    b = store.write_episode("同样的排查", importance=0.4)

    assert a.id != b.id
    assert len(store.list_episodes(limit=10)) == 2


def test_consolidate_reinforces_instead_of_duplicating(env) -> None:
    """复盘最容易产出重复结论：命中的不计入 digest 产出数，也不能悄悄多一条。"""
    _, store = env
    store.embedder = FakeEmbedder({"已有结论": UNIT, "同一结论换个说法": vec_for_sim(0.96)})

    store.write_memory("已有结论")
    store.append_log("当天日志一条")
    result = store.consolidate_apply(
        experience=[{"content": "同一结论换个说法"}], purge=False
    )

    assert any("高度重复" in w for w in result["warnings"])
    assert store.stats()["dbs"]["workspace"]["experience_live"] == 1
    row = store.workspace_conn.execute(
        "SELECT knowledge_count FROM digests ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row["knowledge_count"] == 0, "没新建就不能算进产出"


def test_consolidate_with_invalidates_bypasses_dedup(env) -> None:
    """复盘里写明「取代旧结论」时，查重必须让路。"""
    _, store = env
    store.embedder = FakeEmbedder({"旧结论": UNIT, "新结论": vec_for_sim(0.98)})

    old = store.write_memory("旧结论")
    store.append_log("当天日志一条")
    result = store.consolidate_apply(
        experience=[{"content": "新结论", "invalidates": old.id}], purge=False
    )

    assert len(result["created_experience"]) == 1
    assert result["created_experience"][0]["id"] != old.id
    assert store.get_experience(old.id).live is False
