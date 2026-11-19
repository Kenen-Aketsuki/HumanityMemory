"""令牌 v2：身份只有 key / workspace / label；v1 只识别不降级。

验收线（§13 T1）：
* v1 令牌读到 → `token_v1`，不能被当成「能用的令牌」放过去；
* v2 往返读写一致（save → load 后 key / workspace / label 全对）；
* 落点是 `<root>/.ai_memory/token.json`，凭据与库路径都不在令牌里。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from humanity_memory import token as tok


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return workspace


# ---------------------------------------------------------------- 落点


def test_token_path_is_inside_ai_memory(root: Path) -> None:
    assert tok.token_path(root) == root / ".ai_memory" / "token.json"
    # 传 `.ai_memory` 本身也一样，调用方不必为此补分支
    assert tok.token_path(root / ".ai_memory") == root / ".ai_memory" / "token.json"


def test_resolve_token_path_accepts_directory_and_none(root: Path) -> None:
    assert tok.resolve_token_path(None) is None
    assert tok.resolve_token_path(str(root / ".ai_memory")) == root / ".ai_memory" / "token.json"


# ---------------------------------------------------------------- v2 往返


def test_v2_roundtrip(root: Path) -> None:
    token = tok.build_token(
        tok.token_path(root), workspace=root, library="alpha", label="alpha"
    )
    assert token.key.startswith(tok.KEY_PREFIX)

    saved = token.save()
    assert saved == tok.token_path(root)

    back = tok.Token.load(saved)
    assert back.key == token.key
    assert back.library == "alpha"
    assert back.label == "alpha"
    assert back.version == tok.TOKEN_VERSION
    assert tok.same_path(back.workspace, root)
    assert back.same_workspace(root)
    # save → load → save 结果逐字段一致，不会因往返而漂移
    assert back.to_payload() == token.to_payload()


def test_declined_token_roundtrip(root: Path) -> None:
    """「已拒绝」也是一份令牌：library = null，能读回来、能认出来。"""
    token = tok.build_token(tok.token_path(root), workspace=root, library=None)
    assert token.declined is True

    back = tok.Token.load(token.save())
    assert back.library is None
    assert back.declined is True
    assert '"library": null' in token.path.read_text(encoding="utf-8")


def test_payload_carries_identity_only(root: Path) -> None:
    payload = json.loads(
        tok.build_token(tok.token_path(root), workspace=root, library="alpha")
        .save()
        .read_text(encoding="utf-8")
    )
    assert set(payload) == {"version", "key", "workspace", "library", "label"}
    for gone in ("embedding", "rules"):
        assert gone not in payload


def test_save_is_atomic_and_leaves_no_temp(root: Path) -> None:
    target = tok.token_path(root)
    tok.build_token(target, workspace=root, library="alpha").save()
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_masked_never_prints_whole_key(root: Path) -> None:
    token = tok.build_token(tok.token_path(root), workspace=root, library="alpha")
    masked = token.masked()
    assert masked["key"] != token.key
    assert token.key not in json.dumps(masked, ensure_ascii=False)
    assert masked["label"] == root.name
    assert masked["library"] == "alpha"


def test_relative_workspace_resolves_against_token_dir(root: Path) -> None:
    """相对路径是相对**令牌所在目录**（`.ai_memory`），不是相对 cwd。"""
    token_path = _write(
        tok.token_path(root),
        {"version": 2, "key": tok.generate_key(), "workspace": "..", "library": "alpha"},
    )
    loaded = tok.Token.load(token_path)
    assert tok.same_path(loaded.workspace, root)


# ---------------------------------------------------------------- v1 拒绝


def test_v1_token_is_rejected(root: Path) -> None:
    legacy = _write(
        tok.token_path(root),
        {
            "version": 1,
            "label": "project_island_back",
            "workspace": str(root),
            "rules": None,
            "embedding": {},
        },
    )
    with pytest.raises(tok.TokenV1) as info:
        tok.Token.load(legacy)
    assert info.value.code == "token_v1"
    # 报错要给出迁移动作，而不是只说「版本不对」
    assert "bind adopt" in str(info.value)


def test_missing_version_is_treated_as_v1(root: Path) -> None:
    legacy = _write(tok.token_path(root), {"workspace": str(root), "embedding": {}})
    with pytest.raises(tok.TokenV1) as info:
        tok.Token.load(legacy)
    assert info.value.code == "token_v1"


def test_v1_error_is_still_a_token_error(root: Path) -> None:
    """上层只 catch TokenError 一处，v1 不能是需要单独处理的旁支。"""
    legacy = _write(tok.token_path(root), {"version": 1, "workspace": str(root)})
    with pytest.raises(tok.TokenError):
        tok.Token.load(legacy)


# ---------------------------------------------------------------- 损坏与缺失


def test_missing_file_reports_no_token(root: Path) -> None:
    with pytest.raises(tok.TokenMissing) as info:
        tok.Token.load(tok.token_path(root))
    assert info.value.code == "no_token"


def test_broken_json_reports_token_bad(root: Path) -> None:
    path = tok.token_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(tok.TokenBad) as info:
        tok.Token.load(path)
    assert info.value.code == "token_bad"


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "workspace": ".", "library": "a"},                     # 缺 key
        {"version": 2, "key": "", "workspace": ".", "library": "a"},          # 空 key
        {"version": 2, "key": "not-a-key", "workspace": ".", "library": "a"},  # 前缀不对
        {"version": 2, "key": "hmk_abc", "library": "a"},                     # 缺 workspace
        {"version": 3, "key": "hmk_abc", "workspace": ".", "library": "a"},   # 版本比程序新
        {"version": 2, "key": "hmk_abc", "workspace": "."},                   # 缺 library
        {"version": 2, "key": "hmk_abc", "workspace": ".", "library": 7},     # library 类型不对
    ],
)
def test_malformed_tokens_rejected(root: Path, payload: dict) -> None:
    path = _write(tok.token_path(root), payload)
    with pytest.raises(tok.TokenBad) as info:
        tok.Token.load(path)
    assert info.value.code == "token_bad"


def test_missing_library_is_not_guessed_as_declined(root: Path) -> None:
    """缺字段 ≠ 已拒绝。猜成 null 等于替用户做了「以后别再问」的决定。"""
    path = _write(
        tok.token_path(root),
        {"version": 2, "key": tok.generate_key(), "workspace": "."},
    )
    with pytest.raises(tok.TokenBad) as info:
        tok.Token.load(path)
    assert "library" in str(info.value)


def test_non_integer_version_rejected(root: Path) -> None:
    path = _write(
        tok.token_path(root),
        {"version": "two", "key": tok.generate_key(), "workspace": ".", "library": "a"},
    )
    with pytest.raises(tok.TokenBad):
        tok.Token.load(path)


# ---------------------------------------------------------------- 身份比较


def test_workspace_mismatch_is_its_own_code(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    token = tok.build_token(tok.token_path(root), workspace=root, library="alpha")

    with pytest.raises(tok.TokenWorkspaceMismatch) as info:
        token.assert_workspace(other)
    assert info.value.code == "token_workspace_mismatch"
    assert "bind move" in str(info.value) or "属于另一个工作区" in str(info.value)


def test_workspace_compare_survives_trailing_sep_and_relative(root: Path) -> None:
    token = tok.build_token(tok.token_path(root), workspace=root, library="alpha")
    token.assert_workspace(str(root) + os.sep)
    token.assert_workspace(root / ".." / root.name)


def test_load_does_not_require_workspace_to_exist(root: Path, tmp_path: Path) -> None:
    """搬家后令牌里的旧路径已经不在了 —— 但 `bind move` 必须先能读到它。"""
    gone = tmp_path / "moved_away"
    path = _write(
        tok.token_path(root),
        {"version": 2, "key": tok.generate_key(), "workspace": str(gone), "library": "a"},
    )
    loaded = tok.Token.load(path)
    assert str(loaded.workspace) == str(gone)


def test_build_token_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(tok.TokenError):
        tok.build_token(
            tok.token_path(tmp_path / "nope"), workspace=tmp_path / "nope", library="a"
        )
