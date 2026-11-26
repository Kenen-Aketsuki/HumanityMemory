"""四态与绑定生命周期：未接入 / 已拒绝 / 自有库 / 共享库。

这些用例全部走**公开入口**（`bindings.bind_*` + `resolve_session`），不碰内部函数 ——
准入层的价值全在"从外面看是什么行为"，把内部结构测绿了没有意义。

每个用例都对应文档里的一个承诺：
- §5.2 四态，一态都不许含糊；
- §5.5 第 4/6 步：令牌被复制或手改必须拒绝；
- §6 拒绝契约：未接入时读写工具调用返回拒绝 + checklist（2026-11-24 起不再裁剪工具面）；
- A4 库级配置跟着库走，共享组里 A、B 读到同一份。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from humanity_memory import bindings, keys, paths
from humanity_memory.config import Config
from humanity_memory.mcp_server import build_server
from humanity_memory.token import Token, token_path

ENV_EMBED = {"HM_EMBED_DIM": "8"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """registry 与全局目录都指到临时 home —— 绝不碰真实用户目录。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HM_HOME", str(home))
    monkeypatch.delenv("HM_WORKSPACE", raising=False)
    return tmp_path


def _project(root: Path, *, db: bool = True) -> Path:
    ws = root / ".ai_memory"
    ws.mkdir(parents=True, exist_ok=True)
    if db:
        # 空文件就够：adopt 只要求它存在，真正建表由 open_db 负责
        (ws / "workspace.db").write_bytes(b"")
    return ws


def _cfg(home: Path, library_dir: Path | None = None) -> Config:
    environ = {"HM_HOME": str(home), **ENV_EMBED}
    return Config.load(environ, library_dir=library_dir)


def _tool_names(server) -> list[str]:
    return sorted(t.name for t in asyncio.run(server.list_tools()))


# ---------------------------------------------------------------- 四态


def test_state_no_token(env: Path) -> None:
    root = env / "plain"
    root.mkdir()

    session = bindings.resolve_session(root)

    assert not session.ok
    assert session.code == "no_token"
    assert session.checklist, "拒绝必须带清单，否则用户不知道下一步"
    assert any("bind_new" in item for item in session.checklist)


def test_state_declined(env: Path) -> None:
    root = env / "declined"
    _project(root)

    session = bindings.bind_decline(root)

    assert not session.ok
    assert session.code == "declined"
    assert "bind new" in session.reason
    # 令牌记下了这个决定
    assert Token.load(token_path(root)).library is None


def test_state_bound_self(env: Path) -> None:
    root = env / "alpha"
    _project(root)

    session = bindings.bind_adopt(root)

    assert session.ok
    assert session.record.library == "alpha"
    assert session.library_dir == root / ".ai_memory"
    assert session.access == "rw"


def test_state_bound_shared(env: Path) -> None:
    a = env / "a" / "alpha"
    b = env / "b" / "beta"
    _project(a)
    _project(b, db=False)
    bindings.bind_adopt(a)

    session = bindings.bind_existing(b, "alpha")

    assert session.ok
    assert session.record.library == "alpha"
    assert session.library_dir == a / ".ai_memory", "B 指向 A 的库"


# ---------------------------------------------------------------- 拒绝的几种成因


def test_copied_token_is_rejected(env: Path) -> None:
    a = env / "alpha"
    b = env / "beta"
    _project(a)
    _project(b, db=False)
    bindings.bind_adopt(a)
    # 拷贝工程：令牌跟着 `.ai_memory` 一起被复制
    (b / ".ai_memory" / "token.json").write_text(
        token_path(a).read_text(encoding="utf-8"), encoding="utf-8"
    )

    session = bindings.resolve_session(b)

    assert session.code == "token_workspace_mismatch"
    assert "bind move" in session.reason or "不属于此项目" in session.reason


def test_revoked_key_is_rejected(env: Path) -> None:
    root = env / "alpha"
    _project(root)
    bindings.bind_adopt(root)
    key = Token.load(token_path(root)).key

    with keys.edit() as registry:
        registry.revoke(key)

    session = bindings.resolve_session(root)
    assert session.code == "key_revoked"


def test_hand_edited_library_is_rejected(env: Path) -> None:
    root = env / "alpha"
    _project(root)
    bindings.bind_adopt(root)
    payload = json.loads(token_path(root).read_text(encoding="utf-8"))
    payload["library"] = "someone_else"
    token_path(root).write_text(json.dumps(payload), encoding="utf-8")

    session = bindings.resolve_session(root)
    assert session.code == "key_library_mismatch", "改一个字段就能换库，等于白做"


