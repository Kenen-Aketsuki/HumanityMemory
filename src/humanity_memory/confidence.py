"""可信度：证实加分、证伪扣分（§2.2）。

为什么加分要看间隔
------------------
隔了半年再看一眼它还是对的，比昨天刚验过今天又验一次，信息量大得多 ——
**时间跨度本身就是对稳定性的证明**，所以加分按 `interval ^ alpha` 放大。

为什么证伪更狠
--------------
证实只是"还没被推翻"，证伪是"已经错了"，两者信息量不对等。
同一条幂律，系数换成 `loss_k`（约为 `gain_k` 的三倍），方向取负。

两条硬边界
----------
1. 加分封顶 `1 - c`：不会溢出 1，也不会出现"验一次加 0.7"这种不讲理的跳变；
2. 扣分封顶 `c`：扣到 0 为止，不出负数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 证据级别 → 是否计分由 TOML 的 [confidence.level_weight] 决定。
# L3（只是提到）不开放配置，恒为 0 —— 那是语义约束，不是参数。
EVIDENCE_LEVELS = ("L1", "L2", "L3")


@dataclass(frozen=True)
class ConfidenceCurve:
    """`[confidence]` 段的运行时视图。算分是纯函数，落盘由 store 负责。"""

    gain_k: float
    gain_alpha: float
    loss_k: float
    verify_threshold: float
    fact_threshold: float
    level_weight: Mapping[str, float]

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfidenceCurve":
        data = cfg.confidence_settings
        return cls(
            gain_k=float(data["gain_k"]),
            gain_alpha=float(data["gain_alpha"]),
            loss_k=float(data["loss_k"]),
            verify_threshold=float(data["verify_on_read"]),
            fact_threshold=float(data["treat_as_fact"]),
            level_weight={
                str(k).upper(): float(v)
                for k, v in (data.get("level_weight") or {}).items()
            },
        )

    # ---- 判据 ----
    def weight(self, level: str) -> float:
        """证据级别的计分权重；没配的级别（含 L3）一律 0。"""
        return float(self.level_weight.get(str(level).strip().upper(), 0.0))

    def needs_verify(self, confidence: float) -> bool:
        """低于这条线：被检索命中时要带「先验一下再用」的标记。"""
        return float(confidence) < self.verify_threshold

    def is_fact(self, confidence: float) -> bool:
        """高于这条线：视为事实，不再主动复核（但不豁免被推翻）。"""
        return float(confidence) >= self.fact_threshold

    # ---- 算分 ----
    def gain(self, confidence: float, interval_days: float, level: str) -> float:
        c = max(0.0, min(1.0, float(confidence)))
        room = 1.0 - c
        if room <= 0:
            return 0.0
        raw = (
            self.gain_k
            * max(0.0, float(interval_days)) ** self.gain_alpha
            * self.weight(level)
        )
        return min(room, raw)

    def loss(self, confidence: float, interval_days: float) -> float:
        c = max(0.0, min(1.0, float(confidence)))
        if c <= 0:
            return 0.0
        raw = self.loss_k * max(0.0, float(interval_days)) ** self.gain_alpha
        return -min(c, raw)

    def apply(
        self,
        confidence: float,
        *,
        ok: bool,
        interval_days: float,
        level: str = "L1",
    ) -> tuple[float, float]:
        """返回 (新可信度, 变化量)。`delta == 0` 表示这条证据不计分。"""
        c = max(0.0, min(1.0, float(confidence)))
        delta = self.gain(c, interval_days, level) if ok else self.loss(c, interval_days)
        return max(0.0, min(1.0, c + delta)), delta
