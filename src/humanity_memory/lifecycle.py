"""记忆的生命周期：等级裁定、综合打分、归档判据。

设计取舍
--------
排序的第三项是**遗忘等级**，不是「强度」。旧方案用半衰期衰减 + 访问频次算一个
0~1 的 strength，问题在于它把两件事混在一个数里：内容多久没被碰（衰减）和
被碰过几次（频次）。等级把这件事拆干净了 —— 一个周期裁定一次，只动一级，
而且三个月后能回答「它为什么在这一级」。

`decay` / `lifecycle_strength` / `usage_factor` 已随旧方案删除；
半衰期配置也从 TOML 里撤了，它没有消费者。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DAY_MS = 86_400_000.0


def reinforce(strength: float, boost: float = 0.20) -> float:
    """强化向 1.0 渐近：访问越频繁，单次提升越小，避免直接刷满。"""
    boost = min(max(boost, 0.0), 1.0)
    return min(1.0, strength + (1.0 - strength) * boost)


def similarity_from_cosine_distance(distance: float) -> float:
    """vec0 cosine 度量返回 1 - cos，范围 [0, 2]，映射到 [0, 1]。"""
    return max(0.0, min(1.0, 1.0 - distance / 2.0))


def combine_score(
    *,
    retrieval: float,
    importance: float,
    level: float,
    w_retrieval: float,
    w_importance: float,
    w_level: float,
) -> float:
    """三项加权。`level` 传的是 `level_factor()` 的结果（0~1 倍率），不是等级本身。"""
    total = w_retrieval + w_importance + w_level
    if total <= 0:
        return 0.0
    return (
        w_retrieval * retrieval + w_importance * importance + w_level * level
    ) / total


def archive_decision(
    created_ms: int,
    last_access_ms: int | None,
    access_count: int,
    now_ms: int,
    ttl_days: float,
    max_access: int,
) -> tuple[bool, str]:
    """判断一条记忆该不该归档。

    为什么不用「分数低于阈值」：任何把新鲜度卷进分数的方案都有一个下限
    （新内容不该被惩罚），结果再冷的条目也跌不到 0，拿绝对阈值归档永远不触发。
    改用两个显性条件 —— 「多久没被碰过」+「被访问过几次」，更好解释也好调。

    归档是**可逆的**：只置 archived_at，默认检索不到，内容一行不动。
    """
    touch = last_access_ms if last_access_ms is not None else created_ms
    age_days = max(0.0, now_ms - touch) / DAY_MS

    if access_count > max_access:
        return False, f"访问 {access_count} 次（阈值 {max_access}），保留"
    if age_days < ttl_days:
        return False, f"{age_days:.0f} 天未确认（未到 {ttl_days:.0f} 天）"
    return True, f"{age_days:.0f} 天未用且仅访问 {access_count} 次"


# ---------------------------------------------------------------- 初始粒度
# 仿生记忆的第一个维度：**重要的事一开始就记得细，琐事一开始就模糊**。
# 不能所有经历都从「完整叙述」起步 —— 那等于把「昨天吃了什么」也录成纪录片。

INITIAL_LEVEL_THRESHOLDS = ((0.75, 0), (0.45, 1), (0.0, 2))


def initial_compression_level(importance: float) -> int:
    """由重要性推导初始压缩级别。

    0.75 以上 → level 0（完整叙述）
    0.45~0.75 → level 1（要点）
    0.45 以下 → level 2（一句话）
    """
    value = max(0.0, min(1.0, float(importance)))
    for floor, level in INITIAL_LEVEL_THRESHOLDS:
        if value >= floor:
            return level
    return 2


def compression_target(required: Sequence[int], forget_level: int) -> int:
    """把 `forget_level` 查成目标 `compression_level`。

    越界取边界值而不是报错：等级表由配置驱动，改了档数忘了改压缩表时，
    「压到最深」比「整条流程崩掉」更接近本意。真正的长度校验在启动时做
    （`validate_settings`），这里只是不让运行时因为下标越界炸掉。
    """
    if not required:
        return 0
    index = max(0, min(int(forget_level), len(required) - 1))
    return int(required[index])


# ---------------------------------------------------------------- 遗忘等级
# 等级不是枚举，是数据：档位、周期、双阈值、压缩档全在 TOML 的 [[forget.levels]]。
# 这里只把那张表读成运行时视图，判定本身是纯函数 —— 可单测、也能三个月后照着解释。
#
# 「周期维度」按表分：经验一张表、经历一张表，各配一个周期。
# 分开配置是因为经历忘得快 —— 这与「这条属于四类里的哪一类」无关，类型只有两种。

PERIOD_EXPERIENCE = "experience"
PERIOD_EPISODE = "episode"

_PERIOD_KINDS = frozenset({PERIOD_EXPERIENCE, PERIOD_EPISODE})


def period_kind(row_kind: str | None) -> str:
    """对象类型 → 周期维度。只有经历走快周期，其余（经验 / 日志）走慢周期。"""
    value = (row_kind or "").strip().lower()
    return PERIOD_EPISODE if value in ("episode", "narrative", "case") else PERIOD_EXPERIENCE


@dataclass(frozen=True)
class LevelSpec:
    """等级表的一档。字段全可缺省，缺了就是「这档没有这条规矩」。"""

    level: int
    period_experience_days: float | None = None
    period_episode_days: float | None = None
    up_threshold: float | None = None
    down_threshold: float | None = None
    compress: str | None = None

    def period_days(self, dimension: str) -> float | None:
        if dimension == PERIOD_EPISODE:
            return self.period_episode_days
        return self.period_experience_days


@dataclass(frozen=True)
class Decision:
    """一次裁定的结论。`action` 只有三种，调用方不必认识等级表。"""

    level: int
    action: str  # up / down / hold
    reason: str


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - 配置校验已挡住
        return None


class LevelTable:
    """`[[forget.levels]]` 的运行时视图。

    只做查询，不持有连接、不写库 —— 判定与落盘分开，才能给「干跑」一条同一套逻辑的路。
    """

    __slots__ = ("_specs", "max_level", "levels")

    def __init__(self, levels: Sequence[Mapping[str, Any]]) -> None:
        specs: dict[int, LevelSpec] = {}
        for item in levels:
            spec = LevelSpec(
                level=int(item["level"]),
                period_experience_days=_as_float(item.get("period_experience_days")),
                period_episode_days=_as_float(item.get("period_episode_days")),
                up_threshold=_as_float(item.get("up_threshold")),
                down_threshold=_as_float(item.get("down_threshold")),
                compress=item.get("compress"),
            )
            specs[spec.level] = spec
        self._specs = specs
        self.levels = tuple(sorted(specs, reverse=True))
        self.max_level = self.levels[0] if self.levels else 0

    @classmethod
    def from_config(cls, cfg: Any) -> "LevelTable":
        """`cfg` 只要求有 `forget_levels`（Config 的派生属性），不 import Config。"""
        return cls(cfg.forget_levels)

    # ---- 查询 ----
    def spec(self, level: int) -> LevelSpec | None:
        return self._specs.get(int(level))

    def has(self, level: int) -> bool:
        return int(level) in self._specs

    def period_days(self, level: int, dimension: str = PERIOD_EXPERIENCE) -> float | None:
        spec = self._specs.get(int(level))
        return None if spec is None else spec.period_days(dimension)

    def period_ms(self, level: int, dimension: str = PERIOD_EXPERIENCE) -> float | None:
        days = self.period_days(level, dimension)
        return None if days is None else days * DAY_MS

    def is_due(
        self,
        level: int,
        period_start_ms: int | None,
        now_ms: int,
        dimension: str = PERIOD_EXPERIENCE,
    ) -> bool:
        """到没到该裁定的时刻。

        `period_start_ms is None`（老数据没写过这列）→ 视为「还没开始计时」，
        由调用方拿创建时刻兜底；这里返回 False，避免把一堆老条目一次性全判了。
        """
        if int(level) <= 0:
            return False
        span = self.period_ms(level, dimension)
        if span is None or period_start_ms is None:
            return False
        return (int(now_ms) - int(period_start_ms)) >= span

    # ---- 裁定 ----
    def decide(
        self,
        level: int,
        activity: float,
        dimension: str = PERIOD_EXPERIENCE,
    ) -> Decision:
        """周期末的等级裁定：升一级 / 降一级 / 维持，**最多动一级**。

        滞回：升级线与降级线之间是死区，落在死区不动作。配置校验保证
        升级线恒高于本档与上一档的降级线，所以「同时满足升和降」不可能出现。
        """
        level = int(level)
        spec = self._specs.get(level)
        if spec is None:
            raise ValueError(f"等级 {level} 不在配置的等级表里（{self.levels}）")
        if level <= 0:
            return Decision(level, "hold", "遗忘层不参与周期裁定（只能复活或删除）")

        activity = float(activity)
        up = spec.up_threshold
        if up is not None and activity >= up:
            if level >= self.max_level:
                return Decision(level, "hold", f"活跃度 {activity:.1f} ≥ {up:g}，但 {level} 已是顶档")
            return Decision(level + 1, "up", f"活跃度 {activity:.1f} ≥ 升级线 {up:g}")

        down = spec.down_threshold
        if down is not None and activity < down:
            return Decision(max(0, level - 1), "down", f"活跃度 {activity:.1f} < 降级线 {down:g}")

        if up is None and down is None:
            return Decision(level, "hold", "本档既无升降线，维持")
        if up is None:
            return Decision(level, "hold", f"活跃度 {activity:.1f} 未低于降级线 {down:g}")
        if down is None:
            return Decision(level, "hold", f"活跃度 {activity:.1f} 未达升级线 {up:g}")
        return Decision(
            level, "hold", f"活跃度 {activity:.1f} 落在死区 [{down:g}, {up:g})"
        )


def initial_level(importance: float, bands: Sequence[Mapping[str, Any]]) -> int:
    """按 importance 从 `[[initial.bands]]` 里挑初始等级。

    bands 必须按 `min_importance` 降序（配置校验强制），所以「第一个满足的」就是对的档。
    上限由配置校验保证「顶档不可录入即设」，这里不再重复判断。
    """
    value = max(0.0, min(1.0, float(importance)))
    for band in bands:
        if value >= float(band["min_importance"]):
            return int(band["level"])
    return int(bands[-1]["level"])


def activity_contribution(
    similarity: float, *, sim_floor: float, curve_power: float
) -> float:
    """一次检索命中给活跃度加多少（§2.1）。

    平方而非线性：高相关的一次命中，价值远大于擦边命中的十次 ——
    否则「什么都能蹭一点」的条目会靠数量取胜。
    """
    excess = float(similarity) - float(sim_floor)
    if excess <= 0:
        return 0.0
    return excess ** float(curve_power)


def level_factor(
    level: int,
    max_level: int,
    power: float,
    overrides: Mapping[int, float] | None = None,
) -> float:
    """等级在检索排序里的倍率：`(level / 最高等级) ^ power`。

    0 级恒为 0 —— 遗忘层根本不该进常规检索，倍率为 0 是「万一漏进来也排最后」的兜底。
    想逐级手调就在 TOML 里填映射表，不走公式。
    """
    level = int(level)
    if level <= 0:
        return 0.0
    if overrides and level in overrides:
        return float(overrides[level])
    if int(max_level) <= 0:
        return 0.0
    ratio = min(level, int(max_level)) / int(max_level)
    return ratio ** float(power)
