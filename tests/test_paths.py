"""解析链（A1/A2）：令牌决定身份，cwd 只决定"从哪儿开始找"。

这一层的三个硬约束，每条都对应一次真实的踩坑：

1. **未接入不产生任何目录** —— 老版本会在 cwd 顺手建 `.ai_memory`，
   于是 `~/.humanity-memory/.ai_memory`、`AppData/Local/.ai_memory`
   这类空壳库遍地都是，还会被向上查找撞见。
2. **只认带 `token.json` 的 `.ai_memory`** —— 否则父项目的库会被子目录
   "借"去用，写入落到别人家里。
3. **共享库的 `library_dir` 不等于 `workspace_root`** —— 它甚至不在项目里，
   所以必须由令牌给，不能从路径反推。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from humanity_memory import paths


def _project(root: Path, *, token: bool = True, db: bool = True) -> Path:
    ws = root / ".ai_memory"
    ws.mkdir(parents=True)
    if db:
        (ws / "workspace.db").write_bytes(b"")
    if token:
        (ws / paths.TOKEN_FILE).write_text("{}", encoding="utf-8")
    return ws


# ---------------------------------------------------------------- 不建目录


def test_missing_workspace_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "lonely"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.delenv("HM_WORKSPACE", raising=False)

    layout = paths.resolve_layout(home=tmp_path / "home")

    assert layout.library_dir is None
    assert not (empty / ".ai_memory").exists(), "未接入就是零痕迹"


def test_explicit_path_without_workspace_is_also_none(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert paths.resolve_layout(root, home=tmp_path / "h").library_dir is None
    assert not (root / ".ai_memory").exists()


# ---------------------------------------------------------------- 只认令牌


def test_tokenless_workspace_is_not_a_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有库没令牌 = 未接入。绝不能"撞见就用"。"""
    _project(tmp_path / "legacy", token=False)
    monkeypatch.chdir(tmp_path / "legacy")
    monkeypatch.delenv("HM_WORKSPACE", raising=False)

    assert paths.find_workspace_dir() is None
    # 但 bind_status 要能看见它，好建议 adopt
    found = paths.find_workspace_dir(include_tokenless=True)
    assert found is not None and found.parent.name == "legacy"


def test_nested_tokenless_dir_does_not_shadow_the_real_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子目录里有个空壳 `.ai_memory`，父项目有真库 —— 必须取父项目的。"""
    real = _project(tmp_path / "proj")
    sub = tmp_path / "proj" / "Assets"
    (sub / ".ai_memory").mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("HM_WORKSPACE", raising=False)

    assert paths.find_workspace_dir() == real


def test_hm_workspace_short_circuits_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _project(tmp_path / "proj", token=False)
    monkeypatch.setenv("HM_WORKSPACE", str(tmp_path / "proj"))
    # 显式指定是运维出口，不要求令牌
    assert paths.find_workspace_dir() == ws


# ---------------------------------------------------------------- 两级布局


def test_self_owned_library_root_is_the_project(tmp_path: Path) -> None:
    ws = _project(tmp_path / "alpha")
    layout = paths.resolve_layout(tmp_path / "alpha", home=tmp_path / "h")

    assert layout.workspace_root == tmp_path / "alpha"
    assert layout.library_dir == ws
    assert layout.workspace_dir == ws, "workspace_dir 是 library_dir 的别名"
    assert layout.workspace_db == ws / "workspace.db"


def test_shared_library_keeps_root_from_outside(tmp_path: Path) -> None:
    """共享库在 `libraries/<label>` 下，项目根只能由调用方（令牌）给。"""
    lib = tmp_path / "home" / "libraries" / "island"
    lib.mkdir(parents=True)
    root = tmp_path / "alpha"
    root.mkdir()

    layout = paths.resolve_layout(
        home=tmp_path / "h", library_dir=lib, workspace_root=root
    )
    assert layout.library_dir == lib
    assert layout.workspace_root == root, "库不在项目里，root 只能显式给"
    assert layout.workspace_db == lib / "workspace.db"


def test_rules_stay_with_the_project_when_library_is_shared(tmp_path: Path) -> None:
    """信条跟着项目走（§5.8）：共享库里不该出现第二个信条文件被悄悄写。"""
    lib = tmp_path / "home" / "libraries" / "island"
    lib.mkdir(parents=True)
    root = tmp_path / "alpha"
    conventional = root / paths.CONVENTIONAL_RULES
    conventional.parent.mkdir(parents=True)
    conventional.write_text("# 信条", encoding="utf-8")

    layout = paths.resolve_layout(home=tmp_path / "h", library_dir=lib, workspace_root=root)
    assert layout.workspace_rules == conventional


# ---------------------------------------------------------------- 已删除的机制


def test_last_workspace_mechanism_is_gone() -> None:
    """A3：进程间不再共享「上次工作区」。留着 API 就等于留着那条老路。"""
    for name in ("read_last_workspace", "write_last_workspace", "LAST_WORKSPACE_FILE"):
        assert not hasattr(paths, name), name


def test_auto_create_helpers_are_gone() -> None:
    assert not hasattr(paths, "find_project_root"), "自动建库的入口必须删掉"
