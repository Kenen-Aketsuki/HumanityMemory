"""复盘闸门（`memory_search` 的放行条件）。

这层的存在理由是活跃度：`search` 刻意不改访问强度，`recall` 会强化命中项。
日常拿 search 顶替 recall，真实使用就不计入活跃度，遗忘曲线随之失真。
所以日常必须被逼到 recall 上，search 只在盘点时开。

测试要钉住的四件事：

1. 窗口解析：跨午夜（`22:30-00:30`）不能算错一小时，边界左闭右开；
2. 通行证：过期即视为不存在，损坏不抛异常；
3. `state` 的优先级：有效 flag 覆盖窗口，窗口外无 flag 就关；
4. **非 TTY 不许签**：这是「模型不能自己给自己开门」的物理依据。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from humanity_memory import Config, gate, resolve_layout


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    home = tmp_path / "home"
    home.mkdir()
    return Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})


# ---------------------------------------------------------------- 窗口解析


def test_parse_window_basic_and_cross_midnight():
    assert gate.parse_window("22:30-00:30") == (1350, 30)
    assert gate.parse_window("09:00-18:00") == (540, 1080)
    # 空串 = 不启用时间窗，只认手签通行证
    assert gate.parse_window("") == (-1, -1)
    assert gate.parse_window(None) == (-1, -1)


@pytest.mark.parametrize("bad", ["22:30", "25:00-01:00", "aa:bb-01:00", "24:30-01:00"])
def test_parse_window_rejects_garbage(bad):
    with pytest.raises(gate.GateError):
        gate.parse_window(bad)


def test_in_window_normal():
    window = gate.parse_window("09:00-18:00")
    assert gate.in_window(window, 540) is True  # 左闭
    assert gate.in_window(window, 1079) is True
    assert gate.in_window(window, 1080) is False  # 右开
    assert gate.in_window(window, 480) is False


def test_in_window_cross_midnight():
    window = gate.parse_window("22:30-00:30")
    assert gate.in_window(window, 1350) is True  # 22:30 整
    assert gate.in_window(window, 1439) is True  # 23:59
    assert gate.in_window(window, 0) is True  # 00:00
    assert gate.in_window(window, 29) is True
    assert gate.in_window(window, 30) is False  # 00:30 关
    assert gate.in_window(window, 720) is False  # 中午


def test_in_window_disabled_is_always_closed():
    assert gate.in_window((-1, -1), 1380) is False


def test_window_close_at_cross_midnight():
    window = gate.parse_window("22:30-00:30")
    now = datetime(2026, 11, 20, 23, 0)
    assert gate.window_close_at(window, now) == datetime(2026, 11, 21, 0, 30)


def test_next_open_at_rolls_to_tomorrow():
    window = gate.parse_window("22:30-00:30")
    noon = datetime(2026, 11, 20, 12, 0)
    assert gate.next_open_at(window, noon) == datetime(2026, 11, 20, 22, 30)
    late = datetime(2026, 11, 21, 3, 0)
    assert gate.next_open_at(window, late) == datetime(2026, 11, 21, 22, 30)
    assert gate.next_open_at((-1, -1), noon) is None


# ---------------------------------------------------------------- 通行证


def test_flag_lifecycle(cfg: Config):
    assert gate.read_flag(cfg.home) is None

    payload = gate.open_flag(cfg.home, 30, reason="每日复盘")
    assert payload["expires_at"] > payload["opened_at"]

    back = gate.read_flag(cfg.home)
    assert back is not None
    assert back["reason"] == "每日复盘"

    assert gate.close_flag(cfg.home) is True
    assert gate.read_flag(cfg.home) is None
    # 幂等：本来就不存在也算成功
    assert gate.close_flag(cfg.home) is True


def test_expired_flag_is_invisible(cfg: Config):
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    gate.flag_path(cfg.home).write_text(
        json.dumps({"expires_at": past, "opened_at": past}), encoding="utf-8"
    )
    assert gate.read_flag(cfg.home) is None


def test_corrupt_flag_is_invisible_not_fatal(cfg: Config):
    gate.flag_path(cfg.home).write_text("not json at all", encoding="utf-8")
    assert gate.read_flag(cfg.home) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("30", 30), ("30m", 30), ("2h", 120), (45, 45), (None, 30), ("", 30)],
)
def test_parse_minutes(raw, expected):
    assert gate.parse_minutes(raw, 30) == expected


def test_parse_minutes_rejects_garbage():
    with pytest.raises(gate.GateError):
        gate.parse_minutes("半小时", 30)


# ---------------------------------------------------------------- state


def test_default_window_is_early_morning(cfg: Config):
    """默认窗口钉在 00:00-08:00：复盘在次日凌晨做，盘的是前一天。"""
    assert cfg.review_window == "00:00-08:00"
    assert gate.DEFAULT_WINDOW == cfg.review_window


def test_state_closed_outside_window(cfg: Config):
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    state = gate.state(cfg, now=noon)
    # 12:00 不在 00:00-08:00 内，且没有 flag
    assert state.open is False
    assert state.source == "closed"
    assert state.next_open is not None
    assert "memory_recall" in state.message()


def test_state_open_inside_window(cfg: Config):
    night = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0)
    state = gate.state(cfg, now=night)
    assert state.open is True
    assert state.source == "window"
    assert state.expires_at is not None


def test_flag_overrides_window(cfg: Config):
    """白天没有窗口，但手签通行证能开 —— 这正是「我想现在翻旧账」的路径。"""
    gate.open_flag(cfg.home, 30, reason="手动补复盘")
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    state = gate.state(cfg, now=noon)
    assert state.open is True
    assert state.source == "flag"


def test_broken_window_degrades_to_flag_only(cfg: Config, monkeypatch):
    """配置写错不能让检索彻底瘫痪，也不能默默全开。"""
    monkeypatch.setattr(type(cfg), "review_window", property(lambda self: "乱写的"))
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    assert gate.state(cfg, now=noon).open is False
    gate.open_flag(cfg.home, 10)
    assert gate.state(cfg, now=noon).open is True


# ---------------------------------------------------------------- 审计


def test_audit_appends_jsonl(cfg: Config):
    gate.audit(cfg.home, "search_blocked", query="x")
    log = cfg.home / "diagnostics" / "review.log"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["event"] == "search_blocked"


# ---------------------------------------------------------------- 谁有资格开门


def test_begin_requires_tty(cfg: Config, tmp_path: Path, monkeypatch):
    """管道 stdin（脚本 / agent / 定时任务）没有签发资格。"""
    from humanity_memory import cli

    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    layout = resolve_layout(proj, home=cfg.home)

    monkeypatch.setenv("HM_HOME", str(cfg.home))
    monkeypatch.setattr(cli, "_layout", lambda args=None: layout)
    monkeypatch.setattr(gate, "is_interactive", lambda: False)

    args = cli.argparse.Namespace(action="begin", minutes=None, reason=None, json=False)
    assert cli.cmd_review(args) == 4
    assert gate.read_flag(cfg.home) is None  # 没签出来
    log = (cfg.home / "diagnostics" / "review.log").read_text(encoding="utf-8")
    assert "review_begin_denied" in log


def test_begin_works_in_tty(cfg: Config, tmp_path: Path, monkeypatch):
    from humanity_memory import cli

    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    layout = resolve_layout(proj, home=cfg.home)

    monkeypatch.setenv("HM_HOME", str(cfg.home))
    monkeypatch.setattr(cli, "_layout", lambda args=None: layout)
    monkeypatch.setattr(gate, "is_interactive", lambda: True)

    args = cli.argparse.Namespace(action="begin", minutes="5m", reason="t", json=False)
    assert cli.cmd_review(args) == 0
    flag = gate.read_flag(cfg.home)
    assert flag is not None
    assert flag["minutes"] == 5


# ---------------------------------------------------------------- 加固：配额与总闸


def test_allow_flag_off_ignores_existing_flag(cfg: Config, monkeypatch):
    """关掉手签通道后，连已经存在的通行证也不再认。"""
    gate.open_flag(cfg.home, 30)
    monkeypatch.setattr(type(cfg), "review_allow_flag", property(lambda self: False))
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    assert gate.state(cfg, now=noon).open is False


def test_allow_flag_off_blocks_begin(cfg: Config, tmp_path: Path, monkeypatch):
    from humanity_memory import cli

    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    monkeypatch.setenv("HM_HOME", str(cfg.home))
    monkeypatch.setattr(cli, "_layout", lambda args=None: resolve_layout(proj, home=cfg.home))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(type(cfg), "review_allow_flag", property(lambda self: False))

    args = cli.argparse.Namespace(action="begin", minutes=None, reason=None, json=False)
    assert cli.cmd_review(args) == 4
    assert gate.read_flag(cfg.home) is None


def test_daily_limit_is_a_ceiling(cfg: Config, tmp_path: Path, monkeypatch):
    """TTY 判据就算被绕过，一天也只有这么多张 —— 滥用有天花板。"""
    from humanity_memory import cli

    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    monkeypatch.setenv("HM_HOME", str(cfg.home))
    monkeypatch.setattr(cli, "_layout", lambda args=None: resolve_layout(proj, home=cfg.home))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)

    limit = cfg.review_daily_limit
    for _ in range(limit):
        gate.audit(cfg.home, "review_begin", minutes=30)
    assert gate.count_flag_today(cfg.home) == limit

    args = cli.argparse.Namespace(action="begin", minutes=None, reason=None, json=False)
    assert cli.cmd_review(args) == 4
    assert gate.read_flag(cfg.home) is None

    log = (cfg.home / "diagnostics" / "review.log").read_text(encoding="utf-8")
    assert "daily-limit" in log


def test_audit_carries_process_fingerprint(cfg: Config):
    """留痕要能回答「这笔是终端还是自动化进程」——至少留 pid/ppid。"""
    gate.audit(cfg.home, "review_begin", minutes=30)
    line = (cfg.home / "diagnostics" / "review.log").read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    assert isinstance(record["pid"], int)
    assert isinstance(record["ppid"], int)