def test_missing_library_dir_is_rejected(env: Path) -> None:
    """共享库所在盘没挂载：令牌还在，库不在了 —— 报 library_missing 而不是 no_token。

    自有库做不到这个区分（库目录就是 `.ai_memory`，删了令牌一起没），
    所以这里必须用共享库来造现场。
    """
    host = env / "host"
    guest = env / "guest"
    ws = _project(host)
    _project(guest, db=False)
    bindings.bind_adopt(host)
    bindings.bind_existing(guest, "host")

    # 模拟移动硬盘拔掉：库目录改名，令牌（在 guest 自己的 .ai_memory 里）还在
    ws.rename(env / "host" / ".ai_memory.unmounted")

    session = bindings.resolve_session(guest)
    assert session.code == "library_missing"
    assert "挂载" in session.reason


# ---------------------------------------------------------------- 共享 / 隔离


def test_shared_library_is_physically_shared(env: Path, tmp_path: Path) -> None:
    a = env / "a"
    b = env / "b"
    _project(a)
    _project(b, db=False)
    bindings.bind_adopt(a)
    bindings.bind_existing(b, "a")

    cfg = _cfg(env / "home", a / ".ai_memory")
    from humanity_memory.store import MemoryStore

    with MemoryStore(cfg, paths.resolve_layout(home=cfg.home, library_dir=a / ".ai_memory", workspace_root=a)) as store:
        store.write_memory("共享组的事实")

    cfg_b = _cfg(env / "home", a / ".ai_memory")
    with MemoryStore(
        cfg_b, paths.resolve_layout(home=cfg_b.home, library_dir=a / ".ai_memory", workspace_root=b)
    ) as store_b:
        hits = store_b.search("共享组")
    assert hits, "A 写的东西 B 必须看得见"


def test_independent_libraries_stay_isolated(env: Path) -> None:
    a = env / "a"
    c = env / "c"
    _project(a)
    _project(c)
    bindings.bind_adopt(a)
    bindings.bind_adopt(c)

    from humanity_memory.store import MemoryStore

    cfg = _cfg(env / "home", a / ".ai_memory")
    with MemoryStore(cfg, paths.resolve_layout(home=cfg.home, library_dir=a / ".ai_memory", workspace_root=a)) as store:
        store.write_memory("只属于 A 的秘密")

    cfg_c = _cfg(env / "home", c / ".ai_memory")
    with MemoryStore(
        cfg_c, paths.resolve_layout(home=cfg_c.home, library_dir=c / ".ai_memory", workspace_root=c)
    ) as store_c:
        assert store_c.search("秘密") == [], "C 不该看见 A 的记忆"


def test_decline_does_not_affect_other_workspaces(env: Path) -> None:
    a = env / "a"
    b = env / "b"
    _project(a)
    _project(b)
    bindings.bind_adopt(a)
    bindings.bind_decline(b)

    assert bindings.resolve_session(a).ok
    assert bindings.resolve_session(b).code == "declined"


# ---------------------------------------------------------------- A4 库级配置


def test_library_level_config_follows_the_library(env: Path) -> None:
    a = env / "a"
    b = env / "b"
    ws = _project(a)
    _project(b, db=False)
    bindings.bind_adopt(a)
    bindings.bind_existing(b, "a")
    (ws / "memory.toml").write_text("[dedup]\ndup_floor = 0.42\n", encoding="utf-8")

    cfg_a = _cfg(env / "home", ws)
    cfg_b = _cfg(env / "home", ws)

    assert cfg_a.settings["dedup"]["dup_floor"] == 0.42
    assert cfg_b.settings["dedup"]["dup_floor"] == 0.42, "共享组读同一份库级配置"
    assert [name for name, _ in cfg_a.layers][-1] == "库"


# -------------------------------------- 拒绝态工具面（2026-11-24 改版：不裁剪，改拒绝应答）


def _call(server, name: str, arguments: dict | None = None) -> dict:
    """走 MCPServer.call_tool 调工具，把 dict 结果从 content 里拆出来。"""
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if isinstance(result, dict):
        return result
    return json.loads(result.content[0].text)


def test_refusal_registers_all_tools_and_refuses_reads(env: Path) -> None:
    """工具面不再随接入状态变化：全量注册；拒绝态下读写调用返回 code + checklist。"""
    root = env / "plain"
    root.mkdir()
    session = bindings.resolve_session(root)

    server = build_server(
        _cfg(env / "home"), layout=paths.empty_layout(root=root), session=session
    )

    names = _tool_names(server)
    assert len(names) == 29, "未接入时工具也应全量可见，不再裁剪"
    assert "memory_search" in names
    assert "memory_bind_new" in names

    payload = _call(server, "memory_stats")
    assert payload["ok"] is False
    assert payload["code"] == "no_token"
    assert payload["checklist"], "拒绝必须带清单，否则用户不知道下一步"


