"""星图的遗忘等级轴由配置生成（2026-11-17 改）。

原来星图里藏着两个「一共 5 档」的常数：

* 后端 `level_max = max(max(leveled), 5)` —— 老库最高才 4 级时，为了图例好看
  硬补到 5；
* 前端 `LVMAX=...||5`，而且图例用 `for(let l=LVMAX;l>=0;l--)` 遍历 ——
  这同时假设了「等级是 0~N 连续整数」。

等级表本来就是配置里的 `[[forget.levels]]`（现在 0~10 共 11 档，加一档只要
新增一段），所以档数、每档周期都该从配置读。本文件守四件事：

1. 配置里有几档就画几档，非连续档位（0/2/5）照样画对；
2. 配置读不到时退回「库里实际出现过的档位」，不凭空补档；
3. 空库给单档占位，且不会因为分母是 0 算出 NaN；
4. 日志的中性档取表中正间，不再写死 3。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import starmap


# ---------------------------------------------------------------- 测试用配置

# 4 档小表：故意和出厂的 11 档不一样，用来证明「档数真的从配置来」。
# 校验器要求 required_compression 长度 = 档数、bands 落在 [2, 顶档-1]，
# 所以这两段必须一起改，不能只覆盖 [[forget.levels]]。
CONFIG_4_LEVELS = """
[compress]
required_compression = [3, 2, 1, 0]

[initial]
[[initial.bands]]
min_importance = 0.0
level = 2

[[forget.levels]]
level = 3
period_experience_days = 5
period_episode_days = 5
down_threshold = 5.0
compress = "full"

[[forget.levels]]
level = 2
period_experience_days = 3
period_episode_days = 3
up_threshold = 7.0
down_threshold = 3.0
compress = "moderate"

[[forget.levels]]
level = 1
period_experience_days = 1
period_episode_days = 1
up_threshold = 4.0
down_threshold = 1.0
compress = "sentence"

[[forget.levels]]
level = 0
compress = "title"
"""

# 非连续档位：0 / 2 / 5，中间跳档
CONFIG_GAPPED = """
[forget]
# 表里没有 1 级，复活目标也得跟着改 —— 校验器会查「复活目标是不是存在的等级」
revive_to_level = 2

[compress]
required_compression = [3, 2, 1]

[initial]
[[initial.bands]]
min_importance = 0.0
level = 2

[[forget.levels]]
level = 5
period_experience_days = 30
period_episode_days = 30
down_threshold = 4.0
compress = "full"

[[forget.levels]]
level = 2
period_experience_days = 3
period_episode_days = 3
up_threshold = 5.0
down_threshold = 0.5
compress = "moderate"

