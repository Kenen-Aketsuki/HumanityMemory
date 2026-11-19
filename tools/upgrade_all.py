"""把机器上所有 v3 记忆库批量升到 v4 —— `upgrade.bat` 的批量版。

为什么需要批量
--------------
`mcp.json` 是**用户级**配置，所有项目共用同一个入口。一旦入口指向 v4 源码，
每个项目自己的 `.ai_memory/workspace.db` 都会被 `open_db` 的版本闸门拒绝
（`SchemaVersionError`，退出码 3）—— 记忆功能在该项目里直接不可用。
所以「切到 v4」不只是当前项目的事，是这台机器上**每一个**库的事。

和单库拖拽的关系
----------------
单个库走 `upgrade.bat`（→ `humanity_memory.upgrade.inplace.run_inplace`）。
本脚本只多做三件事：**找库、批量循环、汇总报告**；「转换 → 校验 → 留档 → 替换」
一个字都不重复实现，免得两份逻辑哪天走岔。

用法
----
    python tools/upgrade_all.py                          # 干跑：只探测、只报告
    python tools/upgrade_all.py --apply                  # 真做
    python tools/upgrade_all.py --apply --skip alpha
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import localenv

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
GLOBAL_DIR = HOME / ".humanity-memory"

# 默认扫描根：项目库都在 <根>/<项目>/.ai_memory/workspace.db
# 具体位置是本机配置（local.env 的 HM_SCAN_ROOTS，逗号分隔）；
# 未配置时退回项目所在目录，保证换台机器也能跑起来。
DEFAULT_ROOTS = tuple(Path(p) for p in localenv.get_list("HM_SCAN_ROOTS")) or (ROOT.parent,)

TARGET_VERSION = 4


def _ok(message: str) -> None:
    print(f"  ✓ {message}")


def _skip(message: str) -> None:
    print(f"  · {message}")


def _bad(message: str) -> None:
    print(f"  ✗ {message}")


# ---------------------------------------------------------------- 发现


def discover(roots: tuple[Path, ...]) -> list[Path]:
    """项目库 + 全局库。只认标准布局，不递归乱扫。"""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        # sorted 保证报告顺序稳定，出问题好对照
        for child in sorted(root.iterdir()):
            db = child / ".ai_memory" / "workspace.db"
            if db.is_file():
                found.append(db)
    global_db = GLOBAL_DIR / "global.db"
    if global_db.is_file():
        found.append(global_db)
    return found


# ---------------------------------------------------------------- 探测


def probe_version(path: Path) -> str:
    """只读探测版本号；认不出来就返回可读字符串，不抛。"""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"打不开({exc})"
    try:
        names = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not names:
            return "空库"
        if "meta" in names:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return str(row[0]) if row and row[0] is not None else "unknown"
        if "facts" in names:
            return "2"
        return "unknown"
    except sqlite3.DatabaseError as exc:
        return f"读不了({exc})"
    finally:
        conn.close()


def count_rows(path: Path) -> str:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "?"
    try:
        parts = []
        for table in ("knowledge", "episodes", "logs"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                parts.append(str(n))
            except sqlite3.DatabaseError:
                parts.append("-")
        return "/".join(parts)
    finally:
        conn.close()


# ---------------------------------------------------------------- 单库


def upgrade_one(db: Path, *, apply: bool) -> dict[str, object]:
    """返回结果字典；`status` ∈ done / skip / dry / blocked / failed。"""
    version = probe_version(db)
    result: dict[str, object] = {"db": db, "status": "?", "version": version}

    print(f"\n{db}")
    print(f"  版本 {version}  条数(知识/经历/日志) {count_rows(db)}")

    if version == str(TARGET_VERSION):
        _skip("已经是 v4，跳过")
        result["status"] = "skip"
        return result
    if not version.isdigit():
        _bad("版本认不出，人工确认后再处理（升级器不会猜结构）")
        result["status"] = "failed"
        return result
    if not apply:
        _skip("待升级（干跑，未改动）")
        result["status"] = "dry"
        return result

    from humanity_memory.upgrade.inplace import InplaceError, run_inplace

    try:
        report = run_inplace(db)
    except InplaceError as exc:
        # 占用是可恢复的（关客户端再跑），和真失败分开报
        text = str(exc)
        _bad(text.splitlines()[0])
        result["status"] = "blocked" if "占用" in text else "failed"
        return result
    except Exception as exc:  # noqa: BLE001 - 批量跑不能因为一个库中断整轮
        _bad(f"{type(exc).__name__}: {exc}")
        result["status"] = "failed"
        return result

    if report.get("noop"):
        _skip(str(report.get("reason")))
        result["status"] = "skip"
        return result
    if not report.get("ok"):
        _bad(str(report.get("reason")))
        result["status"] = "failed"
        return result

    _ok(f"已是 v4，原件留档 {report['archived']}")
    result["status"] = "done"
    result["archived"] = report["archived"]
    return result


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量把 v3 记忆库升到 v4")
    parser.add_argument("--apply", action="store_true", help="真做；不加只干跑")
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="项目库扫描根，可多次给；默认取 local.env 的 HM_SCAN_ROOTS（未配置则为项目所在目录）",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help="按路径片段跳过（如 --skip alpha）",
    )
    parser.add_argument("paths", nargs="*", help="只处理指定的库文件")
    args = parser.parse_args(argv)

    if args.paths:
        targets = [Path(p).expanduser() for p in args.paths]
    else:
        roots = tuple(Path(r) for r in (args.root or DEFAULT_ROOTS))
        targets = discover(roots)

    skips = [s.lower() for s in args.skip]
    if skips:
        kept = []
        for db in targets:
            hit = next((s for s in skips if s in str(db).lower()), None)
            if hit:
                print(f"跳过 {db}（--skip {hit}）")
            else:
                kept.append(db)
        targets = kept

    if not targets:
        print("没找到任何记忆库。")
        return 0

    print(f"共 {len(targets)} 个库，模式：{'执行' if args.apply else '干跑'}")
    results = [upgrade_one(db, apply=args.apply) for db in targets]

    print("\n" + "=" * 74)
    labels = {
        "done": "已升级",
        "dry": "待升级",
        "skip": "跳过",
        "blocked": "被占用",
        "failed": "失败",
    }
    for r in results:
        db = Path(str(r["db"]))
        print(f"{labels.get(str(r['status']), '?'):<6} {str(r['version']):>7}  {db}")

    pending = [r for r in results if r["status"] in ("blocked", "failed")]
    todo = [r for r in results if r["status"] == "dry"]
    if not args.apply and todo:
        print(f"\n{len(todo)} 个库待升级。确认后加 --apply。")
    elif args.apply:
        done = [r for r in results if r["status"] == "done"]
        print(f"\n已升级 {len(done)} 个。")
    if pending:
        print(f"还有 {len(pending)} 个没处理成，见上面的 ✗。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
