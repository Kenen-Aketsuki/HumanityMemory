"""把运行态从「v3 exe + v3 库」切到「v4 源码 + v4 库」。

为什么需要单独一个脚本、且必须人工在客户端关闭后跑
--------------------------------------------------
v0.2.0 / v0.3.0 / v0.3.1 三个已发布 exe 里编的全是 schema v3 的代码。
它们最大的危险不是"读不了 v4 库"，而是 `ensure_schema` 里的
`set_meta(..., "schema_version", "3")`：碰到 v4 库时它不会报错，
而是把版本号静默改回 3，把库变成「结构是 v4、标签写 v3」的废库。

所以顺序只能是：

    关客户端  →  替换库文件  →  撤 current  →  mcp.json 指回源码态  →  重开

而「替换库文件」在 Windows 上要求没有进程开着那个文件，
这件事只有关掉客户端之后才成立 —— 这就是它必须人工跑的原因。

用法
----
    python tools/switch_to_v4.py              # 干跑：只检查、只报告
    python tools/switch_to_v4.py --apply      # 真做
    python tools/switch_to_v4.py --revert     # 换回 v3 库（旧 exe 立即可用）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS_DIR = ROOT / ".ai_memory"
HOME = Path.home()
GLOBAL_DIR = HOME / ".humanity-memory"
MCP_JSON = HOME / ".workbuddy" / "mcp.json"
ENTRY = "humanity-memory"
VENV_ENTRY = ROOT / ".venv" / "Scripts" / "humanity-memory.exe"

DEFAULT_RELEASE = Path(os.environ.get("LOCALAPPDATA", str(HOME))) / "HumanityMemory" / "release"
RELEASE = Path(os.environ.get("HM_RELEASE_ROOT", str(DEFAULT_RELEASE)))
CURRENT_LINK = RELEASE / "current"

TAG = time.strftime("%Y%m%d-%H%M%S")

# 需要替换的库：(描述, 目录, 文件名)
PAIRS = (
    ("项目库", WS_DIR, "workspace.db"),
    ("全局库", GLOBAL_DIR, "global.db"),
)


def _fail(message: str) -> None:
    print(f"✗ {message}", file=sys.stderr)
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"✓ {message}")


def _is_locked(path: Path) -> bool:
    """用「改名再改回」探测占用 —— 纯 read 探测在 Windows 上不可靠。"""
    probe = path.with_name(path.name + ".lockprobe")
    try:
        path.rename(probe)
    except OSError:
        return True
    try:
        probe.rename(path)
    except OSError:  # pragma: no cover - 极端情况：改得回不去
        _fail(f"{path} 探测后无法复原，请手工把 {probe.name} 改回 {path.name}")
    return False


def _clear_sidecars(db: Path) -> None:
    """库文件换掉后，旧的 -wal / -shm 必须一起清掉，否则会挂到新文件上。"""
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            side.unlink()


def plan() -> list[str]:
    """干跑：逐项报告能不能切、拿什么切。"""
    lines: list[str] = []
    ready = True

    for label, directory, name in PAIRS:
        old = directory / name
        new = directory / f"{name[:-3]}.v4.db"
        if not old.is_file():
            lines.append(f"! {label}：不存在 {old}")
            ready = False
            continue
        if not new.is_file():
            lines.append(f"! {label}：缺升级产物 {new.name}（先跑 upgrade --out）")
            ready = False
            continue
        locked = _is_locked(old)
        lines.append(
            f"{'✗' if locked else '✓'} {label}：{name} "
            f"({old.stat().st_size/1024/1024:.1f} MB) ← {new.name} "
            f"({new.stat().st_size/1024/1024:.1f} MB)"
            + ("  【被进程占用，先关客户端】" if locked else "  可替换")
        )
        ready = ready and not locked

    if CURRENT_LINK.exists():
        lines.append(f"✓ 发布闸门：会撤掉 {CURRENT_LINK}")
    else:
        lines.append("· 发布闸门：current 不存在（无需撤）")

    if MCP_JSON.is_file():
        try:
            data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
            entry = (data.get("mcpServers") or {}).get(ENTRY) or {}
            lines.append(
                f"✓ MCP 入口：{entry.get('command', '(缺)')}\n"
                f"          → {VENV_ENTRY}"
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"! MCP 配置读不了：{exc}")
            ready = False
    else:
        lines.append(f"! 找不到 {MCP_JSON}")
        ready = False

    if not VENV_ENTRY.is_file():
        lines.append(f"! 源码态入口不存在：{VENV_ENTRY}（先 pip install -e .）")
        ready = False

    lines.append("")
    lines.append("可以切换。" if ready else "还差条件，见上面 ✗ / ! 的项。")
    return lines


def apply() -> None:
    for label, directory, name in PAIRS:
        old = directory / name
        new = directory / f"{name[:-3]}.v4.db"
        if not old.is_file() or not new.is_file():
            _fail(f"{label}：缺 {old.name} 或 {new.name}")
        if _is_locked(old):
            _fail(f"{label} 正被占用 —— 请先关掉客户端（含所有 MCP 进程）再跑")
        archived = directory / f"{name}.v3-{TAG}"
        old.rename(archived)
        try:
            new.rename(old)
        except OSError:
            archived.rename(old)  # 换上失败要能退回原状
            raise
        _clear_sidecars(old)
        _ok(f"{label}：{name} 已换成 v4，原件留档 {archived.name}")

    if CURRENT_LINK.exists():
        # 只删 junction 本身，不带 /s：目标版本目录必须留着，回退时还要用
        result = subprocess.run(
            ["cmd", "/c", "rmdir", str(CURRENT_LINK)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _fail(f"撤 current 失败：{result.stderr.strip()}")
        _ok(f"发布闸门已下线：{CURRENT_LINK}")
    else:
        print("· 发布闸门本来就不在")

    _rewrite_mcp()
    _self_check()


def _rewrite_mcp() -> None:
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})
    old = servers.get(ENTRY) or {}
    args = [str(a) for a in old.get("args", [])]
    token = str(HOME / ".humanity-memory" / "active.token.json")
    for i, arg in enumerate(args):
        if arg == "--token" and i + 1 < len(args):
            token = args[i + 1]
    new = {
        "command": str(VENV_ENTRY),
        # --token 是全局参数，必须在子命令前；写成 `serve --token x` 会 unrecognized 退出
        "args": ["--token", token, "serve"],
    }
    if old.get("env"):
        new["env"] = old["env"]
    if old == new:
        print("· MCP 入口已是源码态，没改")
        return
    backup = MCP_JSON.with_name(MCP_JSON.name + f".bak-{TAG}")
    shutil.copy2(MCP_JSON, backup)
    servers[ENTRY] = new
    MCP_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _ok(f"MCP 入口已指回源码态（原配置留档 {backup.name}）")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """跑子进程并强制按 UTF-8 解码。

    `text=True` 在 Windows 上默认用系统代码页（GBK）解码，而 CLI 的
    `_setup_stdout` 会把 stdout 重配成 UTF-8 —— 于是自检自己抛
    `UnicodeDecodeError`，看上去像被检对象坏了，其实是读输出的姿势不对。
    """
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _self_check() -> None:
    print("\n自检：")
    for label, directory, name in PAIRS:
        db = directory / name
        proc = _run([str(VENV_ENTRY), "upgrade", str(db)])
        out = (proc.stdout or "") + (proc.stderr or "")
        if "已经是 v4" in out:
            _ok(f"{label} schema=4")
        else:
            print(f"✗ {label} 自检异常：{out.strip()[:400]}")

    proc = _run([str(VENV_ENTRY), "doctor"])
    print(f"\ndoctor 退出码 {proc.returncode}")
    print((proc.stdout or "")[-1200:])
    if proc.returncode != 0:
        print((proc.stderr or "")[-600:], file=sys.stderr)


def revert() -> None:
    stamps = sorted(
        (p for p in WS_DIR.glob("workspace.db.v3-*") if p.suffix != ".lockprobe"),
        key=lambda p: p.name,
        reverse=True,
    )
    if not stamps:
        _fail("找不到 workspace.db.v3-* 留档，无法回退")
    target = stamps[0]
    live = WS_DIR / "workspace.db"
    if _is_locked(live):
        _fail("当前库被占用，先关客户端")
    v4_keep = WS_DIR / f"workspace.db.v4-{TAG}"
    live.rename(v4_keep)
    target.rename(live)
    _clear_sidecars(live)
    _ok(f"项目库已换回 v3：{target.name} → workspace.db（v4 留档 {v4_keep.name}）")

    g_stamps = sorted(
        GLOBAL_DIR.glob("global.db.v3-*"), key=lambda p: p.name, reverse=True
    )
    if g_stamps:
        g_live = GLOBAL_DIR / "global.db"
        if _is_locked(g_live):
            _fail("全局库被占用，先关客户端")
        g_keep = GLOBAL_DIR / f"global.db.v4-{TAG}"
        g_live.rename(g_keep)
        g_stamps[0].rename(g_live)
        _clear_sidecars(g_live)
        _ok(f"全局库已换回 v3：{g_stamps[0].name} → global.db")
    print("\n注意：current 已下线。要回到旧 exe，需要重建 junction 并改回 mcp.json。")
    print(f"  旧 exe 目录仍在：{RELEASE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="切到 v4（库 + 入口），或回退")
    parser.add_argument("--apply", action="store_true", help="真做；不加则只干跑")
    parser.add_argument("--revert", action="store_true", help="换回 v3 库")
    args = parser.parse_args(argv)

    if args.revert:
        revert()
        return 0

    lines = plan()
    print("\n".join(lines))
    if not args.apply:
        print("\n（干跑结束，没有改动任何文件。确认后加 --apply）")
        return 0

    print("\n开始切换……")
    apply()
    print("\n完成。现在可以重开客户端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
