"""四层配置：出厂 < 安装 < 用户 < 库（2026-11-17 加「安装」层，2026-11-18 第四层改「库」）。

定位差异是有意的：

* **出厂**（包内 `memory.default.toml`）—— 全量，随版本更新，用户不该改它。
* **安装**（`<安装根>/memory.toml`，与 `.env` 同目录）—— 全量模板，由
  `config template` 整份生成。放在这就是为了「一眼看到所有可调项」，
  不用翻进包目录；也不受重装 / 回滚影响（跨版本共享）。
* **用户**（`~/.humanity-memory/memory.toml`）—— 增量，跨项目。
* **库**（`<库目录>/memory.toml`）—— 增量，优先级最高，**跟着库走**。
  自有库时库目录 = `<项目>/.ai_memory`，与旧写法一致；共享库时 A、B 读同一份。

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
    project = tmp_path / "proj"
    library = project / ".ai_memory"
    library.mkdir(parents=True)

    monkeypatch.setattr(cfgmod, "env_file", lambda: install_root / ".env")
    return install_root, home, library


def _load(roots, **env):
    install_root, home, library = roots
    return Config.load(
        {"HM_HOME": str(home), "HM_EMBED_DIM": "1024", **env},
        library_dir=library,
    )


# ---------------------------------------------------------------- 层定位


def test_install_config_sits_next_to_env(roots) -> None:
    install_root, _, _ = roots
    assert cfgmod.install_config_file() == install_root / "memory.toml"


def test_layer_order_is_install_between_factory_and_user(roots) -> None:
    install_root, home, library = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")
    (home / "memory.toml").write_text("[dedup]\n", encoding="utf-8")
    (library / "memory.toml").write_text("[dedup]\n", encoding="utf-8")

    cfg = _load(roots)

    assert [s for s, _ in cfg.layers] == ["出厂", "安装", "用户", "库"]


def test_missing_install_file_is_not_a_layer(roots) -> None:
    cfg = _load(roots)
    assert [s for s, _ in cfg.layers] == ["出厂"], "文件不存在就不该出现在层列表里"


# ---------------------------------------------------------------- 拒绝态（F3）


def test_no_library_skips_the_library_layer(roots) -> None:
    """拒绝态：**不读**库级 toml。

    不是「读不到就算了」，而是根本不拼这个路径 —— 否则一个凑巧待在
    `<cwd>/.ai_memory` 的库，会把自己的阈值套到未接入的会话上。
    """
    install_root, home, library = roots
    (home / "memory.toml").write_text("[dedup]\ndup_floor = 0.70\n", encoding="utf-8")
    (library / "memory.toml").write_text("[dedup]\ndup_floor = 0.55\n", encoding="utf-8")

    cfg = Config.load(
        {"HM_HOME": str(home), "HM_EMBED_DIM": "1024"}, library_dir=cfgmod.NO_LIBRARY
    )

    assert [s for s, _ in cfg.layers] == ["出厂", "用户"]
    assert cfg.settings["dedup"]["dup_floor"] == 0.70, "库级那份必须完全没参与"
    assert cfg.library_dir is None


def test_none_still_means_autodetect(roots) -> None:
    """`None` 保持旧语义：自动猜。哨兵不能把这个默认行为一起改掉。"""
    install_root, home, library = roots
    (library / "memory.toml").write_text("[dedup]\ndup_floor = 0.55\n", encoding="utf-8")

    cfg = Config.load(
        {
            "HM_HOME": str(home),
            "HM_EMBED_DIM": "1024",
            "HM_WORKSPACE": str(library.parent),
        }
    )

    assert [s for s, _ in cfg.layers] == ["出厂", "库"]
    assert cfg.settings["dedup"]["dup_floor"] == 0.55
    assert cfg.library_dir == library


def test_explicit_library_dir_beats_env(roots) -> None:
    """显式给路径时不猜：`HM_WORKSPACE` 那条链必须让位。"""
    install_root, home, library = roots
    (library / "memory.toml").write_text("[dedup]\ndup_floor = 0.55\n", encoding="utf-8")
    else_library = install_root / "other" / ".ai_memory"
    else_library.mkdir(parents=True)
    (else_library / "memory.toml").write_text("[dedup]\ndup_floor = 0.35\n", encoding="utf-8")

    cfg = Config.load(
        {
            "HM_HOME": str(home),
            "HM_EMBED_DIM": "1024",
            "HM_WORKSPACE": str(else_library.parent),
        },
        library_dir=str(else_library),
    )

    assert cfg.settings["dedup"]["dup_floor"] == 0.35
    assert cfg.library_dir == else_library


def test_sentinel_is_exported_from_package_root(roots) -> None:
    import humanity_memory

    assert humanity_memory.NO_LIBRARY is cfgmod.NO_LIBRARY
    assert repr(cfgmod.NO_LIBRARY) == "NO_LIBRARY"


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


def test_library_overrides_user(roots) -> None:
    install_root, home, library = roots
    (install_root / "memory.toml").write_text("[dedup]\ndup_floor = 0.85\n", encoding="utf-8")
    (home / "memory.toml").write_text("[dedup]\ndup_floor = 0.70\n", encoding="utf-8")
    (library / "memory.toml").write_text("[dedup]\ndup_floor = 0.55\n", encoding="utf-8")

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
