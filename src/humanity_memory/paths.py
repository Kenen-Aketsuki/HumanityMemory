"""工作区与全局库的动态寻址（严格物理隔离）。

设计要点
--------
1. 从 cwd 向上逐级查找 `.ai_memory/`，命中即用 —— 这样在项目的子目录里
   启动也能找到项目根的记忆库，而不是在子目录里再造一个。
2. 找不到才在当前目录创建。绝不往盘符根目录创建。
3. 全局库固定在 `~/.humanity-memory/`，只放跨项目事实与全局信条。
4. 两个库物理独立：删项目目录 = 删项目记忆，不存在残留。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_DIR = ".ai_memory"
WORKSPACE_DB = "workspace.db"
GLOBAL_DB = "global.db"
RULES_FILE = "CORE_RULES.md"
LAST_WORKSPACE_FILE = "last-workspace.txt"

# 项目里已有的约定位置：有则直接复用，不再新建 CORE_RULES.md
CONVENTIONAL_RULES = Path(".workbuddy") / "memory" / "信条.md"

# 出现这些标记的目录才被认为是「项目根」，才允许自动创建 .ai_memory。
# 目的是避免在安装目录、用户主目录这类地方乱拉屎。
PROJECT_MARKERS = (
    ".git",
    ".workbuddy",
    "ProjectSettings",   # Unity
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
)

DEFAULT_HOME_NAME = ".humanity-memory"


def global_home() -> Path:
    """全局库目录：`HM_HOME` 优先，否则 `~/.humanity-memory`。"""
    raw = os.environ.get("HM_HOME")
    return Path(raw).expanduser() if raw else Path.home() / DEFAULT_HOME_NAME


# ---------------------------------------------------------------- 上次工作区
# 一条配置服务多个空间的关键：client 每次重启进程，不一定带着项目路径。
# 把上次用过的工作区记下来，下次启动就能接上，agent 也能用工具显式切。


def last_workspace_file(home: Path | str | None = None) -> Path:
    base = Path(home).expanduser() if home else global_home()
    return base / LAST_WORKSPACE_FILE


def read_last_workspace(home: Path | str | None = None) -> Path | None:
    """返回上次使用过的项目根目录；文件不存在或目录已消失则返回 None。"""
    path = last_workspace_file(home)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    root = Path(os.path.expandvars(raw)).expanduser()
    return root if root.is_dir() else None


def write_last_workspace(
    project_root: Path | str, home: Path | str | None = None
) -> Path:
    path = last_workspace_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(Path(project_root).expanduser().resolve()), encoding="utf-8")
    return path


def _is_filesystem_root(path: Path) -> bool:
    return path.parent == path


def find_workspace_dir(
    start: Path | str | None = None, *, create: bool = True
) -> Path | None:
    """自下而上查找 `.ai_memory`；没有则（可选）在起始目录创建。

    `HM_WORKSPACE` 可直接指定工作区根目录，跳过查找。
    """
    explicit = os.environ.get("HM_WORKSPACE")
    if explicit:
        root = Path(explicit).expanduser().resolve()
        target = root / WORKSPACE_DIR if root.name != WORKSPACE_DIR else root
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for candidate_root in (current, *current.parents):
        candidate = candidate_root / WORKSPACE_DIR
        if candidate.is_dir():
            return candidate

    if not create:
        return None

    # 不在盘符根、也不在用户主目录制造 .ai_memory（那里只该有全局库）
    try:
        home = Path.home().resolve()
    except OSError:  # pragma: no cover
        home = None
    if _is_filesystem_root(current) or (home is not None and current == home):
        return None

    target = current / WORKSPACE_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def find_project_root(start: Path | str | None = None) -> Path | None:
    """向上找第一个「像项目根」的目录（含 .git / ProjectSettings / package.json 等）。

    在子目录（如 `<项目>/Assets/Scripts`）里启动时，工作区应该建在项目根，
    而不是当前子目录。用户主目录和盘符根一律不算项目。
    """
    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    try:
        home = Path.home().resolve()
    except OSError:  # pragma: no cover
        home = None

    for candidate in (current, *current.parents):
        if _is_filesystem_root(candidate) or (home is not None and candidate == home):
            return None
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return None


@dataclass(slots=True)
class Layout:
    """一次解析得到的全部路径。"""

    workspace_dir: Path | None
    workspace_db: Path | None
    global_dir: Path
    global_db: Path
    workspace_rules: Path | None
    global_rules: Path

    @property
    def has_workspace(self) -> bool:
        return self.workspace_dir is not None

    def describe(self) -> dict[str, str | None]:
        return {
            "workspace_dir": str(self.workspace_dir) if self.workspace_dir else None,
            "workspace_db": str(self.workspace_db) if self.workspace_db else None,
            "global_dir": str(self.global_dir),
            "global_db": str(self.global_db),
        }


def resolve_layout(
    start: Path | str | None = None,
    *,
    create: bool = True,
    home: Path | str | None = None,
    rules_path: Path | str | None = None,
) -> Layout:
    """解析一次完整的路径布局。

    - `home` 显式指定全局库目录；不传则读环境变量 / 默认值。
    - `rules_path` 显式指定工作区信条文件；不传则用 `<工作区>/CORE_RULES.md`。
      这样能复用项目里已有的 Markdown 信条（如 `.workbuddy/memory/信条.md`），
      不必再维护第二份信条。
    """
    ws_dir = find_workspace_dir(start, create=create)
    g_dir = Path(home).expanduser() if home is not None else global_home()
    if create:
        g_dir.mkdir(parents=True, exist_ok=True)

    override = rules_path or os.environ.get("HM_RULES_PATH")
    if override:
        ws_rules = Path(override).expanduser()
    elif ws_dir is not None:
        # 优先复用项目里已有的 Markdown 信条，避免维护第二份
        conventional = ws_dir.parent / CONVENTIONAL_RULES
        ws_rules = conventional if conventional.is_file() else ws_dir / RULES_FILE
    else:
        ws_rules = None

    return Layout(
        workspace_dir=ws_dir,
        workspace_db=(ws_dir / WORKSPACE_DB) if ws_dir else None,
        global_dir=g_dir,
        global_db=g_dir / GLOBAL_DB,
        workspace_rules=ws_rules,
        global_rules=g_dir / RULES_FILE,
    )


# ---------------------------------------------------------------- 信条模板

RULES_TEMPLATE = """# 绝对信条（CORE RULES）