[[forget.levels]]
level = 0
compress = "title"
"""


def _project(tmp_path: Path, toml: str | None) -> tuple[Path, dict[str, str]]:
    """造一个项目：<proj>/.ai_memory/{workspace.db, memory.toml}。"""
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "proj" / ".ai_memory"
    ws.mkdir(parents=True)
    if toml is not None:
        (ws / "memory.toml").write_text(toml, encoding="utf-8")
    return ws / "workspace.db", {"HM_HOME": str(home), "HM_EMBED_DIM": "8"}


def _nodes(*levels: int | None) -> list[dict]:
    """level=None 表示「没有等级列」（日志那种）。"""
    out = []
    for i, lv in enumerate(levels):
        node = {"kind": "experience" if lv is not None else "log", "id": f"n{i}"}
        if lv is not None:
            node["forget_level"] = lv
            node["has_level"] = True
        else:
            node["forget_level"] = 3
            node["has_level"] = False
        out.append(node)
    return out


# ---------------------------------------------------------------- 配置优先


def test_level_count_comes_from_config(tmp_path: Path) -> None:
    """出厂是 11 档，项目把它压到 4 档 → 星图就只画 4 档。"""
    db, env = _project(tmp_path, CONFIG_4_LEVELS)

    table, source, neutral = starmap.resolve_levels(db, _nodes(2, 1), env)

    assert source == "config"
    assert [item["level"] for item in table] == [3, 2, 1, 0], "档位来自配置且降序"
    assert table[0]["days"] == 5, "每档周期也带出来（图例上标几天）"
    assert neutral == 1, "4 档的正中间是第 3 项（index 2 → level 1）"


def test_default_config_is_eleven_levels(tmp_path: Path) -> None:
    """不写项目配置时用出厂表：0~10 共 11 档，顶档 10。"""
    db, env = _project(tmp_path, None)

    table, source, _ = starmap.resolve_levels(db, _nodes(5), env)

    assert source == "config"
    assert len(table) == 11
    assert table[0]["level"] == 10
    assert table[-1]["level"] == 0


def test_gapped_levels_are_not_treated_as_contiguous(tmp_path: Path) -> None:
    """0 / 2 / 5 这种跳档的表，前端不能按 0..N 循环画（否则会造出 L1/L3/L4）。"""
    db, env = _project(tmp_path, CONFIG_GAPPED)

    table, source, neutral = starmap.resolve_levels(db, _nodes(2), env)

    assert source == "config"
    assert [item["level"] for item in table] == [5, 2, 0]
    assert neutral == 2


# ---------------------------------------------------------------- 退路


def test_falls_back_to_db_levels_without_config(tmp_path: Path) -> None:
    """配置读不到：只画库里真实存在的档，不补 5。"""
    db, env = _project(tmp_path, None)
    (db.parent / "memory.toml").write_text("这不是 TOML [[[", encoding="utf-8")

    table, source, _ = starmap.resolve_levels(db, _nodes(4, 2, 2), env)

    assert source == "db"
    assert [item["level"] for item in table] == [4, 2, 0], "缺档就缺档，不凭空补到 5"


def test_empty_db_gets_single_level_placeholder(tmp_path: Path) -> None:
    """空库没有可画的等级：给单档占位，别让图例整块空白。"""
    db, env = _project(tmp_path, None)
    (db.parent / "memory.toml").write_text("坏配置 [[[", encoding="utf-8")

    table, source, neutral = starmap.resolve_levels(db, [], env)

    assert source == "default"
    assert table == [{"level": 0}]
    assert neutral == 0


# ---------------------------------------------------------------- 日志中性档


def test_log_neutral_level_is_table_middle(tmp_path: Path) -> None:
    """日志没有等级列：占位档取表中正间，不写死 3。"""
    db, env = _project(tmp_path, CONFIG_4_LEVELS)
    nodes = _nodes(3, None)

    table, _, neutral = starmap.resolve_levels(db, nodes, env)

    assert neutral == 1, "4 档表的中性档是 1，不是 3"
    assert nodes[1]["has_level"] is False, "占位改值但 has_level 必须仍是 False"


# ---------------------------------------------------------------- 前端断言


def test_frontend_builds_legend_from_level_table() -> None:
    """图例必须遍历配置带来的表，不能 `for(let l=LVMAX;...)` 自己数。"""
    src = Path(starmap.__file__).read_text(encoding="utf-8")

    assert "const LEVELS=(DATA.meta&&DATA.meta.levels" in src
    assert "LEVELS.forEach(item=>" in src, "图例按等级表遍历"
    assert "for(let l=LVMAX;l>=0;l--)" not in src, "不能假设 0~N 连续"
    assert "' · '+item.days+' 天'" in src, "周期来自配置，写进图例"
    assert "（配置外）" in src, "配置外的残留等级也要列出来，不能静默丢掉"
    assert "const LVDEN=Math.max(1,LVMAX);" in src, "顶档为 0 时不能除零"
    assert "(0.02+(lv/LVDEN)*0.375)" in src, "透明度用同一个分母"


def test_python_side_has_no_hardcoded_level_count() -> None:
    """后端不许再出现「至少画到 5 档」这种常数。"""
    src = Path(starmap.__file__).read_text(encoding="utf-8")

    assert "max(max(leveled), 5)" not in src
    assert "level_max = 5" not in src
    assert "||5" not in src
