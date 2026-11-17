"""经历压缩队列（TODO 8 / §3.3.1）。

这一层的边界是硬划的：**降级判定是纯 SQL，压缩改写是生成任务**。
所以这里只测两件事 —— 队列怎么推出来、覆盖怎么落下去，
不测「AI 压得好不好」（那是生成质量，进不了单测）。

四条口径：
1. 队列没有「待处理」标记：`compression_level < required[forget_level]` 就是全部判据；
2. 压缩是**内容压缩**，不是条目合并：id、at、条目数都不动；
3. `level` 只能落在 `(当前, 目标]` —— 超目标会假装压得更深，写回当前值等于没压；
4. 有损操作必须留痕：`memory_events` 落 `compress`，带压缩前后的长度。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout


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


def _set_level(store: MemoryStore, item_id: str, level: int) -> None:
    """直接改等级：裁定是纯 SQL，测试不该为了造状态去跑一整个周期。"""
    store.workspace_conn.execute(
        "UPDATE episodes SET forget_level = ? WHERE id = ?", (level, item_id)
    )


def _episode(store: MemoryStore, text: str, *, importance: float = 0.8):
    """写一条经历。

    importance 0.8 → 初始等级 4、compression 0（完整叙述），
    正好是「还没被压过」的起点；0.6 落到 1，就不是干净起点了。
    """
    return store.write_episode(text, importance=importance)


# ---------------------------------------------------------------- 队列推导


def test_queue_derives_target_from_forget_level(env) -> None:
    cfg, store = env
    required = cfg.required_compression

    deep = _episode(store, "完整叙述的经历，细节很多。" * 6)
    mid = _episode(store, "中度压缩就够了的一段经历。" * 5)
    keep = _episode(store, "还在高位，不该被压。")

    _set_level(store, deep.id, 1)   # required[1] = 2
    _set_level(store, mid.id, 2)    # required[2] = 1
    _set_level(store, keep.id, 4)   # required[4] = 0，已经是 0

    queue = store.compression_queue()
    got = {item["id"]: item for item in queue["items"]}

    assert set(got) == {deep.id, mid.id}, "只有压得不够深的两条该入队"
    assert got[deep.id]["target_level"] == required[1]
    assert got[mid.id]["target_level"] == required[2]
    assert got[deep.id]["current_level"] == 0
    assert queue["total"] == 2


def test_empty_queue_when_nothing_to_compress(env) -> None:
    _, store = env
    _episode(store, "一条完整叙述。")

    queue = store.compression_queue()
    assert queue["items"] == []
    assert queue["total"] == 0


def test_queue_skips_archived(env) -> None:
    _, store = env
    item = _episode(store, "已经归档的经历。" * 4)
    _set_level(store, item.id, 1)
    store.workspace_conn.execute(
        "UPDATE episodes SET archived_at = 1 WHERE id = ?", (item.id,)
    )

    assert store.compression_queue()["items"] == []


def test_queue_only_lists_episodes(env) -> None:
    """经验永不压缩 —— 压缩会丢精确性，而经验的价值就在精确。"""
    _, store = env
    exp = store.write_memory("一条经验，不该出现在压缩队列里", importance=0.7)

    ids = {item["id"] for item in store.compression_queue()["items"]}
    assert exp.id not in ids


# ---------------------------------------------------------------- 定向覆盖


def test_compress_overwrites_in_place(env) -> None:
    _, store = env
    item = _episode(store, "很长的经历记录。" * 20)
    _set_level(store, item.id, 1)  # 目标 2

    before = store.get(item.id)
    total_before = store.workspace_conn.execute(
        "SELECT count(*) FROM episodes"
    ).fetchone()[0]

    result = store.compression_apply([{"id": item.id, "content": "压到一句话的经历。"}])

    assert result["count"] == 1
    assert result["skipped"] == []

    after = store.get(item.id)
    assert after.id == before.id, "id 不能变"
    assert after.at == before.at, "at 指向事件真实发生时刻，不能漂成压缩时刻"
    assert after.compression_level == 2, "不传 level 就写到目标值"
    assert after.content == "压到一句话的经历。"
    assert len(after.content) < len(before.content)

    total_after = store.workspace_conn.execute(
        "SELECT count(*) FROM episodes"
    ).fetchone()[0]
    assert total_after == total_before, "压缩是内容压缩，不是条目合并"


def test_compressed_item_leaves_queue(env) -> None:
    _, store = env
    item = _episode(store, "完整叙述，先降级再压缩。" * 8)
    _set_level(store, item.id, 1)

    assert {i["id"] for i in store.compression_queue()["items"]} == {item.id}

    store.compression_apply([{"id": item.id, "content": "一句话。"}])

    assert store.compression_queue()["items"] == [], "压到目标值就该自动出队"


def test_partial_progress_stays_in_queue(env) -> None:
    """压不到目标深度也能提交，但条目留在队列里等下一轮。"""
    _, store = env
    item = _episode(store, "完整叙述。" * 20)
    _set_level(store, item.id, 1)  # 目标 2

    store.compression_apply([{"id": item.id, "content": "压到一半。", "level": 1}])

    after = store.get(item.id)
    assert after.compression_level == 1
    assert {i["id"] for i in store.compression_queue()["items"]} == {item.id}


# ---------------------------------------------------------------- 三条硬校验


def test_rejects_longer_content(env) -> None:
    _, store = env
    item = _episode(store, "原来就挺短的一段。")
    _set_level(store, item.id, 1)

    result = store.compression_apply(
        [{"id": item.id, "content": "压完之后反而变长了，这显然不是压缩。" * 2}]
    )

    assert result["count"] == 0
    assert "不是压缩" in result["skipped"][0]["reason"]
    assert store.get(item.id).content == "原来就挺短的一段。"


def test_rejects_level_above_target(env) -> None:
    """写超目标会把队列钉死：compression_level 永远 ≥ 目标，却根本不是那条该有的详略。"""
    _, store = env
    item = _episode(store, "一段经历。" * 10)
    _set_level(store, item.id, 2)  # 目标 1

    result = store.compression_apply(
        [{"id": item.id, "content": "一句话。", "level": 3}]
    )

    assert result["count"] == 0
    assert "超过目标" in result["skipped"][0]["reason"]


def test_rejects_no_progress(env) -> None:
    _, store = env
    item = _episode(store, "一段经历。" * 10)
    _set_level(store, item.id, 1)  # 目标 2

    result = store.compression_apply(
        [{"id": item.id, "content": "短版。", "level": 0}]
    )

    assert result["count"] == 0
    assert "等于没压" in result["skipped"][0]["reason"]


def test_rejects_experience_id(env) -> None:
    _, store = env
    exp = store.write_memory("一条经验", importance=0.7)

    result = store.compression_apply([{"id": exp.id, "content": "短版"}])
    assert result["count"] == 0
    assert "不是经历" in result["skipped"][0]["reason"]


def test_rejects_archived(env) -> None:
    _, store = env
    item = _episode(store, "已归档的经历。" * 5)
    _set_level(store, item.id, 1)
    store.workspace_conn.execute(
        "UPDATE episodes SET archived_at = 1 WHERE id = ?", (item.id,)
    )

    result = store.compression_apply([{"id": item.id, "content": "短版。"}])
    assert result["count"] == 0
    assert "已归档" in result["skipped"][0]["reason"]


# ---------------------------------------------------------------- 审计


def test_compress_leaves_audit_trail(env) -> None:
    """压缩有损，掉得离谱时（比如 500→8 字）得有据可查。"""
    _, store = env
    item = _episode(store, "原始内容。" * 30)
    _set_level(store, item.id, 1)
    before_len = len(store.get(item.id).content)

    store.compression_apply([{"id": item.id, "content": "一句话。"}])

    row = store.workspace_conn.execute(
        "SELECT event, from_value, to_value, delta, meta FROM memory_events"
        " WHERE item_id = ? AND event = 'compress'",
        (item.id,),
    ).fetchone()

    assert row is not None
    assert row["from_value"] == "0"
    assert row["to_value"] == "2"
    assert row["delta"] == float(len("一句话。") - before_len)

    meta = json.loads(row["meta"])
    assert meta["before_len"] == before_len
    assert meta["after_len"] == len("一句话。")
    assert meta["target_level"] == 2


# ---------------------------------------------------------------- 压缩区与考察期
# 1~5 级同时是「录入落点」和「晋升考察期」：记忆在这里证明自己够不够格升到
# 长期段。压缩是覆盖写 content，一旦压了，升上去也回不来 —— 所以压缩区
# 必须整个落在考察期之下。这两条守住这个边界，防止调 bands 或调压缩表时
# 又把考察期埋进压缩区（那等于用销毁证据的方式来做判断）。


def test_compression_zone_stays_below_judgement_band(env) -> None:
    """最低录入档及以上一律不压：正文在考察期必须完整。"""
    cfg, _ = env
    required = cfg.required_compression
    lowest_initial = min(int(b["level"]) for b in cfg.initial_bands)

    for level in range(lowest_initial, cfg.max_level + 1):
        assert required[level] == 0, (
            f"L{level} 的目标压缩级别是 {required[level]}，但它是考察期的一部分："
            "正文一旦被压，这条记忆就再也没法证明自己了"
        )


def test_compression_targets_are_monotonic(env) -> None:
    """等级越低压得越狠：数组必须随下标单调不增，否则低档反而比高档完整。"""
    cfg, _ = env
    required = cfg.required_compression
    reverse_pairs = [
        (level, required[level], required[level + 1])
        for level in range(len(required) - 1)
        if required[level] < required[level + 1]
    ]
    assert not reverse_pairs, f"压缩目标倒挂（低档比高档轻）：{reverse_pairs}"


# ---------------------------------------------------------------- 压缩区与考察期
# 1~5 级同时是「录入落点」和「晋升考察期」：记忆在这里证明自己够不够格升到
# 长期段。压缩是覆盖写 content，一旦压了，升上去也回不来 —— 所以压缩区
# 必须整个落在考察期之下。这两条守住这个边界，防止调 bands 或调压缩表时
# 又把考察期埋进压缩区（那等于用销毁证据的方式来做判断）。


def test_compression_zone_stays_below_judgement_band(env) -> None:
    """最低录入档及以上一律不压：正文在考察期必须完整。"""
    cfg, _ = env
    required = cfg.required_compression
    lowest_initial = min(int(b["level"]) for b in cfg.initial_bands)

    for level in range(lowest_initial, cfg.max_level + 1):
        assert required[level] == 0, (
            f"L{level} 的目标压缩级别是 {required[level]}，但它是考察期的一部分："
            "正文一旦被压，这条记忆就再也没法证明自己了"
        )


def test_compression_targets_are_monotonic(env) -> None:
    """等级越低压得越狠：数组必须随下标单调不增，否则低档反而比高档完整。"""
    cfg, _ = env
    required = cfg.required_compression
    reverse_pairs = [
        (level, required[level], required[level + 1])
        for level in range(len(required) - 1)
        if required[level] < required[level + 1]
    ]
    assert not reverse_pairs, f"压缩目标倒挂（低档比高档轻）：{reverse_pairs}"
