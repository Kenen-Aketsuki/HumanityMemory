"""星图点大小 = 遗忘等级（2026-11-17 改）。

原来是 `3 + importance × 8`，好处是一眼看出「哪条重要」，
坏处是它和记忆机制的实际驱动脱节 —— 等级才是决定「这条还能不能被翻出来」的东西，
而 importance 只在录入那一刻用一次。

这里钉住两件事：
1. 每个节点都带 `forget_level`，且来自库里而不是猜的；
2. 日志没有等级列，必须落到中性值 3 且显式标 `has_level=false` ——
   否则「日志没有等级」和「日志恰好是 3 级」在数据里分不开，
   tooltip 就会对着一条原始日志说「遗忘等级 L3」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory import starmap


@pytest.fixture()
def store(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    s = MemoryStore(cfg, resolve_layout(proj, home=cfg.home))
    yield s
    s.close()


def _nodes(store: MemoryStore) -> dict[str, dict]:
    rows = starmap.read_nodes(store.workspace_conn)
    return {n["id"]: n for n in rows}


# ---------------------------------------------------------------- 后端字段


def test_nodes_carry_real_forget_level(store: MemoryStore) -> None:
    top = store.write_memory("顶档的经验", importance=0.9)
    store.workspace_conn.execute(
        "UPDATE knowledge SET forget_level = 5 WHERE id = ?", (top.id,)
    )
    low = store.write_memory("沉底的经验", importance=0.3)
    store.workspace_conn.execute(
        "UPDATE knowledge SET forget_level = 0 WHERE id = ?", (low.id,)
    )

    nodes = _nodes(store)

    assert nodes[top.id]["forget_level"] == 5
    assert nodes[top.id]["has_level"] is True
    assert nodes[low.id]["forget_level"] == 0, "0 级要如实给 0，不能被 `or` 兜成默认值"
    assert nodes[low.id]["has_level"] is True


def test_episode_level_is_read_too(store: MemoryStore) -> None:
    ep = store.write_episode("一条经历", importance=0.8)
    store.workspace_conn.execute(
        "UPDATE episodes SET forget_level = 2 WHERE id = ?", (ep.id,)
    )

    nodes = _nodes(store)
    assert nodes[ep.id]["forget_level"] == 2


def test_logs_get_neutral_level_but_are_marked(store: MemoryStore) -> None:
    """日志没有等级列：大小给中性 3，但要标 has_level=false，免得 tooltip 说谎。"""
    store.append_log("一条原始日志")

    node = next(n for n in _nodes(store).values() if n["kind"] == "log")

    assert node["forget_level"] == 3
    assert node["has_level"] is False


# ---------------------------------------------------------------- 前端断言


def test_body_size_is_importance_glow_is_level() -> None:
    """三个视觉通道各管一个维度，别互相顶替。

    这是硬编码在 HTML 模板里的，只能对着模板源码断言 —— 星图没有回归用例，
    改错了只有肉眼能发现，而肉眼恰好会被「所有样本都在同一档」骗过去。

    2026-11-17 二次修改后通道划分：大小 = importance、辉光颜色 = 活跃度、
    辉光透明度 = 遗忘等级。颜色换成了三锚点渐变，透明度那套数值刻意没动。
    """
    src = Path(starmap.__file__).read_text(encoding="utf-8")

    # 本体大小回到 importance（用户 2026-11-17 定：星星大小维持原样）
    assert "r:3+Math.min(9,Math.max(0,n.importance)*8)" in src, "本体半径由 importance 算"
    assert "n.forget_level||0))*2.3" not in src, "等级不该再决定本体大小"

    # 辉光透明度由遗忘等级算，且按量程归一（档数可变，不能写死 5）
    assert "0.02+ratio*0.375" in src, "辉光 alpha 必须随等级线性张开"
    assert "const LVMAX=" in src, "等级量程必须取自数据，不能写死档数"
    assert "Math.min(5,n.forget_level" not in src, "档数不能写死成 5"
    assert "ctx.fillStyle=n.gcol" in src, "辉光颜色必须走活跃度色标"
    assert "ctx.fillStyle=n.kind==='digest'?'#ffe08a':'#dbe8ff'" not in src, "辉光不该再是统一白蓝"

    # 三锚点必须是用户指定的那三个色，且颜色由 act01 决定
    assert "[0,[15,224,229]]" in src, "0 → #0FE0E5"
    assert "[.5,[246,222,20]]" in src, "0.5 → #F6DE14"
    assert "[1,[241,0,0]]" in src, "1 → #F10000"
    assert "gcol:n.kind==='digest'?'#ffe08a':(n.kind==='log'?LOG_GLOW:glowColor(n.act01||0))" in src
    assert "const LOG_GLOW='#8898ad'" in src, "原始日志的辉光固定灰，不去蹭色标通道"

    # 图例要分别说清三个通道，且顶档名不能写死
    assert "点大小 = 重要度 · 辉光颜色 = 活跃度 · 辉光透明度 = 遗忘等级" in src
    assert "L'+LVMAX+' 最实" in src, "图例里的顶档要跟量程走"
    assert "原始日志（不参与等级）" in src


def test_glow_and_radius_span_the_whole_level_range() -> None:
    """辉光与半径都按量程归一：5 档与 10 档都要铺满同一段亮度。

    档数变多后相邻档差必然变小（同样 0.375 的跨度分 10 份而非 5 份），
    这是「切更多刀」的代价，不是回归。守的是两件事：
    1. 起止值不随档数漂移 —— L0 恒为 0.02（近乎熄灭）、顶档恒为 0.395；
    2. 等距铺满 —— 不允许在低档堆亮度、高档挤成一片。
    """
    def glow(level: int, lvmax: int) -> float:
        ratio = max(0, min(lvmax, level)) / lvmax
        return 0.02 + ratio * 0.375

    def halo(level: int, lvmax: int) -> float:
        ratio = max(0, min(lvmax, level)) / lvmax
        return 2.05 + ratio * 0.65

    for lvmax in (5, 10):
        assert glow(0, lvmax) == pytest.approx(0.02)
        assert glow(lvmax, lvmax) == pytest.approx(0.395)
        assert glow(lvmax, lvmax) / glow(0, lvmax) >= 10, "顶档与遗忘层要差一个数量级"
        gaps = [glow(lv + 1, lvmax) - glow(lv, lvmax) for lv in range(lvmax)]
        assert min(gaps) == pytest.approx(0.375 / lvmax), "等距铺满，不厚此薄彼"

        # 辉光半径同样归一：L0 是 2.05r，顶档是 2.70r
        assert halo(0, lvmax) == pytest.approx(2.05)
        assert halo(lvmax, lvmax) == pytest.approx(2.70)
        assert halo(lvmax, lvmax) / halo(0, lvmax) > 1.3, "顶档的灯要明显更大"
