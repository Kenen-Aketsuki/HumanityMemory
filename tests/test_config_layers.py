"""四层配置：出厂 < 安装 < 用户 < 项目（2026-11-17 加「安装」层）。

定位差异是有意的：

* **出厂**（包内 `memory.default.toml`）—— 全量，随版本更新，用户不该改它。
* **安装**（`<安装根>/memory.toml`，与 `.env` 同目录）—— 全量模板，由
  `config template` 整份生成。放在这就是为了「一眼看到所有可调项」，
  不用翻进包目录；也不受重装 / 回滚影响（跨版本共享）。
* **用户**（`~/.humanity-memory/memory.toml`）—— 增量，跨项目。
* **项目**（`<项目>/.ai_memory/memory.toml`）—— 增量，优先级最高。

合并走 `deep_merge` 逐键递归，所以后三层只写要改的键，缺的从更底层补 ——
这也是「安装层是全量模板、而出厂升级又加了新键」不会报缺项的原因。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from humanity_memory import Config
from humanity_memory import config as cfgmod


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把「安装位置」和「用户目录」都指到临时目录，别碰真实配置。"""
    install_root = tmp_path / "install"
    install_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj" / ".ai_memory"
    project.mkdir(parents=True)

    monkeypatch.setattr(cfgmod, "env_file", lambda: install_root / ".env")
    return install_root, home, project


def _load(roots, **env):
    install_root, home, project = roots
    return Config.load(
        {"HM_HOME": str(home), "HM_EMBED_DIM": "1024", **env},
        workspace=project,
    )


# ---------------------------------------------------------------- 层定位


def test_install_config_sits_next_to_env(roots) -> None:
    install_root, _, _ = roots
    assert cfgmod.install_config_file() == install_root / "memory.toml"


def test_layer_order_is_install_between_factory_and_user(roots) -> None:
    install_root, home, project = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")
    (home / "memory.toml").write_text("[dedup]\n", encoding="utf-8")
    (project / "memory.toml").write_text("[dedup]\n", encoding="utf-8")

    cfg = _load(roots)

    assert [s for s, _ in cfg.layers] == ["出厂", "安装", "用户", "项目"]


def test_missing_install_file_is_not_a_layer(roots) -> None:
    cfg = _load(roots)
    assert [s for s, _ in cfg.layers] == ["出厂"], "文件不存在就不该出现在层列表里"


# ---------------------------------------------------------------- 覆盖方向


def test_install_overrides_factory(roots) -> None:
    install_root, _, _ = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")

    cfg = _load(roots)
    assert cfg.settings["dedup"]["dup_floor"] == 0.85


def test_user_overrides_install(roots) -> None:
    install_root, home, _ = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")
    (home / "memory.toml").write_text("[dedup]\ndup_floor = 0.70\n", encoding="utf-8")

    assert _load(roots).settings["dedup"]["dup_floor"] == 0.70


def test_project_overrides_user(roots) -> None:
    install_root, home, project = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")
    (home / "memory.toml").write_text("[dedup]\ndup_floor = 0.70\n", encoding="utf-8")
    (project / "memory.toml").write_text("[dedup]\ndup_floor = 0.55\n", encoding="utf-8")

    assert _load(roots).settings["dedup"]["dup_floor"] == 0.55


def test_install_layer_can_be_partial_too(roots) -> None:
    """「安装层是全量模板」是约定，不是代码约束：它只写一个键也照样能用。

    这正是「出厂升级加了新键、安装那份还是旧的」不会炸的原因 ——
    缺的键从出厂补。
    """
    install_root, _, _ = roots
    (install_root / "memory.toml").write_text("[retrieval]\nw_level = 0.30\n", encoding="utf-8")

    cfg = _load(roots)
    assert cfg.w_level == 0.30
    assert cfg.settings["dedup"]["dup_floor"] == 0.90, "没写的键必须继承出厂"
    assert cfg.max_level == 10


def test_unknown_key_in_install_layer_still_fails_loudly(roots) -> None:
    """缺键能补，但**写错键名**不补 —— 静默忽略错别字才是灾难。"""
    install_root, _, _ = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_flor = 0.85\n", encoding="utf-8")

    cfg = _load(roots)
    # 打错的键不会变成 dup_floor；它只是躺在合并结果里没人读
    assert cfg.settings["dedup"]["dup_floor"] == 0.90
    assert "dup_flor" in cfg.settings["dedup"]


# ---------------------------------------------------------------- 模板生成


def test_config_template_writes_full_default(roots, monkeypatch) -> None:
    install_root, _, _ = roots
    from humanity_memory import cli

    target = install_root / "memory.toml"
    code = cli.cmd_config(_args(force=False))

    assert code == 0
    text = target.read_text(encoding="utf-8")
    assert "[forget" in text and "dup_floor" in text, "模板必须是全量，不是只有一段"
    assert len(text.splitlines()) > 100, "全量模板该有百来行"
    # 生成后立刻成为生效层
    assert cfgmod.install_config_file().is_file()


def test_config_template_refuses_to_clobber(roots) -> None:
    install_root, _, _ = roots
    from humanity_memory import cli

    target = install_root / "memory.toml"
    target.write_text("# 我改过的东西\n[dedup]\ndup_floor = 0.6\n", encoding="utf-8")

    code = cli.cmd_config(_args(force=False))

    assert code == 2, "已存在就该拒绝，而不是默默覆盖"
    assert "0.6" in target.read_text(encoding="utf-8")


def test_config_template_force_overwrites(roots) -> None:
    install_root, _, _ = roots
    from humanity_memory import cli

    target = install_root / "memory.toml"
    target.write_text("# 旧内容\n", encoding="utf-8")

    assert cli.cmd_config(_args(force=True)) == 0
    assert "旧内容" not in target.read_text(encoding="utf-8")


def _args(*, force: bool):
    import argparse

    return argparse.Namespace(force=force, action="template")
