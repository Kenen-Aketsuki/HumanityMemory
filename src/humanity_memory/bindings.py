"""绑定层：把「登记库 + 签发令牌 + 退役旧状态」这套组合动作收在一处。

分工
----
- `keys.py` 只管 registry 的哈希与归属（钥匙是谁的、库在哪）；
- `token.py` 只管身份文件的读写（`<root>/.ai_memory/token.json`）；
- **本模块** 负责按正确顺序把它们组合起来，并保证「重复跑一次不会更糟」。

为什么不写在 CLI 里
------------------
`bind adopt` / `bind migrate --all` 都要做同一件事，只是来源不同（一个目录 vs 一次扫描）。
放在 CLI 里会出现两份实现，然后哪天只改了一份 —— 这类不一致最难查，
因为两边单跑都"成功"。CLI 与 MCP 的 `bind_*` 工具都应该走这里。

两条硬约束
----------
1. **顺序**：先登记库、再发令牌。反了就会出现「令牌指向 registry 里不存在的库」，
   那是拒绝态里最让人困惑的一种（看着像已接入，实际一个都读不到）。
2. **幂等**：令牌已经是 v2、钥匙在 registry、库与工作区都对得上 → 一律 `keep`，
   不重签。重签会让 registry 里多一条孤儿记录，也会让「跑第二次」变成一次风险动作。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import keys
from .paths import (
    WORKSPACE_DIR,
    Layout,
    find_workspace_root,
    global_home,
    resolve_layout,
)
from .token import Token, TokenError, build_token, same_path, token_path

WORKSPACE_DIR = ".ai_memory"
DB_NAME = "workspace.db"

# 扫描时直接跳过的目录：它们下面不可能有项目根，进去只会拖慢整轮迁移
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "Library",
    "Temp",
    "obj",
    "bin",
    "Build",
    "Builds",
    "Logs",
    "BackUp",
}

# 迁移时认的「有内容」表；缺表不算错（老库可能没有 logs / digests）
_COUNT_TABLES = ("knowledge", "episodes", "logs", "digests")


# ---------------------------------------------------------------- 命名


def slug(name: str) -> str:
    """目录名 → 库标签：小写 + 下划线，只留 ASCII。

    registry 的标签会出现在令牌与报错里，让人对着一个中文或带空格的标签
    去敲命令是自找麻烦。中文目录名会退化成 `library`，靠 `--label` 显式覆盖。
    """
    text = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()
    return text or "library"


# ---------------------------------------------------------------- 发现


def discover(roots: list[Path], *, depth: int = 3) -> list[Path]:
    """在给定目录下找 `<root>/.ai_memory`，返回工作区目录（不是项目根）。

    只读：不创建、不打开库。`depth` 是项目根相对扫描根的层数，
    默认 3（`D:/Wrok_proj` 这种「一堆工程平铺」的布局一层就够，
    留两层给 `work/2026/xxx` 那种按年份归档的习惯）。
    """
    found: list[Path] = []
    seen: set[str] = set()
    for base in roots:
        base = Path(os.path.expandvars(str(base))).expanduser()
        if not base.is_dir():
            continue
        base_depth = len(base.resolve().parts)
        for dirpath, dirnames, _ in os.walk(base):
            here = Path(dirpath)
            # 先认项目根，再决定要不要往下走 —— 顺序反了会把 `.ai_memory`
            # 从 dirnames 里清掉，然后永远认不出这一层
            cand = here / WORKSPACE_DIR
            if cand.is_dir():
                key = os.path.normcase(str(cand.resolve()))
                if key not in seen:
                    seen.add(key)
                    found.append(cand)
                dirnames.remove(WORKSPACE_DIR)
            if len(here.resolve().parts) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
    return sorted(found)


def _counts(db: Path) -> dict[str, int | str]:
    """只读统计。库可能正被 MCP 进程打开（WAL），`mode=ro` 不抢写锁。"""
    out: dict[str, int | str] = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return {t: "?" for t in _COUNT_TABLES}
    try:
        for table in _COUNT_TABLES:
            try:
                out[table] = int(con.execute(f"select count(*) from {table}").fetchone()[0])
            except sqlite3.Error:
                out[table] = "-"
    finally:
        con.close()
    return out


def _git_report(root: Path) -> dict[str, Any]:
    """git 拦截现状：是不是仓库、令牌路径是否被忽略、pre-commit 有没有覆盖它。

    **只核对，不修改**（§10.2 第 3 步）：这些钩子是人手写进各仓库的，
    自动"补齐"会在别人的工程里留下看不懂的改动。
    """
    def run(*args: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.returncode, (proc.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:  # git 不在 PATH
            return 127, str(exc)

    code, out = run("rev-parse", "--is-inside-work-tree")
    if code != 0 or out != "true":
        return {"repo": False, "ignored": None, "hook": None}

    ignored = run("check-ignore", "-q", f"{WORKSPACE_DIR}/token.json")[0] == 0

    hooks: list[Path] = []
    dot_git = root / ".git"
    if dot_git.is_dir():
        hooks.append(dot_git / "hooks" / "pre-commit")
    else:
        # worktree / submodule：`.git` 是文件，真实目录写在里面
        code, out = run("rev-parse", "--git-path", "hooks")
        if code == 0 and out:
            hooks.append((root / out).resolve() / "pre-commit")

    hook = "none"
    for hook_path in hooks:
        if hook_path.is_file():
            try:
                text = hook_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                hook = "unreadable"
                continue
            hook = "covers" if WORKSPACE_DIR in text else "no-cover"
            break
    return {"repo": True, "ignored": ignored, "hook": hook}


# ---------------------------------------------------------------- 计划


@dataclass(slots=True)
class Candidate:
    """一个待迁移的工作区。`action` 决定 apply 时动不动它。"""

    root: Path
    library_dir: Path
    label: str
    db: Path | None
    counts: dict[str, int | str] = field(default_factory=dict)
    token_state: str = "none"  # none | v2-ok | v2-stale | v1 | bad
    action: str = "skip"  # register | keep | skip | conflict
    reason: str = ""
    git: dict[str, Any] = field(default_factory=lambda: {"repo": False})

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "library_dir": str(self.library_dir),
            "label": self.label,
            "db": str(self.db) if self.db else None,
            "counts": self.counts,
            "token": self.token_state,
            "action": self.action,
            "reason": self.reason,
            "git": self.git,
        }


def _label_ok(label: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-z][0-9a-z_]*", label))


def _registered_labels(registry: keys.Registry, ws_dir: Path) -> list[str]:
    """这个目录在 registry 里已经叫什么（可能为空）。标签的真相在这里，不在目录名。"""
    return sorted(
        name for name, lib in registry.libraries.items() if same_path(lib.path, ws_dir)
    )


def _inspect(ws_dir: Path, label: str, registry: keys.Registry) -> Candidate:
    root = ws_dir.parent
    db_file = ws_dir / DB_NAME
    cand = Candidate(
        root=root,
        library_dir=ws_dir,
        label=label,
        db=db_file if db_file.is_file() else None,
        git=_git_report(root),
    )
    if cand.db is None:
        cand.action, cand.reason = "skip", "目录里没有 workspace.db（空壳，不迁移）"
        return cand
    cand.counts = _counts(cand.db)

    # 标签撞车：同一个 label 已指向别的目录 —— 绝不静默改名（那会让 token 和 registry 各说一套）
    existing = registry.library(label)
    if existing is not None and not same_path(existing.path, ws_dir):
        cand.action = "conflict"
        cand.reason = f"标签 `{label}` 已被占用：{existing.path}"
        return cand

    tk_path = token_path(root)

    # 目录已经登记过：**沿用已登记的标签**，别按目录名重算。
    # 否则重跑一次 migrate（忘了带 --label）就会算出另一个标签，
    # 然后要么造出第二条指向同一目录的记录，要么把令牌重签成新标签。
    registered = _registered_labels(registry, ws_dir)
    if registered and label not in registered:
        cand.action = "conflict"
        cand.reason = (
            f"该目录已登记为 `{registered[0]}`；改标签会多出一条指向同一目录的记录。"
            f"要么沿用 `{registered[0]}`，要么先 revoke 旧条目。"
        )
        return cand
    if registered:
        cand.label = label = registered[0]

    if not tk_path.is_file():
        cand.token_state, cand.action = "none", "register"
        cand.reason = "无令牌：登记库 + 签发 v2 令牌"
        return cand

    try:
        tok = Token.load(tk_path)
    except TokenError as exc:
        cand.token_state = "v1" if getattr(exc, "code", "") == "token_v1" else "bad"
        cand.action = "register"
        cand.reason = f"令牌不可用（{exc.code}）：重签 v2"
        return cand

    record = registry.record_for(tok.key)

    # 显式声明「已拒绝」的令牌不是待办事项：那是已经做过的决定，迁移不许替人翻案
    if tok.library is None:
        if (
            record is not None
            and record.active
            and record.library is None
            and same_path(record.workspace, root)
            and same_path(tok.workspace, root)
        ):
            cand.token_state, cand.action = "declined", "keep"
            cand.reason = "已选择不接入（library=null），保持原样"
        else:
            cand.token_state = "declined"
            cand.action = "skip"
            cand.reason = "令牌声明不接入，但 registry 没这条记录：先人工确认，不自动接入"
        return cand

    if (
        tok.library == label
        and same_path(tok.workspace, root)
        and record is not None
        and record.active
        and record.library == label
        and same_path(record.workspace, root)
    ):
        cand.token_state, cand.action = "v2-ok", "keep"
        cand.reason = "令牌与 registry 已一致"
        return cand

    cand.token_state = "v2-stale"
    cand.action = "register"
    if record is None:
        cand.reason = "令牌的钥匙不在 registry（换过 HM_HOME？）：按现有库重签"
    elif tok.library != label:
        cand.reason = f"令牌写的是 library={tok.library!r}，与登记标签 `{label}` 不符：重签"
    else:
        cand.reason = "令牌与 registry 的归属对不上：重签"
    return cand


def _norm_key(path: Path | str) -> str:
    """路径身份比较的键：展开 + 绝对化 + normcase（与 token.normalize_path 同口径）。"""
    p = Path(os.path.expandvars(str(path))).expanduser()
    try:
        p = p.resolve()
    except OSError:  # pragma: no cover - 目录被删了也不该让整轮迁移失败
        p = Path(os.path.abspath(p))
    return os.path.normcase(str(p))


def build_plan(
    scan_roots: list[Path],
    *,
    labels: dict[str, str] | None = None,
    depth: int = 3,
    registry: keys.Registry | None = None,
) -> list[Candidate]:
    """扫描 + 逐个体检，返回计划。不写任何东西 —— dry-run 与 apply 共用这一步。"""
    reg = registry or keys.read()
    overrides = {_norm_key(k): v for k, v in (labels or {}).items()}

    plan: list[Candidate] = []
    used: dict[str, Path] = {}
    for ws_dir in discover(scan_roots, depth=depth):
        root = ws_dir.parent
        # 标签优先级：显式 --label > registry 里已登记的 > 目录名
        label = (
            overrides.get(_norm_key(root))
            or (_registered_labels(reg, ws_dir) or [None])[0]
            or slug(root.name)
        )
        if not _label_ok(label):
            cand = Candidate(root=root, library_dir=ws_dir, label=label, db=None)
            cand.action, cand.reason = "skip", f"标签 `{label}` 不合法（只允许小写字母/数字/下划线）"
            plan.append(cand)
            continue
        # 同一轮里两个项目算出同一个标签：后到的让位，避免互相覆盖
        if label in used:
            cand = Candidate(root=root, library_dir=ws_dir, label=label, db=None)
            cand.action, cand.reason = "skip", f"与本轮的 {used[label]} 抢同一个标签，用 --label 指定"
            plan.append(cand)
            continue
        used[label] = root
        plan.append(_inspect(ws_dir, label, reg))
    return plan


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


def _retire(path: Path, *, dry_run: bool) -> dict[str, Any]:
    """旧令牌改名留档：不删除，回滚只需改回来。"""
    if not path.is_file():
        return {"path": str(path), "retired": None, "reason": "不存在"}
    target = path.with_name(f"{path.name}.retired-{_stamp()}")
    n = 2
    while target.exists():
        target = path.with_name(f"{path.name}.retired-{_stamp()}-{n}")
        n += 1
    if not dry_run:
        os.replace(path, target)
    return {"path": str(path), "retired": str(target)}


def _retire_mcp_token(mcp_json: Path, *, dry_run: bool) -> dict[str, Any]:
    """删掉 mcp.json 里 humanity-memory 的 `--token <路径>`，其他 server 一律不碰。"""
    if not mcp_json.is_file():
        return {"path": str(mcp_json), "changed": False, "reason": "配置不存在"}
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"path": str(mcp_json), "changed": False, "reason": f"不是合法 JSON：{exc}"}

    touched: list[str] = []
    skipped: list[str] = []
    servers = data.get("mcpServers") or {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        argv = server.get("args") or []
        if "--token" not in argv:
            continue
        command = f"{name} {server.get('command') or ''}".lower()
        if "humanity-memory" not in command:
            skipped.append(name)
            continue
        cleaned: list[str] = []
        skip_next = False
        for item in argv:
            if skip_next:
                skip_next = False
                continue
            if item == "--token":
                skip_next = True
                continue
            cleaned.append(item)
        server["args"] = cleaned
        touched.append(name)

    result: dict[str, Any] = {
        "path": str(mcp_json),
        "changed": bool(touched),
        "servers": touched,
        "skipped": skipped,
    }
    if not touched:
        return result
    if dry_run:
        result["backup"] = None
        return result

    backup = mcp_json.with_name(f"{mcp_json.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(mcp_json.read_text(encoding="utf-8"), encoding="utf-8")
    mcp_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result["backup"] = str(backup)
    return result


def apply_plan(
    plan: list[Candidate],
    *,
    dry_run: bool = True,
    registry_path: Path | str | None = None,
    retire_paths: list[Path] | None = None,
    mcp_json: Path | None = None,
) -> dict[str, Any]:
    """按计划落地。全程幂等：`keep` 的条目一个字节都不动。

    `dry_run=True` 时只算不写 —— 包括不创建 registry.json。
    """
    todo = [c for c in plan if c.action == "register"]
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "registry": str(keys.registry_path(registry_path)),
        "libraries": [c.to_json() for c in plan],
        "issued": [],
        "retired": [],
        "mcp_json": None,
        "warnings": [],
    }

    if dry_run:
        report["issued"] = [
            {"root": str(c.root), "label": c.label, "key": "(dry-run 未签发)"} for c in todo
        ]
        report["retired"] = [
            {"path": str(p), "retired": f"{p.name}.retired-{_stamp()}"}
            for p in (retire_paths or [])
            if p.is_file()
        ]
        report["mcp_json"] = (
            _retire_mcp_token(mcp_json, dry_run=True) if mcp_json else None
        )
        return report

    # 1) registry：一次锁内登记 + 发钥匙，避免 N 次锁竞争
    plain: dict[str, str] = {}
    if todo:
        with keys.edit(registry_path) as reg:
            for cand in todo:
                reg.set_library(cand.label, cand.library_dir)
                plain[str(cand.root)] = reg.issue_key(cand.root, cand.label)

    # 2) 令牌：库已登记，现在写身份文件
    for cand in todo:
        key = plain.get(str(cand.root))
        if not key:  # pragma: no cover - todo 与 plain 一一对应
            continue
        token = build_token(
            token_path(cand.root),
            workspace=cand.root,
            library=cand.label,
            key=key,
        )
        token.save()
        report["issued"].append(
            {
                "root": str(cand.root),
                "label": cand.label,
                "token": str(token.path),
                "key": token.masked()["key"],
            }
        )

    # 3) 旧全局令牌退役
    for path in retire_paths or []:
        report["retired"].append(_retire(path, dry_run=False))

    # 4) mcp.json 去掉 --token
    if mcp_json:
        report["mcp_json"] = _retire_mcp_token(mcp_json, dry_run=False)
        if report["mcp_json"].get("skipped"):
            report["warnings"].append(
                "mcp.json 里还有其他 server 带 --token，未改动："
                + "、".join(report["mcp_json"]["skipped"])
            )

    if any(c.action == "conflict" for c in plan):
        report["ok"] = False
        report["warnings"].append("有标签冲突的条目被跳过，处理完重跑一次")
    return report


# ---------------------------------------------------------------- 准入：七步校验


@dataclass(slots=True)
class Session:
    """一次会话的绑定结果：要么可用（`ok`），要么是一条拒绝 + 清单。

    拒绝不是一个异常 —— 它是**正常返回**。调用方（MCP 工具 / CLI）要能把它
    原样打印给 agent 看，所以错误码、人话、清单必须一起带出来。
    """

    ok: bool
    code: str | None = None
    reason: str = ""
    workspace_root: Path | None = None
    expected_token: Path | None = None
    checklist: list[str] = field(default_factory=list)
    token: Token | None = None
    record: keys.KeyRecord | None = None
    library_dir: Path | None = None
    layout: Layout | None = None
    access: str = "rw"

    def to_json(self) -> dict[str, Any]:
        """§6.1 的统一返回。字段名固定，agent 会按它复述。"""
        if self.ok:
            return {
                "ok": True,
                "code": None,
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
                "library": self.record.library if self.record else None,
                "library_dir": str(self.library_dir) if self.library_dir else None,
                "access": self.access,
            }
        payload: dict[str, Any] = {
            "ok": False,
            "error": "memory_unavailable",
            "code": self.code,
            "reason": self.reason,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "expected_token": str(self.expected_token) if self.expected_token else None,
        }
        if self.checklist:
            payload["checklist"] = list(self.checklist)
        return payload


def _refuse(
    code: str,
    reason: str,
    *,
    root: Path | None = None,
    token_file: Path | None = None,
    checklist: list[str] | None = None,
) -> Session:
    return Session(
        ok=False,
        code=code,
        reason=reason,
        workspace_root=root,
        expected_token=token_file,
        checklist=checklist or [],
    )


def checklist_for(root: Path | None, code: str) -> list[str]:
    """§6.2 的「出路」清单。首项按现场情况排序，不写死。

    清单的读者是 agent：每条都要能直接变成一次工具调用或一句复述。
    """
    if code == "no_workspace":
        return [
            "① 传入项目根：`memory_bind_status(workspace=\"D:/work/你的项目\")` 预检",
            "② 确认路径后：bind new（新建）/ bind existing（并入已有库）/ bind adopt（沿用本目录已有的库）",
        ]

    ws_dir = (root / WORKSPACE_DIR) if root else None
    db = (ws_dir / DB_NAME) if ws_dir else None
    items: list[str] = []
    if db is not None and db.is_file():
        counts = _counts(db)
        tally = " / ".join(f"{k} {v}" for k, v in counts.items())
        items.append(
            f"① 本目录已有 .ai_memory/workspace.db（{tally}）"
            "→ memory_bind_adopt：把现有库登记为自有库"
        )
    else:
        items.append("① 本目录还没有库 → memory_bind_new(label=…)：新建独立库")
    items.append('② 想并入已有库 → memory_bind_existing(library="island")')
    items.append('③ 新建独立库 → memory_bind_new(label="你的项目名")')
    items.append("④ 本次不接入 → memory_bind_decline")
    return items


def resolve_session(
    workspace_hint: Path | str | None = None,
    *,
    home: Path | None = None,
    rules_path: Path | str | None = None,
) -> Session:
    """§5.5 七步校验。任一失败返回拒绝（不抛异常、不静默回落）。

    `workspace_hint` 来自 `--workspace` / `HM_WORKSPACE` / 工具入参；
    不给就用 cwd 向上找。
    """
    # 1) 确定 root
    if workspace_hint:
        given = Path(os.path.expandvars(str(workspace_hint))).expanduser()
        root = given.parent if given.name == WORKSPACE_DIR else given
        if not root.is_dir():
            return _refuse(
                "no_workspace",
                f"传入的工作区目录不存在：{root}",
                root=root,
                checklist=checklist_for(None, "no_workspace"),
            )
    else:
        root = find_workspace_root()
        if root is None:
            return _refuse(
                "no_workspace",
                f"从 {Path.cwd()} 向上没有找到 .ai_memory；当前目录不是任何项目的工作区。",
                checklist=checklist_for(None, "no_workspace"),
            )

    expected = token_path(root)

    # 2) 令牌存在且可解析
    try:
        token = Token.load(expected)
    except TokenError as exc:
        code = exc.code if exc.code in ("token_v1",) else "no_token" if not expected.is_file() else "token_bad"
        reason = str(exc)
        if code == "no_token":
            reason = f"{root} 下没有 .ai_memory/token.json，该工作区尚未接入记忆库。"
        elif code == "token_v1":
            reason = (
                f"令牌是 v1 格式（{expected}），v2 只识别不降级。\n"
                "它的 embedding 字段已拆除，库归属改由服务端 registry 决定。"
            )
        return _refuse(
            code,
            reason,
            root=root,
            token_file=expected,
            checklist=checklist_for(root, code),
        )

    # 3/4) 版本与归属：Token.load 已经保证了 version==2，这里核 workspace
    try:
        token.assert_workspace(root)
    except TokenError as exc:
        hint = (
            "旧路径仍在 → 这份令牌不属于此项目（复制过工程）"
            if token.workspace.is_dir()
            else f"旧路径 {token.workspace} 已不存在 → 项目搬过家，用 `bind move --from <旧> --to <新>`"
        )
        return _refuse(
            exc.code,
            f"{exc}\n{hint}",
            root=root,
            token_file=expected,
            checklist=[f"① {hint}", "② 重发令牌：bind adopt（或 bind move 修路径）"],
        )

    # 5/6) registry：存在、未撤销、workspace 与 library 都对得上
    try:
        registry = keys.read()
    except keys.RegistryError as exc:
        return _refuse(
            exc.code,
            f"{exc}\nregistry 只是钥匙归属的索引，删掉重跑 `bind adopt` 即可，库文件不受影响。",
            root=root,
            token_file=expected,
            checklist=["① 删掉 registry.json 后重跑 bind adopt"],
        )

    try:
        record = registry.check_key(token.key, root, token.library)
    except keys.RegistryError as exc:
        return _refuse(
            exc.code,
            str(exc),
            root=root,
            token_file=expected,
            checklist=[
                "① 重发令牌：bind adopt（会签到本机 registry）",
                "② 若这把钥匙是被主动撤销的 → 说明它不该再用，别绕过",
            ],
        )

    # 7) 库：非 null、目录在
    if record.library is None:
        return _refuse(
            "declined",
            f"{root} 已选择不接入记忆库（令牌里 library=null）。\n"
            "想改主意：`bind new` 新建一个库，或 `bind existing` 并入已有库。",
            root=root,
            token_file=expected,
            checklist=[
                '① 改主意新建库 → memory_bind_new(label="…")',
                '② 改主意并入已有库 → memory_bind_existing(library="…")',
            ],
        )

    lib_dir = registry.library_path(record.library)
    if lib_dir is None or not lib_dir.is_dir():
        return _refuse(
            "library_missing",
            f"库 `{record.library}` 的目录不在了：{lib_dir}\n"
            "组库常在移动硬盘 / 网络盘上 —— 挂载后重试；换了位置就 `bind existing` 重新绑。",
            root=root,
            token_file=expected,
            checklist=[
                "① 挂载/恢复该目录后重试",
                "② 库已挪走 → memory_bind_existing(library=…) 指向新位置",
            ],
        )

    layout = resolve_layout(
        home=home,
        library_dir=lib_dir,
        workspace_root=root,
        rules_path=rules_path,
        create=False,
    )
    return Session(
        ok=True,
        workspace_root=root,
        expected_token=expected,
        token=token,
        record=record,
        library_dir=lib_dir,
        layout=layout,
        access=record.access,
    )


# ---------------------------------------------------------------- 绑定动作


class BindingError(Exception):
    """绑定失败。带 code 与 checklist，CLI / MCP 都原样返回给用户。"""

    def __init__(
        self, code: str, message: str, *, checklist: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.checklist = checklist or []

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": "memory_unavailable",
            "code": self.code,
            "reason": self.message,
        }
        if self.checklist:
            payload["checklist"] = list(self.checklist)
        return payload


def _root_of(path: Path | str) -> Path:
    root = Path(os.path.expandvars(str(path))).expanduser()
    if root.name == WORKSPACE_DIR:
        root = root.parent
    if not root.is_dir():
        raise BindingError(
            "no_workspace",
            f"目录不存在：{root}",
            checklist=checklist_for(None, "no_workspace"),
        )
    return root


def _settle(root: Path) -> Session:
    """绑完立刻按七步校验复核一遍 —— 写进去的东西读不回来，等于没绑。"""
    session = resolve_session(root)
    if not session.ok:
        raise BindingError(
            session.code or "token_bad", session.reason, checklist=session.checklist
        )
    return session


def bind_new(
    workspace: Path | str, label: str | None = None, *, registry_path: Path | str | None = None
) -> Session:
    """新建独立库。建目录是**显式动作** —— 只有这条路径允许创建 `.ai_memory`。"""
    root = _root_of(workspace)
    name = (label or slug(root.name)).strip()
    if not _label_ok(name):
        raise BindingError(
            "library_conflict", f"标签 `{name}` 不合法：只允许小写字母、数字、下划线"
        )
    lib_dir = root / WORKSPACE_DIR
    lib_dir.mkdir(parents=True, exist_ok=True)
    try:
        with keys.edit(registry_path) as registry:
            existing = registry.library(name)
            if existing is not None and not same_path(existing.path, lib_dir):
                raise BindingError(
                    "library_conflict",
                    f"标签 `{name}` 已被占用：{existing.path}\n换个 label，或 bind existing 用它。",
                )
            registry.set_library(name, lib_dir)
            key = registry.issue_key(root, name)
        build_token(token_path(root), workspace=root, library=name, key=key).save()
    except keys.RegistryError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    except TokenError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    return _settle(root)


def bind_adopt(
    workspace: Path | str, label: str | None = None, *, registry_path: Path | str | None = None
) -> Session:
    """把本目录已有的 `.ai_memory/workspace.db` 登记为自有库（存量迁移入口）。"""
    root = _root_of(workspace)
    lib_dir = root / WORKSPACE_DIR
    if not (lib_dir / DB_NAME).is_file():
        raise BindingError(
            "no_token",
            f"{root} 下没有 .ai_memory/workspace.db —— 没有可沿用的库。\n"
            "新建用 bind new，并入已有库用 bind existing。",
            checklist=checklist_for(root, "no_token"),
        )
    try:
        with keys.edit(registry_path) as registry:
            name = label or registry.unique_label(slug(root.name))
            registry.set_library(name, lib_dir)
            key = registry.issue_key(root, name)
        build_token(token_path(root), workspace=root, library=name, key=key).save()
    except keys.RegistryError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    except TokenError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    return _settle(root)


def bind_existing(
    workspace: Path | str,
    library: str,
    *,
    registry_path: Path | str | None = None,
) -> Session:
    """绑到 registry 里已有的库（A、B 共用一个库）。"""
    root = _root_of(workspace)
    try:
        with keys.edit(registry_path) as registry:
            if registry.library(library) is None:
                known = "、".join(sorted(registry.libraries)) or "（还没有任何库）"
                raise BindingError(
                    "library_conflict", f"没有叫 `{library}` 的库。现有：{known}"
                )
            key = registry.issue_key(root, library)
        build_token(token_path(root), workspace=root, library=library, key=key).save()
    except keys.RegistryError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    except TokenError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    return _settle(root)


def bind_decline(
    workspace: Path | str, *, registry_path: Path | str | None = None
) -> Session:
    """选择不接入。返回的是**拒绝态 session**（code=declined），不是错误。"""
    root = _root_of(workspace)
    try:
        with keys.edit(registry_path) as registry:
            key = registry.issue_key(root, None)
        build_token(token_path(root), workspace=root, library=None, key=key).save()
    except keys.RegistryError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    except TokenError as exc:
        raise BindingError(exc.code, str(exc)) from exc
    return resolve_session(root)


def bind_move(
    old: Path | str,
    new: Path | str,
    *,
    registry_path: Path | str | None = None,
) -> dict[str, Any]:
    """项目搬家：改令牌与 registry 两处，**不猜、不自动改写**。

    `old` 通常已经不存在了（这就是搬家的定义），所以这里不能 `_root_of(old)`。
    """
    old_root = Path(os.path.expandvars(str(old))).expanduser()
    new_root = _root_of(new)
    new_token_path = token_path(new_root)
    if not new_token_path.is_file():
        raise BindingError(
            "no_token",
            f"{new_root} 下没有令牌 —— 搬家时 `.ai_memory` 应该一起搬过来。\n"
            "若确实是新目录，用 bind adopt（沿用搬来的库）或 bind new。",
            checklist=checklist_for(new_root, "no_token"),
        )
    try:
        token = Token.load(new_token_path)
    except TokenError as exc:
        raise BindingError(exc.code, str(exc)) from exc

    moved = {"token": str(new_token_path), "library": token.library, "registry": None}
    try:
        with keys.edit(registry_path) as registry:
            record = registry.record_for(token.key)
            if record is None:
                raise BindingError(
                    "key_unknown",
                    "这把钥匙不在本机 registry 里，无从改归属。重发令牌：bind adopt。",
                )
            if not same_path(record.workspace, old_root) and not same_path(
                record.workspace, new_root
            ):
                raise BindingError(
                    "key_workspace_mismatch",
                    f"registry 记的归属是 {record.workspace}，与 --from {old_root} 和 --to {new_root} 都不符。\n"
                    "先确认要改的是哪一份，不要靠猜。",
                )
            record.workspace = new_root
            if token.library:
                lib = registry.library(token.library)
                if lib is not None and same_path(lib.path, old_root / WORKSPACE_DIR):
                    lib.path = new_root / WORKSPACE_DIR
                    moved["registry"] = str(lib.path)
    except keys.RegistryError as exc:
        raise BindingError(exc.code, str(exc)) from exc

    token.workspace = new_root
    token.save()
    return moved


# ---------------------------------------------------------------- 默认退役清单


def legacy_token_paths(home: Path | None = None) -> list[Path]:
    """旧机制留下的全局令牌（v1）。迁移后它们只会让新程序拒绝启动。"""
    base = Path(home).expanduser() if home else global_home()
    return [base / "active.token.json", base / "dev.token.json"]
