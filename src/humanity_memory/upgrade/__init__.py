"""历史库升级器 —— 离线路径，CLI 专用，不注册 MCP tool、不对 AI 暴露（§8.4）。

和 `starmap` 是同一类东西：**离线、只认文件路径、产物单一**。
只把星图那条「绝不写库」改成「绝不碰源库」。

五条边界
--------
1. 只在 CLI 用，不进 MCP 工具表 —— 升级不可逆，不给模型手。
2. 零侵入：不 import `db` / `store` / `models` / `lifecycle`，也不依赖 `Config`。
   旧结构一律由各转换器自带字面量快照。
3. 源库只读（`mode=ro`），不 rename、不就地改。失败直接重跑，不需要回滚。
   就地替换是另一层的事（`inplace.py`），它只在本模块的校验全过之后才动文件；
   本模块任何时候都不碰源库 —— 这两条不冲突，别把它们合成一层。
4. `--out` 不给默认值：给了才写，不给只报。默认行为是「预览会变成什么样」。
5. 一个跨版本一个模块，runner 按链依次调用。

链式升级怎么串
--------------
`CHAIN[起始版本] → 转换器`，每个转换器只认「自己的源版本 → 自己的目标版本」。
v3 的库要走到 v5，就跑 `v3_to_v4` 再跑 `v4_to_v5`：中间产物落在临时目录，
只作为下一步的只读输入，最后一步才写用户指定的 `--out`。
**每一段都自己核自己的三件事** —— 拿 v3 的 `knowledge` 条数去对 v5 的 `experience`
条数没有意义（case 是跨表搬进来的），校验口径必须逐段随映射走。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from . import v3_to_v4, v4_to_v5

# 版本号 → 单步转换器。加版本时在这里挂一格即可，不要写 N×N 的两两转换。
CHAIN: dict[int, Any] = {
    3: v3_to_v4,
    4: v4_to_v5,
}

LATEST_VERSION = v4_to_v5.TARGET_VERSION

# 识别源库结构的表名（与 db.LEGACY_TABLES 是同一份知识，但故意各写一份：
# 升级器不认识主程序的常量，主程序也不认识升级器的）
_LEGACY_MARKERS = ("facts",)
_CANDIDATE_NAMES = ("workspace.db", "global.db", "memory.db")


class UpgradeError(RuntimeError):
    """升级前置条件不满足：路径不对、版本不认识、产物已存在等。"""


# ---------------------------------------------------------------- 路径


def resolve_db_path(raw: str | Path) -> Path:
    """接受 `.ai_memory/`、项目根、或直接给 `.db` 文件 —— 拖进去就行。"""
    path = Path(raw).expanduser()
    if not path.exists():
        raise UpgradeError(f"路径不存在：{path}")
    if path.is_file():
        return path
    if not path.is_dir():
        raise UpgradeError(f"不是可识别的路径：{path}")

    # 目录本身是 .ai_memory
    direct = path / "workspace.db"
    if direct.is_file():
        return direct
    nested = path / ".ai_memory" / "workspace.db"
    if nested.is_file():
        return nested
    for name in _CANDIDATE_NAMES:
        candidate = path / name
        if candidate.is_file():
            return candidate
    raise UpgradeError(
        f"目录里找不到记忆库：{path}\n"
        "  期望路径：<项目根>/.ai_memory/workspace.db 或 <目录>/workspace.db"
    )


# ---------------------------------------------------------------- 探测


def detect_version(conn: sqlite3.Connection) -> str:
    try:
        names = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise UpgradeError(f"不是有效的 SQLite 库：{exc}") from exc

    if not names:
        raise UpgradeError("库是空的（没有任何表），没有可升级的内容")
    if "meta" in names:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row and row[0] is not None:
            return str(row[0])
        return "unknown"
    if any(marker in names for marker in _LEGACY_MARKERS):
        return "2"
    return "unknown"


def _load_vec(conn: sqlite3.Connection) -> str:
    """给连接装上 sqlite-vec —— 读写 vec0 虚拟表必须先加载扩展。

    这里不走 `db.load_vec_extension`（边界 2），自己写一份最小实现。
    """
    try:
        import sqlite_vec
    except ImportError as exc:
        raise UpgradeError(
            "缺少 sqlite-vec，无法处理向量表。请先安装：pip install sqlite-vec"
        ) from exc
    conn.enable_load_extension(True)
    try:
        path = sqlite_vec.loadable_path()
        conn.load_extension(path)
    finally:
        conn.enable_load_extension(False)
    return str(path)


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 主流程


def run(source: str | Path, out: str | Path | None = None) -> dict[str, Any]:
    """预览或执行一次升级。返回可直接 `json.dumps` 的报告。

    `out is None` 时源库一个字节不动，只报告会变成什么样。
    """
    src_path = resolve_db_path(source)
    report: dict[str, Any] = {
        "source": str(src_path),
        "source_size": src_path.stat().st_size,
        "out": str(out) if out else None,
        "target_version": LATEST_VERSION,
        "ok": True,
    }

    src = _connect_ro(src_path)
    try:
        version = detect_version(src)
        report["source_version"] = version
        if version == str(LATEST_VERSION):
            report.update(
                ok=False,
                reason=f"已经是 v{LATEST_VERSION}，无需升级",
            )
            return report
        try:
            version_no = int(version)
        except ValueError:
            raise UpgradeError(
                f"认不出这个库的 schema 版本（读到 {version!r}）。\n"
                "  升级器只处理有明确 meta.schema_version 的库；\n"
                "  用途不明或结构损坏的文件请人工确认后再处理。"
            ) from None
        if version_no not in CHAIN:
            raise UpgradeError(
                f"没有从 v{version_no} 出发的升级路径。"
                f"当前支持的起点：{', '.join(f'v{v}' for v in sorted(CHAIN))}"
            )
        if version_no > LATEST_VERSION:
            raise UpgradeError(
                f"源库是 v{version_no}，比当前程序认识的 v{LATEST_VERSION} 更新。"
                "请升级程序本身，不要降级库。"
            )

        _load_vec(src)

        steps: list[str] = []
        cursor_version = version_no
        while cursor_version < LATEST_VERSION:
            step = CHAIN.get(cursor_version)
            if step is None:
                raise UpgradeError(f"升级链在 v{cursor_version} 断了")
            steps.append(f"v{cursor_version} → v{step.TARGET_VERSION}")
            cursor_version = step.TARGET_VERSION
        report["chain"] = steps

        if out is None:
            # 只预览第一段：第二段的输入是第一段的产物，那文件现在还不存在。
            # 真跑的时候每段都会拿到自己那份 real input 再做 survey。
            report["plans"] = [
                {
                    "from": version_no,
                    "to": CHAIN[version_no].TARGET_VERSION,
                    "plan": CHAIN[version_no].survey(src),
                }
            ]
            report["plan"] = report["plans"][0]["plan"]
            report["dry_run"] = True
            report["next"] = (
                "预览完成，源库未改动。确认后加 --out <新库路径> 才会真正产出文件。"
            )
            return report

        target = Path(out).expanduser()
        if target.exists():
            raise UpgradeError(
                f"产物已存在，拒绝覆盖：{target}\n"
                "  升级器的产物要么是全新文件、要么请你自己先删/改名 —— 不做静默覆盖。"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        report["out"] = str(target)
        report["written"] = _write_target(
            src_path, target, version=version_no, report=report
        )
    finally:
        src.close()

    return report


def _write_target(
    src_path: Path, target: Path, *, version: int, report: dict[str, Any]
) -> dict[str, Any]:
    """按链逐段转换，最后一段写 `target`，中间产物落在临时目录。"""
    started = int(time.time() * 1000)
    tmpdir = Path(tempfile.mkdtemp(prefix="hm-upgrade-"))
    plans: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    written: dict[str, Any] = {}
    all_ok = True

    try:
        cur_path = src_path
        cur: sqlite3.Connection | None = _connect_ro(cur_path)
        _load_vec(cur)
        try:
            cursor = version
            while cursor < LATEST_VERSION:
                step = CHAIN[cursor]
                last = step.TARGET_VERSION == LATEST_VERSION
                dst_path = (
                    target
                    if last
                    else tmpdir / f"v{cursor}-to-v{step.TARGET_VERSION}.db"
                )
                plans.append(
                    {
                        "from": cursor,
                        "to": step.TARGET_VERSION,
                        "plan": step.survey(cur),
                    }
                )
                step_written, checks = _run_step(
                    step, cur, dst_path, started=started
                )
                all_ok = all_ok and bool(checks.get("all_ok"))
                details.append(
                    {
                        "from": cursor,
                        "to": step.TARGET_VERSION,
                        "out": None if last else dst_path.name,
                        "written": step_written,
                        "checks": checks,
                    }
                )
                written = step_written
                cur.close()
                if last:
                    cur = None
                    break
                cur = _connect_ro(dst_path)
                _load_vec(cur)
                cursor = step.TARGET_VERSION
        finally:
            if cur is not None:
                cur.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    report["plans"] = plans
    report["plan"] = plans[0]["plan"] if plans else {}
    report["steps"] = details
    # 报告正面那份 checks 取**第一段没过的**：红了要指到出问题的那一段，
    # 而不是拿最后一段的全绿把它盖住。
    first_bad = next(
        (d["checks"] for d in details if not d["checks"].get("all_ok")), None
    )
    merged: dict[str, Any] = dict(first_bad or (details[-1]["checks"] if details else {}))
    merged["all_ok"] = all_ok
    merged["per_step"] = [d["checks"].get("all_ok") for d in details]
    report["checks"] = merged
    if not all_ok:
        report["ok"] = False
        report["reason"] = "迁移完成但校验未全过，请先不要替换原库"
    return written


def _run_step(
    step: Any,
    src: sqlite3.Connection,
    dst_path: Path,
    *,
    started: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一段转换 + 核这一段的三件事。"""
    dst = sqlite3.connect(str(dst_path), isolation_level=None)
    dst.row_factory = sqlite3.Row
    try:
        dst.execute("PRAGMA journal_mode = WAL")
        dst.execute("PRAGMA synchronous = NORMAL")
        dst.execute("PRAGMA foreign_keys = ON")
        _load_vec(dst)

        tokenizer = step.probe_tokenizer(dst)
        written = step.build(src, dst, tokenizer=tokenizer, period_start=started)
        # WAL 折进主文件：下一段用 mode=ro 打开它，读不到未归并的尾巴会漏数据
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        checks = _verify(src, dst, spec=step.VERIFY, target=dst_path)
        return written, checks
    finally:
        dst.close()


