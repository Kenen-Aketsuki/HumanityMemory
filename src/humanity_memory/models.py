"""领域模型：事实 / 经历 / 日志。"""

from __future__ import annotations

import datetime as _dt
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["workspace", "global"]
MemoryType = Literal["fact", "episode", "log"]


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


# ---------------------------------------------------------------- 事实


@dataclass(slots=True)
class Fact:
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
    embedded: bool
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "Fact":
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
            embedded=bool(row["embedded"]),
            meta=_loads(row["meta"], {}),
            db=db,
        )

    @property
    def live(self) -> bool:
        return self.invalid_at is None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": "fact",
            "id": self.id,
            "content": self.content,
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "scope": self.scope,
            "db": self.db,
            "live": self.live,
            "created_at": self.created_at,
            "created_iso": iso(self.created_at),
            "access_count": self.access_count,
        }
        if self.source:
            data["source"] = self.source
        if self.invalid_at:
            data["invalid_at"] = self.invalid_at
            data["invalid_iso"] = iso(self.invalid_at)
        if self.invalidated_by:
            data["invalidated_by"] = self.invalidated_by
        return data

    def to_line(self) -> str:
        mark = "" if self.live else "·已失效"
        scope = "全局" if self.scope == "global" else "项目"
        return f"[{self.id}] ({scope}事实{mark}) {self.content}"


# ---------------------------------------------------------------- 经历


@dataclass(slots=True)
class Episode:
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
    embedded: bool
    meta: dict[str, Any] = field(default_factory=dict)
    db: str = "workspace"

    @classmethod
    def from_row(cls, row: Any, db: str = "workspace") -> "Episode":
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
            embedded=bool(row["embedded"]),
            meta=_loads(row["meta"], {}),
            db=db,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "episode",
            "id": self.id,
            "content": self.content,
            "day": self.day,
            "at": self.at,
            "at_iso": iso(self.at),
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "digest_id": self.digest_id,
            "db": self.db,
        }

    def to_line(self) -> str:
        return f"[{self.id}] ({self.day}) {self.content}"


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "log",
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
    item: Any  # Fact | Episode | LogEntry
    score: float
    vector_similarity: float | None = None
    lexical_score: float | None = None
    distance: float | None = None

    @property
    def kind(self) -> str:
        name = type(self.item).__name__
        return {"Fact": "fact", "Episode": "episode", "LogEntry": "log"}.get(name, "unknown")

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
