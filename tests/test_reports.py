"""旁路产物：裁定报告 + 检索统计。

守四件事：

1. **裁定报告**要覆盖「每个节点」，不只是到期的那几条 —— 否则调阈值时
   看不到大部分节点离升降线还有多远；
2. **检索统计**要能说清「这次加了谁多少分」：before / delta / after 三件套齐；
3. **保留策略**真的会清旧文件，不然目录会无限长；
4. **开关**关掉就一个字节都不写（旁路产物也该有先尊重配置）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.lifecycle import DAY_MS
from humanity_memory.retrieval_view import render_retrieval_html

from test_store import tx


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    ws = proj / ".ai_memory"
    ws.mkdir(parents=True)

    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    yield cfg, store, ws
    store.close()


def _force(store: MemoryStore, *, item_id: str, **columns) -> None:
    sets = ", ".join(f"{key} = ?" for key in columns)
    with tx(store.workspace_conn):
        store.workspace_conn.execute(
            f"UPDATE knowledge SET {sets} WHERE id = ?",
            (*columns.values(), item_id),
        )


# ---------------------------------------------------------------- 裁定报告


def test_judge_writes_report_with_every_node(env) -> None:
    """报告要含全部活跃节点，而不只是到期项。"""
    cfg, store, ws = env
    now = 3_000_000_000_000
    due = store.write_memory("该被裁定的那条", importance=0.5)
    idle = store.write_memory("还没到期的这条", importance=0.5)
    _force(store, item_id=due.id, forget_level=2, activity=0.0,
           period_start=now - int(10 * DAY_MS))
    _force(store, item_id=idle.id, forget_level=2, activity=0.0, period_start=now)
    with tx(store.workspace_conn):
        store.workspace_conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_judge_at', ?)",
            (str(now - int(DAY_MS)),),
        )

    report = store.judge_due(now=now)

    assert report["report_path"], "裁定完应当落一份报告"
    md = Path(report["report_path"])
    assert md.is_file()
    text = md.read_text(encoding="utf-8")

    assert "## 本次裁定" in text
    assert "## 全部节点" in text
    assert due.id in text, "到期的节点要出现在裁定明细里"
    assert idle.id in text, "未到期的节点也要出现在全量快照里"
    assert "升级线" in text and "降级线" in text

    payload = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["total_nodes"] == 2
    node = next(n for n in payload["nodes"] if n["id"] == idle.id)
    assert node["level"] == 2
    assert node["up_threshold"] is not None
    assert node["down_threshold"] is not None


def test_judge_report_marks_dry_run(env) -> None:
    """干跑也出报告，但文件名和内容都要标明。"""
    _, store, ws = env
    now = 3_000_000_000_000
    store.write_memory("随便一条", importance=0.5)

    report = store.judge_due(now=now, dry_run=True)

    path = Path(report["report_path"])
    assert "dryrun" in path.name
    assert "dry-run" in path.read_text(encoding="utf-8")


def test_judge_keep_trims_old_reports(env) -> None:
    """只保留最近 N 份，旧的一并清掉（含同名 json）。"""
    cfg, store, ws = env
    cfg.settings.setdefault("report", {})["judge_keep"] = 2
    base = 3_000_000_000_000

    for i in range(4):
        store.judge_due(now=base + i * 86_400_000)

    folder = ws / cfg.settings["report"]["judge_dir"]
    assert len(list(folder.glob("*.md"))) == 2
    assert len(list(folder.glob("*.json"))) == 2


def test_judge_report_can_be_disabled(env) -> None:
    """开关关掉就完全不出报告，但裁定照常。"""
    cfg, store, ws = env
    cfg.settings.setdefault("report", {})["judge_enabled"] = False
    now = 3_000_000_000_000
    item = store.write_memory("该降的", importance=0.5)
    _force(store, item_id=item.id, forget_level=2, activity=0.0,
           period_start=now - int(10 * DAY_MS))

    report = store.judge_due(now=now)

    assert "report_path" not in report
    assert report["due"] == 1, "报告关掉不影响裁定本身"
    assert not (ws / "reports").exists()


# ---------------------------------------------------------------- 检索统计


def test_recall_writes_activity_delta(env) -> None:
    """一次 recall 要留下：问了什么、翻出谁、每条加了多少活跃度。"""
    cfg, store, ws = env
    item = store.write_memory("sfm 状态机切换的坑", importance=0.5)
    _force(store, item_id=item.id, activity=1.5)

    hits = store.recall("sfm 状态机", limit=5)
    assert hits, "关键词命中至少该捞回一条"

    files = list((ws / "retrieval").glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[-1])

    assert record["query"] == "sfm 状态机"
    assert record["hit_count"] >= 1
    row = next(h for h in record["hits"] if h["id"] == item.id)
    assert row["rank"] >= 1
    assert row["activity_before"] == pytest.approx(1.5, abs=1e-3)
    assert row["delta"] is not None
    assert row["activity_after"] == pytest.approx(1.5 + row["delta"], abs=1e-3)
    assert row["level"] == item.forget_level


def test_search_does_not_log(env) -> None:
    """search 是只读检索：不加强化，也不该写统计。"""
    _, store, ws = env
    store.write_memory("一条经验", importance=0.5)

    store.search("经验")

    assert not (ws / "retrieval").exists()


def test_retrieval_can_be_disabled(env) -> None:
    """关掉统计后 recall 照常返回结果，只是不落文件。"""
    cfg, store, ws = env
    cfg.settings.setdefault("report", {})["retrieval_enabled"] = False
    store.write_memory("一条经验", importance=0.5)

    hits = store.recall("经验")

    assert hits
    assert not (ws / "retrieval").exists()


def test_retrieval_keep_days_trims_old_files(env) -> None:
    """按天轮转：超出保留天数的文件会被清掉。"""
    from humanity_memory.reports import append_retrieval

    cfg, store, ws = env
    cfg.settings.setdefault("report", {})["retrieval_keep_days"] = 3
    folder = ws / cfg.settings["report"]["retrieval_dir"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")

    append_retrieval(ws, cfg, {"at": 3_000_000_000_000, "query": "x", "hits": []},
                     moment=3_000_000_000_000)

    assert not (folder / "2020-01-01.jsonl").exists()
    assert len(list(folder.glob("*.jsonl"))) == 1


# ---------------------------------------------------------------- HTML


def test_html_contains_query_and_delta() -> None:
    """可视化只是渲染：数据原样进去，不丢字段。"""
    records = [
        {
            "at": 1_700_000_000_000,
            "query": "设施存档 逻辑类绑定",
            "deep": False,
            "elapsed_ms": 42.0,
            "hit_count": 1,
            "hits": [
                {
                    "id": "k_abc",
                    "kind": "experience",
                    "rank": 1,
                    "score": 0.83,
                    "vec_sim": 0.79,
                    "lex": 0.91,
                    "level": 4,
                    "activity_before": 3.2,
                    "delta": 0.19,
                    "activity_after": 3.39,
                    "forgotten": False,
                }
            ],
        }
    ]

    html = render_retrieval_html(records)

    assert "设施存档 逻辑类绑定" in html
    assert "k_abc" in html
    assert "const DATA =" in html
    assert "<title>" in html


def test_html_escapes_script_breakers() -> None:
    """查询里带 </script> 不能把页面撑破。"""
    html = render_retrieval_html(
        [{"at": 1, "query": "</script><b>x</b>", "hits": []}]
    )

    assert "</script><b>x</b>" not in html
    assert "\\u003c/script\\u003e" in html


def test_html_carries_analysis_inputs() -> None:
    """页面要能自己算「维持需命中」：等级表与节点快照必须一起送进去。"""
    meta = {
        "levels": [
            {"level": 2, "period": 2, "up": 3.5, "down": 1.6},
            {"level": 4, "period": 5, "up": 7.0, "down": 4.0},
        ],
        "nodes": [{"level": 4, "activity": 0.0, "up": 7.0, "down": 4.0}],
        "generated": 1_700_000_000_000,
    }
    html = render_retrieval_html([], meta=meta)

    assert '"levels"' in html and '"nodes"' in html
    assert "维持需命中" in html and "升级需命中" in html
    assert "降级线下" in html
    assert "renderDiag" in html, "诊断区要真的算，不是静态文案"


def test_latest_snapshot_and_levels(env) -> None:
    """CLI 靠这两个函数给页面备料：读最新正式报告 + 当前阈值表。"""
    cfg, store, ws = env
    from humanity_memory.reports import latest_judge_snapshot, level_thresholds

    assert latest_judge_snapshot(ws, cfg) is None, "没有报告时要能优雅降级"

    report = store.judge_due(now=3_000_000_000_000)
    snapshot = latest_judge_snapshot(ws, cfg)
    assert snapshot is not None
    assert snapshot["generated_at"] == report["now"]

    levels = level_thresholds(cfg)
    assert [lv["level"] for lv in levels] == list(range(1, cfg.max_level + 1))
    assert all(lv["down"] is not None for lv in levels)


def test_html_carries_analysis_inputs() -> None:
    """页面要能自己算「维持需命中」：等级表与节点快照必须一起送进去。"""
    meta = {
        "levels": [
            {"level": 2, "period": 2, "up": 3.5, "down": 1.6},
            {"level": 4, "period": 5, "up": 7.0, "down": 4.0},
        ],
        "nodes": [{"level": 4, "activity": 0.0, "up": 7.0, "down": 4.0}],
        "generated": 1_700_000_000_000,
    }
    html = render_retrieval_html([], meta=meta)

    assert '"levels"' in html and '"nodes"' in html
    assert "维持需命中" in html and "升级需命中" in html
    assert "降级线下" in html
    assert "renderDiag" in html, "诊断区要真的算，不是静态文案"


def test_latest_snapshot_and_levels(env) -> None:
    """CLI 靠这两个函数给页面备料：读最新正式报告 + 当前阈值表。"""
    cfg, store, ws = env
    from humanity_memory.reports import latest_judge_snapshot, level_thresholds

    assert latest_judge_snapshot(ws, cfg) is None, "没有报告时要能优雅降级"

    report = store.judge_due(now=3_000_000_000_000)
    snapshot = latest_judge_snapshot(ws, cfg)
    assert snapshot is not None
    assert snapshot["generated_at"] == report["now"]

    levels = level_thresholds(cfg)
    assert [lv["level"] for lv in levels] == list(range(1, cfg.max_level + 1))
    assert all(lv["down"] is not None for lv in levels)
