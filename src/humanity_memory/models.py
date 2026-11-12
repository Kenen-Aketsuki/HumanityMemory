"""领域模型：陈述（知识/现状） / 经历（案例/叙述） / 日志。

命名取舍
--------
这一层原来叫「事实（fact）」，但那是个误导性的名字 ——
"Bug #10805 的根因是 X" 也是一条事实，而它恰恰不该进这个库。
它要收的是**可迁移**的东西，所以陈述层分两种 kind：

    knowledge  知识：离开具体实例仍成立，靠被使用保值，不参与失效
    state      现状：当前为真但会变（版本、依赖、路径、资源配置），必须能失效

经历层同理：case 是压到三句话的定论，narrative 是保留过程的叙述。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["workspace", "global"]
MemoryType = Literal["knowledge", "state", "case", "narrative", "log"]

KnowledgeKind = Literal["knowledge", "state"]
EpisodeKind = Literal["case", "narrative"]

KNOWLEDGE_KINDS: tuple[str, ...] = ("knowledge", "state")
EPISODE_KINDS: tuple[str, ...] = ("case", "narrative")
ALL_KINDS: tuple[str, ...] = ("knowledge", "state", "case", "narrative", "log")

_KIND_LABEL = {
    "knowledge": "知识",
    "state": "现状",
    "case": "案例",
    "narrative": "经历",
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


# ---------------------------------------------------------------- 陈述层


@dataclass(slots=True)
class Knowledge:
    """一条陈述：知识（knowledge）或现状（state）。"""

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
    kind: str = "knowledge"
    verified_at: int | None = None
    state_interval_days: float = 7.0
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "Knowledge":
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
            kind=row["kind"] if "kind" in keys else "knowledge",
            verified_at=row["verified_at"] if "verified_at" in keys else None,
            state_interval_days=(
                float(row["state_interval_days"])
                if "state_interval_days" in keys and row["state_interval_days"] is not None
                else 7.0
            ),
            meta=_loads(row["meta"], {}),
            db=db,
        )

    @property
    def live(self) -> bool:
        return self.invalid_at is None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_state(self) -> bool:
        return self.kind == "state"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": "knowledge",
            "kind": self.kind,
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
        }
        if self.source:
            data["source"] = self.source
        if self.verified_at:
            data["verified_at"] = self.verified_at
            data["verified_iso"] = iso(self.verified_at)
        if self.is_state:
            data["state_interval_days"] = round(self.state_interval_days, 1)
            # 下次该复核的时刻：上次确认（或创建）之后过一个当前周期
            due = (self.verified_at or self.created_at) + int(
                self.state_interval_days * 86_400_000
            )
            data["review_due_iso"] = iso(due)
        if self.invalid_at:
            data["invalid_at"] = self.invalid_at
            data["invalid_iso"] = iso(self.invalid_at)
        if self.invalidated_by:
            data["invalidated_by"] = self.invalidated_by
        return data

    def to_line(self) -> str:
        mark = "" if self.live else "·已失效"
        scope = "全局" if self.scope == "global" else "项目"
        label = _KIND_LABEL.get(self.kind, "知识")
        return f"[{self.id}] ({scope}{label}{mark}) {self.content}"


# 旧名保留为别名，方便外部脚本过渡；新代码一律用 Knowledge
Fact = Knowledge


# ---------------------------------------------------------------- 经历层


@dataclass(slots=True)
class Episode:
    """一次具体事件：案例（case）或叙述（narrative）。"""

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
    kind: str = "narrative"
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"

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
            kind=row["kind"] if "kind" in keys else "narrative",
            meta=_loads(row["meta"], {}),
            db=db,
        )

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_case(self) -> bool:
        return self.kind == "case"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "episode",
            "kind": self.kind,
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
            "db": self.db,
        }

    def to_line(self) -> str:
        label = "案例" if self.is_case else "经历"
        return f"[{self.id}] ({self.day}·{label}) {self.content}"


# ---------------------------------------------------------------- 日志层


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

    def to_line(self) -> str:
        return f"[{self.id}] {iso(self.at)} {self.content}"


# ---------------------------------------------------------------- 检索结果


@dataclass(slots=True)
class Hit:
    item: Any  # Knowledge | Episode | LogEntry
    score: float
    vector_similarity: float | None = None
    lexical_score: float | None = None
    distance: float | None = None

    @property
    def kind(self) -> str:
        return getattr(self.item, "kind", "unknown")

    def to_dict(self) -> dict[str, Any]:
        data = self.item.to_dict()
        data["score"] = round(self.score, 4)
        if self.vector_similarity is not None:
            data["vector_similarity"] = round(self.vector_similarity, 4)
        if self.lexical_score is not None:
            data["lexical_score"] = round(self.lexical_score, 4)
        return data

    def to_line(self) -> str:
        return self.item.to_line()
