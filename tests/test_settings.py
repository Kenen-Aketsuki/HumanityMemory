"""统一配置：三级深合并与启动校验（§6.3 / §6.6）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import config as cfgmod
from humanity_memory.config import (
    Config,
    ConfigError,
    deep_merge,
    validate_settings,
)


@pytest.fixture(autouse=True)
def _hide_install_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件只测「出厂 / 用户 / 库」三层。

    仓库根真实存在的 `<安装根>/memory.toml` 会多出一层，让层数断言随机器状态漂移 ——
    安装层单独由 `test_config_layers.py` 负责，这里把它挡掉才能保证测试可重复。
    """
    monkeypatch.setattr(cfgmod, "env_file", lambda: tmp_path / "no-install" / ".env")


def _write_levels(path: Path, levels: list[dict], **forget_overrides) -> None:
    """把一份等级表写成库级 memory.toml（数组表是整体替换，必须写全）。"""
    forget = {
        "level0_purge_days": 180,
        "archived_purge_days": 180,
        "revive_hits": 2,
        "revive_to_level": 1,
        **forget_overrides,
    }
    lines = ["[forget]"]
    lines += [f"{k} = {v}" for k, v in forget.items()]
    lines.append("")
    for item in levels:
        lines.append("[[forget.levels]]")
        for key, value in item.items():
            lines.append(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {value}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _default_levels() -> list[dict]:
    return [
        {"level": 5, "period_knowledge_days": 365, "period_episode_days": 180,
         "down_threshold": 20.0, "compress": "full"},
        {"level": 4, "period_knowledge_days": 180, "period_episode_days": 90,
         "up_threshold": 25.0, "down_threshold": 12.0, "compress": "full"},
        {"level": 3, "period_knowledge_days": 90, "period_episode_days": 30,
         "up_threshold": 15.0, "down_threshold": 6.0, "compress": "full"},
        {"level": 2, "period_knowledge_days": 30, "period_episode_days": 14,
         "up_threshold": 8.0, "down_threshold": 3.0, "compress": "moderate"},
        {"level": 1, "period_knowledge_days": 7, "period_episode_days": 7,
         "up_threshold": 4.0, "down_threshold": 1.0, "compress": "sentence"},
        {"level": 0, "compress": "title"},
    ]


# ---------------------------------------------------------------- 深合并


def test_deep_merge_keeps_sibling_keys() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": {"z": 3}}
    over = {"a": {"y": 20}}
    merged = deep_merge(base, over)
    assert merged == {"a": {"x": 1, "y": 20}, "b": {"z": 3}}
    # 不改原对象：配置层是共享的，就地改会污染下一轮加载
    assert base["a"]["y"] == 2


def test_deep_merge_replaces_arrays_wholesale() -> None:
    """数组表必须整体替换：按元素拼会得到自相矛盾的等级表。"""
    base = {"levels": [5, 4, 3]}
    assert deep_merge(base, {"levels": [2, 1]})["levels"] == [2, 1]


# ---------------------------------------------------------------- 出厂层


def test_default_toml_loads_and_validates(tmp_path: Path) -> None:
    cfg = Config.load({"HM_HOME": str(tmp_path / "home"), "HM_DB_PATH": str(tmp_path / "m.db")})
    assert [lv["level"] for lv in cfg.forget_levels] == list(range(10, -1, -1))
    assert cfg.max_level == 10
    assert [scope for scope, _ in cfg.layers] == ["出厂"]
    assert validate_settings(cfg.settings) == []


def test_defaults_come_from_toml_not_code(tmp_path: Path) -> None:
    """抽查几个值：它们必须能在出厂 TOML 里找到，而不是代码里的字面量。"""
    cfg = Config.load({"HM_HOME": str(tmp_path / "home"), "HM_EMBED_DIM": "8"})
    assert cfg.embed_dim == 8  # 环境变量最高优先级
    assert cfg.settings["compress"]["required_compression"] == [3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert cfg.settings["dedup"]["dup_floor"] == 0.90
    assert cfg.default_importance["log"] == 0.30
    assert cfg.w_level == cfg.settings["retrieval"]["w_level"]


# ---------------------------------------------------------------- 三层覆盖


def test_library_overrides_single_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = tmp_path / "proj" / ".ai_memory"
    (ws / "memory.toml").parent.mkdir(parents=True)
    ws.joinpath("memory.toml").write_text("[dedup]\ndup_floor = 0.75\n", encoding="utf-8")

    cfg = Config.load({"HM_HOME": str(home)}, library_dir=ws)
    assert [scope for scope, _ in cfg.layers] == ["出厂", "库"]
    assert cfg.settings["dedup"]["dup_floor"] == 0.75
    # 同段其它键、别的段，全都继承出厂
    assert cfg.settings["dedup"]["dup_bonus"] == 0.30
    assert cfg.settings["activity"]["sim_floor"] == 0.35


def test_user_layer_sits_between_default_and_library(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "memory.toml").write_text("[dedup]\ndup_floor = 0.80\n", encoding="utf-8")
    ws = tmp_path / "proj" / ".ai_memory"
    ws.mkdir(parents=True)
    (ws / "memory.toml").write_text("[dedup]\ndup_floor = 0.70\n", encoding="utf-8")

    cfg = Config.load({"HM_HOME": str(home)}, library_dir=ws)
    assert [scope for scope, _ in cfg.layers] == ["出厂", "用户", "库"]
    assert cfg.settings["dedup"]["dup_floor"] == 0.70  # 库赢


# ---------------------------------------------------------------- 校验拒绝启动


def _expect_error(tmp_path: Path, mutate, needle: str) -> None:
    levels = _default_levels()
    mutate(levels)
    ws = tmp_path / "proj" / ".ai_memory"
    _write_levels(ws / "memory.toml", levels)
    with pytest.raises(ConfigError) as exc:
        Config.load({"HM_HOME": str(tmp_path / "home")}, library_dir=ws)
    assert needle in str(exc.value)


def test_missing_up_threshold_names_the_level(tmp_path: Path) -> None:
    def mutate(levels: list[dict]) -> None:
        for item in levels:
            if item["level"] == 3:
                item.pop("up_threshold")

    _expect_error(tmp_path, mutate, "level=3 缺少 up_threshold")


def test_hysteresis_inversion_is_rejected(tmp_path: Path) -> None:
    """level=3 的升档线压到 level=4 的降档线之下 → 活跃度抖一下就横跳。"""

    def mutate(levels: list[dict]) -> None:
        for item in levels:
            if item["level"] == 3:
                item["up_threshold"] = 10.0  # < level4.down_threshold = 12.0

    _expect_error(tmp_path, mutate, "滞回倒挂")


def test_compression_array_length_must_match_levels(tmp_path: Path) -> None:
    levels = _default_levels()
    ws = tmp_path / "proj" / ".ai_memory"
    _write_levels(ws / "memory.toml", levels)
    (ws / "memory.toml").write_text(
        (ws / "memory.toml").read_text(encoding="utf-8")
        + "\n[compress]\nrequired_compression = [3, 2, 1]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        Config.load({"HM_HOME": str(tmp_path / "home")}, library_dir=ws)
    assert "required_compression" in str(exc.value)


def test_revive_target_must_exist(tmp_path: Path) -> None:
    levels = _default_levels()
    ws = tmp_path / "proj" / ".ai_memory"
    _write_levels(ws / "memory.toml", levels, revive_to_level=9)
    with pytest.raises(ConfigError) as exc:
        Config.load({"HM_HOME": str(tmp_path / "home")}, library_dir=ws)
    assert "revive_to_level" in str(exc.value)


def test_confidence_band_must_be_ordered(tmp_path: Path) -> None:
    ws = tmp_path / "proj" / ".ai_memory"
    _write_levels(ws / "memory.toml", _default_levels())
    (ws / "memory.toml").write_text(
        (ws / "memory.toml").read_text(encoding="utf-8")
        + "\n[confidence]\nverify_on_read = 0.9\ntreat_as_fact = 0.6\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        Config.load({"HM_HOME": str(tmp_path / "home")}, library_dir=ws)
    assert "verify_on_read" in str(exc.value)


def test_broken_toml_reports_the_file(tmp_path: Path) -> None:
    ws = tmp_path / "proj" / ".ai_memory"
    ws.mkdir(parents=True)
    (ws / "memory.toml").write_text("[dedup]\ndup_floor = = 0.9\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        Config.load({"HM_HOME": str(tmp_path / "home")}, library_dir=ws)
    assert "memory.toml" in str(exc.value)