def test_declined_state_refuses_reads_with_declined_code(env: Path) -> None:
    root = env / "declined_ws"
    _project(root)
    bindings.bind_decline(root)
    session = bindings.resolve_session(root)
    server = build_server(
        _cfg(env / "home"), layout=paths.empty_layout(root=root), session=session
    )

    payload = _call(server, "memory_stats")
    assert payload["ok"] is False
    assert payload["code"] == "declined"


def test_rescue_tools_stay_usable_and_binding_unblocks_reads(env: Path) -> None:
    """拒绝范围只盖原被裁剪的读写工具；bind / use_workspace 自救通道不堵。"""
    root = env / "plain"
    root.mkdir()
    session = bindings.resolve_session(root)
    server = build_server(
        _cfg(env / "home"), layout=paths.empty_layout(root=root), session=session
    )

    # 自救通道在拒绝态下照常应答
    payload = _call(server, "memory_bind_status")
    assert payload["ok"] is False
    assert payload["code"] == "no_token"

    # 同一 server 实例内接进去：store 就位，读写立刻放行，无需重建或重挂载
    bound = env / "alpha"
    _project(bound)
    bindings.bind_adopt(bound)
    payload = _call(server, "memory_use_workspace", {"path": str(bound)})
    assert payload["ok"] is True, f"use_workspace 应放行：{payload}"

    payload = _call(server, "memory_stats")
    assert payload["ok"] is True, f"接入后读写应立刻放行：{payload}"


# ---------------------------------------------------------------- 搬家


def test_bind_move_updates_token_and_registry(env: Path, tmp_path: Path) -> None:
    import shutil

    old = env / "old"
    ws = _project(old)
    bindings.bind_adopt(old)

    new = env / "new"
    shutil.move(str(old), str(new))

    moved = bindings.bind_move(old, new)

    token = Token.load(token_path(new))
    assert token.workspace == new
    assert bindings.resolve_session(new).ok, "搬完必须能直接接入"
    assert moved["library"] == "old", "标签来自原目录名，搬家不改标签"
    registry = keys.read()
    assert registry.library_path("old") == new / ".ai_memory"


# ---------------------------------------------------------------- 统一存放


