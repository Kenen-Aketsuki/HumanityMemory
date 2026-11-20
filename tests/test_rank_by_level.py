"""检索排序接入等级倍率（TODO 11 / §5.5）。

```
final = type_weight × (w_retrieval·sim + w_importance·importance + w_level·level_factor)
```

加这一项的理由不是"更精细"，而是**必须**：旧库实测 top-6 的相似度区间只有 0.07，
没有等级项时噪音条目极易挤进来。等级是唯一能表达「这条已经不重要了」的排序信号。

这里验证的是「同一份内容、只有等级不同」时分数怎么动 —— 相似度是同一个值，
所以分差能直接和公式手算对上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.lifecycle import combine_score, level_factor


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
    store.workspace_conn.execute(
        "UPDATE knowledge SET forget_level = ? WHERE id = ?", (level, item_id)
    )


def _score_map(store: MemoryStore, query: str) -> dict[str, float]:
    # 这些用例验的是「各项权重如何拼出 score」，与相关性门槛无关，显式关掉
    return {
        h.item.id: h.score
        for h in store.search(query, kinds=["experience"], limit=10, min_score=0.0)
    }


# ---------------------------------------------------------------- 排序


def test_higher_level_ranks_first_with_same_content(env) -> None:
    """内容一模一样的两条，等级高的必须排前面。"""
    _, store = env
    text = "删除任务时先取消订阅再销毁节点，否则回调会打到已销毁的对象上"

    high = store.write_memory(text, importance=0.7)
    low = store.write_memory(text, importance=0.7, allow_duplicate=True)
    _set_level(store, high.id, 5)
    _set_level(store, low.id, 2)

    scores = _score_map(store, "删除任务 取消订阅")
    assert high.id in scores and low.id in scores
    assert scores[high.id] > scores[low.id]


def test_score_gap_matches_formula(env) -> None:
    """分差要和手算对上 —— 不是"看起来变大了"，是正好差这么多。"""
    cfg, store = env
    text = "批量导入要先按天分组，否则同一天的日志会跨多个 digest"

    hi = store.write_memory(text, importance=0.7)
    lo = store.write_memory(text, importance=0.7, allow_duplicate=True)
    _set_level(store, hi.id, 5)
    _set_level(store, lo.id, 1)

    scores = _score_map(store, "批量导入 按天分组")
    assert hi.id in scores and lo.id in scores

    total = cfg.w_retrieval + cfg.w_importance + cfg.w_level
    expected = (
        cfg.w_level
        * (
            level_factor(5, cfg.max_level, cfg.level_factor_power)
            - level_factor(1, cfg.max_level, cfg.level_factor_power)
        )
        / total
    )
    # 经验 type_weight = 1.00，所以不用再乘
    assert scores[hi.id] - scores[lo.id] == pytest.approx(expected, abs=1e-6)


def test_importance_and_level_are_separate_terms(env) -> None:
    """两条同等级不同 importance：分差只由 importance 项产生，与等级项无关。"""
    cfg, store = env
    text = "存档写入失败要回滚整个事务，不能只跳过失败的那一条"

    heavy = store.write_memory(text, importance=0.95)
    light = store.write_memory(text, importance=0.3, allow_duplicate=True)
    _set_level(store, heavy.id, 4)
    _set_level(store, light.id, 4)

    scores = _score_map(store, "存档写入失败 回滚事务")
    total = cfg.w_retrieval + cfg.w_importance + cfg.w_level
    expected = cfg.w_importance * (0.95 - 0.3) / total
    assert scores[heavy.id] - scores[light.id] == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------- 边界


def test_level_zero_item_never_surfaces(env) -> None:
    """0 级倍率是 0，而且它压根不进常规检索 —— 两道保险。"""
    _, store = env
    item = store.write_memory("已经沉底的经验", importance=0.9)
    _set_level(store, item.id, 0)

    assert all(h.item.id != item.id for h in store.search("已经沉底"))
    assert level_factor(0, 5, 1.5) == 0.0


def test_logs_get_full_level_factor(env) -> None:
    """日志没有等级列，恒按 1.0 —— 不该因为「没有等级」被扣成 0。"""
    cfg, store = env
    store.append_log("排查沙箱误建目录的那一次")

    hits = store.search("沙箱误建目录", kinds=["log"])
    assert hits, "日志检索不该被等级过滤掉"
    total = cfg.w_retrieval + cfg.w_importance + cfg.w_level
    # 等级项拿满：把已知量代回公式，反推 level 项确实是 1.0 而不是 0
    hit = hits[0]
    sim = hit.vector_similarity or hit.lexical_score or 0.0
    plain = combine_score(
        retrieval=sim,
        importance=hit.item.importance,
        level=0.0,
        w_retrieval=cfg.w_retrieval,
        w_importance=cfg.w_importance,
        w_level=cfg.w_level,
    )
    assert hit.score > plain, "等级项若为 0，这两者会相等"
