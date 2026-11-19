"""registry 验收（§13 T2）：并发不丢更新、只存 `sha256(salt+key)`、跟着 `HM_HOME` 走。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from humanity_memory import keys
from humanity_memory import token as tok


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 registry 关进临时 home —— 绝不允许测试碰真实 `~/.humanity-memory`。"""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HM_HOME", str(root))
    return root


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "alpha"
    ws.mkdir()
    return ws


def _registry_file(home: Path) -> Path:
    return home / "registry.json"


# ---------------------------------------------------------------- 落点


def test_registry_follows_hm_home(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        reg.issue_key(workspace, "alpha")
    assert _registry_file(home).is_file()
    # 换 HM_HOME 就是"另一个 registry"，看不到这条
    assert str(keys.registry_path()).startswith(str(home))


def test_read_never_creates_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv("HM_HOME", str(missing))
    reg = keys.read()
    assert reg.keys == {} and reg.libraries == {}
    assert not missing.exists()  # 只读路径不拉屎（A1）


# ---------------------------------------------------------------- 只存哈希


def test_registry_never_contains_plaintext_key(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        key = reg.issue_key(workspace, "alpha")

    text = _registry_file(home).read_text(encoding="utf-8")
    assert key not in text
    data = json.loads(text)
    (digest,) = data["keys"].keys()
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert key not in digest  # 键是哈希，不是钥匙本身

    stored = data["keys"][digest]
    assert set(stored) == {"workspace", "library", "access", "created", "revoked"}
    assert data["salt"] and data["version"] == 2


def test_hash_is_salted(home: Path, workspace: Path) -> None:
    """换个 salt，同一把 key 的哈希必须不同 —— 否则就是裸 sha256。"""
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        key = reg.issue_key(workspace, "alpha")
        digest = reg.hash_key(key)

    other = keys.Registry(path=Path("x"), salt="0" * 32)
    assert other.hash_key(key) != digest


# ---------------------------------------------------------------- 校验


def test_check_key_roundtrip(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        key = reg.issue_key(workspace, "alpha")

    rec = keys.read().check_key(key, workspace)
    assert rec.library == "alpha"
    assert rec.access == "rw"
    assert rec.active


def test_unknown_key(home: Path, workspace: Path) -> None:
    keys.read()  # 连 registry 都没有
    with pytest.raises(keys.KeyUnknown) as info:
        keys.read().check_key(tok.generate_key(), workspace)
    assert info.value.code == "key_unknown"


def test_revoked_key(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        key = reg.issue_key(workspace, "alpha")
    with keys.edit() as reg:
        assert reg.revoke(key) is True
        assert reg.revoke(key) is False  # 重跑迁移不该炸

    with pytest.raises(keys.KeyRevoked) as info:
        keys.read().check_key(key, workspace)
    assert info.value.code == "key_revoked"
    assert "重发" in str(info.value)


def test_key_workspace_mismatch(home: Path, workspace: Path, tmp_path: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        key = reg.issue_key(workspace, "alpha")

    other = tmp_path / "beta"
    other.mkdir()
    with pytest.raises(keys.KeyWorkspaceMismatch) as info:
        keys.read().check_key(key, other)
    assert info.value.code == "key_workspace_mismatch"


def test_declined_key_is_legal(home: Path, workspace: Path) -> None:
    """`library=None` = 用户明确不接入。也要能发钥匙，否则这个决定没法被记住。"""
    with keys.edit() as reg:
        key = reg.issue_key(workspace, None)
    rec = keys.read().check_key(key, workspace, None)
    assert rec.library is None


def test_library_claim_must_match_registry(home: Path, workspace: Path) -> None:
    """令牌里的 library 只是断言：说成别人，或者手改一个 null，都得被抓住。

    否则"改一个 null"就能绕过整个准入，或者让人以为令牌能授予归属。
    """
    with keys.edit() as reg:
        reg.set_library("alpha", workspace / ".ai_memory")
        reg.set_library("island", workspace / "island")
        key = reg.issue_key(workspace, "alpha")
    reg = keys.read()

    assert reg.check_key(key, workspace, "alpha").library == "alpha"

    with pytest.raises(keys.KeyLibraryMismatch) as info:
        reg.check_key(key, workspace, "island")
    assert info.value.code == "key_library_mismatch"

    with pytest.raises(keys.KeyLibraryMismatch):
        reg.check_key(key, workspace, None)  # 手改成"已拒绝"也不行

    # 不传就不比对 —— 迁移 / 运维工具需要先读记录再决定
    assert reg.check_key(key, workspace).library == "alpha"


def test_bad_access_value(home: Path, workspace: Path) -> None:
    with pytest.raises(keys.RegistryError):
        with keys.edit() as reg:
            reg.issue_key(workspace, None, access="admin")


def test_issue_key_requires_registered_library(home: Path, workspace: Path) -> None:
    with pytest.raises(keys.RegistryConflict):
        with keys.edit() as reg:
            reg.issue_key(workspace, "ghost")


# ---------------------------------------------------------------- 库


def test_set_library_conflict_and_replace(home: Path, workspace: Path) -> None:
    other = workspace / "second"
    with keys.edit() as reg:
        reg.set_library("alpha", workspace)
        reg.set_library("alpha", workspace)  # 同路径幂等
        with pytest.raises(keys.RegistryConflict):
            reg.set_library("alpha", other)
        reg.set_library("alpha", other, replace=True)
    assert keys.read().library_path("alpha") == other


def test_unique_label(home: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("demo", Path("x"))
        reg.set_library("demo-2", Path("y"))
        assert reg.unique_label("demo") == "demo-3"
        assert reg.unique_label("fresh") == "fresh"


# ---------------------------------------------------------------- 原子性 / 并发


def test_no_temp_or_lock_leftovers(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("alpha", workspace)
    names = sorted(p.name for p in home.iterdir())
    assert names == ["registry.json"]


def test_salt_is_stable_across_writes(home: Path, workspace: Path) -> None:
    with keys.edit() as reg:
        reg.set_library("a", workspace)
        first = reg.salt
    with keys.edit() as reg:
        reg.set_library("b", workspace)
        assert reg.salt == first  # 换 salt 会让所有旧哈希失效


def test_concurrent_binds_do_not_lose_updates(home: Path, workspace: Path) -> None:
    """R-5：两个窗口同时绑定。10 个线程各写一个库，最后必须 10 条全在。

    块内故意 sleep：如果实现是「先 load、再抢锁、再 save」，等锁的时间足够
    让每个线程都基于**过期快照**覆盖别人，这个用例就会红。
    """
    total = 10
    errors: list[BaseException] = []
    issued: list[str] = []
    guard = threading.Lock()

    def bind(i: int) -> None:
        try:
            with keys.edit() as reg:
                time.sleep(0.01)  # 拉宽竞争窗口
                reg.set_library(f"lib{i}", workspace / f"dir{i}")
                key = reg.issue_key(workspace, f"lib{i}")
            with guard:
                issued.append(key)
        except BaseException as exc:  # noqa: BLE001 - 线程里要带出来
            errors.append(exc)

    threads = [threading.Thread(target=bind, args=(i,)) for i in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    reg = keys.read()
    assert len(reg.libraries) == total
    assert len(reg.active_keys()) == total
    assert len(set(issued)) == total  # 没有两把钥匙撞成同一把


def test_busy_lock_reports_instead_of_writing(home: Path, workspace: Path) -> None:
    lock = home / "registry.json.lock"
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(keys.RegistryBusy) as info:
        with keys.edit(timeout=0.15) as reg:  # pragma: no cover - 不该进去
            reg.set_library("alpha", workspace)
    assert info.value.code == "registry_busy"
    assert not _registry_file(home).exists()  # 没拿到锁就绝不落盘


def test_stale_lock_is_taken_over(home: Path, workspace: Path) -> None:
    """崩在锁里的进程不能永久锁死 registry。"""
    lock = home / "registry.json.lock"
    lock.write_text("crashed", encoding="utf-8")
    old = time.time() - (keys.LOCK_STALE + 10)
    os.utime(lock, (old, old))

    with keys.edit(timeout=0.5) as reg:
        reg.set_library("alpha", workspace)
    assert keys.read().library("alpha") is not None


def test_failed_edit_does_not_persist(home: Path, workspace: Path) -> None:
    with pytest.raises(RuntimeError):
        with keys.edit() as reg:
            reg.set_library("alpha", workspace)
            raise RuntimeError("半路炸了")
    assert not _registry_file(home).exists()


# ---------------------------------------------------------------- 坏文件


@pytest.mark.parametrize(
    "payload",
    [
        "{ not json",
        json.dumps({"version": 2}),                                   # 缺 salt
        json.dumps({"version": 3, "salt": "a" * 32}),                 # 版本不认识
        json.dumps({"version": 2, "salt": "a" * 32, "keys": {"x": {}}}),  # key 缺 workspace
    ],
)
def test_broken_registry_is_reported(home: Path, payload: str) -> None:
    _registry_file(home).write_text(payload, encoding="utf-8")
    with pytest.raises(keys.RegistryBad) as info:
        keys.read()
    assert info.value.code == "registry_bad"


def test_save_outside_edit_is_refused(home: Path) -> None:
    """绕过 edit() 直接 save 就是 R-5 的入口，必须挡住。"""
    with pytest.raises(keys.RegistryBad):
        keys.read().save()
