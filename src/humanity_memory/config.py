"""配置装配：出厂 TOML + 用户/项目覆盖 + 环境变量覆盖。

分工
----
- **行为参数**（阈值、周期、权重、压缩档）全在 TOML 里，三层深合并：
  项目 > 用户 > 出厂。本模块**不含任何可调数值**，只负责
  「找到文件 → 解析 → 合并 → 校验」。
- **凭据**（embedding 的 api_key）只走 `.env` / 环境变量，不进 TOML。
- **路径**（home、库文件、工作区）也不进 TOML：它由运行环境决定，
  写进配置只会让同一份配置在不同机器上打架。

覆盖优先级
----------
`真实环境变量 > TOML（项目 > 用户 > 安装 > 出厂）`

环境变量是最高优先级，保留给三类用法：令牌注入、mcp.json 的 env、调试时临时覆盖。
它不该成为常态配置手段——那样就回到「参数散落在机器上、换台机器行为不一样」的老路。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .resources import resource

DEFAULT_HOME = Path.home() / ".humanity-memory"

DEFAULT_CONFIG_NAME = "memory.default.toml"
OVERRIDE_CONFIG_NAME = "memory.toml"

# 项目里的记忆目录名（与 paths.WORKSPACE_DIR 一致；此处不 import 以免循环依赖）
_WORKSPACE_DIR = ".ai_memory"


class ConfigError(RuntimeError):
    """配置写错。带着「哪一段、哪个键」的信息抛出去，让人能直接改。"""


# ---------------------------------------------------------------- TOML 读写


def _toml_load(path: Path) -> dict[str, Any]:
    """解析一个 TOML 文件。Python 3.10 用 tomli，3.11+ 用标准库 tomllib。"""
    try:
        if sys.version_info >= (3, 11):
            import tomllib as _toml

            with path.open("rb") as fh:
                return _toml.load(fh)
        import tomli as _toml  # type: ignore[no-redef]

        return _toml.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"读不到配置文件 {path}：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 —— tomllib 抛 TOMLDecodeError，tomli 同理
        raise ConfigError(f"配置文件 {path} 语法错误：{exc}") from exc


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """逐键深合并：字典递归，其它类型（含数组、数组表）整体替换。

    数组表（`[[forget.levels]]`）必须是整体替换而不是逐元素合并 —— 等级表的
    顺序和完整性是一体的，按元素覆盖只会拼出一张自相矛盾的表。
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------- 层定位


def _find_workspace_dir(start: Path) -> Path | None:
    """从 start 向上找 `.ai_memory/`，只读不创建（加载配置不该有副作用）。"""
    try:
        here = start.resolve()
    except OSError:
        return None
    for candidate in (here, *here.parents):
        path = candidate / _WORKSPACE_DIR
        if path.is_dir():
            return path
    return None


def peek_workspace_dir(environ: dict[str, str] | None = None, home: Path | None = None) -> Path | None:
    """猜当前工作区（只为了决定读不读项目级 memory.toml）。

    顺序：`HM_WORKSPACE` → `last-workspace.txt` → cwd 向上查找。
    猜错不影响正确性，只影响项目级覆盖是否被读进来；显式传 `workspace=`
    给 `Config.load` 时以传入值为准。
    """
    env = os.environ if environ is None else environ
    raw = env.get("HM_WORKSPACE", "").strip()
    if raw:
        found = _find_workspace_dir(Path(raw).expanduser())
        if found:
            return found
    if home is not None:
        marker = home / "last-workspace.txt"
        if marker.is_file():
            try:
                saved = marker.read_text(encoding="utf-8").strip()
            except OSError:
                saved = ""
            if saved:
                found = _find_workspace_dir(Path(saved).expanduser())
                if found:
                    return found
    return _find_workspace_dir(Path.cwd())


def install_config_file() -> Path:
    """**安装位置**的配置：与 `.env` 同目录，两态各取一处，都不看 cwd。

    - 源码态：`<仓库根>/memory.toml`
    - 冻结态：`<发布根>/memory.toml`（与 `current/` 同级，跨版本共享）

    这一层的定位是「这套安装的**全量**基线」：由 `config write-template` 从出厂默认
    整份生成，人可以在这里一眼看到所有可调项并直接改，不用翻进包目录。
    与用户级 / 项目级的区别只在定位：那两层是**增量**，只写要改的键。
    """
    return env_file().parent / OVERRIDE_CONFIG_NAME


