"""旁路产物：裁定报告与检索统计。

为什么单独一个模块、且只写文件不写库
------------------------------------
这两样东西的定位是「调参时拿来看的」和「事后复盘的证据」，不是记忆本体：

* 报告是派生数据 —— 任何时候都能从库里重新生成，丢了不心疼；
* 检索统计是高频旁路 —— 一次 recall 至少一次写，不该让核心链路上多一个
  事务与 schema 负担，更不该为它升一次 schema 版本。

所以它们都落在 `.ai_memory/` 下的普通文件里：

    reports/judge/20261117-195400.md    每次裁定一份，人可读
    reports/judge/20261117-195400.json  同内容机读版
    retrieval/2026-11-17.jsonl          按天轮转，一行一次检索

与 `starmap.py` 是同一套链路：读数据 → 生成单文件 HTML → 浏览器打开。
这样可视化不需要维护第二套取数逻辑。

唯一的例外是 `read_node_activity`：它用只读连接（`mode=ro`）读一次活跃度，
给检索页的「按活跃度排序」供数。只读、不建表、不写任何一列 —— 那条边界没破。

失败不回滚核心操作
------------------
写报告/写统计失败**不能让检索或裁定失败**：`activity` 已经落库，统计丢了
只是少一行证据。所有入口都吞掉 `OSError`，只在返回值里如实说明。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .lifecycle import DAY_MS, period_kind

# ---------------------------------------------------------------- 配置读取


def _report_conf(cfg: Any) -> Mapping[str, Any]:
    return (getattr(cfg, "settings", {}) or {}).get("report", {}) or {}


def judge_enabled(cfg: Any) -> bool:
    return bool(_report_conf(cfg).get("judge_enabled", True))


def judge_dir_name(cfg: Any) -> str:
    return str(_report_conf(cfg).get("judge_dir", "reports/judge"))


def judge_keep(cfg: Any) -> int:
    return max(0, int(_report_conf(cfg).get("judge_keep", 30)))


def retrieval_enabled(cfg: Any) -> bool:
    return bool(_report_conf(cfg).get("retrieval_enabled", True))


def retrieval_dir_name(cfg: Any) -> str:
    return str(_report_conf(cfg).get("retrieval_dir", "retrieval"))


def retrieval_keep_days(cfg: Any) -> int:
    return max(0, int(_report_conf(cfg).get("retrieval_keep_days", 90)))


# ---------------------------------------------------------------- 裁定报告


def _node_rows(
    conn: Any, level_table: Any, moment: int, table: str
) -> list[dict[str, Any]]:
    """把一张表的全部活跃节点读成报告行（含该档的升降阈值与周期进度）。"""
    from .store import EXPERIENCE, EPISODE

    spec = EXPERIENCE if table == EXPERIENCE.table else EPISODE
    dimension = period_kind(spec.kind)
    rows = conn.execute(
        f"SELECT id, content, importance, forget_level, activity, period_start,"
        f" {spec.time_attr} AS anchor FROM {table}"
        " WHERE archived_at IS NULL"
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        level = int(row["forget_level"] or 0)
        spec_row = level_table.spec(level)
        period_days = spec_row.period_days(dimension) if spec_row else None
        anchor = row["period_start"]
        if anchor is None:
            anchor = int(row["anchor"])
        elapsed = (moment - int(anchor)) / DAY_MS if anchor is not None else 0.0
        remaining = None if period_days is None else period_days - elapsed
        content = str(row["content"] or "").replace("\n", " ")
        out.append(
            {
                "id": row["id"],
                "kind": spec.kind,
                "table": table,
                "level": level,
                "activity": round(float(row["activity"] or 0.0), 3),
                "up_threshold": spec_row.up_threshold if spec_row else None,
                "down_threshold": spec_row.down_threshold if spec_row else None,
                "period_days": period_days,
                "elapsed_days": round(elapsed, 2),
                "remaining_days": None if remaining is None else round(remaining, 2),
                "due": remaining is not None and remaining <= 0,
                "importance": round(float(row["importance"] or 0.0), 3),
                "summary": content[:60],
            }
        )
    return out


def build_judge_report(
    conn: Any,
    cfg: Any,
    *,
    level_table: Any,
    report: Mapping[str, Any],
    moment: int,
) -> dict[str, Any]:
    """组装一份裁定快照：本次动作 + 全量节点。"""
    nodes = _node_rows(conn, level_table, moment, "knowledge") + _node_rows(
        conn, level_table, moment, "episodes"
    )

    actions = {str(item["id"]): item for item in report.get("items", [])}
    for node in nodes:
        act = actions.get(str(node["id"]))
        node["action"] = act["action"] if act else None
        node["reason"] = act["reason"] if act else None
        node["activity_before"] = act["activity"] if act else None

    return {
        "generated_at": moment,
        "dry_run": bool(report.get("dry_run")),
        "frozen_days": report.get("frozen_days", 0),
        "max_gap_days": report.get("max_gap_days"),
        "due": report.get("due", 0),
        "settled": report.get("settled", 0),
        "total_nodes": len(nodes),
        "items": report.get("items", []),
        "nodes": nodes,
    }


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


_KIND_CN = {"experience": "经验", "episode": "经历", "log": "日志"}


def _kind_cn(kind: Any) -> str:
    return _KIND_CN.get(str(kind), str(kind))


def _arrow(action: str | None) -> str:
    return {"up": "↑", "down": "↓", "hold": "="}.get(action or "", "")


def render_judge_markdown(snapshot: Mapping[str, Any], *, title_suffix: str = "") -> str:
    import datetime

    ts = datetime.datetime.fromtimestamp(
        int(snapshot["generated_at"]) / 1000
    ).strftime("%Y-%m-%d %H:%M:%S")
    head = "dry-run" if snapshot["dry_run"] else "已落盘"

    lines: list[str] = []
    lines.append(f"# 遗忘裁定报告 · {ts}{title_suffix}")
    lines.append("")
    frozen = snapshot.get("frozen_days") or 0
    gap_note = (
        f"冻结 {frozen:g} 天（上限 {snapshot.get('max_gap_days') or '—'} 天）"
        if frozen
        else "无冻结"
    )
    lines.append(
        f"{head} · {gap_note} · 到期 {snapshot['due']} / 节点 {snapshot['total_nodes']}"
    )
    lines.append("")

    items = snapshot.get("items") or []
    lines.append(f"## 本次裁定（{len(items)} 条）")
    lines.append("")
    if items:
        lines.append("| id | 类型 | 等级 | 活跃度 | 动作 | 依据 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for it in items:
            lines.append(
                f"| `{it['id']}` | {_kind_cn(it['kind'])} | L{it['from']} |"
                f" {_fmt(float(it['activity']), 2)} |"
                f" {_arrow(it['action'])} L{it['to']} | {it['reason']} |"
            )
    else:
        lines.append("（没有到期的条目：所有节点的倒计时都还没走完）")
    lines.append("")

    nodes = list(snapshot.get("nodes") or [])
    acted = {str(it["id"]) for it in items}
    rest = [n for n in nodes if str(n["id"]) not in acted]
    rest.sort(key=lambda n: (n["level"], n["activity"]), reverse=True)

    lines.append(f"## 全部节点 · 未裁定部分（{len(rest)} 条）")
    lines.append("")
    if rest:
        lines.append(
            "| id | 类型 | 等级 | 活跃度 | 升级线 | 降级线 | 周期 | 剩余 | 摘要 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for n in rest:
            remaining = n["remaining_days"]
            remain_text = "已到期" if n["due"] else f"{_fmt(remaining)} 天"
            lines.append(
                f"| `{n['id']}` | {_kind_cn(n['kind'])} | L{n['level']} |"
                f" {_fmt(n['activity'], 2)} | {_fmt(n['up_threshold'])} |"
                f" {_fmt(n['down_threshold'])} | {_fmt(n['period_days'], 0)} 天 |"
                f" {remain_text} | {n['summary']} |"
            )
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("---")
    lines.append(
        "> 阈值是该节点**当前等级**的升降线；「剩余」= 周期长度 − 已走时长，"
        "负数或 0 即代表该裁定了。"
    )
    lines.append("")
    return "\n".join(lines)


def write_judge_report(
    conn: Any,
    cfg: Any,
    workspace_dir: Path,
    *,
    level_table: Any,
    report: Mapping[str, Any],
    moment: int,
) -> Path | None:
    """落一份裁定报告（.md + .json），并按 `judge_keep` 清理旧文件。

    返回 Markdown 路径；写失败或没有工作区时返回 None（不影响裁定本身）。
    """
    if not judge_enabled(cfg):
        return None
    try:
        snapshot = build_judge_report(
            conn, cfg, level_table=level_table, report=report, moment=moment
        )
        target_dir = workspace_dir / judge_dir_name(cfg)
        target_dir.mkdir(parents=True, exist_ok=True)

        import datetime

        stamp = datetime.datetime.fromtimestamp(moment / 1000).strftime("%Y%m%d-%H%M%S")
        suffix = "-dryrun" if snapshot["dry_run"] else ""
        md_path = target_dir / f"{stamp}{suffix}.md"
        json_path = target_dir / f"{stamp}{suffix}.json"

        md_path.write_text(
            render_judge_markdown(snapshot), encoding="utf-8"
        )
        json_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _trim_reports(target_dir, judge_keep(cfg))
        return md_path
    except OSError:
        # 报告是旁路产物：磁盘满、无权限都不该让裁定失败
        return None


def _trim_reports(target_dir: Path, keep: int) -> None:
    """只保留最近 `keep` 组（.md/.json 各算一组）。keep=0 表示不清理。"""
    if keep <= 0:
        return
    try:
        files = sorted(
            (p for p in target_dir.glob("*.md")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
            stale.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        return


# ---------------------------------------------------------------- 检索统计


def append_retrieval(
    workspace_dir: Path,
    cfg: Any,
    record: Mapping[str, Any],
    *,
    moment: int,
) -> Path | None:
    """往当天的 JSONL 里追加一行；按 `retrieval_keep_days` 清理过期文件。

    JSONL 而不是 JSON 数组：追加是一行一次 `write`，不用读-改-写整个文件，
    并发进程同时 recall 也不会互相覆盖（最坏情况是行序交错，不影响解析）。
    """
    if not retrieval_enabled(cfg):
        return None
    try:
        import datetime

        day = datetime.datetime.fromtimestamp(moment / 1000).strftime("%Y-%m-%d")
        target_dir = workspace_dir / retrieval_dir_name(cfg)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim_retrievals(target_dir, retrieval_keep_days(cfg), day)
        return path
    except OSError:
        return None


def _trim_retrievals(target_dir: Path, keep_days: int, today: str) -> None:
    """按文件名（日期）保留最近 keep_days 天。"""
    if keep_days <= 0:
        return
    try:
        import datetime

        cutoff = datetime.date.fromisoformat(today) - datetime.timedelta(days=keep_days)
        for path in target_dir.glob("*.jsonl"):
            try:
                day = datetime.date.fromisoformat(path.stem)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        return


def read_retrievals(workspace_dir: Path, cfg: Any, *, days: int = 0) -> list[dict[str, Any]]:
    """读回检索记录，按时间正序。`days=0` 读全部。"""
    target_dir = workspace_dir / retrieval_dir_name(cfg)
    if not target_dir.is_dir():
        return []
    files = sorted(target_dir.glob("*.jsonl"))
    if days > 0:
        files = files[-days:]

    out: list[dict[str, Any]] = []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
        except OSError:
            continue
    out.sort(key=lambda r: int(r.get("at") or 0))
    return out


def latest_judge_snapshot(workspace_dir: Path, cfg: Any) -> dict[str, Any] | None:
    """读最新一份正式裁定报告（跳过 dry-run），给检索页当「当前节点状态」。

    没有报告就返回 None —— 页面会退化成只看检索记录，不影响使用。
    """
    folder = workspace_dir / judge_dir_name(cfg)
    if not folder.is_dir():
        return None
    files = [p for p in folder.glob("*.json") if "dryrun" not in p.name]
    if not files:
        files = list(folder.glob("*.json"))
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def level_thresholds(cfg: Any) -> list[dict[str, Any]]:
    """当前等级表的阈值视图，供页面算「维持/升级需要几次命中」。"""
    levels = []
    for item in cfg.forget_levels:
        level = int(item["level"])
        if level <= 0:
            continue
        levels.append(
            {
                "level": level,
                "period": item.get("period_experience_days"),
                "up": item.get("up_threshold"),
                "down": item.get("down_threshold"),
            }
        )
    levels.sort(key=lambda x: x["level"])
    return levels


ACTIVITY_TABLES = ("knowledge", "episodes")


def read_node_activity(workspace_dir: Path) -> dict[str, dict[str, Any]]:
    """库里每个条目的**当前**活跃度，键是节点 id。只读，不写库。

    检索页的「按活跃度排序」拿它当数据源，而不是裁定快照里的 ``activity``：

    * 快照是裁定那一刻的切片，活跃度随时会被下一次召回抬高或按周期清零；
    * 没跑过裁定的工作区连快照都没有，排序不能因此失效。

    只查有 ``activity`` 列的两张表。日志表没有这一列 —— 它不参与等级机制，
    也就无所谓活跃度，查出来会是恒 0 的假值（和星图里日志画固定灰同一个理由）。

    任何异常都当「没有数据」：侧栏少一个排序维度，不该让整张页面生成失败。
    """
    db = workspace_dir / "workspace.db"
    if not db.is_file():
        return {}

    out: dict[str, dict[str, Any]] = {}
    try:
        # 与 starmap.open_readonly 同一套写法：as_posix + quote 才能吃下带中文/空格的路径
        uri = "file:" + quote(db.as_posix(), safe="/:") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return {}
    try:
        for table in ACTIVITY_TABLES:
            try:
                rows = conn.execute(
                    f"SELECT id, activity, forget_level FROM {table}"  # noqa: S608 —— 表名是常量
                ).fetchall()
            except sqlite3.Error:
                continue  # 老库可能还没有这张表，跳过就好
            for node_id, activity, level in rows:
                out[str(node_id)] = {
                    "activity": round(float(activity or 0.0), 4),
                    "level": int(level) if level is not None else None,
                }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out