def test_bind_new_puts_the_library_outside_the_project(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """统一存放：项目里只留 `.ai_memory/token.json`，库落在 libraries/<label>。"""
    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()

    session = bindings.bind_new(root)

    assert session.ok
    assert session.library_dir == libs / "alpha"
    assert not (root / ".ai_memory" / "workspace.db").exists(), "项目里不该再有库"
    assert (root / ".ai_memory" / "token.json").is_file(), "身份锚点还在"


def test_relocate_moves_an_in_project_library(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = env / "legacy"
    ws = _project(root)
    (ws / "CORE_RULES.md").write_text("# 信条", encoding="utf-8")
    bindings.bind_adopt(root)
    from humanity_memory.store import MemoryStore

    cfg = _cfg(env / "home", ws)
    with MemoryStore(cfg, paths.resolve_layout(home=cfg.home, library_dir=ws, workspace_root=root)) as store:
        store.write_memory("搬家前的知识")
        store.append_log("搬家前的日志")

    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    result = bindings.relocate(root)

    assert result["relocated"] is True
    assert result["ok"] is True
    assert result["library_dir"] == str(libs / "legacy")
    assert (libs / "legacy" / "workspace.db").is_file()
    # 信条跟着项目走
    assert (root / ".workbuddy" / "memory" / "信条.md").is_file()
    # 旧文件不删，归到 _moved-<日期>/
    assert any(p.name.startswith("_moved-") for p in ws.iterdir())
    # 搬完仍可读，且内容没丢
    session = bindings.resolve_session(root)
    assert session.ok and session.library_dir == libs / "legacy"
    cfg2 = _cfg(env / "home", session.library_dir)
    with MemoryStore(cfg2, session.layout) as store2:
        assert store2.search("搬家前") != []


def test_relocate_is_idempotent(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()
    bindings.bind_new(root)

    again = bindings.relocate(root)

    assert again["relocated"] is False
    assert again["reason"] == "已在统一目录"


# ---------------------------------------------------------------- 统一存放


def test_bind_new_puts_the_library_outside_the_project(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """统一存放：项目里只留 `.ai_memory/token.json`，库落在 libraries/<label>。"""
    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()

    session = bindings.bind_new(root)

    assert session.ok
    assert session.library_dir == libs / "alpha"
    assert not (root / ".ai_memory" / "workspace.db").exists(), "项目里不该再有库"
    assert (root / ".ai_memory" / "token.json").is_file(), "身份锚点还在"


def test_relocate_moves_an_in_project_library(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = env / "legacy"
    ws = _project(root)
    (ws / "CORE_RULES.md").write_text("# 信条", encoding="utf-8")
    bindings.bind_adopt(root)
    from humanity_memory.store import MemoryStore

    cfg = _cfg(env / "home", ws)
    with MemoryStore(cfg, paths.resolve_layout(home=cfg.home, library_dir=ws, workspace_root=root)) as store:
        store.write_memory("搬家前的知识")
        store.append_log("搬家前的日志")

    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    result = bindings.relocate(root)

    assert result["relocated"] is True
    assert result["ok"] is True
    assert result["library_dir"] == str(libs / "legacy")
    assert (libs / "legacy" / "workspace.db").is_file()
    # 信条跟着项目走
    assert (root / ".workbuddy" / "memory" / "信条.md").is_file()
    # 旧文件不删，归到 _moved-<日期>/
    assert any(p.name.startswith("_moved-") for p in ws.iterdir())
    # 搬完仍可读，且内容没丢
    session = bindings.resolve_session(root)
    assert session.ok and session.library_dir == libs / "legacy"
    cfg2 = _cfg(env / "home", session.library_dir)
    with MemoryStore(cfg2, session.layout) as store2:
        assert store2.search("搬家前") != []


def test_relocate_is_idempotent(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()
    bindings.bind_new(root)

    again = bindings.relocate(root)

    assert again["relocated"] is False
    assert again["reason"] == "已在统一目录"


# ---------------------------------------------------------------- 只读工具的入口


def test_cli_layout_refuses_unbound_and_creates_nothing(
    env: Path, tmp_path: Path
) -> None:
    """`_layout` 必须走准入层：否则 star map / 报告会在空目录里造一个空库。"""
    import argparse

    from humanity_memory import cli

    lonely = tmp_path / "lonely"
    lonely.mkdir()
    args = argparse.Namespace(workspace=str(lonely), token=None, rules=None)

    with pytest.raises(SystemExit) as exc:
        cli._layout(args)

    assert exc.value.code == 2
    assert not (lonely / ".ai_memory").exists(), "拒绝态不许留下任何目录"


def test_starmap_resolves_the_library_through_the_token(env: Path) -> None:
    """拖项目根进来时，星图要能顺着令牌找到 libraries/<label>/workspace.db。"""
    from humanity_memory import starmap

    root = env / "alpha"
    _project(root)
    bindings.bind_adopt(root)

    db, notes = starmap.resolve_db(root)

    assert db.is_file()
    assert db == root / ".ai_memory" / "workspace.db", "自有库仍按原路径命中"


def test_starmap_accepts_a_relocated_library(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from humanity_memory import starmap

    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()
    session = bindings.bind_new(root)
    # 库里得先有文件：真实场景是第一次打开时建表
    from humanity_memory.store import MemoryStore

    MemoryStore(_cfg(env / "home", session.library_dir), session.layout).close()

    db, notes = starmap.resolve_db(root)

    assert db == libs / "alpha" / "workspace.db"
    assert any("按令牌" in n for n in notes), notes

# ---------------------------------------------------------------- 只读入口的四种输入


def _readonly_fixture(env: Path, monkeypatch, tmp_path: Path):
    libs = tmp_path / "libs"
    monkeypatch.setenv("HM_LIBRARIES", str(libs))
    root = env / "alpha"
    root.mkdir()
    session = bindings.bind_new(root)
    from humanity_memory.store import MemoryStore

    MemoryStore(_cfg(env / "home", session.library_dir), session.layout).close()
    return root, session


def test_readonly_accepts_library_dir(env: Path, monkeypatch, tmp_path: Path) -> None:
    """只给库目录：离线看一份拷过来的库，不该要求令牌或 registry。"""
    _, session = _readonly_fixture(env, monkeypatch, tmp_path)

    layout = bindings.resolve_readonly(session.library_dir)

    assert layout.library_dir == session.library_dir


def test_readonly_accepts_token_file(env: Path, monkeypatch, tmp_path: Path) -> None:
    """只给令牌：身份在文件里，库位置由 registry 给出。"""
    root, session = _readonly_fixture(env, monkeypatch, tmp_path)

    layout = bindings.resolve_readonly(token_path(root))

    assert layout.library_dir == session.library_dir


def test_readonly_accepts_db_file(env: Path, monkeypatch, tmp_path: Path) -> None:
    _, session = _readonly_fixture(env, monkeypatch, tmp_path)

    layout = bindings.resolve_readonly(session.library_dir / "workspace.db")

    assert layout.library_dir == session.library_dir
