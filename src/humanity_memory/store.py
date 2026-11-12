"""存储门面：陈述 / 经历 / 日志三层 + 跨库检索 + 复盘固化。

检索策略
--------
vec0 的 KNN 不支持任意 WHERE 过滤，因此每张表都是「先召回候选 rowid，
再 join 主表在 Python 侧过滤打分」。跨库检索时对 workspace 与 global
分别执行再统一排序 —— 全局知识无论当前在哪个项目都参与召回。

kind 与表的关系
---------------
kind 是「行」的属性，表只是物理容器：
    knowledge 表装 knowledge / state 两种 kind
    episodes  表装 case / narrative 两种 kind
所以按 kind 检索是**两级过滤**：先按表选 spec，再按行过滤 kind。
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import DEFAULT_IMPORTANCE, Config
from .db import get_meta, open_db, transaction
from .embeddings import EmbeddingProvider
from .lifecycle import (
    DAY_MS,
    REVIEW_DUE,
    REVIEW_OK,
    REVIEW_OVERDUE,
    archive_decision,
    combine_score,
    initial_compression_level,
    lifecycle_strength,
    next_interval,
    recompress_threshold,
    similarity_from_cosine_distance,
    state_deadlines,
    state_review_status,
)
from .models import (
    ALL_KINDS,
    Episode,
    Hit,
    Knowledge,
    LogEntry,
    day_of,
    iso,
    new_id,
    now_ms,
    parse_day,
)
from .paths import Layout

_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

W_VECTOR_PASS = 0.70
W_LEXICAL_PASS = 0.30

# 叙述的压缩级别越高越「精炼」，检索权重越高；
# 刚复盘出来的完整叙述（level 0）内容长、冗余多，降权避免挤占结果。
EPISODE_LEVEL_WEIGHT = {0: 0.88, 1: 0.93, 2: 0.96, 3: 0.96}

# 案例是压到三句话的定论：比刚复盘的叙述（0.96×0.88=0.845）更该浮上来，
# 但压在 skill 里的知识（1.00）之下 —— 结论优先于故事，通则优先于结论。
CASE_WEIGHT = 0.90

KNOWLEDGE_MAX_CHARS = 500
STATE_MAX_CHARS = 200
CASE_MAX_CHARS = 300

# consolidate_apply 的两个数组各自接受的 kind。放错数组是最容易犯的错
# （把 state 塞进 episodes 会被旧版静默跳过），前置校验必须按这里硬拦。
_KNOWLEDGE_KINDS = frozenset({"knowledge", "state"})
_EPISODE_KINDS = frozenset({"case", "narrative"})


@dataclass(frozen=True)
class TableSpec:
    kind: str
    table: str
    vec: str
    fts: str
    time_attr: str
    type_weight: float
    model: type
    row_kinds: tuple[str, ...]


KNOWLEDGE = TableSpec(
    "knowledge", "knowledge", "knowledge_vec", "knowledge_fts",
    "created_at", 1.00, Knowledge, ("knowledge", "state"),
)
EPISODE = TableSpec(
    "episode", "episodes", "episodes_vec", "episodes_fts",
    "at", 0.96, Episode, ("case", "narrative"),
)
LOG = TableSpec("log", "logs", "logs_vec", "logs_fts", "at", 0.90, LogEntry, ("log",))

ALL_SPECS: tuple[TableSpec, ...] = (KNOWLEDGE, EPISODE, LOG)

# kind 别名 → 该 kind 落在哪张表。检索时用它对用户传的 kind 做归并。
SPEC_BY_KIND: dict[str, TableSpec] = {}
for _spec in ALL_SPECS:
    SPEC_BY_KIND[_spec.kind] = _spec
    for _rk in _spec.row_kinds:
        SPEC_BY_KIND[_rk] = _spec
# 旧词：episode 是 narrative 的同义词
SPEC_BY_KIND["episode"] = EPISODE


class NotFound(LookupError):
    pass


class ScopeError(RuntimeError):
    """需要工作区但当前目录没有。"""


class ConsolidationRejected(ValueError):
    """复盘提交被拒：入参不合法或写入中途失败。

    整批拒绝，不做任何写入，原始日志保持待复盘。调用方改正参数后可原样重跑。
    """


def serialize_float32(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _as_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return sorted({str(v).strip() for v in value if str(v).strip()})


def _slug(text: str, limit: int = 20) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s。，、；：！？,.;:!?]+', "-", text.strip())
    cleaned = cleaned.strip("-")
    return (cleaned[:limit].strip("-") or "untitled")


_KIND_ALIASES = {"fact": "knowledge", "episode": "narrative"}


def _norm_kind(kind: str | None) -> str:
    """归一化 kind：旧词 fact/episode 映射到新词，其余原样返回。"""
    value = (kind or "knowledge").strip().lower()
    return _KIND_ALIASES.get(value, value)


def _check_writable(content: str, kind: str, *, label: str = "写入") -> tuple[str, str]:
    """写入前的唯一内容判据：归一化 kind、去空白、按 kind 卡长度。

    write_knowledge / write_episode / consolidate_apply 共用这一套规则，
    杜绝「前置校验放行、真正写入却被拒」的规则漂移。返回 (正文, 归一化 kind)。
    """
    k = _norm_kind(kind)
    if k not in _KNOWLEDGE_KINDS | _EPISODE_KINDS:
        raise ValueError(f"{label}: 未知 kind={kind!r}")
    text = (content or "").strip()
    if not text:
        raise ValueError(f"{label}: 内容不能为空")
    limit = {
        "knowledge": KNOWLEDGE_MAX_CHARS,
        "state": STATE_MAX_CHARS,
        "case": CASE_MAX_CHARS,
    }.get(k)
    if limit is not None and len(text) > limit:
        hint = (
            "现状要短（版本号、路径、选型，一两句）" if k == "state"
            else "案例只保留「现象 → 根因 → 落点」，过程请写日志或叙述" if k == "case"
            else "知识要能一句话说清；带过程的排查细节请写日志，复盘时再压成案例"
        )
        raise ValueError(f"{label}: {k} 上限 {limit} 字（当前 {len(text)} 字）。{hint}")
    return text, k


def _prepare_apply_items(
    knowledge: Sequence[dict[str, Any]] | None,
    facts: Sequence[dict[str, Any]] | None,
    episodes: Sequence[Any] | None,
    summary: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """复盘提交的整批前置校验：纯函数，不碰数据库。

    旧版把校验失败 `except ValueError: continue` 静默跳过，结果「ok=true
    但少了几条结论、原始日志已被清空」。现在任何一项不合法都整批拒绝，
    此时一条结论都没写、日志仍是 pending，改完参数可原样重跑。
    """
    prepared_knowledge: list[dict[str, Any]] = []
    prepared_episodes: list[dict[str, Any]] = []
    buckets = (
        ("knowledge", list(knowledge or []) + list(facts or []), _KNOWLEDGE_KINDS, "knowledge"),
        ("episodes", list(episodes or []), _EPISODE_KINDS, "narrative"),
    )
    for bucket, raws, allowed, default_kind in buckets:
        for index, raw in enumerate(raws):
            where = f"{bucket}[{index}]"
            if isinstance(raw, str):
                raw = {"content": raw}
            if not isinstance(raw, dict):
                raise ConsolidationRejected(f"{where} 必须是对象或字符串")
            kind = _norm_kind(raw.get("kind") or default_kind)
            if kind not in allowed:
                hint = (
                    "state 属于 knowledge 数组（写成带 kind='state' 的陈述项）"
                    if kind == "state" and bucket == "episodes"
                    else f"{bucket} 只接受 {sorted(allowed)}"
                )
                raise ConsolidationRejected(f"{where} kind={kind!r} 放错了数组；{hint}")
            try:
                text, kind = _check_writable(
                    str(raw.get("content", "")), kind, label=where
                )
            except ValueError as exc:
                raise ConsolidationRejected(str(exc)) from exc

            importance = raw.get("importance")
            if importance is not None:
                try:
                    importance = float(importance)
                except (TypeError, ValueError) as exc:
                    raise ConsolidationRejected(
                        f"{where} importance 必须是数字"
                    ) from exc
                if not 0.0 <= importance <= 1.0:
                    raise ConsolidationRejected(f"{where} importance 必须在 0~1")

            item: dict[str, Any] = {
                "content": text,
                "kind": kind,
                "importance": importance,
                "tags": raw.get("tags"),
            }
            if bucket == "knowledge":
                item["source"] = raw.get("source")
                item["invalidates"] = raw.get("invalidates")
            else:
                item["level"] = raw.get("level")
            (prepared_knowledge if bucket == "knowledge" else prepared_episodes).append(item)

    # 兼容旧行为：没给 episodes 却给了 summary，就把 summary 记成一条叙述。
    # 显式打标记，回执里会提示，避免调用方以为只写了 digest。
    summary_as_narrative = False
    if not prepared_episodes and summary and summary.strip():
        text, _ = _check_writable(summary, "narrative", label="summary")
        prepared_episodes.append(
            {"content": text, "kind": "narrative", "importance": None,
             "tags": None, "level": None}
        )
        summary_as_narrative = True
    return prepared_knowledge, prepared_episodes, summary_as_narrative


def _row_kinds(kinds: Sequence[str] | None, allowed: Sequence[str]) -> set[str] | None:
    """把用户传的 kinds 收敛成该表要过滤的行级 kind 集合。

    返回 None 表示「该表全部行都要」，返回空集表示「该表一行都不要」。
    `all` / None / 空列表都视为要全部。
    """
    if kinds is None:
        return None
    if isinstance(kinds, str):
        kinds = [kinds]
    resolved = {_norm_kind(k) for k in kinds if str(k).strip()}
    if not resolved or "all" in resolved:
        return None
    picked = resolved & set(allowed)
    return picked or set()


class MemoryStore:
    def __init__(self, cfg: Config, layout: Layout) -> None:
        self.cfg = cfg
        self.layout = layout
        self.embedder = EmbeddingProvider(cfg)
        self.global_conn = open_db(layout.global_db, cfg)
        self.workspace_conn = (
            open_db(layout.workspace_db, cfg) if layout.workspace_db else None
        )
        try:
            self.vec_source = get_meta(self.global_conn, "vec_source") or "unknown"
        except Exception:  # noqa: BLE001
            self.vec_source = "unknown"

    # ---------------------------------------------------------------- 基础

    def close(self) -> None:
        self.embedder.close()
        for conn in (self.workspace_conn, self.global_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def has_workspace(self) -> bool:
        return self.workspace_conn is not None

    def _conn_for(self, scope: str) -> tuple[str, Any]:
        if scope == "global":
            return "global", self.global_conn
        if self.workspace_conn is None:
            raise ScopeError(
                "当前目录没有 .ai_memory 工作区，无法写入项目级记忆。"
                "请在项目目录下运行，或用 scope='global' 写全局记忆。"
            )
        return "workspace", self.workspace_conn

    def _all_dbs(self, scope: str | None) -> list[tuple[str, Any]]:
        wanted = (scope or "all").lower()
        if wanted == "workspace":
            if self.workspace_conn is None:
                raise ScopeError("当前目录没有 .ai_memory 工作区")
            return [("workspace", self.workspace_conn)]
        if wanted == "global":
            return [("global", self.global_conn)]
        out: list[tuple[str, Any]] = []
        if self.workspace_conn is not None:
            out.append(("workspace", self.workspace_conn))
        out.append(("global", self.global_conn))
        return out

    # ================================================================ 陈述

    def write_memory(
        self,
        content: str = "",
        *,
        kind: str = "knowledge",
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        scope: str = "workspace",
        source: str | None = None,
        invalidates: str | None = None,
        verifies: str | None = None,
        valid_at: int | None = None,
        meta: dict[str, Any] | None = None,
        at: int | None = None,
        day: str | None = None,
        digest_id: str | None = None,
    ) -> Knowledge | Episode:
        """按 kind 分派的唯一写入口。

        content 为空**只允许**一种情况：`kind='state'` 且带 `verifies` ——
        那是"我确认这条现状还是这样"，不是写新东西。

        `invalidates` 主要用于 state（现状被新现状顶掉）；知识原则上不会被推翻，
        万一某条机制不再成立，也允许走同一条路，不做硬拦。
        """
        k = _norm_kind(kind)

        if verifies and not (content or "").strip():
            if k != "state":
                raise ValueError("只有 state 可以只做确认（verifies）")
            return self.verify_state(verifies)

        if verifies and k != "state":
            raise ValueError("verifies 只能用于 state")

        # 现状不许走「新条目顶掉旧条目」这条路：每次换代都留一条 invalid 记录，
        # 久了就是一条没人愿意看的失效链，而中间每一条都曾是「当时的真相」。
        # 现状的正确更替姿势是「复核发现不符 → 删掉旧条目 → 独立写新的」。
        if invalidates and k == "state":
            raise ValueError(
                "state 不允许用 invalidates 换代：那会攒出冗长的失效链。"
                "复核发现现状已经不成立时，用 memory_review(matched=false) 直接废弃旧条目，"
                "再独立写一条新的现状。"
            )

        if k in ("knowledge", "state"):
            item: Knowledge | Episode = self.write_knowledge(
                content,
                kind=k,
                tags=tags,
                importance=importance,
                scope=scope,
                source=source,
                valid_at=valid_at,
                verified_at=now_ms() if verifies else None,
                meta=meta,
            )
        else:
            if scope == "global":
                raise ValueError("经历（case / narrative）只能写在项目库，不支持 global")
            item = self.write_episode(
                content,
                kind=k,
                tags=tags,
                importance=importance,
                at=at,
                day=day,
                digest_id=digest_id,
                source=source,
                meta=meta,
            )

        if invalidates:
            try:
                self.invalidate(str(invalidates), invalidated_by=item.id)
            except NotFound:
                pass
        return item

    def write_knowledge(
        self,
        content: str,
        *,
        kind: str = "knowledge",
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        scope: str = "workspace",
        source: str | None = None,
        valid_at: int | None = None,
        verified_at: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Knowledge:
        k = _norm_kind(kind)
        if k not in _KNOWLEDGE_KINDS:
            raise ValueError(f"write_knowledge 不接受 kind={kind!r}")

        content, _ = _check_writable(content, k, label="write_knowledge")

        db_name, conn = self._conn_for(scope)
        now = now_ms()
        imp = importance if importance is not None else DEFAULT_IMPORTANCE.get(k, 0.8)
        imp = max(0.0, min(1.0, float(imp)))
        item_id = new_id("k" if k == "knowledge" else "s")
        vectors = self.embedder.try_embed([content])
        vec = vectors[0] if vectors else None

        # 新写下的现状最容易错（写的时候就可能已经变了），所以从梯子最低档起算，
        # 复核确认几次都没变才逐步放宽。knowledge 这一列无意义，跟上默认值即可。
        state_interval = (
            min(self.cfg.state_ladder) if k == "state" else self.cfg.archive_ttl.get("state", 7.0)
        )

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO knowledge(id, kind, content, tags, importance, scope,"
                " source, created_at, updated_at, valid_at, verified_at,"
                " state_interval_days, embedded, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    k,
                    content,
                    json.dumps(_as_tags(tags), ensure_ascii=False),
                    imp,
                    db_name,
                    source,
                    now,
                    now,
                    valid_at if valid_at is not None else now,
                    verified_at,
                    float(state_interval),
                    1 if vec else 0,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            if vec:
                self._vec_put(conn, KNOWLEDGE, int(cur.lastrowid), vec)

        return self.get_knowledge(item_id)

    def write_episode(
        self,
        content: str,
        *,
        kind: str = "narrative",
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        at: int | None = None,
        day: str | None = None,
        digest_id: str | None = None,
        source: str | None = None,
        meta: dict[str, Any] | None = None,
        compression_level: int | None = None,
    ) -> Episode:
        """写一条经历。case 要短（≤300 字），narrative 可以有过程。"""
        k = _norm_kind(kind)
        if k not in _EPISODE_KINDS:
            raise ValueError(f"write_episode 不接受 kind={kind!r}")

        text, _ = _check_writable(content, k, label="write_episode")

        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区，无法写经历")
        conn = self.workspace_conn
        ts = at if at is not None else now_ms()
        imp = importance if importance is not None else DEFAULT_IMPORTANCE.get(k, 0.4)
        imp = max(0.0, min(1.0, float(imp)))
        # case 已经是最简形态，不参与后续压缩；narrative 按重要性定起点
        if compression_level is not None:
            lvl = int(compression_level)
        else:
            lvl = 0 if k == "case" else initial_compression_level(imp)
        ep_id = new_id("c" if k == "case" else "e")
        vectors = self.embedder.try_embed([text])
        vec = vectors[0] if vectors else None

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO episodes(id, kind, content, at, day, tags, importance,"
                " created_at, digest_id, compression_level, embedded, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ep_id,
                    k,
                    text,
                    ts,
                    day or day_of(ts),
                    json.dumps(_as_tags(tags), ensure_ascii=False),
                    imp,
                    now_ms(),
                    digest_id,
                    lvl,
                    1 if vec else 0,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            if vec:
                self._vec_put(conn, EPISODE, int(cur.lastrowid), vec)

        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        return Episode.from_row(row, "workspace")

    def get_knowledge(self, item_id: str) -> Knowledge:
        for db_name, conn in self._all_dbs("all"):
            row = conn.execute(
                "SELECT * FROM knowledge WHERE id = ?", (item_id,)
            ).fetchone()
            if row is not None:
                return Knowledge.from_row(row, db_name)
        raise NotFound(f"找不到记忆 {item_id}")

    def get_episode(self, episode_id: str) -> Episode:
        if self.workspace_conn is None:
            raise NotFound(f"找不到经历 {episode_id}")
        row = self.workspace_conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"找不到经历 {episode_id}")
        return Episode.from_row(row, "workspace")

    def get(self, memory_id: str) -> Knowledge | Episode:
        """按 id 取任意条目 —— 前缀 k_/s_ 是陈述，c_/e_ 是经历。"""
        if memory_id.startswith(("c_", "e_")):
            return self.get_episode(memory_id)
        return self.get_knowledge(memory_id)

    def review_state(self, item_id: str, *, matched: bool) -> dict[str, Any]:
        """复核一条现状：这是 state 唯一的生命周期入口。

        matched=True  → 描述仍然成立：续命，周期升一档（1→3→5→7），到顶就留在 7 天。
        matched=False → 描述已经不成立：**直接物理删除**，不留历史、不建失效链。

        为什么删除而不是归档：现状的价值只在「它现在是真的」。一条已经假了的现状，
        留着只会污染检索 —— 它长得和真话一模一样，读的人没法从正文看出它过期了。
        knowledge 可以归档是因为它有过时的余地，state 没有。
        """
        item = self.get_knowledge(item_id)
        if not item.is_state:
            raise ValueError(f"{item_id} 不是 state，不需要复核（知识不会过期）")
        if item.archived:
            raise ValueError(f"{item_id} 已归档，请先 memory_unarchive 再复核")

        if matched:
            updated = self.verify_state(item_id)
            due, expire = state_deadlines(
                updated.created_at,
                updated.verified_at,
                updated.state_interval_days,
                self.cfg.state_grace_cap_days,
            )
            return {
                "id": item_id,
                "matched": True,
                "action": "renewed",
                "interval_days": round(updated.state_interval_days, 1),
                "verified_at": updated.verified_at,
                "verified_iso": iso(updated.verified_at),
                "next_review_iso": iso(due),
                "expire_iso": iso(expire),
            }

        _db_name, conn = self._conn_for(item.db)
        with transaction(conn):
            conn.execute(f"DELETE FROM {KNOWLEDGE.vec} WHERE rowid = ?", (item.rowid,))
            conn.execute("DELETE FROM knowledge WHERE id = ?", (item_id,))
        return {
            "id": item_id,
            "matched": False,
            "action": "purged",
            "content": item.content,
            "scope": item.scope,
            "note": "现状与实际不符，已物理删除（全文索引由触发器清理）。"
            "需要记录新的现状，请另写一条，不要试图覆盖。",
        }

    def verify_state(self, item_id: str) -> Knowledge:
        """确认一条现状「仍然成立」并续命，同时把复核周期升一档。

        这是 state 唯一的续命方式：被检索到不算，必须有明确的「我看过了」。
        """
        item = self.get_knowledge(item_id)
        if not item.is_state:
            raise ValueError(f"{item_id} 不是 state，不需要确认（知识不会过期）")
        _, conn = self._conn_for(item.db)
        now = now_ms()
        # 升档只升不降：连续确认没变，才配得上更长的复核间隔
        upgraded = next_interval(item.state_interval_days, self.cfg.state_ladder)
        with transaction(conn):
            conn.execute(
                "UPDATE knowledge SET verified_at = ?, updated_at = ?,"
                " last_accessed_at = ?, access_count = access_count + 1,"
                " state_interval_days = ? WHERE id = ?",
                (now, now, now, upgraded, item_id),
            )
        return self.get_knowledge(item_id)

    def _review_state_rows(self, conn: Any, now: int) -> Iterator[tuple[Any, str, str, float, int, int]]:
        """遍历一个库里所有在册的现状，产出 (row, 状态, 说明, 周期, 到期时刻, 失效时刻)。"""
        default_ttl = self.cfg.archive_ttl.get("state", 7.0)
        rows = conn.execute(
            "SELECT rowid, id, content, kind, created_at, verified_at,"
            " state_interval_days, source, scope, archived_at, invalid_at"
            " FROM knowledge"
            " WHERE kind = 'state' AND invalid_at IS NULL AND archived_at IS NULL"
        ).fetchall()
        for row in rows:
            interval = float(row["state_interval_days"] or default_ttl)
            status, reason = state_review_status(
                int(row["created_at"]),
                row["verified_at"],
                interval,
                now,
                self.cfg.state_grace_cap_days,
            )
            due, expire = state_deadlines(
                int(row["created_at"]),
                row["verified_at"],
                interval,
                self.cfg.state_grace_cap_days,
            )
            yield row, status, reason, interval, due, expire

    def state_due(self) -> dict[str, Any]:
        """列出该复核的现状：已到期（due）与宽限已过（overdue）。

        周期再短，没人来看也只是个数字 —— 这个清单就是让「到期」这件事发声的地方。
        日常开会话、复盘前都该扫一眼，别等归档扫描把东西收走。
        """
        now = now_ms()
        items: list[dict[str, Any]] = []
        counts = {REVIEW_DUE: 0, REVIEW_OVERDUE: 0}
        for db_name, conn in self._all_dbs("all"):
            for row, status, reason, interval, due, expire in self._review_state_rows(conn, now):
                if status == REVIEW_OK:
                    continue
                counts[status] = counts.get(status, 0) + 1
                items.append(
                    {
                        "id": row["id"],
                        "db": db_name,
                        "scope": row["scope"],
                        "status": status,
                        "interval_days": round(interval, 1),
                        "content": str(row["content"]),
                        "source": row["source"],
                        "due_iso": iso(due),
                        "expire_iso": iso(expire),
                        "reason": reason,
                    }
                )
        # overdue 排前面：它们马上就要被归档扫描收走了
        items.sort(key=lambda x: (x["status"] != REVIEW_OVERDUE, x["expire_iso"] or ""))
        return {
            "count": len(items),
            "due": counts.get(REVIEW_DUE, 0),
            "overdue": counts.get(REVIEW_OVERDUE, 0),
            "items": items,
            "rule": {
                "ladder_days": list(self.cfg.state_ladder),
                "grace_cap_days": self.cfg.state_grace_cap_days,
            },
            "howto": "仍成立 → memory_review(id, matched=true) 续命并升档；"
            "已经不符 → memory_review(id, matched=false) 直接删除，再独立写新的。",
        }

    def invalidate(
        self,
        item_id: str,
        *,
        invalidated_by: str | None = None,
        force: bool = False,
    ) -> Knowledge:
        """让一条条目失效（保留历史）。**现状不接受这条路**，除非内部强制。

        state 的语义是「要么真、要么删」，中间态只会变成噪音，所以对外一律拒绝，
        只留 promote 这类内部整体搬迁时用 force 通过。
        """
        item = self.get_knowledge(item_id)
        if item.is_state and not force:
            raise ValueError(
                f"{item_id} 是 state：现状不做「标记失效」，请用 memory_review(matched=false) "
                "直接废弃并删除，或 memory_review(matched=true) 续命。"
            )
        if not item.live:
            return item
        _, conn = self._conn_for(item.db)
        now = now_ms()
        with transaction(conn):
            conn.execute(
                "UPDATE knowledge SET invalid_at = ?, updated_at = ?, invalidated_by = ?"
                " WHERE id = ?",
                (now, now, invalidated_by, item_id),
            )
        return self.get_knowledge(item_id)

    def promote(self, item_id: str) -> tuple[Knowledge, Knowledge]:
        """项目条目提升为全局条目：新写一条全局条目，旧条目标记失效并指向新条目。"""
        source = self.get_knowledge(item_id)
        if source.scope == "global":
            raise ValueError(f"{item_id} 已经是全局条目")
        promoted = self.write_knowledge(
            source.content,
            kind=source.kind,
            tags=source.tags,
            importance=source.importance,
            scope="global",
            source=f"promoted:{source.id}",
            verified_at=source.verified_at,
        )
        # 提升是「整体搬迁」，旧条目在这里走失效是内部行为，不受 state 的对外限制
        self.invalidate(source.id, invalidated_by=promoted.id, force=True)
        return source, promoted

    def list_knowledge(
        self,
        *,
        kinds: Sequence[str] | None = None,
        scope: str | None = "all",
        include_invalid: bool = False,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[Knowledge]:
        wanted = _row_kinds(kinds, ("knowledge", "state"))
        out: list[Knowledge] = []
        for db_name, conn in self._all_dbs(scope):
            sql = "SELECT * FROM knowledge"
            clauses: list[str] = []
            params: list[Any] = []
            if wanted:
                clauses.append("kind IN (" + ",".join("?" * len(wanted)) + ")")
                params.extend(sorted(wanted))
            if not include_invalid:
                clauses.append("invalid_at IS NULL")
            if not include_archived:
                clauses.append("archived_at IS NULL")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            for row in conn.execute(sql, tuple(params)).fetchall():
                out.append(Knowledge.from_row(row, db_name))
        out.sort(key=lambda f: f.created_at, reverse=True)
        return out[:limit]

    # ================================================================ 日志

    def append_log(
        self,
        content: str,
        *,
        tags: Iterable[str] | None = None,
        at: int | None = None,
        session_id: str | None = None,
        importance: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> LogEntry:
        """只追加、不改写。白天对话中唯一应该调的写操作。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("日志内容不能为空")

        db_name, conn = self._conn_for("workspace")
        ts = at if at is not None else now_ms()
        log_id = new_id("l")
        imp = importance if importance is not None else DEFAULT_IMPORTANCE["log"]
        vectors = self.embedder.try_embed([content])
        vec = vectors[0] if vectors else None

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO logs(id, at, day, content, tags, session_id,"
                " importance, embedded, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    log_id,
                    ts,
                    day_of(ts),
                    content,
                    json.dumps(_as_tags(tags), ensure_ascii=False),
                    session_id,
                    max(0.0, min(1.0, float(imp))),
                    1 if vec else 0,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            if vec:
                self._vec_put(conn, LOG, int(cur.lastrowid), vec)

        return self.get_log(log_id)

    def get_log(self, log_id: str) -> LogEntry:
        if self.workspace_conn is None:
            raise NotFound(f"找不到日志 {log_id}")
        row = self.workspace_conn.execute(
            "SELECT * FROM logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"找不到日志 {log_id}")
        return LogEntry.from_row(row, "workspace")

    def list_logs(
        self,
        *,
        day: str | None = None,
        include_consolidated: bool = True,
        limit: int = 200,
    ) -> list[LogEntry]:
        if self.workspace_conn is None:
            return []
        sql = "SELECT * FROM logs"
        clauses: list[str] = []
        params: list[Any] = []
        if day:
            clauses.append("day = ?")
            params.append(parse_day(day))
        if not include_consolidated:
            clauses.append("consolidated = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at ASC LIMIT ?"
        params.append(limit)
        rows = self.workspace_conn.execute(sql, tuple(params)).fetchall()
        return [LogEntry.from_row(r, "workspace") for r in rows]

    # ================================================================ 检索

    def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] | str | None = None,
        scope: str | None = "all",
        include_invalid: bool = False,
        include_archived: bool = False,
        limit: int = 8,
        min_score: float = 0.0,
    ) -> list[Hit]:
        """跨层混合检索。

        `kinds` 接受 None / "all" / ["all"] / "knowledge" / ["knowledge", "case"] 等写法。
        注意 kind 是行级属性：knowledge 与 state 同表、case 与 narrative 同表，
        所以这里是「先选表、再按行过滤」两级筛选。
        """
        query = (query or "").strip()
        if not query:
            return []

        targets = self._resolve_targets(kinds)
        if not targets:
            return []

        topk = min(self.cfg.max_candidates, max(limit * self.cfg.candidate_multiplier, 40))
        qvec: list[float] | None = None
        if self.embedder.available:
            vectors = self.embedder.try_embed([query])
            qvec = vectors[0] if vectors else None

        hits: list[Hit] = []
        for db_name, conn in self._all_dbs(scope):
            for spec, row_kinds in targets:
                if db_name == "global" and spec is not KNOWLEDGE:
                    continue  # 全局库只承载跨项目陈述（知识 / 现状）
                hits.extend(
                    self._search_spec(
                        conn, db_name, spec, query, qvec, topk,
                        include_invalid, include_archived, row_kinds,
                    )
                )

        hits = [h for h in hits if h.score >= min_score]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def _resolve_targets(
        self, kinds: Sequence[str] | str | None
    ) -> list[tuple[TableSpec, set[str] | None]]:
        """把 kinds 解析成 [(表, 行级 kind 过滤)]，同表合并。"""
        if kinds is None:
            return [(spec, None) for spec in ALL_SPECS]
        raw = [kinds] if isinstance(kinds, str) else list(kinds)
        norm = {_norm_kind(k) for k in raw if str(k).strip()}
        if not norm or "all" in norm:
            return [(spec, None) for spec in ALL_SPECS]

        grouped: dict[TableSpec, set[str]] = {}
        for kind in norm:
            spec = SPEC_BY_KIND.get(kind)
            if spec is None:
                continue
            grouped.setdefault(spec, set()).add(kind)

        out: list[tuple[TableSpec, set[str] | None]] = []
        for spec, wanted in grouped.items():
            # 表里所有 kind 都要 → 不必行级过滤，省一次判断
            if wanted >= set(spec.row_kinds):
                out.append((spec, None))
            else:
                out.append((spec, wanted))
        return out

    def recall(
        self, context: str, *, limit: int = 6, kinds: Sequence[str] | str | None = None
    ) -> list[Hit]:
        """检索并强化命中项 —— 用进废退的「进」。"""
        hits = self.search(context, kinds=kinds, limit=limit)
        self._reinforce(hits)
        return hits

    def _search_spec(
        self,
        conn: Any,
        db_name: str,
        spec: TableSpec,
        query: str,
        qvec: Sequence[float] | None,
        topk: int,
        include_invalid: bool,
        include_archived: bool = False,
        row_kinds: set[str] | None = None,
    ) -> list[Hit]:
        vec_hits: dict[int, float] = {}
        if qvec:
            try:
                rows = conn.execute(
                    f"SELECT rowid, distance FROM {spec.vec}"
                    " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (serialize_float32(qvec), topk),
                ).fetchall()
                vec_hits = {int(r["rowid"]): float(r["distance"]) for r in rows}
            except Exception:  # noqa: BLE001
                vec_hits = {}

        lex_hits = self._fts_candidates(conn, spec, query, topk)
        if len(lex_hits) < 3:
            for rowid, rank in self._like_candidates(conn, spec, query, topk).items():
                lex_hits.setdefault(rowid, rank)

        candidates = set(vec_hits) | set(lex_hits)
        if not candidates:
            return []

        rows = self._fetch(conn, spec, candidates, db_name)
        now = now_ms()
        out: list[Hit] = []

        for rowid, obj in rows.items():
            item_kind = getattr(obj, "kind", spec.kind)
            if row_kinds is not None and item_kind not in row_kinds:
                continue
            if spec is KNOWLEDGE and not include_invalid and not obj.live:
                continue
            if not include_archived and getattr(obj, "archived", False):
                continue

            hl = self.cfg.halflife.get(item_kind, 180.0)
            vec_sim = (
                similarity_from_cosine_distance(vec_hits[rowid])
                if rowid in vec_hits
                else None
            )
            lex_sim = self._normalize_rank(lex_hits, rowid) if rowid in lex_hits else None
            retrieval = self._fuse(vec_sim, lex_sim)
            strength = lifecycle_strength(
                getattr(obj, spec.time_attr), obj.access_count, now, hl
            )
            base = combine_score(
                retrieval=retrieval,
                importance=obj.importance,
                strength=strength,
                w_retrieval=self.cfg.w_retrieval,
                w_importance=self.cfg.w_importance,
                w_strength=self.cfg.w_strength,
            )
            out.append(
                Hit(
                    item=obj,
                    score=base * self._type_weight(spec, obj),
                    vector_similarity=vec_sim,
                    lexical_score=lex_sim,
                    distance=vec_hits.get(rowid),
                )
            )
        return out

    @staticmethod
    def _type_weight(spec: TableSpec, obj: Any) -> float:
        """层权重 × 形态权重。

        case 给固定 0.90：高于刚复盘的叙述（0.96×0.88=0.845），
        低于陈述（1.00）—— 通则在结论之上，结论在故事之上。
        """
        if spec is EPISODE:
            if getattr(obj, "kind", "narrative") == "case":
                return CASE_WEIGHT
            return spec.type_weight * EPISODE_LEVEL_WEIGHT.get(
                int(obj.compression_level), 0.92
            )
        return spec.type_weight

    def _fetch(
        self, conn: Any, spec: TableSpec, rowids: Iterable[int], db_name: str
    ) -> dict[int, Any]:
        ids = list(rowids)
        out: dict[int, Any] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM {spec.table} WHERE rowid IN ({marks})", tuple(chunk)
            ).fetchall()
            for row in rows:
                out[int(row["rowid"])] = spec.model.from_row(row, db_name)
        return out

    def _fts_candidates(
        self, conn: Any, spec: TableSpec, query: str, topk: int
    ) -> dict[int, float]:
        expr = self._fts_expr(query)
        if not expr:
            return {}
        try:
            rows = conn.execute(
                f"SELECT rowid, rank FROM {spec.fts} WHERE {spec.fts} MATCH ?"
                " ORDER BY rank LIMIT ?",
                (expr, topk),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return {}
        return {int(r["rowid"]): float(r["rank"]) for r in rows}

    def _like_candidates(
        self, conn: Any, spec: TableSpec, query: str, topk: int
    ) -> dict[int, float]:
        grams = self._bigrams(query)
        if not grams:
            return {}
        clause = " OR ".join([f"{spec.table}.content LIKE ?"] * len(grams))
        params = [f"%{g}%" for g in grams]
        rows = conn.execute(
            f"SELECT rowid, content FROM {spec.table} WHERE {clause} LIMIT ?",
            (*params, topk * 4),
        ).fetchall()
        scored: list[tuple[float, int]] = []
        for row in rows:
            text = row["content"]
            hits = sum(1 for g in grams if g in text)
            if hits:
                scored.append((hits / len(grams), int(row["rowid"])))
        scored.sort(reverse=True)
        return {rowid: -score for score, rowid in scored[:topk]}

    @staticmethod
    def _fts_expr(query: str) -> str | None:
        tokens = _TOKEN_RE.findall(query)
        usable = [t for t in tokens if len(t) >= 3]
        if not usable:
            return None
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in usable)

    @staticmethod
    def _bigrams(query: str) -> list[str]:
        grams: list[str] = []
        for token in _TOKEN_RE.findall(query):
            if len(token) < 2:
                continue
            if len(token) <= 3 or not _CJK_RE.search(token):
                grams.append(token)
                continue
            grams.extend(token[i : i + 2] for i in range(len(token) - 1))
        return list(dict.fromkeys(grams))

    @staticmethod
    def _normalize_rank(hits: dict[int, float], rowid: int) -> float:
        values = list(hits.values())
        best, worst = min(values), max(values)
        if worst == best:
            return 1.0
        return (worst - hits[rowid]) / (worst - best)

    @staticmethod
    def _fuse(vec_sim: float | None, lex_sim: float | None) -> float:
        parts: list[tuple[float, float]] = []
        if vec_sim is not None:
            parts.append((W_VECTOR_PASS, max(0.0, min(1.0, vec_sim))))
        if lex_sim is not None:
            parts.append((W_LEXICAL_PASS, max(0.0, min(1.0, lex_sim))))
        if not parts:
            return 0.0
        total = sum(w for w, _ in parts)
        return sum(w * v for w, v in parts) / total

    def _reinforce(self, hits: Sequence[Hit]) -> None:
        if not hits:
            return
        now = now_ms()
        grouped: dict[tuple[str, str], list[Hit]] = {}
        for hit in hits:
            db_name = getattr(hit.item, "db", "workspace")
            table = {
                "Knowledge": "knowledge",
                "Fact": "knowledge",
                "Episode": "episodes",
                "LogEntry": "logs",
            }[type(hit.item).__name__]
            grouped.setdefault((db_name, table), []).append(hit)

        for (db_name, table), group in grouped.items():
            try:
                _, conn = self._conn_for(db_name)
            except ScopeError:
                continue
            with transaction(conn):
                for hit in group:
                    conn.execute(
                        f"UPDATE {table} SET access_count = access_count + 1,"
                        " last_accessed_at = ? WHERE rowid = ?",
                        (now, hit.item.rowid),
                    )
                    hit.item.access_count += 1
                    hit.item.last_accessed_at = now

    # ================================================================ 复盘

    def _latest_pending_day(self) -> str | None:
        """最近一个还有未复盘日志的日期。定时任务靠它自动补齐积压。"""
        if self.workspace_conn is None:
            return None
        row = self.workspace_conn.execute(
            "SELECT day FROM logs WHERE consolidated = 0 ORDER BY day DESC LIMIT 1"
        ).fetchone()
        return row["day"] if row else None

    def consolidate_preview(
        self, day: str | None = None, *, limit: int = 300
    ) -> dict[str, Any]:
        """取出待复盘的原始日志，交给当前会话的模型去总结。

        `day` 不传时：先找最近一个仍有待复盘日志的日期（自动补积压），
        全都复盘完了才回落到今天。
        """
        pending_day = self._latest_pending_day() if day is None else None
        target = pending_day if pending_day else parse_day(day)
        logs = self.list_logs(day=target, include_consolidated=False, limit=limit)
        live = self.list_knowledge(scope="all", limit=20)
        return {
            "day": target,
            "log_count": len(logs),
            "logs": [log.to_dict() for log in logs],
            "existing_knowledge": [f.to_dict() for f in live],
            "instructions": (
                "阅读 logs 后产出三部分：\n"
                "1) knowledge：可迁移的机制/约定，**换掉单号和任务 ID 仍然成立**，"
                "最多 500 字；若与 existing_knowledge 冲突，用 invalidates 传旧 id。\n"
                "2) state：当前为真但会变的事实（版本、依赖、路径、资源配置），"
                "最多 200 字，带 source 说明凭什么这么认为。\n"
                "3) episodes：一块给 kind='case'（现象→根因→落点，≤300 字，一个问题一条），"
                "另一块给 kind='narrative'（当天过程与判断转折，可长）。\n"
                "单号细节只进 case，不要写成 knowledge。\n"
                "然后调用 consolidate_apply 提交，purge=true 会清空已复盘的原始日志。"
            ),
        }

    def consolidate_apply(
        self,
        *,
        day: str | None = None,
        knowledge: Sequence[dict[str, Any]] | None = None,
        facts: Sequence[dict[str, Any]] | None = None,
        episodes: Sequence[Any] | None = None,
        summary: str | None = None,
        purge: bool = True,
        model: str | None = None,
    ) -> dict[str, Any]:
        """提交复盘结果。

        `facts` 是旧参数名，保留兼容：默认按 knowledge 落库。
        `knowledge` 里每项可带 `kind`（knowledge / state）。
        `episodes` 每项可带 `kind`（case / narrative），不传按 narrative。

        两道闸门，防止「结论没写全、原始日志却已被清空」：
        1) 先整批前置校验。任何一项不合法抛 ConsolidationRejected，此时零写入、
           原始日志保持 pending，改完参数可原样重跑。
        2) 所有写入合进同一个事务，中途异常整体回滚；只有条目全部写成功，
           才标记日志、写 digest、按 purge 删除原始日志。
        """
        prepared_knowledge, prepared_episodes, summary_as_narrative = _prepare_apply_items(
            knowledge, facts, episodes, summary
        )

        # day 不传时与 consolidate_preview 对齐：取最近一个仍有积压的日期。
        # 旧版默认 today，补历史时会把经历和 digest 记到今天、并误删今天的日志。
        target = parse_day(day if day is not None else self._latest_pending_day())
        db_name, conn = self._conn_for("workspace")
        pending = self.list_logs(day=target, include_consolidated=False, limit=1000)
        if not pending:
            return {
                "day": target,
                "log_count": 0,
                "created_knowledge": [],
                "created_episodes": [],
                "note": "该日没有待复盘的日志",
            }

        digest_id = new_id("d")
        now = now_ms()
        created_knowledge: list[Knowledge] = []
        created_episodes: list[Episode] = []
        skipped_knowledge: list[dict[str, Any]] = []

        existing = {
            f.content.strip()
            for f in self.list_knowledge(scope="all", include_invalid=False, limit=1000)
        }

        # 整个提交一个事务：write_* 内部的事务会退化成 SAVEPOINT，
        # 任何一步抛异常都整体回滚，日志与 digest 都不会被改动。
        with transaction(conn):
            for index, raw in enumerate(prepared_knowledge):
                content = raw["content"]
                if content in existing:
                    skipped_knowledge.append(
                        {"index": index, "content": content, "reason": "duplicate"}
                    )
                    continue
                try:
                    item = self.write_memory(
                        content,
                        kind=raw["kind"],
                        tags=raw.get("tags"),
                        importance=raw.get("importance"),
                        scope="workspace",
                        source=raw.get("source") or f"digest:{digest_id}",
                    )
                except (ValueError, ScopeError) as exc:
                    raise ConsolidationRejected(
                        f"knowledge[{index}] 写入失败：{exc}"
                    ) from exc
                if not isinstance(item, Knowledge):
                    raise ConsolidationRejected(
                        f"knowledge[{index}] 未生成 knowledge 记录，整批已回滚"
                    )
                existing.add(content)
                created_knowledge.append(item)
                old_id = raw.get("invalidates")
                if old_id:
                    try:
                        self.invalidate(str(old_id), invalidated_by=item.id)
                    except NotFound:
                        pass

            last_at = max(log.at for log in pending)
            for index, item in enumerate(prepared_episodes):
                try:
                    created_episodes.append(
                        self.write_episode(
                            item["content"],
                            kind=item["kind"],
                            importance=item.get("importance"),
                            tags=item.get("tags"),
                            at=last_at,
                            day=target,
                            digest_id=digest_id,
                            source=f"digest:{digest_id}",
                        )
                    )
                except (ValueError, ScopeError) as exc:
                    raise ConsolidationRejected(
                        f"episodes[{index}] 写入失败：{exc}"
                    ) from exc

            # 只有条目全部写成功，才标记日志 / 写 digest / 删除原始日志
            for log in pending:
                conn.execute(
                    "UPDATE logs SET consolidated = 1, digest_id = ? WHERE rowid = ?",
                    (digest_id, log.rowid),
                )
            conn.execute(
                "INSERT INTO digests(id, day, at, log_count, knowledge_count,"
                " episode_count, summary, purged, model, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    digest_id,
                    target,
                    now,
                    len(pending),
                    len(created_knowledge),
                    len(created_episodes),
                    summary,
                    1 if purge else 0,
                    model,
                    json.dumps({"log_ids": [x.id for x in pending]}, ensure_ascii=False),
                ),
            )
            purged = self._purge_logs(digest_id) if purge else 0

        warnings: list[str] = []
        if summary_as_narrative:
            warnings.append(
                "episodes 为空但传了 summary：已按兼容行为把 summary 记成一条 narrative；"
                "若只想写 digest，请不要传 summary"
            )
        if pending and not prepared_knowledge and not prepared_episodes:
            warnings.append(
                f"当天有 {len(pending)} 条日志却零产出："
                "确认不是漏传 knowledge / episodes 数组"
            )

        return {
            "day": target,
            "digest_id": digest_id,
            "log_count": len(pending),
            "requested_knowledge": len(prepared_knowledge),
            "requested_episodes": len(prepared_episodes),
            "created_knowledge": [f.to_dict() for f in created_knowledge],
            "created_episodes": [e.to_dict() for e in created_episodes],
            "skipped_knowledge": skipped_knowledge,
            "warnings": warnings,
            "purged": purged,
        }

    def _insert_episode(
        self,
        conn: Any,
        db_name: str,
        text: str,
        at: int,
        day: str,
        digest_id: str,
        compression_level: int | None = None,
        importance: float | None = None,
        kind: str = "narrative",
    ) -> Episode:
        """低阶插入：给压缩 / 合并这类需要指定 digest 的内部流程用。"""
        return self.write_episode(
            text,
            kind=kind,
            importance=importance,
            at=at,
            day=day,
            digest_id=digest_id,
            compression_level=compression_level,
        )

    def _purge_logs(self, digest_id: str) -> int:
        """物理删除已复盘的原始日志（需求：复盘后清空当天的临时日志）。"""
        if self.workspace_conn is None:
            return 0
        conn = self.workspace_conn
        rows = conn.execute(
            "SELECT rowid FROM logs WHERE digest_id = ? AND consolidated = 1", (digest_id,)
        ).fetchall()
        if not rows:
            return 0
        with transaction(conn):
            for row in rows:
                rowid = int(row["rowid"])
                conn.execute("DELETE FROM logs_vec WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM logs WHERE rowid = ?", (rowid,))
        return len(rows)

    def list_episodes(
        self,
        *,
        kinds: Sequence[str] | str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[Episode]:
        if self.workspace_conn is None:
            return []
        wanted = _row_kinds(kinds, ("case", "narrative"))
        sql = "SELECT * FROM episodes"
        clauses: list[str] = []
        params: list[Any] = []
        if wanted:
            clauses.append("kind IN (" + ",".join("?" * len(wanted)) + ")")
            params.extend(sorted(wanted))
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        rows = self.workspace_conn.execute(sql, tuple(params)).fetchall()
        return [Episode.from_row(r, "workspace") for r in rows]

    def list_digests(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if self.workspace_conn is None:
            return []
        rows = self.workspace_conn.execute(
            "SELECT * FROM digests ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 渐进压缩

    def recompress_preview(self, *, limit: int = 50) -> dict[str, Any]:
        """列出该被进一步压缩的经历。

        两条规则共同决定：
        - 级别：level 0 → 1（基准 30 天），level 1 → 2（基准 180 天）
        - 重要性缩放：门槛 × (0.6 + importance)，重要的压得慢

        所以一条 importance 0.9 的重大经历，要 45 天才从完整叙述降到要点，
        而 importance 0.2 的琐事 24 天就压了。
        """
        if self.workspace_conn is None:
            return {"groups": [], "total": 0}
        now = now_ms()
        groups: list[dict[str, Any]] = []
        for target, base_days in sorted(
            self.cfg.recompress_days.items(), key=lambda kv: int(kv[0])
        ):
            lvl = int(target)
            rows = self.workspace_conn.execute(
                "SELECT id, content, day, created_at, compression_level, importance"
                " FROM episodes WHERE archived_at IS NULL AND kind = 'narrative'"
                " AND compression_level = ?"
                " ORDER BY created_at LIMIT ?",
                (lvl - 1, max(limit * 4, 50)),
            ).fetchall()

            items: list[dict[str, Any]] = []
            for r in rows:
                imp = float(r["importance"] or 0.4)
                threshold = recompress_threshold(base_days, imp)
                age_days = (now - int(r["created_at"])) / DAY_MS
                if age_days < threshold:
                    continue
                items.append(
                    {
                        "id": r["id"],
                        "day": r["day"],
                        "current_level": r["compression_level"],
                        "importance": round(imp, 2),
                        "age_days": round(age_days, 1),
                        "threshold_days": round(threshold, 1),
                        "content": str(r["content"]),
                    }
                )
                if len(items) >= limit:
                    break

            if not items:
                continue
            groups.append(
                {
                    "target_level": lvl,
                    "base_days": base_days,
                    "count": len(items),
                    "items": items,
                }
            )

        return {
            "groups": groups,
            "total": sum(g["count"] for g in groups),
            "note": (
                "level 0 是完整叙述，压到 1 去掉冗余但保留关键步骤；"
                "level 1 → 2 只留结论与根因。改写后调用 recompress_apply 提交。"
                "重要性高的条目门槛更长，所以压得更慢。"
            ),
        }

    def recompress_apply(
        self, items: Sequence[dict[str, Any]], *, level: int
    ) -> dict[str, Any]:
        """把若干经历改写成更短的版本并提升级别。items: [{"id":..., "content":...}]"""
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")
        if level not in (1, 2, 3):
            raise ValueError("level 只能是 1 / 2 / 3")
        updated: list[str] = []
        skipped: list[str] = []
        for item in items:
            mid = str(item.get("id", "")).strip()
            text = str(item.get("content", "")).strip()
            if not mid or not text:
                continue
            row = self.workspace_conn.execute(
                "SELECT rowid FROM episodes WHERE id = ?", (mid,)
            ).fetchone()
            if row is None:
                skipped.append(mid)
                continue
            vectors = self.embedder.try_embed([text])
            vec = vectors[0] if vectors else None
            with transaction(self.workspace_conn):
                self.workspace_conn.execute(
                    "UPDATE episodes SET content = ?, compression_level = ?, embedded = ?"
                    " WHERE id = ?",
                    (text, level, 1 if vec else 0, mid),
                )
                if vec:
                    self._vec_put(self.workspace_conn, EPISODE, int(row["rowid"]), vec)
            updated.append(mid)
        return {
            "updated": updated,
            "skipped": skipped,
            "level": level,
            "count": len(updated),
        }

    # ================================================================ 运维

    # ================================================================ 生命维持
    # 原则：归档可逆、删除不可逆。因此这里只做「降级」和「报告候选」，
    # 真正的物理删除必须由调用方显式指定 id 才会发生。

    def archive_pass(self, *, dry_run: bool = False) -> dict[str, Any]:
        """把「该退场」的陈述与经历转为归档。

        归档 = 置 archived_at，正文一个字不动。默认检索不到，
        `include_archived=True` 仍可查回。信条不在此列（它根本不在库里）。

        TTL 按**行级 kind** 取，不是按表：
          knowledge 3 年、case / narrative 1 年 —— 走「久未触碰 + 访问少」判据。
          **state 不走这套**：它按复核梯子（1→3→5→7 天）+ 宽限判定，
          只有宽限都过了（overdue）才归档。被检索到不算续命，只有 verified_at 算。
        """
        now = now_ms()
        shelved: list[dict[str, Any]] = []
        kept = 0
        actions: list[tuple[Any, TableSpec, int]] = []

        for _db_name, conn in self._all_dbs("all"):
            for spec in (KNOWLEDGE, EPISODE):
                # verified_at / state_interval_days 只有陈述层有；经历层补 NULL 占位
                extra = (
                    "verified_at, state_interval_days"
                    if spec is KNOWLEDGE
                    else "NULL AS verified_at, NULL AS state_interval_days"
                )
                rows = conn.execute(
                    f"SELECT rowid, id, kind, content, created_at, last_accessed_at,"
                    f" access_count, {extra} FROM {spec.table}"
                    f" WHERE archived_at IS NULL"
                ).fetchall()
                for row in rows:
                    item_kind = str(row["kind"] or spec.kind)

                    if item_kind == "state":
                        interval = float(
                            row["state_interval_days"]
                            or self.cfg.archive_ttl.get("state", 7.0)
                        )
                        status, reason = state_review_status(
                            int(row["created_at"]),
                            row["verified_at"],
                            interval,
                            now,
                            self.cfg.state_grace_cap_days,
                        )
                        if status != REVIEW_OVERDUE:
                            kept += 1
                            continue
                    else:
                        ttl = self.cfg.archive_ttl.get(item_kind)
                        if ttl is None:
                            continue
                        should, reason = archive_decision(
                            int(row["created_at"]),
                            row["last_accessed_at"],
                            int(row["access_count"]),
                            now,
                            ttl,
                            self.cfg.archive_max_access,
                        )
                        if not should:
                            kept += 1
                            continue

                    shelved.append(
                        {
                            "type": item_kind,
                            "id": row["id"],
                            "content": str(row["content"])[:60],
                            "reason": reason,
                        }
                    )
                    actions.append((conn, spec, int(row["rowid"])))

        if not dry_run and actions:
            by_conn: dict[int, list[tuple[TableSpec, int]]] = {}
            for conn, spec, rowid in actions:
                by_conn.setdefault(id(conn), []).append((spec, rowid))
            for _db_name, conn in self._all_dbs("all"):
                group = by_conn.get(id(conn))
                if not group:
                    continue
                with transaction(conn):
                    for spec, rowid in group:
                        conn.execute(
                            f"UPDATE {spec.table} SET archived_at = ? WHERE rowid = ?",
                            (now, rowid),
                        )
                        # 归档后从向量索引摘掉，别白占 KNN 候选位
                        conn.execute(f"DELETE FROM {spec.vec} WHERE rowid = ?", (rowid,))
                        conn.execute(
                            f"UPDATE {spec.table} SET embedded = 0 WHERE rowid = ?", (rowid,)
                        )

        return {
            "dry_run": dry_run,
            "archived": shelved,
            "archived_count": len(shelved),
            "kept": kept,
            "rule": {
                "ttl_days": self.cfg.archive_ttl,
                "max_access": self.cfg.archive_max_access,
                "state_review": {
                    "ladder_days": list(self.cfg.state_ladder),
                    "grace_cap_days": self.cfg.state_grace_cap_days,
                    "note": "state 只按复核梯子 + 宽限归档；到期未复核不会立刻消失",
                },
            },
            "note": "归档可逆：include_archived=True 仍能查回，正文未改动",
        }

    def unarchive(self, memory_id: str) -> dict[str, Any]:
        """从归档中恢复一条记忆，并重新嵌入。"""
        for _db_name, conn in self._all_dbs("all"):
            for spec in (KNOWLEDGE, EPISODE):
                row = conn.execute(
                    f"SELECT rowid, content, kind FROM {spec.table} WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if row is None:
                    continue
                vectors = self.embedder.try_embed([row["content"]])
                vec = vectors[0] if vectors else None
                with transaction(conn):
                    conn.execute(
                        f"UPDATE {spec.table} SET archived_at = NULL WHERE id = ?",
                        (memory_id,),
                    )
                    if vec:
                        self._vec_put(conn, spec, int(row["rowid"]), vec)
                        conn.execute(
                            f"UPDATE {spec.table} SET embedded = 1 WHERE rowid = ?",
                            (int(row["rowid"]),),
                        )
                return {"id": memory_id, "type": row["kind"], "restored": True}
        raise NotFound(f"找不到记忆 {memory_id}")

    def purge_candidates(self, *, after_days: float | None = None) -> list[dict[str, Any]]:
        """归档超过 N 天、期间仍未再被访问的条目 —— **仅候选，不删除**。"""
        days = self.cfg.purge_after_days if after_days is None else after_days
        cutoff = now_ms() - int(days * DAY_MS)
        out: list[dict[str, Any]] = []
        for db_name, conn in self._all_dbs("workspace"):
            for spec in (KNOWLEDGE, EPISODE):
                rows = conn.execute(
                    f"SELECT id, kind, content, archived_at, last_accessed_at, access_count"
                    f" FROM {spec.table}"
                    f" WHERE archived_at IS NOT NULL AND archived_at <= ?"
                    f"   AND (last_accessed_at IS NULL OR last_accessed_at < archived_at)",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    out.append(
                        {
                            "type": row["kind"],
                            "db": db_name,
                            "id": row["id"],
                            "content": str(row["content"])[:60],
                            "archived_at": row["archived_at"],
                            "access_count": row["access_count"],
                            "idle_days": round((now_ms() - int(row["archived_at"])) / DAY_MS, 1),
                        }
                    )
        out.sort(key=lambda x: x["archived_at"])
        return out

    def purge_items(self, ids: Sequence[str]) -> dict[str, Any]:
        """物理删除指定记忆（含向量与全文索引）。**不可恢复**，仅由显式调用触发。"""
        removed: list[str] = []
        missing: list[str] = []
        for memory_id in ids:
            hit = False
            for _db_name, conn in self._all_dbs("all"):
                for spec in (KNOWLEDGE, EPISODE):
                    row = conn.execute(
                        f"SELECT rowid, archived_at FROM {spec.table} WHERE id = ?", (memory_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    if row["archived_at"] is None:
                        raise ValueError(
                            f"{memory_id} 尚未归档，拒绝直接删除。先 archive 或显式确认后再删。"
                        )
                    with transaction(conn):
                        conn.execute(f"DELETE FROM {spec.vec} WHERE rowid = ?", (int(row["rowid"]),))
                        conn.execute(f"DELETE FROM {spec.table} WHERE id = ?", (memory_id,))
                    removed.append(memory_id)
                    hit = True
                    break
                if hit:
                    break
            if not hit:
                missing.append(memory_id)
        return {"purged": removed, "missing": missing, "count": len(removed)}

    def compress_candidates(self, *, limit: int = 30) -> dict[str, Any]:
        """列出可压缩的经历：同一个 day 下有多条，且都强度偏低/已归档。"""
        if self.workspace_conn is None:
            return {"groups": [], "total": 0}

        rows = self.workspace_conn.execute(
            "SELECT id, content, day, at, access_count, archived_at FROM episodes"
                " WHERE kind = 'narrative'"
            " ORDER BY day DESC, at ASC"
        ).fetchall()
        by_day: dict[str, list[Any]] = {}
        for row in rows:
            by_day.setdefault(row["day"], []).append(row)

        groups: list[dict[str, Any]] = []
        for day, items in by_day.items():
            if len(items) < 2:
                continue
            groups.append(
                {
                    "day": day,
                    "ids": [r["id"] for r in items],
                    "count": len(items),
                    "archived": sum(1 for r in items if r["archived_at"] is not None),
                    "preview": [str(r["content"])[:50] for r in items[:4]],
                }
            )
        groups.sort(key=lambda g: g["day"], reverse=True)
        return {
            "groups": groups[:limit],
            "total": len(groups),
            "note": "压缩由 agent 总结后调用 compress_apply，不自动执行",
        }

    def compress_apply(
        self, ids: Sequence[str], summary: str, *, day: str | None = None
    ) -> dict[str, Any]:
        """把多条经历合并成一条新经历，原条目转为归档（不删除）。"""
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("压缩后的内容不能为空")

        sources = []
        for memory_id in ids:
            row = self.workspace_conn.execute(
                "SELECT rowid, id, content, day, at, archived_at FROM episodes WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is not None:
                sources.append(row)
        if not sources:
            raise NotFound("指定的经历都不存在")

        target_day = day or sources[0]["day"]
        last_at = max(int(r["at"]) for r in sources)
        new_ep = self._insert_episode(
            self.workspace_conn, "workspace", summary, last_at, target_day, "compress"
        )
        now = now_ms()
        with transaction(self.workspace_conn):
            for row in sources:
                if row["archived_at"] is None:
                    self.workspace_conn.execute(
                        "UPDATE episodes SET archived_at = ? WHERE id = ?", (now, row["id"])
                    )
                    self.workspace_conn.execute(
                        "DELETE FROM episodes_vec WHERE rowid = ?", (int(row["rowid"]),)
                    )
        return {
            "compressed": [r["id"] for r in sources],
            "merged_into": new_ep.to_dict(),
            "day": target_day,
            "note": "原条目已归档（保留可查），未删除",
        }

    def stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "embedding_model": self.cfg.embed_model or None,
            "embedding_available": self.embedder.available,
            "dbs": {},
        }

        for db_name, conn in self._all_dbs("all"):
            path = self.layout.workspace_db if db_name == "workspace" else self.layout.global_db
            info: dict[str, Any] = {"path": str(path)}

            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(invalid_at IS NULL), 0) AS live,"
                " COALESCE(SUM(archived_at IS NOT NULL), 0) AS archived,"
                " COALESCE(SUM(kind = 'knowledge'), 0) AS kn,"
                " COALESCE(SUM(kind = 'state'), 0) AS st"
                " FROM knowledge"
            ).fetchone()
            info["knowledge"] = int(row["total"] or 0)
            info["knowledge_live"] = int(row["live"] or 0)
            info["knowledge_archived"] = int(row["archived"] or 0)
            info["knowledge_only"] = int(row["kn"] or 0)
            info["state"] = int(row["st"] or 0)

            # 该复核的现状：让「到期」在每次看统计时都被看见，而不是等人想起来
            review_now = now_ms()
            due_n = overdue_n = 0
            for _srow, status, _reason, _iv, _due, _exp in self._review_state_rows(
                conn, review_now
            ):
                if status == REVIEW_DUE:
                    due_n += 1
                elif status == REVIEW_OVERDUE:
                    overdue_n += 1
            info["state_due"] = due_n
            info["state_overdue"] = overdue_n

            if db_name == "workspace":
                ep_row = conn.execute(
                    "SELECT COUNT(*) AS n,"
                    " COALESCE(SUM(archived_at IS NOT NULL), 0) AS a,"
                    " COALESCE(SUM(kind = 'case'), 0) AS c"
                    " FROM episodes"
                ).fetchone()
                info["episodes"] = int(ep_row["n"] or 0)
                info["episodes_archived"] = int(ep_row["a"] or 0)
                info["cases"] = int(ep_row["c"] or 0)
                lg = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(consolidated = 0), 0) AS pending FROM logs"
                ).fetchone()
                info["logs"] = int(lg["n"] or 0)
                info["logs_pending"] = int(lg["pending"] or 0)
                info["digests"] = int(
                    conn.execute("SELECT COUNT(*) AS n FROM digests").fetchone()["n"] or 0
                )

            pending_embed = 0
            for spec in ALL_SPECS:
                if db_name == "global" and spec is not KNOWLEDGE:
                    continue
                pending_embed += int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM {spec.table} WHERE embedded = 0"
                    ).fetchone()["n"]
                    or 0
                )
            info["pending_embedding"] = pending_embed
            info["size_bytes"] = path.stat().st_size if path.exists() else 0
            result["dbs"][db_name] = info

        global_live = result["dbs"].get("global", {}).get("knowledge_live", 0)
        result["global_knowledge"] = global_live
        result["global_knowledge_warn"] = global_live > self.cfg.global_knowledge_warn
        if result["global_knowledge_warn"]:
            result["hint"] = (
                f"全局条目已有 {global_live} 条（阈值 {self.cfg.global_knowledge_warn}）。"
                "跨项目内容本该很少：规则类应写进 CORE_RULES.md，"
                "项目细节应留在各自的 workspace 库。"
            )

        due_all = sum(int(d.get("state_due", 0)) for d in result["dbs"].values())
        overdue_all = sum(int(d.get("state_overdue", 0)) for d in result["dbs"].values())
        result["state_review"] = {
            "due": due_all,
            "overdue": overdue_all,
            "ladder_days": list(self.cfg.state_ladder),
        }
        if overdue_all:
            extra = (
                f"有 {overdue_all} 条现状已过宽限期，下次归档扫描会被收走。"
                "先用 memory_review_list 看一眼：仍成立就 memory_review(matched=true) 续命。"
            )
            result["hint"] = (result.get("hint", "") + " " + extra).strip()
        elif due_all:
            extra = (
                f"有 {due_all} 条现状到了复核时间（宽限期内，暂不归档）。"
                "用 memory_review_list 处理。"
            )
            result["hint"] = (result.get("hint", "") + " " + extra).strip()
        return result

    def backfill_embeddings(self, *, limit: int = 500) -> dict[str, Any]:
        if not self.embedder.available:
            return {"embedded": 0, "skipped": "embedding 未配置"}
        done = 0
        for db_name, conn in self._all_dbs("all"):
            for spec in ALL_SPECS:
                if db_name == "global" and spec is not KNOWLEDGE:
                    continue
                # 归档行也留着 embedded=0，但它们已经不参与检索，补向量纯属浪费
                where = "embedded = 0" if spec is LOG else "embedded = 0 AND archived_at IS NULL"
                rows = conn.execute(
                    f"SELECT rowid, content FROM {spec.table} WHERE {where} LIMIT ?",
                    (limit,),
                ).fetchall()
                if not rows:
                    continue
                vectors = self.embedder.try_embed([r["content"] for r in rows])
                if not vectors:
                    continue
                with transaction(conn):
                    for row, vec in zip(rows, vectors):
                        if not vec:
                            continue
                        rowid = int(row["rowid"])
                        self._vec_put(conn, spec, rowid, vec)
                        conn.execute(
                            f"UPDATE {spec.table} SET embedded = 1 WHERE rowid = ?", (rowid,)
                        )
                        done += 1
        return {"embedded": done}

    def reset_vectors(self) -> None:
        for _, conn in self._all_dbs("all"):
            for spec in ALL_SPECS:
                conn.execute(f"DELETE FROM {spec.vec}")
                conn.execute(f"UPDATE {spec.table} SET embedded = 0")

    # ================================================================ 导出

    def export_markdown(self, out_dir: Path | str, *, include_invalid: bool = False) -> dict[str, Any]:
        """把项目库导出成「知识 / 现状 / 案例 / 经历 / 日志」Markdown 视图。

        数据库是唯一事实源，导出的 Markdown 是**只读视图**——
        给人翻阅、给 git 留痕用，不要手改（改了会被下次导出覆盖）。
        注意它**不是备份**：不含 id、access_count、verified_at 与向量，
        恢复后 id 全变、引用链会断。备份请复制 .db（含 -wal / -shm）。

        **只由 `humanity-memory export` 手动触发**：这里不挂任何自动/定时/写入后
        回调，`out_dir` 也不做默认推导。想在收尾时刷新视图，就显式跑一次命令。
        """
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")

        root = Path(out_dir)
        (root / "经历").mkdir(parents=True, exist_ok=True)
        (root / "日志").mkdir(parents=True, exist_ok=True)
        written: list[str] = []

        # --- 陈述：知识 / 现状 各一节，一条一行
        items = self.list_knowledge(
            scope="workspace", include_invalid=include_invalid, limit=5000
        )
        lines = [
            "# 知识 / 现状",
            "",
            "> 由 `humanity-memory export` 手动导出，**请勿手改**——下次导出会覆盖。",
            "> 增删改用 `memory_write` / `memory_invalidate`。",
            "",
            f"## 知识（{sum(1 for i in items if i.kind == 'knowledge')}）",
            "",
        ]
        for item in items:
            if item.kind != "knowledge":
                continue
            mark = "" if item.live else "（已失效）"
            tag = f"  <!-- {' '.join(item.tags)} -->" if item.tags else ""
            lines.append(f"- {item.content}{mark}{tag}")

        lines += ["", f"## 现状（{sum(1 for i in items if i.kind == 'state')}）", ""]
        for item in items:
            if item.kind != "state":
                continue
            mark = "" if item.live else "（已失效）"
            verified = item.to_dict().get("verified_iso", "未确认")
            lines.append(f"- {item.content}{mark}  <!-- 确认于 {verified} -->")

        path = root / "知识现状.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(str(path))

        # --- 经历：一件事一个文件（案例文件名前缀区分）
        episodes = self.list_episodes(limit=2000)
        for ep in episodes:
            prefix = "案例" if ep.is_case else "叙述"
            name = f"{ep.day}-{prefix}-{_slug(ep.content)}.md"
            body = [
                f"# {ep.day} · {_slug(ep.content, 40)}",
                "",
                f"> 导出视图 · id `{ep.id}` · 类型 `{ep.kind}` · 复盘 `{ep.digest_id or 'n/a'}`",
                "",
                ep.content,
                "",
            ]
            target = root / "经历" / name
            target.write_text("\n".join(body), encoding="utf-8")
            written.append(str(target))

        # --- 日志：一天一个文件（复盘后通常已清空）
        logs = self.list_logs(include_consolidated=True, limit=5000)
        by_day: dict[str, list[LogEntry]] = {}
        for entry in logs:
            by_day.setdefault(entry.day, []).append(entry)
        for day, entries in sorted(by_day.items()):
            body = [f"# {day} 日志", ""]
            for entry in entries:
                stamp = (entry.to_dict().get("at_iso") or "")[11:19]
                flag = " ✓已复盘" if entry.consolidated else ""
                body.append(f"- `{stamp}` {entry.content}{flag}")
            target = root / "日志" / f"{day}.md"
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
            written.append(str(target))

        return {
            "out_dir": str(root),
            "knowledge": sum(1 for i in items if i.kind == "knowledge"),
            "state": sum(1 for i in items if i.kind == "state"),
            "episodes": len(episodes),
            "cases": sum(1 for e in episodes if e.is_case),
            "log_days": len(by_day),
            "files": written,
        }

    def _vec_put(
        self, conn: Any, spec: TableSpec, rowid: int, vector: Sequence[float]
    ) -> None:
        conn.execute(f"DELETE FROM {spec.vec} WHERE rowid = ?", (rowid,))
        conn.execute(
            f"INSERT INTO {spec.vec}(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize_float32(vector)),
        )