> 本文件是**思想钢印**：每次会话作为 System Prompt 最顶端静态加载。
> 不进入向量库、不参与检索、不参与遗忘淘汰。
> 只有用户明确要求时才修改；改动越少，KV Cache 命中率越高。

## 禁止事项

- 禁止执行 `git push`、`git commit`、分支切换等写操作，除非用户明确授权
- 禁止 `rm -rf`、`del /S /Q` 等递归删除，尤其在桌面/下载/文档目录
- 禁止手改 Unity 自动生成的 `.asset` / `.unity` / `ProjectSettings`，只改源码
- 禁止向外部服务发送用户私有内容

## 工作方式

- 交付前必须实际验证：代码要真跑过，不能"看起来对"
- 改动只留在工作区，不擅自 add/commit
- 遇到问题先诊断 + 给方案，用户确认后再改代码

## 记忆纪律

- 可迁移的机制与约定 → 写知识（kind=knowledge）；会变的版本/依赖/路径 → 写现状（kind=state）
- 一次具体事件的「现象 → 根因 → 落点」 → 写案例（kind=case），带单号的结论放这里
- 当天发生的事件、排查过程 → 写日志（log），只追加不改
- 现状换代 → 追加新条目并用 invalidates 顶掉旧的，不删除
"""


def ensure_rules(path: Path, *, template: str | None = None) -> Path:
    """信条文件不存在时用模板初始化。已存在则原样保留。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template or RULES_TEMPLATE, encoding="utf-8")
    return path
