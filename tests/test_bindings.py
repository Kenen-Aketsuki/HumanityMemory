"""存量迁移（`bind migrate`）：扫描、登记、发令牌、幂等。

这一层测试的重点不是"跑通"，而是**重复跑不改变结论**：
迁移是一次性动作，正是「不能重跑」的脚本最容易被写坏 —— 一旦重跑会重签令牌，
registry 里就会攒下一堆孤儿钥匙，还得靠人工去分辨哪把是真的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from humanity_memory import bindings
from humanity_memory import keys
from humanity_memory.token import Token, build_token, token_path


def _make_project(root: Path, *, knowledge: int = 0) -> Path:
    """造一个最小工作区：四张表齐全，条数可控。"""
    ws = root / ".ai_memory"
    ws.mkdir(parents=True)
    con = sqlite3.connect(ws / "workspace.db")
    for table in ("knowledge", "episodes", "logs", "digests"):
        con.execute(f"create table {table}(id text)")
    for i in range(knowledge):
        con.execute("insert into knowledge values(?)", (f"k{i}",))
    con.commit()
    con.close()
    return ws


def _reg(path: Path) -> keys.Registry:
    return keys.read(path)


# ---------------------------------------------------------------- 扫描


def test_discover_finds_nested_projects_and_skips_heavy_dirs(tmp_path: Path) -> None:
    _make_project(tmp_path / "alpha")
    _make_project(tmp_path / "work" / "2026" / "beta")
    _make_project(tmp_path / "gamma" / "node_modules" / "pkg")

    found = bindings.discover([tmp_path], depth=4)
    names = sorted(p.parent.name for p in found)

    assert names == ["alpha", "beta"], "node_modules 下的不该算项目"


def test_discover_respects_depth(tmp_path: Path) -> None:
    _make_project(tmp_path / "a" / "b" / "c" / "deep")
    assert bindings.discover([tmp_path], depth=1) == []
    assert [p.parent.name for p in bindings.discover([tmp_path], depth=4)] == ["deep"]


# ---------------------------------------------------------------- 计划


def test_project_without_token_is_registered(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha", knowledge=3)
    plan = bindings.build_plan([tmp_path], registry=_reg(tmp_path / "registry.json"))

    assert len(plan) == 1
    cand = plan[0]
    assert cand.label == "alpha"
    assert cand.action == "register"
    assert cand.token_state == "none"
    assert cand.counts["knowledge"] == 3


def test_label_override_wins(tmp_path: Path) -> None:
    _make_project(tmp_path / "HumanityMemory_dev")
    plan = bindings.build_plan(
        [tmp_path],
        labels={str(tmp_path / "HumanityMemory_dev"): "hm_dev"},
        registry=_reg(tmp_path / "registry.json"),
    )
    assert plan[0].label == "hm_dev"


def test_label_taken_by_another_path_is_a_conflict(tmp_path: Path) -> None:
    ws = _make_project(tmp_path / "Alpha")
    reg_path = tmp_path / "registry.json"
    with keys.edit(reg_path) as reg:
        reg.set_library("alpha", tmp_path / "somewhere" / "else")

    plan = bindings.build_plan([tmp_path], registry=_reg(reg_path))

    assert plan[0].action == "conflict", "绝不静默改名：那会让令牌与 registry 各说一套"
    assert plan[0].library_dir == ws


# ---------------------------------------------------------------- 落地与幂等


def test_apply_writes_token_and_registry(tmp_path: Path) -> None:
    ws = _make_project(tmp_path / "Alpha", knowledge=2)
    reg_path = tmp_path / "registry.json"

    plan = bindings.build_plan([tmp_path], registry=_reg(reg_path))
    report = bindings.apply_plan(plan, dry_run=False, registry_path=reg_path)

    assert report["ok"] is True
    token = Token.load(token_path(tmp_path / "Alpha"))
    assert token.library == "alpha"
    assert token.version == 2

    reg = _reg(reg_path)
    assert reg.library_path("alpha") == ws
    record = reg.record_for(token.key)
    assert record is not None and record.library == "alpha"
    assert record.active


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha")
    reg_path = tmp_path / "registry.json"

    plan = bindings.build_plan([tmp_path], registry=_reg(reg_path))
    bindings.apply_plan(plan, dry_run=True, registry_path=reg_path)

    assert not reg_path.exists(), "dry-run 连 registry.json 都不该创建"
    assert not token_path(tmp_path / "Alpha").exists()


def test_second_apply_changes_nothing(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha")
    reg_path = tmp_path / "registry.json"

    plan = bindings.build_plan([tmp_path], registry=_reg(reg_path))
    bindings.apply_plan(plan, dry_run=False, registry_path=reg_path)

    before_registry = reg_path.read_text(encoding="utf-8")
    before_token = token_path(tmp_path / "Alpha").read_text(encoding="utf-8")

    again = bindings.build_plan([tmp_path], registry=_reg(reg_path))
    assert [c.action for c in again] == ["keep"], "第二次必须是 keep，不能重签"

    bindings.apply_plan(again, dry_run=False, registry_path=reg_path)
    assert reg_path.read_text(encoding="utf-8") == before_registry
    assert token_path(tmp_path / "Alpha").read_text(encoding="utf-8") == before_token


def test_second_run_without_label_reuses_registered_name(tmp_path: Path) -> None:
    """首轮用 --label 起了名，重跑时忘了带 —— 不能按目录名另起一个。"""
    _make_project(tmp_path / "HumanityMemory_dev")
    reg_path = tmp_path / "registry.json"

    first = bindings.build_plan(
        [tmp_path],
        labels={str(tmp_path / "HumanityMemory_dev"): "hm_dev"},
        registry=_reg(reg_path),
    )
    bindings.apply_plan(first, dry_run=False, registry_path=reg_path)

    again = bindings.build_plan([tmp_path], registry=_reg(reg_path))

    assert again[0].label == "hm_dev"
    assert again[0].action == "keep"
    assert len(_reg(reg_path).libraries) == 1, "不能多出一条指向同一目录的记录"


def test_explicit_new_label_on_registered_path_is_conflict(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha")
    reg_path = tmp_path / "registry.json"
    bindings.apply_plan(
        bindings.build_plan([tmp_path], registry=_reg(reg_path)),
        dry_run=False,
        registry_path=reg_path,
    )

    plan = bindings.build_plan(
        [tmp_path], labels={str(tmp_path / "Alpha"): "another"}, registry=_reg(reg_path)
    )

    assert plan[0].action == "conflict"
    assert "alpha" in plan[0].reason


def test_token_whose_key_is_unknown_is_resigned(tmp_path: Path) -> None:
    """换过 HM_HOME / registry 丢了的现场：令牌在，钥匙不在 —— 自愈要重签。"""
    _make_project(tmp_path / "Alpha")
    old = build_token(token_path(tmp_path / "Alpha"), workspace=tmp_path / "Alpha", library="alpha")
    old.save()

    plan = bindings.build_plan([tmp_path], registry=_reg(tmp_path / "registry.json"))

    assert plan[0].token_state == "v2-stale"
    assert plan[0].action == "register"


def test_v1_token_is_resigned(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha")
    token_path(tmp_path / "Alpha").write_text(
        json.dumps({"version": 1, "workspace": str(tmp_path / "Alpha"), "label": "old"}),
        encoding="utf-8",
    )
    plan = bindings.build_plan([tmp_path], registry=_reg(tmp_path / "registry.json"))

    assert plan[0].token_state == "v1"
    assert plan[0].action == "register"


def test_declined_token_is_not_overruled(tmp_path: Path) -> None:
    """「已拒绝」是个决定，不是待办事项 —— 迁移不许替人翻案。"""
    ws = _make_project(tmp_path / "Alpha")
    reg_path = tmp_path / "registry.json"
    with keys.edit(reg_path) as reg:
        reg.set_library("alpha", ws)
        key = reg.issue_key(tmp_path / "Alpha", None)

    build_token(
        token_path(tmp_path / "Alpha"), workspace=tmp_path / "Alpha", library=None, key=key
    ).save()

    plan = bindings.build_plan([tmp_path], registry=_reg(reg_path))

    assert plan[0].token_state == "declined"
    assert plan[0].action == "keep"


# ---------------------------------------------------------------- 两件收尾动作


def test_legacy_tokens_are_retired_not_deleted(tmp_path: Path) -> None:
    _make_project(tmp_path / "Alpha")
    legacy = tmp_path / "active.token.json"
    legacy.write_text('{"version": 1}', encoding="utf-8")

    report = bindings.apply_plan(
        bindings.build_plan([tmp_path], registry=_reg(tmp_path / "r.json")),
        dry_run=False,
        registry_path=tmp_path / "r.json",
        retire_paths=[legacy],
    )

    assert not legacy.exists()
    retired = Path(report["retired"][0]["retired"])
    assert retired.is_file(), "退役 = 改名留档，不是删除"
    assert "retired-" in retired.name


def test_mcp_json_drops_only_humanity_memory_token(tmp_path: Path) -> None:
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "humanity-memory": {
                        "command": "X:/hm.exe",
                        "args": ["--token", "C:/u/active.token.json", "serve"],
                    },
                    "other": {"command": "X:/other.exe", "args": ["--token", "C:/k.json"]},
                }
            }
        ),
        encoding="utf-8",
    )

    report = bindings._retire_mcp_token(mcp, dry_run=False)

    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert data["mcpServers"]["humanity-memory"]["args"] == ["serve"]
    assert data["mcpServers"]["other"]["args"] == ["--token", "C:/k.json"], "别人的 server 不碰"
    assert report["skipped"] == ["other"]
    assert Path(report["backup"]).is_file(), "改客户端配置前必须留备份"


def test_mcp_json_unparsable_is_reported_not_fatal(tmp_path: Path) -> None:
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{ 不是 json", encoding="utf-8")
    report = bindings._retire_mcp_token(mcp, dry_run=False)
    assert report["changed"] is False
    assert "JSON" in report["reason"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("HumanityMemory", "humanitymemory"),
        ("beto-client", "beto_client"),
        ("Aketsuki_Unity_MCP", "aketsuki_unity_mcp"),
        ("中文项目", "library"),
    ],
)
def test_slug(raw: str, expected: str) -> None:
    assert bindings.slug(raw) == expected