# ---------------------------------------------------------------- 校验


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(f"SELECT rowid AS __rowid, * FROM {table}")
    except sqlite3.DatabaseError:
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, raw)) for raw in cur.fetchall()]


def _src_count(
    conn: sqlite3.Connection, table: str, where: tuple[str, Any] | None
) -> int:
    if where is None:
        return _count(conn, table)
    col, val = where
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (val,)
            ).fetchone()[0]
        )
    except sqlite3.DatabaseError:
        return 0


def _where_label(where: tuple[str, Any] | None) -> str:
    return f" WHERE {where[0]}='{where[1]}'" if where else ""


def _verify(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    spec: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    """§8.4「迁移完必须核三件事」，逐条给出可读结果。

    口径由每段转换器自带的 `VERIFY` 声明 —— 跨表搬过的步骤（v4→v5 的 case）
    没法用「同表名比条数」这一条公式套。
    """
    checks: dict[str, Any] = {}

    # ---- 1. 条数对齐：目标表的期望值 = 若干源表（可带过滤）之和 ----
    counts: dict[str, Any] = {}
    ok_counts = True
    for dst_table, sources in spec.get("counts", ()):
        parts = {f"{t}{_where_label(w)}": _src_count(src, t, w) for t, w in sources}
        expected = sum(parts.values())
        actual = _count(dst, dst_table)
        entry: dict[str, Any] = {
            "before": expected,
            "after": actual,
            "ok": expected == actual,
        }
        if len(parts) > 1:
            entry["sources"] = parts
        counts[dst_table] = entry
        ok_counts = ok_counts and expected == actual
    checks["counts"] = counts
    checks["counts_ok"] = ok_counts

    # ---- 2. at / created_at 未漂移（全量比对，不只是抽 10 条）----
    drift: dict[str, Any] = {}
    ok_time = True
    for dst_table, dst_field, sources in spec.get("timestamps", ()):
        before: dict[str, Any] = {}
        for src_table, src_field, where in sources:
            for row in _rows(src, src_table):
                if where and row.get(where[0]) != where[1]:
                    continue
                before[row["id"]] = row.get(src_field)
        after = {r["id"]: r.get(dst_field) for r in _rows(dst, dst_table)}
        mismatched = [k for k in before if k in after and before[k] != after[k]]
        missing = [k for k in before if k not in after]
        drift[f"{dst_table}.{dst_field}"] = {
            "field": dst_field,
            "compared": len(before),
            "mismatched": mismatched[:10],
            "missing": missing[:10],
            "ok": not mismatched and not missing,
        }
        ok_time = ok_time and not mismatched and not missing
    checks["timestamps"] = drift
    checks["timestamps_ok"] = ok_time

    # ---- 3. 向量可搜（抽查 3 条能否检索到自己）----
    checks["vectors"] = _verify_vectors(
        dst, prefixes=spec.get("vectors", ()), target=target
    )
    checks["all_ok"] = ok_counts and ok_time and checks["vectors"]["ok"]
    if spec.get("notes"):
        checks["notes"] = list(spec["notes"])
    return checks


def _verify_vectors(
    dst: sqlite3.Connection, *, prefixes: tuple[str, ...], target: Path
) -> dict[str, Any]:
    """向量是 blob，字段搬过去了不等于可检索：维度不匹配会静默返回空结果。"""
    out: dict[str, Any] = {"ok": True, "probes": []}
    for table in prefixes:
        vec_table = f"{table}_vec"
        try:
            rows = dst.execute(
                f"SELECT rowid, embedding FROM {vec_table} LIMIT 3"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            out["probes"].append({"table": vec_table, "error": str(exc)})
            out["ok"] = False
            continue
        if not rows:
            out["probes"].append({"table": vec_table, "skipped": "没有向量"})
            continue
        for rowid, blob in rows:
            try:
                hit = dst.execute(
                    f"SELECT rowid FROM {vec_table} WHERE embedding MATCH ? AND k = 3",
                    (blob,),
                ).fetchall()
                found = [int(r[0]) for r in hit]
            except sqlite3.DatabaseError as exc:
                out["probes"].append(
                    {"table": vec_table, "rowid": rowid, "error": str(exc)}
                )
                out["ok"] = False
                continue
            ok = int(rowid) in found
            out["probes"].append(
                {"table": vec_table, "rowid": int(rowid), "found": found, "ok": ok}
            )
            out["ok"] = out["ok"] and ok
    out["target"] = str(target)
    return out
