"""工作区与全局库的动态寻址（严格物理隔离）。

解析链（2026-11-18 起，A1/A2）
------------------------------
身份在**令牌**里，位置由 cwd 决定：

1. `HM_WORKSPACE` 显式指定 → 直接用（运维 / 测试出口）；
2. 从 cwd 向上找 `.ai_memory`，**优先取含 `token.json` 的那个**：
   项目里同时躺着父目录的旧库时，令牌指向的才是这个项目的；
3. 一个带令牌的都没有 → 返回最近的那个 `.ai_memory`（让上层能说
   「有库但未接入」，而不是「什么都没有」，那两种情况给的出路不一样）；
4. 都没有 → `None`，**且不创建任何目录**。

第 4 条是 A1 的硬要求：未接入的工作区不该在磁盘上留下任何痕迹。
自动建库已经删除，所以这里不再有 `create` 分支。

布局两级
--------
`workspace_root` 是项目根（`D:/work/alpha`），`library_dir` 是库目录。
自有库时 `<root>/.ai_memory`，共享库时 `~/.humanity-memory/libraries/<label>` ——
后者不在项目里，所以它**不能**从 library_dir 反推出来，必须由令牌给。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_DIR = ".ai_memory"
WORKSPACE_DB = "workspace.db"
GLOBAL_DB = "global.db"
RULES_FILE = "CORE_RULES.md"
# 与 `token.TOKEN_NAME` 必须一致。这里不 import token：token 反过来要 import
# 本模块的 global_home，会成环。
TOKEN_FILE = "token.json"

# 项目里已有的约定位置：有则直接复用，不再新建 CORE_RULES.md
CONVENTIONAL_RULES = Path(".workbuddy") / "memory" / "信条.md"

DEFAULT_HOME_NAME = ".humanity-memory"


def global_home() -> Path:
    """全局库目录：`HM_HOME` 优先，否则 `~/.humanity-memory`。"""
    raw = os.environ.get("HM_HOME")
    return Path(raw).expanduser() if raw else Path.home() / DEFAULT_HOME_NAME


def find_workspace_dir(
    start: Path | str | None = None, *, include_tokenless: bool = False
) -> Path | None:
    """自下而上查找 `.ai_memory`，**只认带 `token.json` 的**。

    **只读**：不创建目录（A1）。找不到就是 `None`，由调用方决定是拒绝
    还是给 bind 清单。

    `include_tokenless=True` 才会返回「有库但没令牌」的那个 —— 这是给
    `bind_status` 准备 adopt 建议用的，主解析链不该拿它当工作区：
    那正是 2026-11-18 之前"随便撞见一个 `.ai_memory` 就打开"的老毛病。

    `HM_WORKSPACE` 是显式指定，直接认（它是运维/测试出口，不是猜测）。
    """
    explicit = os.environ.get("HM_WORKSPACE")
    if explicit:
        root = Path(explicit).expanduser().resolve()
        target = root / WORKSPACE_DIR if root.name != WORKSPACE_DIR else root
        return target if target.is_dir() else None

    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent

    tokenless: Path | None = None
    for candidate_root in (current, *current.parents):
        candidate = candidate_root / WORKSPACE_DIR
        if not candidate.is_dir():
            continue
        if (candidate / TOKEN_FILE).is_file():
            return candidate
        if tokenless is None:
            # 记下来但不立刻返回：再往上可能有个带令牌的真工作区
            tokenless = candidate
    return tokenless if include_tokenless else None


def find_workspace_root(start: Path | str | None = None) -> Path | None:
    """项目根（不论有没有令牌）。供 `bind_status` / 校验层判断"这里像不像工作区"。

    主解析链用 `find_workspace_dir()`（只认令牌）；这个函数故意宽一格，
    因为「有 `.ai_memory` 但没令牌」恰恰是最需要给出路的那个状态。
    """
    ws = find_workspace_dir(start, include_tokenless=True)
    return ws.parent if ws is not None else None


@dataclass(slots=True)
class Layout:
    """一次解析得到的全部路径。"""

    # 项目根。共享库时它由令牌给出；只有 library_dir 时可能为 None。
    workspace_root: Path | None
    # 库目录：自有库 = `<root>/.ai_memory`，共享库 = `libraries/<label>`
    library_dir: Path | None
    workspace_db: Path | None
    global_dir: Path
    # 全局库已于 2026-11-17 关闭（§8.2）。这个字段留着只为了两件事：
    # 一是 `describe()` 能把"它本该在哪"告诉人，二是留给退役文件的路径计算。
    # **任何代码都不该再 `open_db(global_db)`** —— 打开就等于建一个空库回来。
    global_db: Path
    workspace_rules: Path | None
    global_rules: Path

    @property
    def workspace_dir(self) -> Path | None:
        """旧名 = `library_dir`。主路径已改叫 library_dir，这里留一个版本给
        未及改造的调用方，避免同一次改动里既有语义变更又有大面积改名。"""
        return self.library_dir

    @property
    def has_workspace(self) -> bool:
        return self.library_dir is not None

    def describe(self) -> dict[str, str | None]:
        return {
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "library_dir": str(self.library_dir) if self.library_dir else None,
            "workspace_dir": str(self.library_dir) if self.library_dir else None,
            "workspace_db": str(self.workspace_db) if self.workspace_db else None,
            "global_dir": str(self.global_dir),
            "global_db": str(self.global_db),
        }


def empty_layout(
    *, root: Path | str | None = None, home: Path | str | None = None
) -> Layout:
    """没有库的布局：只带全局目录信息，`library_dir` / db / rules 全 None。

    拒绝态用它 —— 绝不能拿"恰好能找到的某个库"去凑一个布局出来。
    """
    g_dir = Path(home).expanduser() if home is not None else global_home()
    return Layout(
        workspace_root=Path(root).expanduser() if root else None,
        library_dir=None,
        workspace_db=None,
        global_dir=g_dir,
        global_db=g_dir / GLOBAL_DB,
        workspace_rules=None,
        global_rules=g_dir / RULES_FILE,
    )


def resolve_layout(
    start: Path | str | None = None,
    *,
    create: bool = True,
    home: Path | str | None = None,
    rules_path: Path | str | None = None,
    library_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Layout:
    """解析一次完整的路径布局。

    - `library_dir` 显式给出库目录（令牌校验通过后的主路径）。给了就不再走
      cwd 查找 —— 库归属由 registry 决定，不由"当前在哪个目录"决定。
    - `workspace_root` 显式给出项目根（共享库时用）。信条跟着项目走，
      所以这个值决定 `CORE_RULES` 落在哪。
    - `rules_path` 显式指定信条文件，优先级最高。
    - `create` 只影响**全局目录**（`~/.humanity-memory`）。项目目录一律不建：
      未接入就该什么痕迹都没有（A1）。
    """
    if library_dir is not None:
        lib = Path(os.path.expandvars(str(library_dir))).expanduser()
        root = Path(workspace_root).expanduser() if workspace_root else None
        if root is None and lib.name == WORKSPACE_DIR:
            root = lib.parent
    elif start is not None:
        # 显式给了路径（CLI `--workspace` / MCP `--workspace`）：直接用，
        # 不搜索、不创建。注意这与"发现"是两件事 —— 显式路径不需要令牌，
        # 调用方自己负责校验（未接入时由 bind 建）。
        given = Path(os.path.expandvars(str(start))).expanduser()
        lib = given if given.name == WORKSPACE_DIR else given / WORKSPACE_DIR
        if not lib.is_dir():
            lib = None
        root = lib.parent if lib is not None else None
    else:
        lib = find_workspace_dir()
        root = lib.parent if lib is not None else None
        if workspace_root is not None:
            root = Path(workspace_root).expanduser()

    g_dir = Path(home).expanduser() if home is not None else global_home()
    if create:
        g_dir.mkdir(parents=True, exist_ok=True)

    override = rules_path or os.environ.get("HM_RULES_PATH")
    if override:
        ws_rules = Path(override).expanduser()
    elif root is not None and (root / CONVENTIONAL_RULES).is_file():
        # 优先复用项目里已有的 Markdown 信条，避免维护第二份
        ws_rules = root / CONVENTIONAL_RULES
    elif lib is not None:
        ws_rules = lib / RULES_FILE
    else:
        ws_rules = None

    return Layout(
        workspace_root=root,
        library_dir=lib,
        workspace_db=(lib / WORKSPACE_DB) if lib else None,
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

- 可迁移的机制与约定、根因 → 写经验（kind=experience）；一次具体事务的过程与试错 → 写经历（kind=episode）。kind 只认这两个，旧值 knowledge / state / case / narrative 会被直接拒绝
- 当天发生的事件、排查过程 → 用 log_append 写日志，只追加不改（触发器强制），复盘后清空
- 经验被推翻 → 追加新经验并用 invalidates 让旧的失效，不删除、不改写正文；经历没有可信度，不参与 memory_verify
- 分值、等级、遗忘、查重一律由工具裁定，不替它们判断；写入只给 confidence 初值与可验证的证据级别
"""


def ensure_rules(path: Path, *, template: str | None = None) -> Path:
    """信条文件不存在时用模板初始化。已存在则原样保留。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template or RULES_TEMPLATE, encoding="utf-8")
    return path
