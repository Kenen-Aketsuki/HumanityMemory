"""复盘闸门：`memory_search` 只在复盘场景开放。

## 为什么要这层

`memory_search` 刻意「不改访问强度」，而 `memory_recall` 会强化命中项。
日常若拿 search 顶替 recall，这些真实使用就一条都不会计入活跃度，
遗忘曲线随之失真 —— 记忆不老化，或者老化了但不是因为它真的没被用。

所以日常必须把检索逼到 recall 上，search 只在盘点（复盘）时开。

## 为什么是 gate 而不是靠模型自觉

放行判据全部是**代码能核验的事实**，模型自述不算数：

- 时间窗：本地时间落在 `[review].window` 内（默认 `00:00-08:00`）。自动化复盘
  天然落在这里，不需要任何人工动作。**这一条完全由代码算出来，不依赖「谁在调」。**
- 手签 flag：在终端跑 `review begin` 写下的 `review.flag`，带过期时间与每日配额。

## 诚实说明 TTY 的强度

`review begin` 会检查 stdin 是不是 TTY（`[review].require_tty`），用来挡住
「agent 在管道里顺手调一次」。但这是**弱信号**：实测同一台机器上，带伪终端
（ConPTY）跑长命令时 `isatty()` 会偶发返回 True。

真正兜底的从来不是它，而是三条不关心「谁在调」的约束：

1. 时间窗 —— 白天就是搜不了，除非有人改配置；
2. 每日配额 —— 就算签出来，一天也只有 `daily_flag_limit` 张、每张几十分钟；
3. 审计 —— 每次签发与被拒都记在 `diagnostics/review.log`，事后查得到。

要绝对硬就关掉整条手签通道：`[review].allow_flag = false`，只留时间窗。

## 边界

这层挡的是「顺手就搜」，不是蓄意对抗：模型若有 shell，仍可伪造伪终端
（`script`）、改配置、或直读 sqlite。前两者会留在审计与 diff 里，后者靠事后
核对。没有任何纯软件手段能区分「用户按的回车」和「agent 调用同一个 exe」——
除非引入用户独有的秘密或物理设备，那不属于这一层的目标。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DEFAULT_REVIEW_WINDOW, Config

#: flag 文件名，落在 `~/.humanity-memory/` 下（与 token / registry 同层）
FLAG_NAME = "review.flag"

#: 时间窗缺省值。00:00 开始是因为复盘在次日凌晨做：盘的是前一天，
#: 而前一晚的日志到这时才齐。真值在 config.DEFAULT_REVIEW_WINDOW，这里只是别名。
DEFAULT_WINDOW = DEFAULT_REVIEW_WINDOW


class GateError(ValueError):
    """窗口或时长写法不合法。"""


# ---------------------------------------------------------------- 时间窗


def _minutes(text: str) -> int:
    """'22:30' -> 1350（当天第几分钟）。'24:00' 允许，等价于 1440。"""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise GateError(f"时间格式应为 HH:MM，收到 {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise GateError(f"时间格式应为 HH:MM，收到 {text!r}") from exc
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
        raise GateError(f"时间超出范围：{text!r}")
    return hour * 60 + minute


def parse_window(text: str | None) -> tuple[int, int]:
    """`'00:00-08:00'` -> `(0, 480)`，单位是「当天第几分钟」。

    允许跨午夜（start > end，例如 `'22:30-00:30'`）。空字符串返回 `(-1, -1)`，
    表示只认手签 flag。
    """
    raw = (text or "").strip()
    if not raw:
        return (-1, -1)
    if "-" not in raw:
        raise GateError(f"窗口写法应为 start-end，收到 {raw!r}")
    left, right = raw.split("-", 1)
    return (_minutes(left), _minutes(right))


def in_window(window: tuple[int, int], now_minute: int) -> bool:
    """当前时刻是否落在窗口内。跨午夜时窗口 = `[start, 1440) ∪ [0, end)`。"""
    start, end = window
    if start < 0 or end < 0:
        return False
    if start == end:
        # 00:00-00:00 视为全天开放，显式写法，不是笔误
        return True
    if start < end:
        return start <= now_minute < end
    return now_minute >= start or now_minute < end


def _at(now: datetime, minute_of_day: int) -> datetime:
    """把「当天第几分钟」落到具体时刻。1440 归到次日 00:00。"""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(minutes=minute_of_day)


def next_open_at(window: tuple[int, int], now: datetime) -> datetime | None:
    """下一次窗口开启的时刻；窗口不启用时返回 None。"""
    start, end = window
    if start < 0 or end < 0:
        return None
    if in_window(window, now.hour * 60 + now.minute):
        return now
    candidate = _at(now, start)
    if candidate <= now:
        candidate = _at(now + timedelta(days=1), start)
    return candidate


def window_close_at(window: tuple[int, int], now: datetime) -> datetime | None:
    """当前这次窗口什么时候关。跨午夜时 end 落在次日。"""
    start, end = window
    if start < 0 or end < 0 or start == end:
        return None
    close = _at(now, end)
    if close <= now:
        close = _at(now + timedelta(days=1), end)
    return close


# ---------------------------------------------------------------- 手签 flag


def flag_path(home: Path | str) -> Path:
    return Path(home) / FLAG_NAME


def parse_minutes(text: str | int | None, default: int = 30) -> int:
    """`'45'` / `'45m'` / `'2h'` / 整数 -> 分钟数。"""
    if text is None:
        return default
    if isinstance(text, int):
        return max(1, text)
    raw = str(text).strip().lower()
    if not raw:
        return default
    try:
        if raw.endswith("h"):
            return max(1, int(float(raw[:-1]) * 60))
        if raw.endswith("m"):
            return max(1, int(float(raw[:-1])))
        return max(1, int(float(raw)))
    except ValueError as exc:
        raise GateError(f"时长写法无法解析：{text!r}（可用 30 / 30m / 2h）") from exc


def is_interactive() -> bool:
    """当前进程的 stdin 是不是真终端。

    **这是弱信号，别把它当成身份验证。** 实测：同一个 agent 运行环境里，
    带伪终端（ConPTY）跑长命令时 `isatty()` 会偶发返回 True。它能挡住
    「顺手在管道里调一次」，挡不住蓄意绕过。

    所以真正兜底的从来不是它，而是：时间窗（代码算出来的）、每日配额
    （审计日志数出来的）、审计留痕（跑没跑过一律查得到）。
    """
    try:
        return bool(sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def count_flag_today(home: Path | str) -> int:
    """今天已经签发过几张通行证。

    数的是审计日志，不另存计数器 —— 状态只有一个来源，不然两份迟早对不上。
    """
    path = Path(home) / "diagnostics" / "review.log"
    today = datetime.now().date().isoformat()
    count = 0
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") != "review_begin":
            continue
        if str(record.get("at", "")).startswith(today):
            count += 1
    return count


def open_flag(
    home: Path | str,
    minutes: int = 30,
    *,
    by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """签发一张限时通行证。到期自动失效，不需要 `review end`。"""
    now = datetime.now()
    payload = {
        "opened_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=max(1, minutes))).isoformat(
            timespec="seconds"
        ),
        "minutes": max(1, minutes),
        "by": by or os.getcwd(),
        "reason": reason or "",
    }
    path = flag_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def read_flag(home: Path | str) -> dict[str, Any] | None:
    """读通行证；不存在、损坏、已过期一律返回 None（过期即视为不存在）。"""
    path = flag_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        expires = datetime.fromisoformat(str(raw.get("expires_at")))
    except (TypeError, ValueError):
        return None
    if expires <= datetime.now():
        return None
    raw["expires_at"] = expires.isoformat(timespec="seconds")
    return raw


def close_flag(home: Path | str) -> bool:
    """提前收回通行证。本来就不存在也算成功（幂等）。"""
    try:
        flag_path(home).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 状态


@dataclass(slots=True)
class GateState:
    open: bool
    source: str  # window | flag | closed
    window: str
    expires_at: str | None = None
    next_open: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "source": self.source,
            "window": self.window,
            "expires_at": self.expires_at,
            "next_open": self.next_open,
            "reason": self.reason,
        }

    def message(self) -> str:
        """给模型/人看的一句话解释。"""
        if self.open and self.source == "flag":
            return f"复盘通行证有效，至 {self.expires_at}"
        if self.open:
            return f"处于复盘时间窗 {self.window}，至 {self.expires_at or '窗口结束'}"
        tail = f"下一次开放：{self.next_open}。" if self.next_open else ""
        return (
            f"memory_search 只在复盘时开放（时间窗 {self.window or '未启用'}）。{tail}"
            "日常请用 memory_recall —— 它会强化命中项、计入活跃度，"
            "search 不会。"
        )


def state(cfg: Config, *, now: datetime | None = None) -> GateState:
    """当前是否放行 search。flag 优先于窗口（手签可以覆盖窗口外）。"""
    now = now or datetime.now()
    window_text = cfg.review_window
    try:
        window = parse_window(window_text)
    except GateError:
        # 配置写错不能让检索彻底瘫痪：退化为「只认 flag」，并把错误说清楚
        window = (-1, -1)

    flag = read_flag(cfg.home) if cfg.review_allow_flag else None
    if flag is not None:
        return GateState(
            open=True,
            source="flag",
            window=window_text,
            expires_at=flag["expires_at"],
            reason=flag.get("reason") or "",
        )

    if in_window(window, now.hour * 60 + now.minute):
        close = window_close_at(window, now)
        return GateState(
            open=True,
            source="window",
            window=window_text,
            expires_at=close.isoformat(timespec="seconds") if close else None,
        )

    upcoming = next_open_at(window, now)
    return GateState(
        open=False,
        source="closed",
        window=window_text,
        next_open=upcoming.isoformat(timespec="seconds") if upcoming else None,
        reason=f"窗口 {window_text or '未启用'}，当前不在窗口内且无有效通行证",
    )


# ---------------------------------------------------------------- 审计


def audit(home: Path | str, event: str, **payload: Any) -> None:
    """记一笔。落在 `diagnostics/review.log`（JSONL，只追加）。

    刻意不进 `memory_events`：那张表的 table_name 有 CHECK 约束，只认三类记忆；
    而改 schema 会触发版本闸门，代价是用户手上所有旧库打不开。
    审计不是每条记忆的事件，放诊断目录更合适，也随时可以整文件删掉。
    """
    try:
        path = Path(home) / "diagnostics" / "review.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            # 进程指纹：事后能看出这一笔是终端里的 shell 还是某个自动化进程
            "pid": os.getpid(),
            "ppid": os.getppid(),
            **payload,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 审计失败绝不影响主流程
        pass
