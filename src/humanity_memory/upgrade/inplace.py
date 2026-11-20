"""就地升级：把「产出副本 → 校验 → 替换」合成一步，给拖拽 bat 用。

为什么单独一层
--------------
`upgrade/__init__.py` 的五条边界里，第 3 条写着「源库只读」，那条不能松 ——
转换器永远不碰源文件，失败直接重跑。但「拖一个库上去就升好」这个用法需要
最后那一步替换，所以把它单独放一层，规则也单独写清楚：

| 层 | 能做 | 不能做 |
| --- | --- | --- |
| `run()`（转换器） | 只读源库、产出新文件、自校验 | 改名、删除、就地改 |
| `run_inplace()`（本模块） | 在**校验全过之后**改名留档 + 顶上 | 在转换/校验通过前动源库一个字节 |

顺序不能换：先 `run()` 产出并核三件事，全过才换文件。任何一步失败都停在替换之前，
线上库原样，重跑即可 —— 和「只读」那条边界要保的性质一致：**失败不需要回滚**。

拖拽场景的两个额外要求
----------------------
1. **占着就别硬来**：Windows 上任何进程开着库文件，rename 必失败。先探一次，
   当场告诉用户"关掉客户端"，而不是让他等完整个转换再看到 OSError。
2. **WAL 先归并**：`run()` 按 `mode=ro` 读，读不到没折进主文件的 WAL 尾巴会漏数据；
   替换后旧 `-wal` / `-shm` 还必须清掉，否则会挂到新库上。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from . import LATEST_VERSION, UpgradeError, detect_version, resolve_db_path, run


class InplaceError(UpgradeError):
    """就地升级做不了（被占用 / 归并不全 / 替换失败）。"""


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _version_of(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return detect_version(conn)
    finally:
        conn.close()


def _ensure_not_locked(path: Path) -> None:
    """改名再改回 —— 纯读探测在 Windows 上问不出占用，改名才真能问出来。"""
    probe = path.with_name(path.name + ".lockprobe")
    try:
        path.rename(probe)
    except OSError:
        raise InplaceError(
            f"库正被进程占用，换不了文件：{path}\n"
            "  先关掉用着这个库的客户端（含 MCP server 进程），再拖一次。\n"
            "  升级没有开始，库文件一个字节都没动。"
        ) from None
    try:
        probe.rename(path)
    except OSError:  # pragma: no cover - 极端情况
        raise InplaceError(
            f"探测占用后改不回去：请手工把 {probe.name} 改回 {path.name}"
        ) from None


def _checkpoint(path: Path) -> None:
    """把 WAL 折进主文件；折不全就不往下走。"""
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise InplaceError(f"WAL 归并失败：{exc}\n  关掉客户端再试。") from None
    finally:
        conn.close()
    if row and row[0] != 0:
        raise InplaceError(
            f"WAL 归并不完整（busy={row[0]}）：还有进程在读写这个库。\n"
            "  关掉客户端再试 —— 现在换文件会丢掉未归并的那部分。"
        )


def _unique(base: Path) -> Path:
    """同名留档不覆盖：同一秒跑两次也不会互相吃掉。"""
    if not base.exists():
        return base
    for i in range(1, 100):
        candidate = base.with_name(f"{base.name}-{i}")
        if not candidate.exists():
            return candidate
    raise InplaceError(f"找不到可用的留档名：{base}")


def _seal_product(product: Path) -> None:
    """产物就位前确保 WAL 已清空。

    连接关闭时 SQLite 通常会 checkpoint 并删掉 -wal，但这是"通常"。
    万一残留，主文件改名走、侧车留下，那部分数据就凭空消失了 —— 所以显式封一次。
    """
    if not product.exists():
        return
    conn = sqlite3.connect(str(product), timeout=10.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _clear_sidecars(db: Path) -> None:
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


def run_inplace(source: str | Path) -> dict[str, Any]:
    """把源库就地升到最新版本，返回可直接 `json.dumps` 的报告。

    已经是当前版本 → `ok=True, noop=True`（拖错了不算失败）。
    """
    path = resolve_db_path(source)
    stamp = _stamp()
    report: dict[str, Any] = {
        "source": str(path),
        "in_place": True,
        "target_version": LATEST_VERSION,
        "ok": True,
    }

    version = _version_of(path)
    report["source_version"] = version
    if version == str(LATEST_VERSION):
        report.update(noop=True, reason=f"已经是 v{LATEST_VERSION}，无需升级")
        return report

    # 先探占用：宁可一句话拒绝，也别让用户等完转换才看到 OSError
    _ensure_not_locked(path)
    _checkpoint(path)

    product = _unique(path.with_name(f"{path.name}.v{LATEST_VERSION}-{stamp}.tmp"))
    converted = run(path, product)
    report.update(
        chain=converted.get("chain"),
        plan=converted.get("plan"),
        checks=converted.get("checks"),
        written=converted.get("written"),
    )

    checks = converted.get("checks") or {}
    if not converted.get("ok") or not checks.get("all_ok"):
        product.unlink(missing_ok=True)
        report.update(
            ok=False,
            reason=converted.get("reason") or "迁移完成但校验未全过，已丢弃产物，源库未改动",
        )
        return report

    _seal_product(product)

    # ---- 替换：原件留档，产物顶上；顶不上要能退回 ----
    # 留档名带**源库自己的版本号**，不是目标版本 —— 回退时一眼看出这是从哪升上来的
    archived = _unique(path.with_name(f"{path.name}.v{version}-{stamp}"))
    try:
        path.rename(archived)
    except OSError as exc:
        product.unlink(missing_ok=True)
        raise InplaceError(f"原件改名失败：{exc}\n  源库未改动。") from None
    try:
        product.rename(path)
    except OSError:
        archived.rename(path)  # 顶不上就把原件放回原位
        product.unlink(missing_ok=True)
        raise InplaceError("产物就位失败，已把原件放回原位。") from None
    _clear_sidecars(path)

    live = _version_of(path)
    if live != str(LATEST_VERSION):
        report.update(
            ok=False,
            archived=archived.name,
            reason=f"替换后版本读作 {live}，异常；原件在同目录 {archived.name}",
        )
        return report

    report.update(swapped=True, archived=archived.name, out=str(path))
    return report
