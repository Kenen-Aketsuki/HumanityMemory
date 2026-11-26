#!/usr/bin/env python
"""发布与回退：`release/` 是生产态，源码树是开发态。

设计边界
--------
* **只有发布能改变生产**：改源码不生效，必须 `--publish` 重新打包。
* **入口稳定、产物版本化**：`release/current` 是目录 junction，mcp.json 只认它；
  发布/回滚只换 junction，客户端配置一个字节都不动。
* **发布自带回滚**：`--publish` 成功即可 `--rollback` 回去，且回滚会连同库快照一起还原。

用法::

    python tools/build_release.py --status        # 现状：当前版本、历史、备份、mcp 配置
    python tools/build_release.py --publish       # 备份库 → 打包 → smoke（首次只产出，不切换）
    python tools/build_release.py --promote       # 把 current 切到最新构建版本
    python tools/build_release.py --rollback      # 回上一版（exe + 库快照）
    python tools/build_release.py --prune --keep 2 # 清理旧版本（保留最近 2 个 + current）

版本号从哪来
------------
pyproject.toml 里的 version 是**源码基线**（git 跟踪，只在正式发版时改）。
`--target` 可以为本次构建单独指定版本号：它只写进 exe 包内的 version.txt 和
`build.json`，**不碰源码**，所以滚动发布不会在 git 里留下版本 diff。
同版本重复构建用 `--force`，旧目录会被改名留档为 `v<ver>.stale-<时间戳>`。

退出码：0 成功；1 失败（打包/smoke/回滚失败）；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import localenv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 发布产物**必须住在项目目录树外**（真的在树里才算，不含同前缀的兄弟目录）。
# 具体位置是「这台机器的事」：从 HM_RELEASE_ROOT 环境变量或项目根的 local.env 读，
# 源码里不出现任何本机路径。未配置时退回用户目录下的通用位置，仅作兜底。
#
# 历史：早先这里认为「同前缀的兄弟目录也会被沙箱拦」，2026-11-18 实测未能复现——
# exe 放在项目同级的 `*-Release` 目录里时，doctor 能读写 `~/.humanity-memory`、
# 真实 MCP 客户端握手拿到全部工具、也能往用户目录正常写文件。
# 旧的 `attempt to write a readonly database` 应是早期沙箱行为，故改为按目录边界判断。
_DEFAULT_RELEASE = Path.home() / "HumanityMemory-Release"
RELEASE = localenv.get_path("HM_RELEASE_ROOT", _DEFAULT_RELEASE)
SRC = ROOT / "src"
TOOLS = ROOT / "tools"
ENTRY = TOOLS / "release_entry.py"
PKG = SRC / "humanity_memory"
APP_NAME = "humanity-memory"
def workspace_db() -> Path | None:
    """当前源码工作区对应的库文件。

    2026-11-19 起库统一放在 `<安装根>/libraries/<label>`，仓库里只剩
    `.ai_memory/token.json`。所以**不能**再假定 `<仓库>/.ai_memory/workspace.db` ——
    那样会在每次发布时静默跳过库备份，回滚时就没有数据还原点。
    """
    legacy = ROOT / ".ai_memory" / "workspace.db"
    if legacy.is_file():          # 老布局（库还在仓库里）
        return legacy
    try:
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        from humanity_memory import bindings  # noqa: PLC0415 - 延迟导入，避免构建期副作用

        session = bindings.resolve_session(ROOT)
        if session.ok and session.library_dir is not None:
            candidate = session.library_dir / "workspace.db"
            return candidate if candidate.is_file() else None
    except Exception as exc:  # noqa: BLE001 - 解析失败只影响快照，不该中断发布
        warn(f"解析库位置失败：{exc}")
    return None

CURRENT_LINK = RELEASE / "current"
CURRENT_TXT = RELEASE / "current.txt"
HISTORY = RELEASE / "history.json"
BACKUPS = RELEASE / "backups"
ENV_DEST = RELEASE / ".env"
# 本地构建序号：住在发布根里（项目树外，天然不被 git 跟踪），
# 大版本号跟源码走，这个序号只用来区分同一大版本下的第 N 次本地打包。
LOCAL_STATE = RELEASE / "local_build.json"


# ---------------------------------------------------------------- 小工具


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr, flush=True)


def rel(path: Path) -> str:
    """打印/记账用的路径：优先相对 `RELEASE`（发布根已在项目外，不能再用 ROOT 相对化）。"""
    try:
        return str(Path(path).relative_to(RELEASE))
    except ValueError:
        return str(path)


def _backup_path(raw: str | None) -> Path | None:
    """history.json 里的快照路径。旧记录相对仓库根（`release/backups/x.db`），
    新记录相对发布根（`backups/x.db`），两种都要能读——回滚链路不该因为搬家一次就断。"""
    if not raw:
        return None
    norm = raw[len("release/") :] if raw.startswith("release/") else raw
    return RELEASE / norm


def die(msg: str, code: int = 1) -> "None":
    print(f"[error] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 版本与构建信息


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        return str(tomllib.loads(text)["project"]["version"])
    except ModuleNotFoundError:  # Python 3.10 没有 tomllib
        match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if not match:
            die("pyproject.toml 里找不到 version")
        return match.group(1)


def package_base_version() -> str | None:
    """源码里 `_BASE_VERSION` 写的基线版本；用来和 pyproject 对齐检查。"""
    try:
        text = (PKG / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^_BASE_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def local_state() -> dict:
    state = read_json(LOCAL_STATE, {}) or {}
    return state if isinstance(state, dict) else {}


def next_local_version(base: str) -> str:
    """生成下一个本地构建版本号：`<基线>-local.<序号>`。

    序号存在发布根的 local_build.json（项目树外，天然不进 git），所以微调打包
    永远不会在源码里留下版本 diff。基线（大版本）一变序号就从 1 重新数——
    那是另一个源码版本，跟旧序号没有可比性。
    """
    state = local_state()
    seq = int(state.get("seq") or 0) if state.get("base") == base else 0
    seq += 1
    version = f"{base}-local.{seq}"
    write_json(
        LOCAL_STATE,
        {
            "base": base,
            "seq": seq,
            "version": version,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return version


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def version_dir(version: str) -> Path:
    return RELEASE / f"v{version}"


_VERSION_RE = re.compile(r"^v\d+(?:\.\d+)*(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")


def built_versions() -> list[Path]:
    """只认 `v<数字>.<数字>` 这种正式版本目录。

    `.stale-*` / `.failed-*` 是留档的垃圾，不能混进"已构建"列表里误导人。
    """
    if not RELEASE.is_dir():
        return []
    return sorted(
        (p for p in RELEASE.iterdir() if p.is_dir() and _VERSION_RE.match(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


def current_dir() -> Path | None:
    """当前 junction 指向的版本目录；没有 junction 就退回 current.txt。"""
    if CURRENT_LINK.exists():
        try:
            return Path(os.readlink(CURRENT_LINK)) if CURRENT_LINK.is_symlink() else CURRENT_LINK.resolve()
        except OSError:
            return None
    if CURRENT_TXT.is_file():
        name = CURRENT_TXT.read_text(encoding="utf-8").strip()
        return RELEASE / name if name else None
    return None


# ---------------------------------------------------------------- 库快照


def db_counts(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return out
    try:
        for table in ("knowledge", "episodes", "logs"):
            try:
                out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return out


def restore_workspace(backup: Path, dest: Path) -> Path:
    """把库快照还原到 `dest`，还原前先把「当前状态」留一份档。

    两个关键点：
    1. **留档是必须的**——回滚本身也可能是个错误决定，没有留档就真的回不去了。
    2. **用 SQLite backup API 灌回去，不要 copy 文件**。直接覆盖文件会让旧的
       `-wal`/`-shm` 留在原地（还删不掉：文件被占用），目标库就处在"新库 + 陈旧
       WAL"的危险组合里。backup API 是页级替换，库自己保证一致，也不碰任何文件句柄。
    """
    BACKUPS.mkdir(parents=True, exist_ok=True)
    safety = BACKUPS / f"workspace.before-rollback-{time.strftime('%Y%m%d-%H%M%S')}.db"

    src = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True, timeout=10.0)
    dst = sqlite3.connect(str(safety))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    snap = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True, timeout=10.0)
    target = sqlite3.connect(str(dest), timeout=15.0)
    try:
        snap.backup(target)
    finally:
        target.close()
        snap.close()
    return safety


def backup_workspace(version: str) -> Path | None:
    source = workspace_db()
    if source is None:
        warn("没找到工作区的库文件（令牌未接入？），跳过库备份（只影响回滚时的数据还原）")
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"workspace.pre-v{version}.db"
    try:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10.0)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as exc:
        warn(f"库备份失败：{exc}")
        return None
    # 备份会继承 WAL 模式，留下 0 字节的 -wal/-shm；快照应当是单个自洽文件
    for side in ("-wal", "-shm"):
        stale = Path(str(dest) + side)
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    say(f"  库快照 → {rel(dest)}  {db_counts(dest)}")
    return dest


# ---------------------------------------------------------------- 打包


def set_aside(path: Path, tag: str) -> Path | None:
    """把已有目录改名留档，而不是删除。

    一是可回滚（真出问题还能捡回来），二是不触发批量删除的保护钩子。
    清理这些 `.stale-*` / `.failed-*` 目录由人决定，脚本不擅自动手。
    """
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        path.rename(target)
    except OSError as exc:
        die(f"无法改名 {path}：{exc}（可能仍有进程占用）")
    warn(f"旧目录留档 → {rel(target)}（确认无用后可手动删）")
    return target


def run_pyinstaller(version: str, target: Path) -> None:
    # 打包目录必须落在**发布根所在的盘**上：产物最终要 rename 成 v{ver}，
    # 同盘 rename 是原子操作；跨盘时 shutil.move 退化成「整体复制 + 删源目录」，
    # 一次删掉 150+ 文件会撞上批量删除保护钩子（实测拦过一次发布）。
    # 每次构建仍用独立目录，PyInstaller 自己 rmtree 固定目录同样会撞钩子。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging = RELEASE / f".staging-{stamp}"
    dist = staging / "dist"
    work = ROOT / "build" / f"work-{stamp}"
    dist.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    # 版本号随包注入，而不是写死进源码：exe 的 --version / doctor / MCP initialize
    # 都读包里这个 version.txt，所以同一个源码基线可以打出任意版本号，
    # 打包多少次都不会在 git 里留下 version diff。
    # 文件放 work/ 而不是 staging/：staging 在产物搬走后会被回收，留个文件会挡住 rmdir。
    version_file = work / "version.txt"
    version_file.write_text(version.strip() + "\n", encoding="utf-8")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",           # onedir：冷启动快，不会被杀软误杀当自解压包
        "--console",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
        "--paths",
        str(SRC),
        "--add-data",
        f"{PKG / 'schema.sql'}{os.pathsep}humanity_memory",
        "--add-data",
        f"{PKG / 'memory.default.toml'}{os.pathsep}humanity_memory",
        # 注入的版本号：落到 _internal/humanity_memory/version.txt，
        # 由 __init__._packaged_version() 读取（源码态没有这个文件，回落到基线）
        "--add-data",
        f"{version_file}{os.pathsep}humanity_memory",
        "--collect-all",
        "sqlite_vec",
        # cli 里的 upgrade / starmap 都是延迟 import（未调用时零开销），
        # PyInstaller 静态分析看不到，必须显式收全，否则冻结态一调就 ModuleNotFoundError
        "--collect-submodules",
        "humanity_memory",
        "--hidden-import",
        "humanity_memory.mcp_server",
        str(ENTRY),
    ]
    say("  打包中（PyInstaller，约 30~90 秒）…")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-25:])
        die(f"PyInstaller 失败（退出码 {proc.returncode}）：\n{tail}")

    built = dist / APP_NAME
    if not (built / f"{APP_NAME}.exe").is_file():
        die(f"打包产物里没找到 {APP_NAME}.exe：{built}")
    set_aside(target, "stale")
    built.rename(target)  # 同盘 rename，不产生跨卷复制/删除
    say(f"  产物 → {rel(target)}（中间目录 {rel(staging)} 可随时清掉）")
    # 产物搬走后 staging 通常是空的；只在真为空时回收，有任何残留就留着给人看
    for leftover in (dist, staging):
        try:
            leftover.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- smoke test


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "hm-smoke", "version": "0"},
    },
}


def smoke_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HM_WORKSPACE": str(tmp / "ws"),
            "HM_HOME": str(tmp / "home"),
            # 库统一放在 <安装根>/libraries；smoke 必须把它指回沙箱，
            # 否则每跑一次发布就会在生产库目录里留下一个 smoke 库。
            "HM_LIBRARIES": str(tmp / "libs"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    env.pop("HM_RULES_PATH", None)
    return env


def smoke_prepare(exe: Path, tmp: Path) -> None:
    """让 smoke 沙箱处于「已接入」态。

    A1 之后空目录启动会被**正确拒绝**（no_workspace），所以老 smoke 里
    「空目录跑 doctor 应 all_ok」的假设已经不成立了。这里先用同一个 exe
    走一遍真实接入路径（bind adopt），顺带把 bind 也 smoke 了 ——
    这比绕过准入直接塞一个令牌更接近用户实际会做的事。
    """
    ws = tmp / "ws" / ".ai_memory"
    ws.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(exe), "bind", "new", str(tmp / "ws"), "--label", "smoke"],
        cwd=str(tmp),
        env=smoke_env(tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "smoke 准备失败：bind new 未成功\n"
            f"  stdout: {(proc.stdout or '')[-400:]}\n"
            f"  stderr: {(proc.stderr or '')[-400:]}"
        )
    if not (ws / "token.json").is_file():
        raise SystemExit("smoke 准备失败：bind new 说成功了，但没有写出 token.json")


def smoke_doctor(exe: Path, tmp: Path) -> tuple[list[str], dict]:
    """跑 doctor 并解析 JSON。重点不是 all_ok，而是 detail 里有没有「未配置」。"""
    proc = subprocess.run(
        [str(exe), "doctor"],
        cwd=str(tmp),
        env=smoke_env(tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    payload: dict = {}
    for chunk in _json_objects(proc.stdout or ""):
        payload = chunk
    if not payload:
        return [f"doctor 没有输出可解析的 JSON（退出码 {proc.returncode}）：{(proc.stderr or '')[-400:]}"], {}

    problems: list[str] = []
    if payload.get("error"):
        problems.append(f"doctor 报错：{payload['error']}")
    checks = payload.get("checks") or []
    names = {str(c.get("check")) for c in checks}
    for required in ("sqlite-vec", "fts5", "schema", "embedding"):
        if required not in names:
            problems.append(f"doctor 缺少检查项：{required}")
    for check in checks:
        detail = check.get("detail")
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        if "未配置" in text:
            # 冻结态缺资源时 doctor 仍会 ok=True 地报「未配置」，只看颜色必漏
            problems.append(f"{check.get('check')} 显示未配置：{text[:200]}")
        elif check.get("ok") is not True:
            problems.append(f"{check.get('check')} 未通过：{text[:200]}")
    return problems, payload


def smoke_serve(exe: Path, tmp: Path, timeout: float = 90.0) -> list[str]:
    """真起一次 MCP server 并握手。doctor 不 import mcp，只有这条路能证明 serve 没坏。"""
    proc = subprocess.Popen(
        [str(exe), "serve"],
        cwd=str(tmp),
        env=smoke_env(tmp),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(_INIT) + "\n")
        proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != 1:
                continue
            if "result" in msg:
                return []
            return [f"initialize 返回错误：{line[:300]}"]
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        return [f"serve 在 {timeout:.0f}s 内没有响应 initialize；stderr 尾部：{stderr[-600:]}"]
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=10)


def _json_objects(text: str):
    """从混合输出里捡出 JSON 对象（doctor 会先打印提示行）。"""
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        yield obj
        idx = end


def smoke(exe: Path) -> dict:
    say("  smoke test：绑定 → doctor → serve 握手")
    # 沙盒环境里 AppData\Temp 可能不可写（WinError 5），所以用仓库内目录：
    # 顺手也能验证"在项目里跑"这个真实姿势。目录很小，跑完就清。
    base = ROOT / "build" / f"smoke-{time.strftime('%Y%m%d-%H%M%S')}"
    (base / "ws").mkdir(parents=True, exist_ok=True)
    (base / "home").mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    report: dict = {}
    try:
        try:
            smoke_prepare(exe, base)
        except subprocess.TimeoutExpired:
            problems.append("bind adopt 超时（120s）")
        try:
            doc_problems, report = smoke_doctor(exe, base)
        except subprocess.TimeoutExpired:
            doc_problems = ["doctor 超时（180s）"]
        problems += doc_problems
        try:
            problems += smoke_serve(exe, base)
        except subprocess.TimeoutExpired:
            problems.append("serve 握手超时")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if problems:
        raise SystemExit("smoke 未通过：\n  - " + "\n  - ".join(p for p in problems if p))
    vec = next(
        (
            c.get("detail")
            for c in report.get("checks", [])
            if c.get("check") == "sqlite-vec"
        ),
        "?",
    )
    say(f"  ✓ smoke 通过（vec 来源：{vec}）")
    return report


# ---------------------------------------------------------------- junction 与历史


def set_current(target: Path) -> None:
    RELEASE.mkdir(parents=True, exist_ok=True)
    if CURRENT_LINK.exists() or CURRENT_LINK.is_symlink():
        try:
            os.rmdir(CURRENT_LINK)   # junction：只删链接，不动目标目录
        except OSError as exc:
            die(
                f"无法移除旧 junction {CURRENT_LINK}：{exc}\n"
                "若它已经变成一个真实目录（不是 junction），请先人工确认内容再删。"
            )
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(CURRENT_LINK), str(target)],
        capture_output=True,
    )
    if proc.returncode != 0:
        # cmd 的输出是本地代码页（中文 Windows 是 GBK），不能按 UTF-8 解
        detail = (proc.stdout + proc.stderr).decode("gbk", errors="replace").strip()
        die(f"创建 junction 失败：{detail}")
    CURRENT_TXT.write_text(target.name, encoding="utf-8")


def load_history() -> dict:
    return read_json(HISTORY, {"current": "", "versions": [], "rollbacks": []})


def upsert_history(entry: dict) -> dict:
    hist = load_history()
    versions = [v for v in hist.get("versions", []) if v.get("dir") != entry["dir"]]
    versions.append(entry)
    hist["versions"] = versions
    hist["current"] = entry["dir"]
    write_json(HISTORY, hist)
    return hist


# ---------------------------------------------------------------- mcp.json


def mcp_targets():
    try:
        sys.path.insert(0, str(TOOLS))
        from verify_mcp_config import ENTRY as entry_name, MCP_JSON as config_path

        return Path(config_path), entry_name
    except Exception:  # noqa: BLE001 - 拿不到就退回默认位置，不阻塞发布
        return Path.home() / ".workbuddy" / "mcp.json", "humanity-memory"


def rewrite_mcp(assume_yes: bool) -> None:
    config_path, entry_name = mcp_targets()
    data = read_json(config_path, None)
    if not isinstance(data, dict):
        warn(f"读不到 {config_path}，跳过客户端配置（不影响发布）")
        return
    servers = data.setdefault("mcpServers", {})
    old = servers.get(entry_name)
    if not isinstance(old, dict):
        warn(f"{config_path} 里没有 {entry_name} 条目，跳过（请手工配置）")
        return

    old_args = [str(a) for a in old.get("args", [])]
    had_token = "--token" in old_args

    # `--token` 不再是身份来源（A2）：令牌就在 `<项目>/.ai_memory/token.json`，
    # 由 cwd 发现，客户端配置只剩「exe + serve」。
    # 这里**不再回填** --token —— 否则迁移（bind migrate）删掉的那一项，
    # 下次发版又会被原样塞回去，升级一次退一次。
    new = {
        "command": str(CURRENT_LINK / f"{APP_NAME}.exe"),
        "args": ["serve"],
    }
    if old.get("env"):
        new["env"] = old["env"]

    if old.get("command") == new["command"] and old_args == new["args"]:
        say("  mcp.json 已指向 release/current，无需改动")
        return

    say("\n  mcp.json 变更：")
    say(f"    command: {old.get('command')}  →  {new['command']}")
    say(f"    args   : {old_args}  →  {new['args']}")
    if had_token:
        say("    （--token 已废弃：令牌改由 <项目>/.ai_memory/token.json 按 cwd 发现）")
    if not confirm("  确认改写客户端配置？", assume_yes):
        warn("已跳过 mcp.json 改写——生产仍会跑旧入口")
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_suffix(config_path.suffix + f".bak-{stamp}")
    try:
        backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        warn(f"配置备份失败（继续）：{exc}")
    servers[entry_name] = new
    write_json(config_path, data)
    say(f"  已改写 {config_path}（备份 {backup.name}）")


def ensure_env() -> None:
    if ENV_DEST.is_file():
        return
    source = ROOT / ".env"
    if not source.is_file():
        warn(f"没有 {ENV_DEST}，也没有 {source}；冻结态凭据会为空（doctor 会报「未配置」）")
        return
    shutil.copy2(source, ENV_DEST)
    say(f"  已把 {source.name} 复制到 {rel(ENV_DEST)}（跨版本共享，回滚不换凭据）")


# ---------------------------------------------------------------- 四个动作


def cmd_publish(args: argparse.Namespace) -> int:
    base = project_version()
    version = args.target or next_local_version(base)
    target = version_dir(version)
    say(f"发布 v{version} → {rel(target)}")
    if args.target:
        if version != base:
            say(f"  版本号来自 --target：源码基线仍是 v{base}，不修改任何源码文件")
    else:
        say(f"  本地构建号（源码基线 v{base}，序号存在 {rel(LOCAL_STATE)}，不进 git）")

    if target.exists() and not args.force:
        die(
            f"{rel(target)} 已存在。换版本号（pyproject.toml）或用 --force 覆盖。\n"
            "注意：正在运行中的 exe 被 Windows 锁定，覆盖会失败——先停客户端。"
        )

    say("  [1/5] 备份库快照")
    backup = backup_workspace(version)

    say("  [2/5] 读取版本真源 + 打包")
    sha = git_sha()
    run_pyinstaller(version, target)

    say("  [3/5] smoke test")
    ensure_env()   # 冻结态从 release/.env 读凭据，smoke 之前必须先就位
    try:
        report = smoke(target / f"{APP_NAME}.exe")
    except SystemExit:
        set_aside(target, "failed")
        raise

    schema_version = next(
        (
            str(c.get("detail", "")).split("version=")[-1].strip()
            for c in report.get("checks", [])
            if c.get("check") == "schema"
        ),
        "?",
    )
    build_info = {
        "version": version,
        "dir": target.name,
        "git_sha": sha,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "pyinstaller": _pyinstaller_version(),
        "schema_version": schema_version,
        "frozen_deps": _pip_freeze(),
    }
    write_json(target / "build.json", build_info)
    say(f"  build.json 已写入（sha={sha[:8]} schema={schema_version}）")

    if args.no_switch:
        say("\n  已按 --no-switch 只产出产物，未切换")
        return 0

    if current_dir() is None:
        say(
            "\n  首次发布：产物已就绪，但**没有切换**（生产仍跑源码态，天然安全）。\n"
            f"  确认可用后运行：python tools/build_release.py --promote --target {version}"
        )
        return 0

    return promote(version, backup, build_info, args)


def promote(
    version: str,
    backup: Path | None,
    build_info: dict,
    args: argparse.Namespace,
    *,
    entry_override: dict | None = None,
) -> int:
    target = version_dir(version)
    if not (target / f"{APP_NAME}.exe").is_file():
        die(f"{rel(target)} 里没有 {APP_NAME}.exe，先 --publish")

    say(f"  [4/5] 切换 release/current → {target.name}")
    set_current(target)
    ensure_env()
    entry = entry_override or {
        "version": version,
        "dir": target.name,
        "schema_version": build_info.get("schema_version", "?"),
        "git_sha": build_info.get("git_sha", "unknown"),
        "built_at": build_info.get("built_at", ""),
        "backup": rel(backup) if backup else None,
        "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    upsert_history(entry)

    say("  [5/5] 客户端配置")
    rewrite_mcp(args.yes)
    say(
        "\n发布完成。**重启 MCP 客户端**才会用上新版本。\n"
        f"  回滚：python tools/build_release.py --rollback"
    )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    version = args.target
    if not version:
        built = built_versions()
        if not built:
            die("release/ 下没有任何构建产物，先 --publish")
        version = built[-1].name.lstrip("v")
    target = version_dir(version)
    info = read_json(target / "build.json", {})
    backup = BACKUPS / f"workspace.pre-v{version}.db"
    return promote(version, backup if backup.is_file() else None, info, args)


def cmd_rollback(args: argparse.Namespace) -> int:
    hist = load_history()
    versions = hist.get("versions", [])
    if len(versions) < 2:
        die("历史里不足两个版本，没有可回退的目标")

    current = hist.get("current") or (current_dir().name if current_dir() else "")
    dirs = [v.get("dir") for v in versions]
    if current not in dirs:
        die(f"history.json 的 current（{current}）不在版本列表里，拒绝猜测，请人工检查")
    idx = dirs.index(current)
    if idx == 0:
        die("当前已经是最早的版本，没有更早的可回退")
    prev = versions[idx - 1]

    prev_dir = RELEASE / str(prev["dir"])
    if not (prev_dir / f"{APP_NAME}.exe").is_file():
        die(f"{prev_dir} 里没有 exe，产物已丢失——只能用应急通道（.venv 源码态）或重新打包")

    say(f"回退：{current} → {prev['dir']}")
    loss = {}
    # history 里存的是相对仓库根的路径，别拿 RELEASE 再拼一次
    backup = _backup_path(prev.get("backup"))
    if args.keep_db:
        backup = None
        say("  --keep-db：只退 exe，库保持现状（新版本写入的数据会成为孤儿字段）")
    elif backup and backup.is_file():
        now_counts = db_counts(workspace_db())
        lost = db_counts(backup)
        loss = {k: now_counts.get(k, 0) - lost.get(k, 0) for k in set(now_counts) | set(lost)}
        say(f"  库快照 {backup.name}：回滚后这些写入会丢 {loss}")

    if not confirm("  确认回退（含库还原）？", args.yes):
        say("  已取消")
        return 0

    target_db = workspace_db()
    if backup and backup.is_file() and target_db is not None:
        try:
            safety = restore_workspace(backup, target_db)
            say(f"  库已还原（回滚前的状态留档 {safety.name}）")
            after = db_counts(target_db)
            say(f"  还原后条数 {after}")
        except (sqlite3.Error, OSError) as exc:
            die(
                f"库还原失败：{exc}\n"
                "多半是客户端还开着库。先退出客户端再回滚，或加 --keep-db 只退 exe。"
            )

    set_current(prev_dir)
    hist["current"] = prev["dir"]
    hist.setdefault("rollbacks", []).append(
        {
            "from": current,
            "to": prev["dir"],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lost": loss,
        }
    )
    write_json(HISTORY, hist)

    say(f"  smoke 回滚后的 {prev['dir']}")
    smoke(prev_dir / f"{APP_NAME}.exe")
    say("\n回退完成。**重启 MCP 客户端**生效。")
    if loss and any(v > 0 for v in loss.values()):
        say(f"  注意：本次回滚丢弃了这些写入 {loss}")
    return 0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def tree_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def cmd_prune(args: argparse.Namespace) -> int:
    """清理旧版本目录与对应库快照。

    保留口径：最近 `--keep` 个（按 mtime）+ 永远保留 current。写不进去/被占用的
    目录只 warn 跳过，绝不因为一个目录删不掉就中断整场清理。
    """
    keep = max(1, args.keep)
    built = built_versions()          # 按 mtime 升序
    if not built:
        say("release/ 下没有版本目录，没得清理")
        return 0

    cur = current_dir()
    keep_dirs = set(built[-keep:])
    if cur is not None and cur in built:
        keep_dirs.add(cur)
    victims = [p for p in built if p not in keep_dirs]
    if not victims:
        say(f"没有可清理的版本：共 {len(built)} 个，保留 {keep} 个 + current")
        return 0

    freed = sum(tree_size(p) for p in victims)
    say(f"保留：{', '.join(p.name for p in built if p in keep_dirs)}")
    say(f"清理：{len(victims)} 个版本，约 {human_size(freed)}")
    for p in victims:
        say(f"    {p.name}  {human_size(tree_size(p))}")
    if not confirm("  确认删除这些版本目录（连同对应的库快照）？", args.yes):
        say("  已取消")
        return 0

    removed: list[Path] = []
    actual_freed = 0
    for p in victims:
        size = tree_size(p)
        try:
            shutil.rmtree(p)
        except OSError as exc:
            warn(f"  跳过 {p.name}：{exc}（可能还有进程在用这个 exe）")
            continue
        removed.append(p)
        actual_freed += size
        say(f"  已删除 {p.name}")

    if not removed:
        warn("  一个都没删掉——先退出 MCP 客户端再试")
        return 1

    # history 必须跟着删：留着指向不存在目录的条目，--rollback 会直接 die 在目标解析上
    gone = {p.name for p in removed}
    hist = load_history()
    kept_versions = [v for v in hist.get("versions", []) if v.get("dir") not in gone]
    if len(kept_versions) != len(hist.get("versions", [])):
        hist["versions"] = kept_versions
        write_json(HISTORY, hist)

    for p in removed:
        snapshot = BACKUPS / f"workspace.pre-{p.name}.db"
        if not snapshot.is_file():
            continue
        try:
            snapshot.unlink()
            say(f"  已删除快照 {snapshot.name}")
        except OSError as exc:
            warn(f"  快照 {snapshot.name} 删除失败：{exc}")

    say(f"\n清理完成：释放约 {human_size(actual_freed)}。**current 未动**，不需要重启客户端。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    version = project_version()
    state = local_state()
    cur = current_dir()
    hist = load_history()
    say(f"源码基线版本 : {version}（大版本，跟 git 走；只在源码大版本变化时改）")
    say(f"本地构建号   : {state.get('version') or '（还没本地打包过）'}")
    built_info = read_json(cur / "build.json", {}) if cur else {}
    current_label = cur.name if cur else "（未发布，生产跑源码态）"
    if cur and built_info.get("version"):
        current_label = f"{cur.name}（构建版本 {built_info['version']}）"
    say(f"current      : {current_label}")
    pkg_base = package_base_version()
    if pkg_base and pkg_base != version:
        warn(f"版本基线不一致：pyproject={version} / __init__._BASE_VERSION={pkg_base}，请先对齐")
    say(f"junction 存在  : {CURRENT_LINK.exists()}")
    say(f"release/.env   : {'有' if ENV_DEST.is_file() else '无'}")
    built = built_versions()
    say(f"已构建         : {', '.join(p.name for p in built) or '无'}")
    if hist.get("versions"):
        say("历史：")
        for item in hist["versions"]:
            mark = "  ← current" if item.get("dir") == hist.get("current") else ""
            say(
                f"  {item.get('dir')}  schema={item.get('schema_version')}  "
                f"{(item.get('git_sha') or '')[:8]}  {item.get('built_at', '')}{mark}"
            )
    else:
        say("历史           : 空（还没 promote 过）")
    backups = sorted(BACKUPS.glob("*.db")) if BACKUPS.is_dir() else []
    say(f"库快照         : {', '.join(p.name for p in backups) or '无'}")
    config_path, entry_name = mcp_targets()
    entry = (read_json(config_path, {}) or {}).get("mcpServers", {}).get(entry_name, {})
    say(f"mcp.json       : {entry.get('command', '（未配置）')}")
    return 0


def _pyinstaller_version() -> str:
    try:
        import PyInstaller

        return str(PyInstaller.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _pip_freeze() -> list[str]:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="发布 / 回退：release/ 是生产态，源码树是开发态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--publish", action="store_true", help="备份库 → 打包 → smoke（首次只产出不切换）")
    action.add_argument("--promote", action="store_true", help="把 current 切到指定/最新构建版本")
    action.add_argument("--rollback", action="store_true", help="回上一版（exe + 库快照）")
    action.add_argument(
        "--prune",
        action="store_true",
        help="清理旧版本目录与对应库快照（保留最近 --keep 个 + current）",
    )
    action.add_argument("--status", action="store_true", help="查看当前版本、历史、备份、客户端配置")
    parser.add_argument(
        "--target",
        metavar="VER",
        help="显式指定版本号（默认自动生成 <pyproject基线>-local.N，不改源码）",
    )
    parser.add_argument("--force", action="store_true", help="覆盖同版本目录（需先停客户端）")
    parser.add_argument("--keep", type=int, default=3, metavar="N", help="--prune 保留的版本数（默认 3；current 永远保留）")
    parser.add_argument("--no-switch", action="store_true", help="只打包，不切 current")
    parser.add_argument("--keep-db", action="store_true", help="回滚时不还原库快照，只退 exe")
    parser.add_argument("--yes", action="store_true", help="跳过所有交互确认")
    return parser


def _assert_release_outside_project() -> None:
    """发布根必须在项目树外。判断按**目录边界**比较，不做裸字符串前缀。

    沙箱拦的是「工作区内启动的进程写工作区外路径」，命中条件是路径真的落在
    项目树里面，而不是名字恰好同前缀。早先这里用 `release.startswith(root)`
    做过保守拦截，代价是把项目同级的 `*-Release` 兄弟目录也挡了；
    2026-11-18 实测：exe 放在这种同级目录里仍能正常启动 doctor、
    读写 `%USERPROFILE%\\.humanity-memory` 全局库、完成真实 MCP 握手，故放宽。
    """
    try:
        release_s = str(RELEASE.resolve())
        root_s = str(ROOT.resolve())
    except OSError:  # pragma: no cover
        return
    inside = release_s == root_s or release_s.startswith(root_s + os.sep)
    if inside:
        die(
            f"发布根不能放在项目目录树内：{RELEASE}\n"
            "宿主沙箱会拦「工作区内启动的进程写工作区外路径」，\n"
            "exe 可能打不开 ~/.humanity-memory，客户端表现为 MCP error -32000: Connection closed。\n"
            "换一个项目树之外的位置（点名的同级目录即可），写进 local.env 的 HM_RELEASE_ROOT。"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _assert_release_outside_project()
    try:
        if args.publish:
            return cmd_publish(args)
        if args.promote:
            return cmd_promote(args)
        if args.rollback:
            return cmd_rollback(args)
        if args.prune:
            return cmd_prune(args)
        return cmd_status(args)
    except KeyboardInterrupt:
        say("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
