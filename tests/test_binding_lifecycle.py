"""四态与绑定生命周期：未接入 / 已拒绝 / 自有库 / 共享库。

这些用例全部走**公开入口**（`bindings.bind_*` + `resolve_session`），不碰内部函数 ——
准入层的价值全在"从外面看是什么行为"，把内部结构测绿了没有意义。

每个用例都对应文档里的一个承诺：
- §5.2 四态，一态都不许含糊；
- §5.5 第 4/6 步：令牌被复制或手改必须拒绝；
- §6.4 拒绝态只剩 bind_*；接进去之后它们当场出现（R-11 自救）；
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


# ---------------------------------------------------------------- 工具面裁剪


def test_refusal_exposes_only_bind_tools(env: Path) -> None:
    root = env / "plain"
    root.mkdir()
    session = bindings.resolve_session(root)

    server = build_server(
        _cfg(env / "home"), layout=paths.empty_layout(root=root), session=session
    )

    names = _tool_names(server)
    assert names == sorted(
        [
            "memory_bind_status",
            "memory_bind_new",
            "memory_bind_existing",
            "memory_bind_adopt",
            "memory_bind_decline",
            "memory_use_workspace",
        ]
    ), "拒绝态不该暴露任何读写工具"


def test_adopt_activates_the_full_toolset(env: Path) -> None:
    """R-11：boot 实例落在未接入目录，接进去之后工具必须当场出现。"""
    root = env / "alpha"
    _project(root)

    session = bindings.resolve_session(root)
    server = build_server(
        _cfg(env / "home"), layout=paths.empty_layout(root=root), session=session
    )
    assert len(_tool_names(server)) == 6

    # 走同一份 bindings 逻辑（MCP 工具与 CLI 都调它）
    bound = bindings.bind_adopt(root)
    assert bound.ok

    server2 = build_server(_cfg(env / "home", bound.library_dir), layout=bound.layout, session=bound)
    assert "memory_search" in _tool_names(server2)
    assert len(_tool_names(server2)) == 29


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
