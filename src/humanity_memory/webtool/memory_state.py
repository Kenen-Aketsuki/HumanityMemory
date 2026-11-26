"""记忆现状视图：当前全量记忆 + 实时"距下次裁定"倒计时。

数据源：库只读扫描 knowledge / episodes（设计文档 §6.2）。
不读快照（快照的 remaining_days 是裁定那一刻的切片，会过时），不裁定、不写库。

倒计时与 lifecycle.is_due 同判据：
    span_ms = LevelTable.period_ms(level, dimension)
    next_ms = period_start + span_ms
    remain_ms = next_ms - now
dimension 由 period_kind(kind) 决定（episode 快周期，其余慢周期）。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..lifecycle import period_kind
from .datasource import WebSource

_DAY_MS = 86_400_000.0


def _due_state(level: int, period_start: int | None, kind: str, source: WebSource, now: int) -> dict[str, Any]:
    """算一条记忆的裁定状态。返回 {code, label, remain_ms, next_ms}。"""
    if level <= 0:
        return {"code": "forgotten", "label": "遗忘层·不裁定", "remain_ms": None, "next_ms": None}
    if not period_start:
        return {"code": "unstarted", "label": "未开始计时", "remain_ms": None, "next_ms": None}
    dim = period_kind(kind)
    span = source.levels.period_ms(level, dim)
    if span is None:
        return {"code": "no_period", "label": "该档无周期", "remain_ms": None, "next_ms": None}
    next_ms = int(period_start) + int(span)
    remain = next_ms - now
    if remain <= 0:
        return {"code": "due", "label": "已到期", "remain_ms": remain, "next_ms": next_ms}
    return {"code": "pending", "label": "待裁定", "remain_ms": remain, "next_ms": next_ms}


def _predict(source: WebSource, level: int, activity: float, kind: str) -> dict[str, Any]:
    """预计裁定结果：复用生命周期现成的 ``decide()``，不重写升降线判据。

    与真实裁定同一份代码，预测才不会漂。遗忘层 / 配置外档位显式交代，
    不假装能裁定。返回 {action, label, reason}。
    """
    if level <= 0:
        return {"action": "hold", "label": "遗忘层·不裁定", "reason": "遗忘层只能复活或删除"}
    dim = period_kind(kind)
    try:
        d = source.levels.decide(level, activity, dim)
    except ValueError:
        # 配置外的残留档位：decide 按契约抛 ValueError，这里转成可读的预测
        return {"action": "hold", "label": "配置外档位", "reason": "等级不在配置表里，裁定器拒绝"}
    if d.action == "up":
        label = f"预计升级 → L{d.level}"
    elif d.action == "down":
        label = f"预计降级 → L{d.level}"
    else:
        label = f"维持 L{level}"
    return {"action": d.action, "label": label, "reason": d.reason}


def get(source: WebSource) -> dict[str, Any]:
    now = int(time.time() * 1000)
    nodes: list[dict[str, Any]] = []
    conn = source.connect()
    try:
        for table, kind in (("knowledge", "experience"), ("episodes", "episode")):
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
                sel = ["id", "content", "forget_level", "activity", "period_start"]
                sel = [c for c in sel if c in cols]
                rows = conn.execute(  # noqa: S608
                    f"SELECT {', '.join(sel)} FROM {table} WHERE archived_at IS NULL"
                    if "archived_at" in cols else f"SELECT {', '.join(sel)} FROM {table}"
                ).fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                keys = set(r.keys())
                level = int(r["forget_level"] or 0) if "forget_level" in keys else 0
                activity = float(r["activity"] or 0.0) if "activity" in keys else 0.0
                start = r["period_start"] if "period_start" in keys else None
                state = _due_state(level, start, kind, source, now)
                spec = source.levels.spec(level)
                nodes.append(
                    {
                        "id": r["id"],
                        "kind": kind,
                        "level": level,
                        "activity": round(activity, 2),
                        "content": str(r["content"] or "").replace("\n", " ")[:80],
                        "period_start": start,
                        "next_judge_ms": state["next_ms"],
                        "remain_ms": state["remain_ms"],
                        "due_code": state["code"],
                        "due_label": state["label"],
                        "up": spec.up_threshold if spec else None,
                        "down": spec.down_threshold if spec else None,
                        "period_days": spec.period_days(period_kind(kind)) if spec else None,
                        "predict": _predict(source, level, activity, kind),
                    }
                )
    finally:
        conn.close()

    nodes.sort(key=lambda n: (n["level"], n["activity"]), reverse=True)
    return {
        "ok": True,
        "nodes": nodes,
        "meta": {"levels": source.cfg.forget_levels, "now": now},
    }
