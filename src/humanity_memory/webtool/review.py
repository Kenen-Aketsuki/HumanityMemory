"""复盘记录取数：按天分组 + 活跃度 diff + 新增/顶替/合并明细。

数据来源（设计文档 §6.1）：
* 快照本体  -> reports/judge/*.json（文件名 YYYYMMDD-HHMMSS，同天取最新非 dryrun）
* 等级变动  -> 快照 items[]（裁定那一刻的活跃度）
* 活跃度 diff -> 相邻两天快照 nodes[] 按 id 算差
* 新增       -> knowledge/ episodes 的 created_at 落当天
* 顶替       -> knowledge.invalidated_by + invalid_at 落当天
* 合并       -> memory_events(event=dedup) 的 at 落当天

只读：不写库、不改任何文件。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

from .datasource import WebSource


def _day_of(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _stamp_day(name: str) -> str:
    """文件名 20261120-100413.json 的前 8 位 → 2026-11-20。"""
    stem = name[:8]
    if len(stem) != 8 or not stem.isdigit():
        return ""
    try:
        return _dt.datetime.strptime(stem, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _read_snapshots(source: WebSource) -> list[dict[str, Any]]:
    """扫 reports/judge/*.json，返回按日期分组的快照（每天一份，最新非 dryrun 优先）。"""
    if not source.judge_dir.is_dir():
        return []
    by_day: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(source.judge_dir.glob("*.json")):
        day = _stamp_day(path.name)
        if not day:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data = dict(data)
        data["_day"] = day
        data["_file"] = path.name
        data["_dry"] = bool(data.get("dry_run")) or "dryrun" in path.name
        data["_generated"] = int(data.get("generated_at") or 0)
        by_day.setdefault(day, []).append(data)

    out: list[dict[str, Any]] = []
    for day, items in by_day.items():
        # 同天多份：非 dryrun 优先，再按 generated_at 取最新
        formal = [x for x in items if not x["_dry"]]
        chosen = max(formal or items, key=lambda x: x["_generated"])
        out.append(chosen)
    out.sort(key=lambda x: x["_day"], reverse=True)
    return out


def _changes(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """当天遗忘等级变动 = 快照 items[]。"""
    out = []
    for it in snap.get("items") or []:
        out.append(
            {
                "id": str(it.get("id") or ""),
                "kind": it.get("kind"),
                "from": it.get("from"),
                "to": it.get("to"),
                "activity": it.get("activity"),   # 裁定那一刻的活跃度
                "action": it.get("action"),
                "reason": it.get("reason"),
            }
        )
    return out


def _activity_deltas(
    snap: dict[str, Any], prev: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """相邻两天 nodes[] 按 id 算活跃度差；只含两天都在的条目（纯新增归 added）。"""
    if prev is None:
        return []
    cur = {str(n.get("id")): n for n in snap.get("nodes") or []}
    old = {str(n.get("id")): n for n in prev.get("nodes") or []}
    out = []
    for nid, node in cur.items():
        if nid not in old:
            continue
        a = float(node.get("activity") or 0.0)
        b = float(old[nid].get("activity") or 0.0)
        out.append(
            {
                "id": nid,
                "kind": node.get("kind"),
                "prev": round(b, 2),
                "current": round(a, 2),
                "delta": round(a - b, 2),
            }
        )
    out.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return out


def _live_details(source: WebSource) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], ...]:
    """从库实时读三类明细，按 (day, kind) 分组。

    返回 (added, replaced, deduped) 三个 dict，键 (day, table)。
    """
    added: dict[tuple[str, str], list[dict[str, Any]]] = {}
    replaced: dict[tuple[str, str], list[dict[str, Any]]] = {}
    deduped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    conn = source.connect()
    try:
        # 新增
        for table, kind in (("knowledge", "experience"), ("episodes", "episode")):
            try:
                rows = conn.execute(
                    f"SELECT id, created_at, forget_level, activity, content FROM {table} "  # noqa: S608
                    "WHERE created_at IS NOT NULL"
                ).fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                day = _day_of(r["created_at"])
                if not day:
                    continue
                added.setdefault((day, kind), []).append(
                    {
                        "id": r["id"],
                        "kind": kind,
                        "level": r["forget_level"],
                        "activity": round(float(r["activity"] or 0.0), 2),
                        "summary": str(r["content"] or "").replace("\n", " ")[:60],
                    }
                )

        # 顶替（只有 knowledge 有 invalidated_by 列）
        try:
            rows = conn.execute(
                "SELECT id, content, invalidated_by, invalid_at FROM knowledge "
                "WHERE invalidated_by IS NOT NULL AND invalid_at IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            day = _day_of(r["invalid_at"])
            if not day:
                continue
            replaced.setdefault((day, "experience"), []).append(
                {
                    "old_id": r["id"],
                    "new_id": r["invalidated_by"],
                    "summary": str(r["content"] or "").replace("\n", " ")[:60],
                    "at": r["invalid_at"],
                }
            )

        # 合并（dedup 事件）
        try:
            rows = conn.execute(
                "SELECT at, item_id, delta, reason FROM memory_events "
                "WHERE event='dedup' AND at IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            day = _day_of(r["at"])
            if not day:
                continue
            reason = str(r["reason"] or "")
            deduped.setdefault((day, "experience"), []).append(
                {
                    "item_id": r["item_id"],
                    "delta": round(float(r["delta"] or 0.0), 2),
                    "summary": reason.split("：", 1)[-1][:60],
                    "at": r["at"],
                }
            )
    finally:
        conn.close()
    return added, replaced, deduped


def get(source: WebSource) -> dict[str, Any]:
    snaps = _read_snapshots(source)
    added, replaced, deduped = _live_details(source)
    days: list[dict[str, Any]] = []
    for i, snap in enumerate(snaps):
        day = snap["_day"]
        # 前一天 = 日历日相邻且非 dry-run（§6.1：dry-run 不参与 diff）
        prev = None
        if not snap["_dry"] and i + 1 < len(snaps):
            cand = snaps[i + 1]
            d = _dt.date.fromisoformat(day) - _dt.date.fromisoformat(cand["_day"])
            if d.days == 1 and not cand["_dry"]:
                prev = cand
        deltas = [] if snap["_dry"] else _activity_deltas(snap, prev)
        ex_k = ("experience", "episode")
        days.append(
            {
                "date": day,
                "at": snap["_generated"],
                "file": snap["_file"],
                "dry": snap["_dry"],
                "changes": _changes(snap),
                "activity_deltas": deltas,
                "added": [x for k in ex_k for x in added.get((day, k), [])],
                "replaced": replaced.get((day, "experience"), []),
                "deduped": deduped.get((day, "experience"), []),
                "summary": {
                    "due": snap.get("due", 0),
                    "settled": snap.get("settled", 0),
                    "total_nodes": snap.get("total_nodes", 0),
                },
            }
        )
    return {"ok": True, "days": days}