def config_layers(home: Path, workspace: Path | None = None) -> list[tuple[str, Path]]:
    """按优先级从低到高返回配置层。只返回**实际存在**的覆盖文件。

    合并是 `deep_merge` 逐键递归的，所以任何一层都只写自己关心的键即可 ——
    缺的键从更底层补。这也让「安装层是全量模板、出厂升级又加了新键」不会报缺项。
    """
    layers: list[tuple[str, Path]] = [("出厂", resource(DEFAULT_CONFIG_NAME))]
    install = install_config_file()
    if install.is_file():
        layers.append(("安装", install))
    user = home / OVERRIDE_CONFIG_NAME
    if user.is_file():
        layers.append(("用户", user))
    if workspace is not None:
        project = workspace / OVERRIDE_CONFIG_NAME
        if project.is_file():
            layers.append(("项目", project))
    return layers


def _dig(data: dict[str, Any], *path: str) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"配置缺少必需项：[{'.'.join(path)}]（检查 memory.default.toml 是否完整）")
        node = node[key]
    return node


def _optional(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


# ---------------------------------------------------------------- 启动校验


def validate_settings(settings: dict[str, Any]) -> list[str]:
    """§6.6 的一致性检查。返回问题清单，空列表才算通过。

    每条都指到**具体键**：配置写错时，人的第一反应是打开文件改一行，
    报错信息必须能直接对应到那一行。
    """
    problems: list[str] = []

    levels = _optional(settings, "forget", "levels", default=[])
    if not isinstance(levels, list) or not levels:
        return ["[forget.levels] 为空：至少要有一档，且必须含 0 级"]

    try:
        numbers = [int(item["level"]) for item in levels]
    except (KeyError, TypeError, ValueError):
        return ["[forget.levels] 每档都必须有整数 level"]

    if len(set(numbers)) != len(numbers):
        dupes = sorted({n for n in numbers if numbers.count(n) > 1})
        problems.append(f"[[forget.levels]] 的 level 重复：{dupes}")
    if 0 not in numbers:
        problems.append("[[forget.levels]] 缺少 0 级（遗忘层是删除判定的锚点，不能省）")

    # 降序排列：后面「高等级 vs 低等级」的滞回检查按相邻对遍历，方向必须确定
    ordered = sorted(
        (item for item in levels if "level" in item),
        key=lambda i: int(i["level"]),
        reverse=True,
    )
    plain = [int(i["level"]) for i in ordered]
    top = max(plain)
    for item in ordered:
        level = int(item["level"])
        if level == top:
            if "up_threshold" in item:
                problems.append(f"[[forget.levels]] level={level} 是顶档，不能再有 up_threshold")
        elif level != 0 and "up_threshold" not in item:
            problems.append(f"[[forget.levels]] level={level} 缺少 up_threshold（少了会静默不升档）")
        if level != 0:
            if "down_threshold" not in item:
                problems.append(f"[[forget.levels]] level={level} 缺少 down_threshold（少了会静默不降档）")
            for key in ("period_experience_days", "period_episode_days"):
                if key not in item:
                    problems.append(f"[[forget.levels]] level={level} 缺少 {key}（不参与裁定就没有周期）")

    # 滞回缓冲不能倒挂。两条线指的是同一条边界：
    #   「升到 level=N」看 N 的 up_threshold，「从 N 跌回 N-1」看 N 的 down_threshold。
    # 所以 N 的升档线必须高于 N 的降档线，也高于 N+1 的降档线 ——
    # 后者一旦倒挂，活跃度抖一下就跨两档横跳，等级永远稳不下来。
    by_level = {int(item["level"]): item for item in ordered}
    for level, item in by_level.items():
        up = item.get("up_threshold")
        if up is None:
            continue
        own_down = item.get("down_threshold")
        if own_down is not None and float(up) <= float(own_down):
            problems.append(
                f"滞回倒挂：level={level} 的 up_threshold={up} "
                f"不高于同档 down_threshold={own_down}（进来就被弹走）"
            )
        above = by_level.get(level + 1)
        above_down = above.get("down_threshold") if above else None
        if above_down is not None and float(up) <= float(above_down):
            problems.append(
                f"滞回倒挂：level={level} 的 up_threshold={up} "
                f"不高于 level={level + 1} 的 down_threshold={above_down}"
            )

    bands = _optional(settings, "initial", "bands", default=[])
    if not isinstance(bands, list) or not bands:
        problems.append("[initial.bands] 为空：录入时无法决定初始等级")
    else:
        band_levels = [int(b.get("level", -1)) for b in bands]
        if any(lv < 0 for lv in band_levels):
            problems.append("[initial.bands] 每档都必须有 level")
        else:
            if max(band_levels) > top - 1:
                problems.append(
                    f"[initial.bands] 最大 level={max(band_levels)} 超过上限 {top - 1}：顶档只能靠活跃度挣，不能录入即设"
                )
            if min(band_levels) < 2:
                problems.append(
                    f"[initial.bands] 最小 level={min(band_levels)} 低于下限 2：新记忆不该一进来就在遗忘边缘"
                )
        mins = [b.get("min_importance") for b in bands]
        if any(not isinstance(m, (int, float)) for m in mins):
            problems.append("[initial.bands] 每档都必须有数值 min_importance")
        elif list(mins) != sorted(mins, reverse=True):
            problems.append("[initial.bands] min_importance 必须按降序排列（按顺序匹配第一个满足的档）")

    verify = _optional(settings, "confidence", "verify_on_read")
    fact = _optional(settings, "confidence", "treat_as_fact")
    if verify is None or fact is None:
        problems.append("[confidence] 缺少 verify_on_read / treat_as_fact")
    elif not (0 < float(verify) < float(fact) < 1):
        problems.append(
            f"[confidence] 必须满足 0 < verify_on_read({verify}) < treat_as_fact({fact}) < 1：中间带不存在就无法表达「存疑」"
        )

    revive = _optional(settings, "forget", "revive_to_level")
    if revive is None:
        problems.append("[forget] 缺少 revive_to_level")
    elif int(revive) not in plain:
        problems.append(f"[forget] revive_to_level={revive} 不是存在的等级：复活目标落不了地")

    required = _optional(settings, "compress", "required_compression")
    if not isinstance(required, list):
        problems.append("[compress] required_compression 必须是数组")
    elif len(required) != len(levels):
        problems.append(
            f"[compress] required_compression 长度 {len(required)} ≠ 等级档数 {len(levels)}："
            "它按 forget_level 当下标取值，短了会越界、长了会静默错位"
        )

    dup_floor = _optional(settings, "dedup", "dup_floor")
    if dup_floor is None:
        problems.append("[dedup] 缺少 dup_floor（置 0 表示关闭查重，但键必须在）")
    elif not (0 <= float(dup_floor) <= 1):
        problems.append(f"[dedup] dup_floor={dup_floor} 不在 0~1：它比的是余弦相似度")
    dup_bonus = _optional(settings, "dedup", "dup_bonus")
    if dup_bonus is None:
        problems.append("[dedup] 缺少 dup_bonus")
    elif float(dup_bonus) < 0:
        problems.append(f"[dedup] dup_bonus={dup_bonus} 为负：重复写入只会让既有条目更难被裁掉")
    topk = _optional(settings, "dedup", "topk")
    if topk is None:
        problems.append("[dedup] 缺少 topk")
    elif int(topk) < 1:
        problems.append(f"[dedup] topk={topk} 小于 1：查重没有候选可看")

    return problems


# ---------------------------------------------------------------- .env 解析


def parse_env_file(path: Path) -> dict[str, str]:
    """解析极简 .env：KEY=VALUE，支持 # 注释、export 前缀、成对引号。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


# `.env` 的位置：两态各取一处，**都不看 cwd**。
#
#   - 源码态：本文件在 `<仓库>/src/humanity_memory/`，parents[2] 即仓库根。
#     仓库整体搬走时无需改任何代码。
#   - 冻结态（PyInstaller onedir）：`sys.executable` 是启动路径，可能经过
#     `current` 这个目录 junction，必须先 resolve() 才落到 `v{ver}/`，
#     再退两级才是发布根的 `.env`。放发布根而不是版本目录，好处是跨版本共享一份、
#     回滚不必跟着换凭据。
#
# 刻意不读 cwd/.env：exe 的 cwd 由 MCP 客户端决定，比源码态更不可信，
# 凭据静默失效是最难查的一类故障。
def env_file() -> Path:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return exe.resolve().parent.parent / ".env"
    return Path(__file__).resolve().parents[2] / ".env"


ENV_FILE = env_file()


def _collect_env() -> dict[str, str]:
    """优先级：真实环境变量 > ENV_FILE（唯一的 .env 来源）。

    只读 ENV_FILE 这一个文件，不再叠加 ~/.humanity-memory/.env 或 cwd/.env：
    配置加载与启动目录彻底解耦，避免 cwd 漂移让凭据静默失效。
    真实环境变量仍作为最高优先级保留 —— 令牌注入、mcp.json 的 env、
    CLI 工具的显式覆盖都依赖这一层。
    """
    merged = parse_env_file(ENV_FILE)
    merged.update({k: v for k, v in os.environ.items() if v != ""})
    return merged


# ---------------------------------------------------------------- Config


@dataclass(slots=True)
class Config:
    # 存储（路径不进 TOML，由运行环境决定）
    home: Path
    db_path: Path
    default_namespace: str

    # embedding
    embed_base_url: str
    embed_api_key: str
    embed_model: str
    embed_dim: int
    embed_batch: int
    embed_timeout: float

    # 检索权重
    w_retrieval: float
    w_importance: float
    w_level: float

    # 录入默认值
    default_importance: dict[str, float]

    # 生命周期
    archive_ttl: dict[str, float]
    archive_max_access: int
    purge_after_days: float
    reinforce_boost: float

    # 其它
    vec_extension: str | None
    candidate_multiplier: int
    max_candidates: int

    # 合并后的完整 TOML（新机制从这里取值，别再加平铺字段）。
    # 全部字段无默认值：默认值只允许来自 TOML，留一个 0.0 占位就会
    # 出现「代码里悄悄生效的兜底值」，那正是这套配置要消灭的东西。
    settings: dict[str, Any]
    # 实际生效的层：[(层名, 路径), ...]，供 doctor 展示「这个值是谁给的」
    layers: list[tuple[str, Path]]

    # ---- 派生属性 ----
    @property
    def embedding_configured(self) -> bool:
        if not self.embed_model or not self.embed_base_url:
            return False
        if self.embed_api_key:
            return True
        # 本地服务通常不需要 key
        host = self.embed_base_url.lower()
        return any(h in host for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))

    @property
    def embed_endpoint(self) -> str:
        base = self.embed_base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    @property
    def forget_levels(self) -> list[dict[str, Any]]:
        """按 level 降序整理后的等级表，供生命周期判定使用。"""
        levels = _optional(self.settings, "forget", "levels", default=[])
        return sorted((dict(i) for i in levels), key=lambda i: int(i["level"]), reverse=True)

    @property
    def max_level(self) -> int:
        levels = self.forget_levels
        return int(levels[0]["level"]) if levels else 0

    @property
    def initial_bands(self) -> list[dict[str, Any]]:
        """录入时的初始等级档位（已由启动校验保证降序、上下限合法）。"""
        return [dict(item) for item in _optional(self.settings, "initial", "bands", default=[])]

    @property
    def activity_sim_floor(self) -> float:
        return float(_dig(self.settings, "activity", "sim_floor"))

    @property
    def activity_curve_power(self) -> float:
        return float(_dig(self.settings, "activity", "curve_power"))

    @property
    def dedup_floor(self) -> float:
        """经验查重阈值；<=0 表示关闭查重。"""
        return float(_dig(self.settings, "dedup", "dup_floor"))

    @property
    def dedup_bonus(self) -> float:
        return float(_dig(self.settings, "dedup", "dup_bonus"))

    @property
    def dedup_topk(self) -> int:
        return max(1, int(_dig(self.settings, "dedup", "topk")))

    @property
    def required_compression(self) -> list[int]:
        """`forget_level` → 目标 `compression_level` 的查表。

        下标就是等级，所以这张表的长度必须等于等级档数（启动校验保证）。
        压缩队列完全由它推导 —— 没有「待压缩」标记字段，不需要补偿逻辑。
        """
        raw = _optional(self.settings, "compress", "required_compression", default=[])
        return [int(v) for v in (raw or [])]

    @property
    def level_factor_power(self) -> float:
        return float(_dig(self.settings, "retrieval", "level_factor_power"))

    @property
    def deep_recall_floor(self) -> float:
        """常规检索的「失败线」：最高相似度低于它就值得去遗忘层翻一翻。"""
        return float(_dig(self.settings, "retrieval", "deep_recall_floor"))

    @property
    def revive_hits_needed(self) -> int:
        """0 级条目要被深度回忆命中几次才复活。"""
        return max(1, int(self.forget_settings.get("revive_hits", 2)))

    @property
    def revive_to_level(self) -> int:
        """复活落点。默认 1 —— 回 3 级等于给一次免费续命，遗忘层会变回收站。"""
        return max(1, int(self.forget_settings.get("revive_to_level", 1)))

    @property
    def level0_purge_days(self) -> float:
        return float(self.forget_settings.get("level0_purge_days", 180.0))

    @property
    def level_factor_overrides(self) -> dict[int, float]:
        """可选的逐级手调表；不填就走公式。"""
        raw = _optional(self.settings, "retrieval", "level_factor", default={}) or {}
        return {int(k): float(v) for k, v in raw.items()}

    @property
    def forget_settings(self) -> dict[str, Any]:
        """`[forget]` 段（revive_hits / revive_to_level / *_purge_days 等）。"""
        return _optional(self.settings, "forget", default={}) or {}

    @property
    def confidence_settings(self) -> dict[str, Any]:
        """`[confidence]` 段：两条阈值 + 加分/扣分曲线 + 证据级别权重。"""
        return _optional(self.settings, "confidence", default={}) or {}

    def ensure_home(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 装配 ----
    @classmethod
    def load(
        cls,
        environ: dict[str, str] | None = None,
        workspace: Path | None = None,
    ) -> "Config":
        """装配配置。

        - `environ` 为 None 时读 `.env` + 真实环境变量；显式传入则只认这一份
          （测试用，避免机器上的 `.env` 偷偷参与）。
        - `workspace` 是 `.ai_memory` 目录本身；不传则自动推断。
        """
        env = dict(_collect_env() if environ is None else environ)

        home = Path(env.get("HM_HOME") or DEFAULT_HOME).expanduser()
        ws_dir = workspace if workspace is not None else peek_workspace_dir(env, home)

        layers = config_layers(home, ws_dir)
        settings: dict[str, Any] = {}
        for _, path in layers:
            settings = deep_merge(settings, _toml_load(path))

        problems = validate_settings(settings)
        if problems:
            where = "、".join(f"{name}({path})" for name, path in layers)
            raise ConfigError(
                "配置校验未通过，拒绝启动：\n  - "
                + "\n  - ".join(problems)
                + f"\n生效的配置层：{where}\n"
                f"改法：在 {home / OVERRIDE_CONFIG_NAME} 或 "
                f"{ws_dir / OVERRIDE_CONFIG_NAME if ws_dir else '<项目>/.ai_memory/memory.toml'} "
                "里修正上面点名的键。"
            )

        def get(key: str) -> str:
            return (env.get(key) or "").strip()

        def env_float(key: str, fallback: float) -> float:
            raw = get(key)
            if not raw:
                return fallback
            try:
                return float(raw)
            except ValueError:
                raise ConfigError(f"环境变量 {key}={raw!r} 不是数字") from None

        def env_int(key: str, fallback: int) -> int:
            raw = get(key)
            if not raw:
                return fallback
            try:
                return int(raw)
            except ValueError:
                raise ConfigError(f"环境变量 {key}={raw!r} 不是整数") from None

        def section_float(section: str, key: str) -> float:
            return float(_dig(settings, section, key))

        def env_or(section: str, key: str, env_key: str) -> float:
            return env_float(env_key, section_float(section, key))

        archive_ttl = {
            kind: float(value)
            for kind, value in _dig(settings, "lifecycle", "archive", "ttl").items()
        }
        archive_ttl["experience"] = env_float(
            "HM_ARCHIVE_TTL_EXPERIENCE", archive_ttl["experience"]
        )
        archive_ttl["episode"] = env_float(
            "HM_ARCHIVE_TTL_EPISODE", archive_ttl["episode"]
        )

        return cls(
            home=home,
            db_path=Path(get("HM_DB_PATH") or str(home / "memory.db")).expanduser(),
            default_namespace=get("HM_NAMESPACE") or "default",
            embed_base_url=get("HM_EMBED_BASE_URL") or str(_dig(settings, "embedding", "base_url")),
            # 凭据只从 env 来，配置里没有这一项
            embed_api_key=get("HM_EMBED_API_KEY"),
            embed_model=get("HM_EMBED_MODEL") or str(_dig(settings, "embedding", "model")),
            embed_dim=env_int("HM_EMBED_DIM", int(_dig(settings, "embedding", "dim"))),
            embed_batch=max(1, env_int("HM_EMBED_BATCH", int(_dig(settings, "embedding", "batch")))),
            embed_timeout=env_float("HM_EMBED_TIMEOUT", float(_dig(settings, "embedding", "timeout"))),
            w_retrieval=env_or("retrieval", "w_retrieval", "HM_W_RETRIEVAL"),
            w_importance=env_or("retrieval", "w_importance", "HM_W_IMPORTANCE"),
            w_level=env_or("retrieval", "w_level", "HM_W_LEVEL"),
            default_importance={
                kind: float(value)
                for kind, value in _dig(settings, "defaults", "importance").items()
            },
            archive_ttl=archive_ttl,
            archive_max_access=env_int(
                "HM_ARCHIVE_MAX_ACCESS",
                int(_dig(settings, "lifecycle", "archive", "max_access")),
            ),
            purge_after_days=env_float(
                "HM_PURGE_AFTER_DAYS",
                float(_dig(settings, "lifecycle", "purge_after_days")),
            ),
            reinforce_boost=env_float(
                "HM_REINFORCE_BOOST",
                float(_dig(settings, "lifecycle", "reinforce_boost")),
            ),
            vec_extension=get("HM_VEC_EXTENSION") or None,
            candidate_multiplier=env_int(
                "HM_CANDIDATE_MULTIPLIER",
                int(_dig(settings, "search", "candidate_multiplier")),
            ),
            max_candidates=env_int(
                "HM_MAX_CANDIDATES", int(_dig(settings, "search", "max_candidates"))
            ),
            settings=settings,
            layers=layers,
        )

    # ---- 展示 ----
    def describe(self) -> dict[str, object]:
        """脱敏后的配置摘要，供 doctor / stats 展示。"""
        key = self.embed_api_key
        masked = "not set"
        if key:
            masked = key[:6] + "..." + key[-4:] if len(key) > 12 else "set"
        return {
            "home": str(self.home),
            "db_path": str(self.db_path),
            "namespace": self.default_namespace,
            "layers": [
                {"scope": name, "path": str(path)} for name, path in self.layers
            ],
            "embedding": {
                "configured": self.embedding_configured,
                "base_url": self.embed_base_url or "not set",
                "model": self.embed_model or "not set",
                "dim": self.embed_dim,
                "api_key": masked,
            },
            "weights": {
                "retrieval": self.w_retrieval,
                "importance": self.w_importance,
                "level": self.w_level,
            },
            "archive_ttl_days": self.archive_ttl,
            "archive_max_access": self.archive_max_access,
            "purge_after_days": self.purge_after_days,
            "required_compression": self.required_compression,
        }

    def describe_settings(self) -> dict[str, object]:
        """合并后的生效值（新机制参数）。doctor 用它证明「配置真的被读进去了」。"""
        return {
            "sources": [{"scope": name, "path": str(path)} for name, path in self.layers],
            "forget": {
                "levels": [
                    {
                        "level": int(item["level"]),
                        "period_experience_days": item.get("period_experience_days"),
                        "period_episode_days": item.get("period_episode_days"),
                        "up_threshold": item.get("up_threshold"),
                        "down_threshold": item.get("down_threshold"),
                        "compress": item.get("compress"),
                    }
                    for item in self.forget_levels
                ],
                "level0_purge_days": _optional(self.settings, "forget", "level0_purge_days"),
                "archived_purge_days": _optional(self.settings, "forget", "archived_purge_days"),
                "revive_hits": _optional(self.settings, "forget", "revive_hits"),
                "revive_to_level": _optional(self.settings, "forget", "revive_to_level"),
            },
            "initial_bands": _optional(self.settings, "initial", "bands", default=[]),
            "confidence": {
                "verify_on_read": _optional(self.settings, "confidence", "verify_on_read"),
                "treat_as_fact": _optional(self.settings, "confidence", "treat_as_fact"),
                "gain_k": _optional(self.settings, "confidence", "gain_k"),
                "gain_alpha": _optional(self.settings, "confidence", "gain_alpha"),
                "loss_k": _optional(self.settings, "confidence", "loss_k"),
                "level_weight": _optional(
                    self.settings, "confidence", "level_weight", default={}
                ),
            },
            "activity": _optional(self.settings, "activity", default={}),
            "retrieval": _optional(self.settings, "retrieval", default={}),
            "dedup": _optional(self.settings, "dedup", default={}),
            "compress": _optional(self.settings, "compress", default={}),
        }
