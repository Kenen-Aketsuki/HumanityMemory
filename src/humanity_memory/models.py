"""领域模型：经验 / 经历 / 日志。

两类记忆，判据只有一条 —— **它是否需要回答「什么时候发生的」**：

    experience  经验：可复用的判定与结论，与具体事件解耦，不会压缩，会被推翻重写
    episode     经历：发生过的事，一条一个单一事务，带上发生时刻，会逐级压缩
    log         日志：当天原始记录，append-only，喂给复盘

旧的四类（knowledge / state / case / narrative）已废弃：
「知识 vs 现状」的差别本质是「多容易变」，用离散类别去表达连续属性，判据只能写在
prompt 里靠模型自觉；改成初始可信度后，易变的条目自己会被反复验证纠正。

类型由**表**承载，没有行级 kind 列 —— 所以这里 `kind` 是只读属性，不参与写入。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["workspace", "global"]
MemoryType = Literal["experience", "episode", "log"]

# 三类对象，两类记忆 + 一类原料
ALL_KINDS: tuple[str, ...] = ("experience", "episode", "log")

_KIND_LABEL = {
    "experience": "经验",
    "episode": "经历",
    "log": "日志",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def day_of(ms: int | None = None) -> str:
    """本地时区的 YYYY-MM-DD。"""
    ts = (ms if ms is not None else now_ms()) / 1000
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def parse_day(value: str | None) -> str:
    """接受 YYYY-MM-DD / today / yesterday。"""
    if not value:
        return day_of()
    text = value.strip().lower()
    if text in ("today", "今天"):
        return day_of()
    if text in ("yesterday", "昨天"):
        return day_of(now_ms() - 86_400_000)
    try:
        _dt.date.fromisoformat(text)
        return text
    except ValueError as exc:
        raise ValueError(f"无法解析日期 {value!r}，请用 YYYY-MM-DD") from exc


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------- 经验


@dataclass(slots=True)
class Experience:
    """一条经验：可复用的判定与结论。"""

    rowid: int
    id: str
    content: str
    tags: list[str]
    importance: float
    scope: str
    source: str | None
    created_at: int
    updated_at: int
    last_accessed_at: int | None
    access_count: int
    valid_at: int | None
    invalid_at: int | None
    invalidated_by: str | None
    archived_at: int | None
    embedded: bool
    verified_at: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"
    # 三个评分维度：可信度、活跃度、遗忘等级，以及活跃度周期的锚点
    forget_level: int = 3
    confidence: float = 0.3
    activity: float = 0.0
    period_start: int | None = None
    level0_since: int | None = None
    # 不落库：只在「本次写入被查重拦下、返回的是既有条目」时为 True。
    # 字段放在实体上而不是包装成 WriteResult，是为了让既有调用点保持 `item.id` 的写法。
    deduped: bool = False
    dup_similarity: float | None = None

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "Experience":
        keys = row.keys()
        return cls(
            rowid=row["rowid"],
            id=row["id"],
            content=row["content"],
            tags=_loads(row["tags"], []),
            importance=float(row["importance"]),
            scope=row["scope"],
            source=row["source"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_accessed_at=row["last_accessed_at"],
            access_count=int(row["access_count"]),
            valid_at=row["valid_at"],
            invalid_at=row["invalid_at"],
            invalidated_by=row["invalidated_by"],
            archived_at=row["archived_at"],
            embedded=bool(row["embedded"]),
            verified_at=row["verified_at"] if "verified_at" in keys else None,
            meta=_loads(row["meta"], {}),
            db=db,
            forget_level=int(row["forget_level"]) if "forget_level" in keys else 3,
            confidence=(
                float(row["confidence"])
                if "confidence" in keys and row["confidence"] is not None
                else 0.3
            ),
            activity=float(row["activity"]) if "activity" in keys else 0.0,
            period_start=row["period_start"] if "period_start" in keys else None,
            level0_since=row["level0_since"] if "level0_since" in keys else None,
        )

    @property
    def kind(self) -> str:
        return "experience"

    @property
    def live(self) -> bool:
        return self.invalid_at is None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": "experience",
            "kind": "experience",
            "id": self.id,
            "content": self.content,
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "scope": self.scope,
            "db": self.db,
            "live": self.live,
            "archived": self.archived,
            "created_at": self.created_at,
            "created_iso": iso(self.created_at),
            "access_count": self.access_count,
            # 三个维度：等级决定多久裁一次，可信度决定读之前要不要先验
            "forget_level": self.forget_level,
            "confidence": round(self.confidence, 3),
            "activity": round(self.activity, 2),
        }
        if self.source:
            data["source"] = self.source
        if self.verified_at:
            data["verified_at"] = self.verified_at
            data["verified_iso"] = iso(self.verified_at)
        if self.invalid_at:
            data["invalid_at"] = self.invalid_at
            data["invalid_iso"] = iso(self.invalid_at)
        if self.invalidated_by:
            data["invalidated_by"] = self.invalidated_by
        if self.deduped:
            # 语义：这条**没有被新建**，命中的就是库里的这条；活跃度已按 dup_bonus 加过
            data["deduped"] = True
            if self.dup_similarity is not None:
                data["dup_similarity"] = round(self.dup_similarity, 4)
        return data

    def to_line(self, *, needs_verify: bool = False, forgotten: bool = False) -> str:
        mark = "" if self.live else "·已失效"
        if needs_verify and self.live:
            mark = "·待验证"
        if forgotten:
            mark += "·已遗忘"
        scope = "全局" if self.scope == "global" else "项目"
        return f"[{self.id}] ({scope}经验{mark}) {self.content}"


# ---------------------------------------------------------------- 经历


@dataclass(slots=True)
class Episode:
    """一条经历：发生过的事，带上发生时刻。"""

    rowid: int
    id: str
    content: str
    at: int
    day: str
    tags: list[str]
    importance: float
    access_count: int
    created_at: int
    last_accessed_at: int | None
    digest_id: str | None
    archived_at: int | None
    compression_level: int
    embedded: bool
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"
    # 经历不用 confidence 列：它的「可信度」是 forget_level 的派生值（§2.2）
    forget_level: int = 3
    activity: float = 0.0
    period_start: int | None = None
    revive_hits: int = 0
    level0_since: int | None = None

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "Episode":
        keys = row.keys()
        return cls(
            rowid=row["rowid"],
            id=row["id"],
            content=row["content"],
            at=int(row["at"]),
            day=row["day"],
            tags=_loads(row["tags"], []),
            importance=float(row["importance"]),
            access_count=int(row["access_count"]),
            created_at=int(row["created_at"]),
            last_accessed_at=row["last_accessed_at"],
            digest_id=row["digest_id"],
            archived_at=row["archived_at"],
            compression_level=int(row["compression_level"] or 0),
            embedded=bool(row["embedded"]),
            meta=_loads(row["meta"], {}),
            db=db,
            forget_level=int(row["forget_level"]) if "forget_level" in keys else 3,
            activity=float(row["activity"]) if "activity" in keys else 0.0,
            period_start=row["period_start"] if "period_start" in keys else None,
            revive_hits=int(row["revive_hits"]) if "revive_hits" in keys else 0,
            level0_since=row["level0_since"] if "level0_since" in keys else None,
        )

    @property
    def kind(self) -> str:
        return "episode"

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "episode",
            "kind": "episode",
            "id": self.id,
            "content": self.content,
            "day": self.day,
            "at": self.at,
            "at_iso": iso(self.at),
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "digest_id": self.digest_id,
            "archived": self.archived,
            "compression_level": self.compression_level,
            "forget_level": self.forget_level,
            "activity": round(self.activity, 2),
            "db": self.db,
        }

    def to_line(self, *, needs_verify: bool = False, forgotten: bool = False) -> str:
        mark = "·已遗忘" if forgotten else ""
        return f"[{self.id}] ({self.day}·经历{mark}) {self.content}"


# ---------------------------------------------------------------- 日志


@dataclass(slots=True)
class LogEntry:
    rowid: int
    id: str
    at: int
    day: str
    content: str
    tags: list[str]
    session_id: str | None
    importance: float
    access_count: int
    last_accessed_at: int | None
    consolidated: bool
    digest_id: str | None
    embedded: bool
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "LogEntry":
        return cls(
            rowid=row["rowid"],
            id=row["id"],
            at=int(row["at"]),
            day=row["day"],
            content=row["content"],
            tags=_loads(row["tags"], []),
            session_id=row["session_id"],
            importance=float(row["importance"]),
            access_count=int(row["access_count"]),
            last_accessed_at=row["last_accessed_at"],
            consolidated=bool(row["consolidated"]),
            digest_id=row["digest_id"],
            embedded=bool(row["embedded"]),
            meta=_loads(row["meta"], {}),
            db=db,
        )

    @property
    def kind(self) -> str:
        return "log"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "log",
            "kind": "log",
            "id": self.id,
            "content": self.content,
            "day": self.day,
            "at": self.at,
            "at_iso": iso(self.at),
            "tags": self.tags,
            "session_id": self.session_id,
            "consolidated": self.consolidated,
            "db": self.db,
        }

    def to_line(self, *, needs_verify: bool = False, forgotten: bool = False) -> str:
        return f"[{self.id}] {iso(self.at)} {self.content}"


# ---------------------------------------------------------------- 检索结果


@dataclass(slots=True)
class Hit:
    item: Any  # Experience | Episode | LogEntry
    score: float
    # 纯相关性：向量与词法融合后的分，不含重要度 / 等级 / 类型权重。
    # 检索的门槛与排序都只看它，`score` 仅作展示与破平。
    retrieval: float = 0.0
    vector_similarity: float | None = None
    lexical_score: float | None = None
    distance: float | None = None
    # 低于 verify_on_read 的经验，被读到时提醒先验证再用（§2.2）
    needs_verify: bool = False
    # 从遗忘层（0 级）捞出来的：它已经在常规视野之外，读到它得知道这一点（§3.2）
    forgotten: bool = False
    # 这一下正好凑满复活次数（只在 deep_recall 里可能为真）
    revived: bool = False
    # 严格阈值没捞到东西，由宽泛层（RRF / max-pooling）兜底召回。
    # 精度低于常规检索，读的人该知道它"只是沾边"。
    fallback: bool = False

    @property
    def kind(self) -> str:
        return getattr(self.item, "kind", "unknown")

    def to_dict(self) -> dict[str, Any]:
        data = self.item.to_dict()
        data["score"] = round(self.score, 4)
        data["retrieval"] = round(self.retrieval, 4)
        if self.vector_similarity is not None:
            data["vector_similarity"] = round(self.vector_similarity, 4)
        if self.lexical_score is not None:
            data["lexical_score"] = round(self.lexical_score, 4)
        if self.needs_verify:
            data["needs_verify"] = True
        if self.fallback:
            data["fallback"] = True
        if self.forgotten:
            data["forgotten"] = True
        if self.revived:
            data["revived"] = True
        return data

    def to_line(self) -> str:
        return self.item.to_line(needs_verify=self.needs_verify, forgotten=self.forgotten)
