"""存储门面：经验 / 经历 / 日志 + 跨库检索 + 复盘固化。

检索策略
--------
vec0 的 KNN 不支持任意 WHERE 过滤，因此每张表都是「先召回候选 rowid，
再 join 主表在 Python 侧过滤打分」。跨库检索时对 workspace 与 global
分别执行再统一排序 —— 全局经验无论当前在哪个项目都参与召回。

类型与表的关系
--------------
记忆只有两类，类型由**表**承载，没有行级 kind：
    experience  经验（可复用的判定与结论，不压缩，会被推翻重写）
    episodes    经历（发生过的事，会逐级压缩）
    logs        日志（当天原始记录，复盘后被清掉）
所以按类型检索就是选表；`kinds` 只做表级归并，不再有第二层过滤。
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config
from .confidence import ConfidenceCurve
from .db import get_meta, open_db, transaction
from .embeddings import EmbeddingProvider
from .lifecycle import (
    DAY_MS,
    LevelTable,
    PERIOD_EPISODE,
    PERIOD_EXPERIENCE,
    activity_contribution,
    archive_decision,
    combine_score,
    compression_target,
    initial_compression_level,
    initial_level,
    level_factor,
    period_kind,
    similarity_from_cosine_distance,
)
from .models import (
    ALL_KINDS,
    Episode,
    Experience,
    Hit,
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

# 经历的压缩级别越高越「精炼」，检索权重越高；
# 刚复盘出来的完整叙述（level 0）内容长、冗余多，降权避免挤占结果。
EPISODE_LEVEL_WEIGHT = {0: 0.88, 1: 0.93, 2: 0.96, 3: 0.96}

EXPERIENCE_MAX_CHARS = 500

# 类型只有两个字面量，写错了直接报错并列出口径。
VALID_KINDS: tuple[str, ...] = ("experience", "episode")


@dataclass(frozen=True)
class TableSpec:
    kind: str
    table: str
    vec: str
    fts: str
    time_attr: str
    type_weight: float
    model: type


EXPERIENCE = TableSpec(
    "experience", "knowledge", "knowledge_vec", "knowledge_fts",
    "created_at", 1.00, Experience,
)
EPISODE = TableSpec(
    "episode", "episodes", "episodes_vec", "episodes_fts",
    "at", 0.96, Episode,
)
LOG = TableSpec("log", "logs", "logs_vec", "logs_fts", "at", 0.90, LogEntry)

ALL_SPECS: tuple[TableSpec, ...] = (EXPERIENCE, EPISODE, LOG)

SPEC_BY_KIND: dict[str, TableSpec] = {spec.kind: spec for spec in ALL_SPECS}

# 全局库已关闭（§8.2）。保留这句话是为了让后来的人一眼知道「这里曾有一个
# 全局库，为什么没了」，而不是靠翻 git log 猜。
GLOBAL_SCOPE_CLOSED = (
    "全局库已关闭（§8.2）：AI 自动写入的跨项目库会污染所有项目，"
    "跨项目该共享的硬约束请写进 CORE_RULES.md（静态注入），"
    "其余内容留在各自的项目库里。原库已导出并改名留档。"
)


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


def _record_event(
    conn: Any,
    *,
    moment: int,
    table: str,
    item_id: str,
    event: str,
    from_value: str | None = None,
    to_value: str | None = None,
    delta: float | None = None,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """往 memory_events 追加一行审计。

    这张表只增不改（触发器挡着 UPDATE/DELETE），所以「这条为什么降级/为什么置信」
    三个月后仍然查得到 —— 代价只是一次 INSERT。
    """
    conn.execute(
        "INSERT INTO memory_events(at, table_name, item_id, event,"
        " from_value, to_value, delta, reason, meta) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            moment,
            table,
            item_id,
            event,
            from_value,
            to_value,
            delta,
            reason,
            json.dumps(meta, ensure_ascii=False) if meta else "{}",
        ),
    )


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


def _norm_kind(kind: str | None, *, writable: bool = True) -> str:
    """归一化类型。

    只有 experience / episode 两个合法值，写错就报错 —— 没有别名、没有旧词映射。
    `writable=False` 时额外放行 log：日志不可 write_memory 写入（它有独立的
    append_log），但它是一类记忆，检索与列表要能指定它。
    """
    value = (kind or "experience").strip().lower()
    allowed = VALID_KINDS if writable else ALL_KINDS
    if value not in allowed:
        raise ValueError(
            f"未知 kind={kind!r}：记忆只有两类 —— experience（经验）/ episode（经历）"
            + ("" if writable else "，检索时另可指定 log（日志）")
        )
    return value


def _check_writable(content: str, kind: str, *, label: str = "写入") -> tuple[str, str]:
    """写入前的唯一内容判据：归一化类型、去空白、按类型卡长度。

    write_experience / write_episode / consolidate_apply 共用这一套规则，
    杜绝「前置校验放行、真正写入却被拒」的规则漂移。返回 (正文, 归一化类型)。
    """
    k = _norm_kind(kind)
    if k not in ("experience", "episode"):
        raise ValueError(f"{label}: 未知 kind={kind!r}")
    text = (content or "").strip()
    if not text:
        raise ValueError(f"{label}: 内容不能为空")
    if k == "experience" and len(text) > EXPERIENCE_MAX_CHARS:
        raise ValueError(
            f"{label}: 经验上限 {EXPERIENCE_MAX_CHARS} 字（当前 {len(text)} 字）。"
            "经验要能一句话说清；带过程与时间线的细节写 episode，让它自己压缩"
        )
    return text, k


def _prepare_apply_items(
    experience: Sequence[dict[str, Any]] | None,
    episodes: Sequence[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """复盘提交的整批前置校验：纯函数，不碰数据库。

    任何一项不合法都整批拒绝：此时一条结论都没写、日志仍是 pending，
    改完参数可原样重跑。沉默跳过会让「ok=true 但少了几条、日志已被清空」发生。
    """
    prepared_experience: list[dict[str, Any]] = []
    prepared_episodes: list[dict[str, Any]] = []
    for bucket, raws, allowed, default_kind in (
        ("experience", list(experience or []), ("experience",), "experience"),
        ("episodes", list(episodes or []), ("episode",), "episode"),
    ):
        for index, raw in enumerate(raws):
            where = f"{bucket}[{index}]"
            if isinstance(raw, str):
                raw = {"content": raw}
            if not isinstance(raw, dict):
                raise ConsolidationRejected(f"{where} 必须是对象或字符串")
            try:
                kind = _norm_kind(raw.get("kind") or default_kind)
            except ValueError as exc:
                raise ConsolidationRejected(f"{where}：{exc}") from exc
            if kind not in allowed:
                raise ConsolidationRejected(
                    f"{where} kind={kind!r} 放错了数组；{bucket} 只接受 {list(allowed)}"
                )
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
            if bucket == "experience":
                item["source"] = raw.get("source")
                item["invalidates"] = raw.get("invalidates")
                item["confidence"] = raw.get("confidence")
            else:
                item["level"] = raw.get("level")
            (prepared_experience if bucket == "experience" else prepared_episodes).append(item)

    return prepared_experience, prepared_episodes


class MemoryStore:
    def __init__(self, cfg: Config, layout: Layout) -> None:
        self.cfg = cfg
        self.layout = layout
        self.embedder = EmbeddingProvider(cfg)
        # 等级表在这里一次性读成运行时视图：配置改了就随下次启动生效，判定时不再回表
        self.level_table = LevelTable.from_config(cfg)
        self.confidence_curve = ConfidenceCurve.from_config(cfg)
        # 全局库已按 §8.2 关闭：这里**不打开** layout.global_db。
        # 打开就会顺手建出一个空库，等于把刚关掉的东西又造回来。
        self.global_conn = None
        self.workspace_conn = (
            open_db(layout.workspace_db, cfg) if layout.workspace_db else None
        )
        try:
            self.vec_source = (
                get_meta(self.workspace_conn, "vec_source")
                if self.workspace_conn is not None
                else "unknown"
            ) or "unknown"
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
        if (scope or "").lower() == "global":
            raise ScopeError(GLOBAL_SCOPE_CLOSED)
        if self.workspace_conn is None:
            raise ScopeError(
                "当前目录没有 .ai_memory 工作区，无法写入项目级记忆。请在项目目录下运行。"
            )
        return "workspace", self.workspace_conn

    def _all_dbs(self, scope: str | None) -> list[tuple[str, Any]]:
        """现在只有一个库。`scope` 参数留着是因为调用方到处在传，
        但除了显式要 global（会报错）之外，全都落到项目库。"""
        wanted = (scope or "all").lower()
        if wanted == "global":
            raise ScopeError(GLOBAL_SCOPE_CLOSED)
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")
        return [("workspace", self.workspace_conn)]

    # ================================================================ 写入

    def write_memory(
        self,
        content: str,
        *,
        kind: str = "experience",
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        scope: str = "workspace",
        source: str | None = None,
        invalidates: str | None = None,
        valid_at: int | None = None,
        meta: dict[str, Any] | None = None,
        at: int | None = None,
        day: str | None = None,
        digest_id: str | None = None,
        allow_duplicate: bool = False,
    ) -> Experience | Episode:
        """按类型分派的唯一写入口。

        判据只有一条：**它是否需要回答「什么时候发生的」**。
        需要 → episode（必须带 at，由条目自己承载时间）；不需要 → experience。

        `confidence` 只对经验有效：一条「现在是怎么回事」的结论，初次写下时
        有多可信，是调用方唯一该给的主观值。之后的分值全部由工具按证据算。

        经验会先查重（§5.3）：命中就转强化，返回的是既有条目、`deduped=True`。
        `invalidates` 非空时自动跳过查重 —— 「我这条取代旧的」本来就该和被取代者相似。
        """
        k = _norm_kind(kind)

        if k == "experience":
            item: Experience | Episode = self.write_experience(
                content,
                tags=tags,
                importance=importance,
                confidence=confidence,
                scope=scope,
                source=source,
                valid_at=valid_at,
                meta=meta,
                allow_duplicate=allow_duplicate or bool(invalidates),
            )
        elif k == "episode":
            if (scope or "").lower() == "global":
                raise ValueError(GLOBAL_SCOPE_CLOSED)
            item = self.write_episode(
                content,
                tags=tags,
                importance=importance,
                at=at,
                day=day,
                digest_id=digest_id,
                source=source,
                meta=meta,
            )
        else:
            raise ValueError(f"write_memory 不接受 kind={kind!r}：只有 experience / episode")

        if invalidates:
            try:
                self.invalidate(str(invalidates), invalidated_by=item.id)
            except NotFound:
                pass
        return item

    def _default_importance(self, kind: str) -> float:
        """按类型取录入默认重要性；表里没有就退回 0，不再散落字面量。"""
        table = self.cfg.default_importance
        return float(table.get(kind) or 0.0)

    def _dedup_probe(
        self, conn: Any, db_name: str, vec: Sequence[float]
    ) -> tuple[Experience, float] | None:
        """在经验层里找最像的一条：相似度达到 `dup_floor` 才算重复。

        只比**还活着且未归档**的经验 —— 一条已经被推翻的结论不构成重复，
        新写的很可能正是它的替代品。向量表缺失或扩展不可用都返回 None，
        让写入照常进行：查重是优化，不能变成写入的前置条件。
        """
        floor = self.cfg.dedup_floor
        if floor <= 0:
            return None
        try:
            rows = conn.execute(
                f"SELECT rowid, distance FROM {EXPERIENCE.vec}"
                " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (serialize_float32(vec), self.cfg.dedup_topk),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return None

        best: tuple[Experience, float] | None = None
        for row in rows:
            sim = similarity_from_cosine_distance(float(row["distance"]))
            if sim < floor:
                continue
            hit = conn.execute(
                f"SELECT * FROM {EXPERIENCE.table} WHERE rowid = ?", (int(row["rowid"]),)
            ).fetchone()
            if hit is None or hit["invalid_at"] is not None or hit["archived_at"] is not None:
                continue
            if best is None or sim > best[1]:
                best = (Experience.from_row(hit, db_name), sim)
        return best

    def write_experience(
        self,
        content: str,
        *,
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        scope: str = "workspace",
        source: str | None = None,
        valid_at: int | None = None,
        verified_at: int | None = None,
        meta: dict[str, Any] | None = None,
        allow_duplicate: bool = False,
    ) -> Experience:
        """写一条经验，写之前先查重。

        同一件事写成两条属于录入阶段的错，靠压缩阶段合并补救不了：两条本来就
        各自「正确」，没有哪一条有理由被丢掉。所以判重在录入时做 ——
        命中就放弃新建，转而给既有条目加 `dup_bonus` 活跃度（被写第二遍，
        说明它重要），并把既有条目原样返回，`deduped=True` 表示这一条没新建。

        `allow_duplicate=True` 用于「我要写一条新的来取代旧的」：
        那种情况相似是**预期**的，拦下反而会让旧条被失效却没有新条顶上。
        """
        content, _ = _check_writable(content, "experience", label="write_experience")

        db_name, conn = self._conn_for(scope)
        now = now_ms()
        imp = importance if importance is not None else self._default_importance("experience")
        imp = max(0.0, min(1.0, float(imp)))
        vectors = self.embedder.try_embed([content])
        vec = vectors[0] if vectors else None

        if vec and not allow_duplicate:
            dup = self._dedup_probe(conn, db_name, vec)
            if dup is not None:
                kept, sim = dup
                bonus = self.cfg.dedup_bonus
                with transaction(conn):
                    conn.execute(
                        f"UPDATE {EXPERIENCE.table} SET activity = activity + ? WHERE rowid = ?",
                        (bonus, kept.rowid),
                    )
                    _record_event(
                        conn,
                        moment=now,
                        table=EXPERIENCE.table,
                        item_id=kept.id,
                        event="dedup",
                        from_value="write",
                        to_value="reinforced",
                        delta=bonus,
                        reason=f"重复录入已拦下（相似度 {sim:.3f}）：{content[:80]}",
                    )
                kept.activity += bonus
                kept.deduped = True
                kept.dup_similarity = sim
                return kept

        # 初始可信度：调用方给了就用（易变的现状给低值），没给就不猜，用库默认
        item_id = new_id("x")

        with transaction(conn):
            columns = [
                "id", "content", "tags", "importance", "scope", "source",
                "created_at", "updated_at", "valid_at", "verified_at",
                "embedded", "meta", "forget_level", "period_start",
            ]
            values: list[Any] = [
                item_id,
                content,
                json.dumps(_as_tags(tags), ensure_ascii=False),
                imp,
                db_name,
                source,
                now,
                now,
                valid_at if valid_at is not None else now,
                verified_at,
                1 if vec else 0,
                json.dumps(meta or {}, ensure_ascii=False),
                # 初始等级由 importance 定档（顶档只能靠活跃度挣），周期从此刻起算
                initial_level(imp, self.cfg.initial_bands),
                now,
            ]
            if confidence is not None:
                # 只有调用方明确给了初值才写；没给就让 schema 默认生效
                columns.append("confidence")
                values.append(max(0.0, min(1.0, float(confidence))))
            marks = ",".join("?" * len(columns))
            cur = conn.execute(
                f"INSERT INTO {EXPERIENCE.table}({', '.join(columns)}) VALUES ({marks})",
                tuple(values),
            )
            if vec:
                self._vec_put(conn, EXPERIENCE, int(cur.lastrowid), vec)

        return self.get_experience(item_id)

    def write_episode(
        self,
        content: str,
        *,
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        at: int | None = None,
        day: str | None = None,
        digest_id: str | None = None,
        source: str | None = None,
        meta: dict[str, Any] | None = None,
        compression_level: int | None = None,
    ) -> Episode:
        """写一条经历：一条一个单一事务，时间戳必填（不填就是此刻）。"""
        text, _ = _check_writable(content, "episode", label="write_episode")

        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区，无法写经历")
        conn = self.workspace_conn
        ts = at if at is not None else now_ms()
        imp = importance if importance is not None else self._default_importance("episode")
        imp = max(0.0, min(1.0, float(imp)))
        if compression_level is not None:
            lvl = int(compression_level)
        else:
            lvl = initial_compression_level(imp)
        ep_id = new_id("e")
        vectors = self.embedder.try_embed([text])
        vec = vectors[0] if vectors else None

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO episodes(id, content, at, day, tags, importance,"
                " created_at, digest_id, compression_level, embedded, meta,"
                " forget_level, period_start)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ep_id,
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
                    # 经历同样按 importance 定初始等级；周期从事件发生时刻起算
                    initial_level(imp, self.cfg.initial_bands),
                    ts,
                ),
            )
            if vec:
                self._vec_put(conn, EPISODE, int(cur.lastrowid), vec)

        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        return Episode.from_row(row, "workspace")

    # ================================================================ 读取

    def get_experience(self, item_id: str) -> Experience:
        for db_name, conn in self._all_dbs("all"):
            row = conn.execute(
                f"SELECT * FROM {EXPERIENCE.table} WHERE id = ?", (item_id,)
            ).fetchone()
            if row is not None:
                return Experience.from_row(row, db_name)
        if self.workspace_conn is not None:
            hit = self.workspace_conn.execute(
                "SELECT 1 FROM episodes WHERE id = ?", (item_id,)
            ).fetchone()
            if hit is not None:
                raise ValueError(f"{item_id} 是经历，不是经验：这个操作只对经验有意义")
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

    def get(self, memory_id: str) -> Experience | Episode:
        """按 id 取任意条目。

        不靠 id 前缀分派：迁移过来的存量 id 还带着 k_ / s_ / c_ 的老前缀，
        用前缀猜表会让这些条目「查得到却进错表」。两张表都查一遍最稳。
        """
        try:
            return self.get_experience(memory_id)
        except ValueError:
            # get_experience 抛这个，说明它已经确认过 id 落在 episodes 里
            return self.get_episode(memory_id)
        except NotFound:
            try:
                return self.get_episode(memory_id)
            except NotFound:
                raise NotFound(f"找不到记忆 {memory_id}") from None

    # ================================================================ 可信度

    def verify(self, item_id: str, *, ok: bool = True, level: str = "L1",
               reason: str | None = None, now: int | None = None) -> dict[str, Any]:
        """一条证据落到一条记忆上（§2.2 / TODO 6 的逐条入口）。

        只有经验参与可信度演进：经历不谈真假，它的「可信度」是 forget_level 的派生值。
        所以对经历调用会明确报错，而不是静默返回一个假分数。

        证伪不删除条目：经验的价值在于「它曾经是个结论」，被推翻本身是知识。
        它只需降到低分，被后续检索标上 needs_verify，让读的人先核一眼。
        """
        item = self.get(item_id)
        if isinstance(item, Episode):
            raise ValueError(
                f"{item_id} 是经历：经历的可信度是 forget_level 的派生值（§2.2），不单独演进"
            )
        moment = now if now is not None else now_ms()
        result = self.adjust_confidence(
            item_id, ok=ok, level=level, reason=reason, now=moment
        )
        result["action"] = "confirmed" if ok else "contradicted"
        return result

    def adjust_confidence(
        self,
        item_id: str,
        *,
        ok: bool,
        level: str = "L1",
        reason: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """按一条证据调整经验的可信度（§2.2）。

        间隔不进参数，由库里的时间戳算 —— **间隔本身就是证据**，
        让人传等于让人给自己发证据。`ok=False` 时不更新 `verified_at`：
        被推翻过的条目没有「刚验过」这回事。
        """
        moment = now if now is not None else now_ms()
        curve = self.confidence_curve
        for db_name, conn in self._all_dbs("all"):
            row = conn.execute(
                f"SELECT * FROM {EXPERIENCE.table} WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                continue
            obj = Experience.from_row(row, db_name)
            interval_days = max(0.0, (moment - int(obj.verified_at or obj.created_at)) / DAY_MS)
            new_confidence, delta = curve.apply(
                obj.confidence, ok=ok, interval_days=interval_days, level=level
            )
            event = "verify" if ok else "contradict"
            note = reason or f"{str(level).upper()} 证据，间隔 {interval_days:.1f} 天"
            with transaction(conn):
                sets = ["confidence = ?"]
                params: list[Any] = [new_confidence]
                if ok:
                    sets.append("verified_at = ?")
                    params.append(moment)
                params.append(obj.rowid)
                conn.execute(
                    f"UPDATE {EXPERIENCE.table} SET {', '.join(sets)} WHERE rowid = ?", params
                )
                _record_event(
                    conn,
                    moment=moment,
                    table=EXPERIENCE.table,
                    item_id=item_id,
                    event=event,
                    from_value=f"{obj.confidence:.3f}",
                    to_value=f"{new_confidence:.3f}",
                    delta=delta,
                    reason=note,
                )
            obj.confidence = new_confidence
            if ok:
                obj.verified_at = moment
            return {
                "ok": True,
                "db": db_name,
                "id": item_id,
                "event": event,
                "level": str(level).upper(),
                "interval_days": round(interval_days, 2),
                "from": round(float(row["confidence"]), 3),
                "to": round(new_confidence, 3),
                "delta": round(delta, 3),
                "counted": delta != 0,
                "needs_verify": curve.needs_verify(new_confidence),
                "is_fact": curve.is_fact(new_confidence),
            }

        for db_name, conn in self._all_dbs("all"):
            hit = conn.execute("SELECT id FROM episodes WHERE id = ?", (item_id,)).fetchone()
            if hit is not None:
                raise ValueError(
                    f"{item_id} 是经历：经历的可信度是 forget_level 的派生值（§2.2），不单独演进"
                )
        raise NotFound(f"找不到条目 {item_id}")

    # ================================================================ 遗忘等级裁定

    def judge_due(
        self,
        *,
        scope: str = "workspace",
        now: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """跑一遍到期的等级裁定（§5.1）：纯机器动作，不调模型、不动正文。

        只改三列 —— `forget_level` / `activity` / `period_start`。
        「维持」也要回写：不清零活跃度、不重置周期的话，这条会一直处于「已到期」，
        每跑一次都被重新扫出来，判定就变成了每轮重算同一个结论。

        `scope="all"` 时两个库各跑一遍。**跨库不是同一个事务**（两个文件，SQLite 给不了），
        但每个库内部是：先只读扫出到期清单，为空就完全不开事务 —— 空转不该拿写锁。
        """
        moment = now if now is not None else now_ms()
        per_db: list[dict[str, Any]] = []
        all_items: list[dict[str, Any]] = []
        total_due = 0
        total_settled = 0

        for db_name, conn in self._all_dbs(scope):
            pending = self._due_candidates(conn, db_name, moment)
            if not pending:
                per_db.append({"db": db_name, "due": 0, "settled": 0, "items": []})
                continue

            items = [entry[0] for entry in pending]
            all_items.extend(items)
            total_due += len(items)

            settled = 0
            if not dry_run:
                with transaction(conn):
                    for summary, decision, rowid, table in pending:
                        self._settle_period(
                            conn,
                            table=table,
                            rowid=rowid,
                            item_id=str(summary["id"]),
                            level=int(summary["from"]),
                            decision=decision,
                            moment=moment,
                        )
                        settled += 1
            total_settled += settled
            per_db.append(
                {"db": db_name, "due": len(items), "settled": settled, "items": items}
            )

        return {
            "ok": True,
            "db": per_db[0]["db"] if len(per_db) == 1 else "all",
            "now": moment,
            "dry_run": dry_run,
            "due": total_due,
            "settled": total_settled,
            "items": all_items,
            "dbs": per_db,
        }

    def _due_candidates(
        self, conn: Any, db_name: str, moment: int
    ) -> list[tuple[dict[str, Any], Any, int, str]]:
        """只读地扫出「该裁定了」的条目：[(摘要, 判定, rowid, 表名)]。

        与落盘分开是为了「没有到期项就一个字节都不写」—— 空事务也会拿写锁，
        定时任务里每天白拿一次锁没有意义。
        """
        table = self.level_table
        out: list[tuple[dict[str, Any], Any, int, str]] = []
        for spec in (EXPERIENCE, EPISODE):
            dimension = period_kind(spec.kind)
            rows = conn.execute(
                f"SELECT rowid, id, forget_level, activity, period_start,"
                f" {spec.time_attr} AS anchor FROM {spec.table}"
                " WHERE forget_level > 0 AND archived_at IS NULL"
            ).fetchall()
            for row in rows:
                level = int(row["forget_level"])
                anchor = row["period_start"]
                if anchor is None:
                    # 老数据没写过这列：拿创建/发生时刻兜底，不因此跳过判定
                    anchor = int(row["anchor"])
                if not table.is_due(level, anchor, moment, dimension):
                    continue
                activity = float(row["activity"])
                decision = table.decide(level, activity, dimension)
                out.append(
                    (
                        {
                            "db": db_name,
                            "table": spec.table,
                            "id": row["id"],
                            "kind": spec.kind,
                            "from": level,
                            "to": decision.level,
                            "action": decision.action,
                            "activity": round(activity, 2),
                            "reason": decision.reason,
                        },
                        decision,
                        int(row["rowid"]),
                        spec.table,
                    )
                )
        return out

    @staticmethod
    def _settle_period(
        conn: Any,
        *,
        table: str,
        rowid: int,
        item_id: str,
        level: int,
        decision: Any,
        moment: int,
    ) -> None:
        """落一次裁定：改等级、清零活跃度、周期起点推到 now，并留一行审计。"""
        sets = ["forget_level = ?", "activity = 0", "period_start = ?"]
        params: list[Any] = [decision.level, moment]
        if decision.level == 0:
            # 落到遗忘层要记「什么时候下来的」，删除判定靠它算停留时长
            sets.append("level0_since = ?")
            params.append(moment)
        elif level == 0:
            sets.append("level0_since = NULL")
        params.append(rowid)
        conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE rowid = ?", params)
        if decision.level != level:
            _record_event(
                conn,
                moment=moment,
                table=table,
                item_id=item_id,
                event="level",
                from_value=str(level),
                to_value=str(decision.level),
                delta=float(decision.level - level),
                reason=decision.reason,
            )

    def invalidate(
        self,
        item_id: str,
        *,
        invalidated_by: str | None = None,
    ) -> Experience:
        """让一条经验失效（保留历史，追加式）。

        经验被更好的结论取代时走这里：旧条置 invalid_at 且正文不动，
        检索默认跳过它，但「曾经这么认为」这件事还查得到。

        只对经验有意义 —— 经历是「发生过的事」，没有真假可推翻，
        记错了就记错了，不能被一条新结论顶掉。
        """
        item = self.get_experience(item_id)
        if not item.live:
            return item
        _, conn = self._conn_for(item.db)
        now = now_ms()
        with transaction(conn):
            conn.execute(
                f"UPDATE {EXPERIENCE.table} SET invalid_at = ?, updated_at = ?, invalidated_by = ?"
                " WHERE id = ?",
                (now, now, invalidated_by, item_id),
            )
        return self.get_experience(item_id)

    def promote(self, item_id: str) -> tuple[Experience, Experience]:
        """已下线（§8.2）：不再有全局库可以提升过去。

        跨项目该共享的硬约束属于信条，写进 `CORE_RULES.md` 静态注入；
        剩下那些「跨项目但不硬」的内容，绝大多数其实只属于某一个项目。
        """
        raise ValueError(
            "memory_promote 已下线（§8.2）：全局库已关闭，没有可提升的去处。"
            "硬约束请写进 CORE_RULES.md（`rules_append`），其余留在对应项目库。"
        )

    def list_experience(
        self,
        *,
        scope: str | None = "all",
        include_invalid: bool = False,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[Experience]:
        out: list[Experience] = []
        for db_name, conn in self._all_dbs(scope):
            clauses: list[str] = []
            params: list[Any] = []
            if not include_invalid:
                clauses.append("invalid_at IS NULL")
            if not include_archived:
                clauses.append("archived_at IS NULL")
            sql = f"SELECT * FROM {EXPERIENCE.table}"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            for row in conn.execute(sql, tuple(params)).fetchall():
                out.append(Experience.from_row(row, db_name))
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
        imp = importance if importance is not None else self._default_importance("log")
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
        """混合检索。

        `kinds` 接受 None / "all" / "experience" / ["experience", "episode"]，
        也认中文「经验 / 经历 / 日志」。类型就是表，所以这里只做表级选择。

        **0 级（遗忘层）不进常规检索**（§3.1）——它与归档同义：正文还在，
        但只有当常规检索捞不到东西时，才由 `deep_recall` 去翻。
        """
        query = (query or "").strip()
        if not query:
            return []

        hits, _ = self._collect(query, kinds=kinds, scope=scope,
                                include_invalid=include_invalid,
                                include_archived=include_archived, limit=limit)
        return hits

    def _collect(
        self,
        query: str,
        *,
        kinds: Sequence[str] | str | None,
        scope: str | None,
        include_invalid: bool,
        include_archived: bool,
        limit: int,
        level_filter: str = "live",
        min_score: float = 0.0,
    ) -> tuple[list[Hit], list[TableSpec]]:
        """检索本体：`level_filter` = live（常规）| forgotten（遗忘层）| all。"""
        targets = self._resolve_targets(kinds)
        if not targets:
            return [], []

        topk = min(self.cfg.max_candidates, max(limit * self.cfg.candidate_multiplier, 40))
        qvec: list[float] | None = None
        if self.embedder.available:
            vectors = self.embedder.try_embed([query])
            qvec = vectors[0] if vectors else None

        hits: list[Hit] = []
        for db_name, conn in self._all_dbs(scope):
            for spec in targets:
                hits.extend(
                    self._search_spec(
                        conn, db_name, spec, query, qvec, topk,
                        include_invalid, include_archived, level_filter,
                    )
                )

        hits = [h for h in hits if h.score >= min_score]
        hits.sort(key=lambda h: h.score, reverse=True)
        top = hits[:limit]
        # 可信度低于 verify_on_read 的经验，读到时带「先验证再用」标记（§2.2）
        curve = self.confidence_curve
        for hit in top:
            if isinstance(hit.item, Experience):
                hit.needs_verify = curve.needs_verify(hit.item.confidence)
        return top, targets

    def _resolve_targets(self, kinds: Sequence[str] | str | None) -> list[TableSpec]:
        """把 kinds 解析成要检索的表。"""
        if kinds is None:
            return list(ALL_SPECS)
        raw = [kinds] if isinstance(kinds, str) else list(kinds)
        if not raw or any(not str(k).strip() for k in raw):
            return list(ALL_SPECS)
        if any(str(k).strip().lower() == "all" for k in raw):
            return list(ALL_SPECS)
        norm = {_norm_kind(k, writable=False) for k in raw}

        out: list[TableSpec] = []
        for kind in norm:
            spec = SPEC_BY_KIND.get(kind)
            if spec is None:
                raise ValueError(
                    f"未知类型 {kind!r}：只有 {[s.kind for s in ALL_SPECS]}"
                )
            if spec not in out:
                out.append(spec)
        return out

    def recall(
        self,
        context: str,
        *,
        limit: int = 6,
        kinds: Sequence[str] | str | None = None,
        deep: bool = True,
    ) -> list[Hit]:
        """检索并强化命中项 —— 用进废退的「进」。

        `deep=True` 时按 §3.2 兜一层：常规结果里最高的**相似度**低于
        `deep_recall_floor` 就算「检索失败」，转去遗忘层再捞一次。
        判定用相似度而不是最终分数 —— 分数里有 importance 与强度加成，
        一条重要的老记忆分数可能很高，但其实什么也没匹配上。
        """
        hits = self.search(context, kinds=kinds, limit=limit)
        self._reinforce(hits)

        if deep and self._is_miss(hits):
            forgotten = self.deep_recall(context, kinds=kinds, limit=max(1, limit // 2))
            hits = hits + forgotten["hits"]
        return hits

    def _is_miss(self, hits: Sequence[Hit]) -> bool:
        """常规检索是不是「什么也没捞到」：最高相似度低于 deep_recall_floor。"""
        floor = self.cfg.deep_recall_floor
        return max((self._similarity_of(h) for h in hits), default=0.0) < floor

    @staticmethod
    def _similarity_of(hit: Hit) -> float:
        """取这一条的相关性：有向量用余弦，没有就退到词法相似度。

        用相似度而不是最终分数 —— 分数里有 importance 与强度加成，
        一条重要的老记忆分数可能很高，但其实什么也没匹配上。
        """
        sim = hit.vector_similarity
        if sim is None:
            sim = hit.lexical_score
        return float(sim or 0.0)

    def deep_recall(
        self,
        query: str,
        *,
        kinds: Sequence[str] | str | None = None,
        scope: str | None = "all",
        limit: int = 5,
    ) -> dict[str, Any]:
        """深度回忆：**只在遗忘层（0 级）里检索**（§3.2）。

        命中的条目会被记一次 `revive_hits`；累计到 `revive_hits` 就复活到
        `revive_to_level`（默认 1），周期起点推到现在、活跃度清零。
        回 1 级而不是 3 级：想起一次不代表记住了，它得在下个周期再证明一次。

        经验没有 `revive_hits` 列（§7.1 只给 episodes 配了它），所以经验被捞出来
        只标记「已遗忘」、不计次也不复活 —— 如实暴露 schema 的不对称，不假装支持。
        """
        query = (query or "").strip()
        if not query:
            return {"hits": [], "revived": [], "count": 0, "floor": 0.0}

        floor = self.cfg.deep_recall_floor
        hits, _ = self._collect(
            query, kinds=kinds, scope=scope,
            include_invalid=False, include_archived=False,
            limit=max(limit * 2, 10), level_filter="forgotten",
        )
        # 遗忘层里也得够相关才算「想起」：否则任何一点词法重叠都会给复合计次
        hits = [h for h in hits if self._similarity_of(h) >= floor][:limit]
        if not hits:
            return {"hits": [], "revived": [], "count": 0, "floor": floor}

        moment = now_ms()
        revived: list[str] = []
        for hit in hits:
            hit.forgotten = True
            if not isinstance(hit.item, Episode):
                continue
            if self._revive_hit(hit, moment):
                revived.append(hit.item.id)

        return {
            "hits": hits,
            "revived": revived,
            "count": len(hits),
            "floor": floor,
            "note": (
                "这些是已落到遗忘层的记忆：正文完整，但不在常规检索里。"
                "经历需要被深度回忆命中多次才复活，经验没有复活计数（只能人工恢复）。"
            ),
        }

    def _revive_hit(self, hit: Hit, moment: int) -> bool:
        """给 0 级经历记一次深度命中；凑满次数就复活。返回是否复活。"""
        if self.workspace_conn is None:
            return False
        item = hit.item
        row = self.workspace_conn.execute(
            "SELECT rowid, revive_hits FROM episodes WHERE id = ?", (item.id,)
        ).fetchone()
        if row is None:
            return False

        needed = self.cfg.revive_hits_needed
        hits = int(row["revive_hits"] or 0) + 1
        rowid = int(row["rowid"])

        if hits < needed:
            with transaction(self.workspace_conn):
                self.workspace_conn.execute(
                    "UPDATE episodes SET revive_hits = ? WHERE rowid = ?", (hits, rowid)
                )
                _record_event(
                    self.workspace_conn,
                    moment=moment,
                    table=EPISODE.table,
                    item_id=item.id,
                    event="deep_hit",
                    from_value=str(hits - 1),
                    to_value=str(hits),
                    delta=1.0,
                    reason=f"深度回忆命中 {hits}/{needed}，仍在 0 级",
                )
            item.revive_hits = hits
            return False

        target = self.cfg.revive_to_level
        with transaction(self.workspace_conn):
            self.workspace_conn.execute(
                "UPDATE episodes SET forget_level = ?, revive_hits = 0,"
                " activity = 0, period_start = ?, level0_since = NULL"
                " WHERE rowid = ?",
                (target, moment, rowid),
            )
            _record_event(
                self.workspace_conn,
                moment=moment,
                table=EPISODE.table,
                item_id=item.id,
                event="revive",
                from_value="0",
                to_value=str(target),
                delta=float(target),
                reason=f"深度回忆累计命中 {hits} 次，从遗忘层复活到 {target} 级",
            )
        item.forget_level = target
        item.revive_hits = 0
        item.activity = 0.0
        item.level0_since = None
        hit.revived = True
        return True

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
        level_filter: str = "live",
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
            if spec is EXPERIENCE and not include_invalid and not obj.live:
                continue
            if not include_archived and getattr(obj, "archived", False):
                continue
            # 遗忘层与常规视野互斥：0 级只在 deep_recall 里可见（§3.1 / §3.2）
            # 日志不参与等级机制，永远算常规视野，也不会出现在遗忘层
            level = int(getattr(obj, "forget_level", 0) or 0) if spec in (EXPERIENCE, EPISODE) else None
            if level_filter == "live" and level is not None and level <= 0:
                continue
            if level_filter == "forgotten" and (level is None or level > 0):
                continue

            vec_sim = (
                similarity_from_cosine_distance(vec_hits[rowid])
                if rowid in vec_hits
                else None
            )
            lex_sim = self._normalize_rank(lex_hits, rowid) if rowid in lex_hits else None
            retrieval = self._fuse(vec_sim, lex_sim)
            base = combine_score(
                retrieval=retrieval,
                importance=obj.importance,
                level=self._level_of(spec, obj),
                w_retrieval=self.cfg.w_retrieval,
                w_importance=self.cfg.w_importance,
                w_level=self.cfg.w_level,
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

    def _level_of(self, spec: TableSpec, obj: Any) -> float:
        """这条的等级倍率（0~1）。

        日志不参与等级机制（没有 `forget_level` 列），恒按 1.0 —— 它靠
        `type_weight` 与复盘清空来体现「原始素材」的定位，不该再被等级罚一次。
        """
        if spec not in (EXPERIENCE, EPISODE):
            return 1.0
        return level_factor(
            int(getattr(obj, "forget_level", 0) or 0),
            self.cfg.max_level,
            self.cfg.level_factor_power,
            self.cfg.level_factor_overrides,
        )

    @staticmethod
    def _type_weight(spec: TableSpec, obj: Any) -> float:
        """层权重 × 形态权重：经验最高，压过的经历次之，刚复盘的完整叙述最低。"""
        if spec is EPISODE:
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
            if isinstance(hit.item, Experience):
                table = EXPERIENCE.table
            elif isinstance(hit.item, Episode):
                table = "episodes"
            else:
                table = "logs"
            grouped.setdefault((db_name, table), []).append(hit)

        floor = self.cfg.activity_sim_floor
        power = self.cfg.activity_curve_power
        for (db_name, table), group in grouped.items():
            try:
                _, conn = self._conn_for(db_name)
            except ScopeError:
                continue
            # 日志没有 activity 列：它不进等级体系，复盘完就被清掉
            traces = table in (EXPERIENCE.table, EPISODE.table)
            with transaction(conn):
                for hit in group:
                    if traces:
                        # 活跃度按相关性平方累加：擦边十次不如命中一次
                        delta = activity_contribution(
                            self._fuse(hit.vector_similarity, hit.lexical_score),
                            sim_floor=floor,
                            curve_power=power,
                        )
                        conn.execute(
                            f"UPDATE {table} SET access_count = access_count + 1,"
                            " last_accessed_at = ?, activity = activity + ? WHERE rowid = ?",
                            (now, delta, hit.item.rowid),
                        )
                        hit.item.activity = float(getattr(hit.item, "activity", 0.0) or 0.0) + delta
                    else:
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
        return {
            "day": target,
            "log_count": len(logs),
            "logs": [log.to_dict() for log in logs],
            "instructions": (
                "阅读 logs 后产出两部分，判据只有一条：**它是否需要回答「什么时候发生的」**。\n"
                "1) experience：可复用的判定与结论（机制、约定、选型、根因），"
                "**换掉单号和任务 ID 仍然成立**，最多 500 字一条。"
                "易变的结论给低 confidence（0.3），稳定的机制给高一点（0.6~0.8）。\n"
                "2) episodes：发生过的事，一条一个单一事务（「今天吃饭」「今天洗澡」是两条），"
                "可长，带上时间。**不要把可复用的结论写进 episode**。\n"
                "然后调用 consolidate_apply 提交，purge=true 会清空已复盘的原始日志。\n"
                "不需要读存量经验做逐条比对：查重在写入时由工具完成，"
                "只有你确信某条旧结论被这次发现推翻时才带 invalidates。"
            ),
        }

    def consolidate_apply(
        self,
        *,
        day: str | None = None,
        experience: Sequence[dict[str, Any]] | None = None,
        episodes: Sequence[Any] | None = None,
        summary: str | None = None,
        purge: bool = True,
        model: str | None = None,
    ) -> dict[str, Any]:
        """提交复盘结果。

        `experience` 每项是「可复用的结论」，可带 source / confidence / invalidates；
        `episodes` 每项是「发生过的事」，默认按 episode 落库。

        两道闸门，防止「结论没写全、原始日志却已被清空」：
        1) 先整批前置校验。任何一项不合法抛 ConsolidationRejected，此时零写入、
           原始日志保持 pending，改完参数可原样重跑。
        2) 所有写入合进同一个事务，中途异常整体回滚；只有条目全部写成功，
           才标记日志、写 digest、按 purge 删除原始日志。
        """
        prepared_experience, prepared_episodes = _prepare_apply_items(experience, episodes)

        # day 不传时与 consolidate_preview 对齐：取最近一个仍有积压的日期。
        # 旧版默认 today，补历史时会把经历和 digest 记到今天、并误删今天的日志。
        target = parse_day(day if day is not None else self._latest_pending_day())
        db_name, conn = self._conn_for("workspace")
        pending = self.list_logs(day=target, include_consolidated=False, limit=1000)
        if not pending:
            return {
                "day": target,
                "log_count": 0,
                "created_experience": [],
                "created_episodes": [],
                "note": "该日没有待复盘的日志",
            }

        digest_id = new_id("d")
        now = now_ms()
        created_experience: list[Experience] = []
        created_episodes: list[Episode] = []
        skipped_experience: list[dict[str, Any]] = []
        deduped_experience: list[str] = []

        existing = {
            f.content.strip()
            for f in self.list_experience(scope="all", include_invalid=False, limit=1000)
        }

        # 整个提交一个事务：write_* 内部的事务会退化成 SAVEPOINT，
        # 任何一步抛异常都整体回滚，日志与 digest 都不会被改动。
        with transaction(conn):
            for index, raw in enumerate(prepared_experience):
                content = raw["content"]
                if content in existing:
                    skipped_experience.append(
                        {"index": index, "content": content, "reason": "duplicate"}
                    )
                    continue
                try:
                    item = self.write_experience(
                        content,
                        tags=raw.get("tags"),
                        importance=raw.get("importance"),
                        confidence=raw.get("confidence"),
                        scope="workspace",
                        source=raw.get("source") or f"digest:{digest_id}",
                        # 复盘里写明「这条取代旧结论」时不能查重：
                        # 拦下来会让旧条失效、新条没写，等于空掉一条
                        allow_duplicate=bool(raw.get("invalidates")),
                    )
                except (ValueError, ScopeError) as exc:
                    raise ConsolidationRejected(
                        f"experience[{index}] 写入失败：{exc}"
                    ) from exc
                existing.add(content)
                created_experience.append(item)
                if getattr(item, "deduped", False):
                    # 命中了存量，没新建：digest 的产出数不能把它算进去
                    deduped_experience.append(item.id)
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
                    len(created_experience) - len(deduped_experience),
                    len(created_episodes),
                    summary,
                    1 if purge else 0,
                    model,
                    json.dumps({"log_ids": [x.id for x in pending]}, ensure_ascii=False),
                ),
            )
            purged = self._purge_logs(digest_id) if purge else 0

        warnings: list[str] = []
        if deduped_experience:
            warnings.append(
                f"{len(deduped_experience)} 条经验与存量高度重复，未新建，已给原条目加活跃度："
                + ", ".join(deduped_experience)
            )
        if pending and not prepared_experience and not prepared_episodes:
            warnings.append(
                f"当天有 {len(pending)} 条日志却零产出："
                "确认不是漏传 experience / episodes 数组"
            )

        return {
            "day": target,
            "digest_id": digest_id,
            "log_count": len(pending),
            "requested_experience": len(prepared_experience),
            "requested_episodes": len(prepared_episodes),
            "created_experience": [f.to_dict() for f in created_experience],
            "created_episodes": [e.to_dict() for e in created_episodes],
            "skipped_experience": skipped_experience,
            "warnings": warnings,
            "purged": purged,
        }

    def _insert_episode(
        self,
        text: str,
        at: int,
        day: str,
        digest_id: str,
        compression_level: int | None = None,
        importance: float | None = None,
    ) -> Episode:
        """低阶插入：给压缩 / 合并这类需要指定 digest 的内部流程用。"""
        return self.write_episode(
            text,
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
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[Episode]:
        if self.workspace_conn is None:
            return []
        sql = "SELECT * FROM episodes"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY at DESC LIMIT ?"
        rows = self.workspace_conn.execute(sql, (limit,)).fetchall()
        return [Episode.from_row(r, "workspace") for r in rows]

    def list_digests(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if self.workspace_conn is None:
            return []
        rows = self.workspace_conn.execute(
            "SELECT * FROM digests ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 压缩队列
    # 边界（docs §3.3.1）：降级判定是纯 SQL，压缩改写是生成任务，两者不进同一个事务。
    # 所以这边只做两件事 —— 按等级推出「压得不够深」的队列，以及接收 AI 压完的结果
    # 定向覆盖。没有「待压缩」标记字段：压完 compression_level 落到目标值，自然出队。

    def compression_queue(self, *, limit: int = 50) -> dict[str, Any]:
        """按 `forget_level` 推出待压缩队列（只读）。

        判据就是一条：`compression_level < required[forget_level]`。
        队列天然自愈 —— 不需要「已处理」标记，不需要补偿逻辑，空时整步不进 prompt。

        只列经历：经验永不压缩（压缩会丢精确性，而经验的价值就在精确）。
        """
        if self.workspace_conn is None:
            return {"items": [], "total": 0, "by_target": {}}

        required = self.cfg.required_compression
        rows = self.workspace_conn.execute(
            "SELECT id, at, day, content, compression_level, forget_level, importance"
            " FROM episodes WHERE archived_at IS NULL"
            " ORDER BY forget_level ASC, at ASC"
        ).fetchall()

        items: list[dict[str, Any]] = []
        by_target: dict[int, int] = {}
        for row in rows:
            level = int(row["forget_level"] or 0)
            target = compression_target(required, level)
            current = int(row["compression_level"] or 0)
            if current >= target:
                continue
            by_target[target] = by_target.get(target, 0) + 1
            items.append(
                {
                    "id": row["id"],
                    "at": int(row["at"]),
                    "day": row["day"],
                    "forget_level": level,
                    "current_level": current,
                    "target_level": target,
                    "length": len(str(row["content"])),
                    "content": str(row["content"]),
                }
            )
            if len(items) >= limit:
                break

        return {
            "items": items,
            "total": len(items),
            "by_target": {str(k): v for k, v in sorted(by_target.items())},
            "note": (
                "定向覆盖：压完用 compression_apply 提交 {id, content}"
                "（可选 level，缺省就写目标值）。不要新建一条压缩版再归档旧条 ——"
                "那会让同一件事在库里留下多份详略，且新条目的 at 会漂成压缩时刻。"
            ),
        }

    def compression_apply(self, items: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """把 AI 压好的内容**定向覆盖**回原条目。

        `items`: `[{"id": ..., "content": ..., "level": 可选}, ...]`

        三条硬校验，都是为了让「有损操作」留下可查的痕迹：
        1. 必须是存在的、未归档的经历 —— 压缩不碰经验
        2. 新内容不能比原来长 —— 变长的不是压缩，是改写，该走别的路
        3. `level` 只能落在 `(当前, 目标]` 区间 —— 写超目标等于假装压得更深，
           写回当前值等于没压，两种情况都会让队列永远出不来
        """
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")

        required = self.cfg.required_compression
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for raw in items:
            mid = str(raw.get("id", "")).strip()
            text = str(raw.get("content", "")).strip()
            if not mid or not text:
                skipped.append({"id": mid, "reason": "缺少 id 或 content"})
                continue

            row = self.workspace_conn.execute(
                "SELECT rowid, id, content, compression_level, forget_level, archived_at"
                " FROM episodes WHERE id = ?",
                (mid,),
            ).fetchone()
            if row is None:
                skipped.append({"id": mid, "reason": "不存在，或不是经历"})
                continue
            if row["archived_at"] is not None:
                skipped.append({"id": mid, "reason": "已归档"})
                continue

            before = str(row["content"])
            current = int(row["compression_level"] or 0)
            target = compression_target(required, int(row["forget_level"] or 0))
            level = raw.get("level")
            level = target if level is None else int(level)

            if len(text) > len(before):
                skipped.append(
                    {"id": mid, "reason": f"比原文长（{len(before)}→{len(text)}），不是压缩"}
                )
                continue
            if level > target:
                skipped.append(
                    {"id": mid, "reason": f"level={level} 超过目标 {target}，会把队列钉死"}
                )
                continue
            if level <= current:
                skipped.append(
                    {"id": mid, "reason": f"level={level} 没有比当前 {current} 更深，等于没压"}
                )
                continue

            vec = None
            if self.embedder.available:
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
                _record_event(
                    self.workspace_conn,
                    moment=now_ms(),
                    table=EPISODE.table,
                    item_id=mid,
                    event="compress",
                    from_value=str(current),
                    to_value=str(level),
                    delta=float(len(text) - len(before)),
                    reason=(
                        f"forget_level={row['forget_level']} 目标 {target}："
                        f"{len(before)} 字 → {len(text)} 字"
                    ),
                    meta={
                        "before_len": len(before),
                        "after_len": len(text),
                        "compression_level": level,
                        "target_level": target,
                    },
                )

            updated.append(
                {
                    "id": mid,
                    "from_level": current,
                    "to_level": level,
                    "before_len": len(before),
                    "after_len": len(text),
                }
            )

        return {
            "updated": updated,
            "skipped": skipped,
            "count": len(updated),
        }

    # ================================================================ 生命维持
    # 原则：归档可逆、删除不可逆。因此这里只做「降级」和「报告候选」，
    # 真正的物理删除必须由调用方显式指定 id 才会发生。

    def archive_pass(self, *, dry_run: bool = False) -> dict[str, Any]:
        """把「该退场」的经验与经历转为归档。

        归档 = 置 archived_at，正文一个字不动。默认检索不到，
        `include_archived=True` 仍可查回。信条不在此列（它根本不在库里）。

        TTL 按表取：经验 3 年、经历 1 年 —— 走「久未触碰 + 访问少」判据。
        """
        now = now_ms()
        shelved: list[dict[str, Any]] = []
        kept = 0
        actions: list[tuple[Any, TableSpec, int]] = []

        for _db_name, conn in self._all_dbs("all"):
            for spec in (EXPERIENCE, EPISODE):
                ttl = self.cfg.archive_ttl.get(spec.kind)
                if ttl is None:
                    continue
                rows = conn.execute(
                    f"SELECT rowid, id, content, created_at, last_accessed_at,"
                    f" access_count FROM {spec.table}"
                    f" WHERE archived_at IS NULL"
                ).fetchall()
                for row in rows:
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
                            "type": spec.kind,
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
            },
            "note": "归档可逆：include_archived=True 仍能查回，正文未改动",
        }

    def unarchive(self, memory_id: str) -> dict[str, Any]:
        """从归档中恢复一条记忆，并重新嵌入。"""
        for _db_name, conn in self._all_dbs("all"):
            for spec in (EXPERIENCE, EPISODE):
                row = conn.execute(
                    f"SELECT rowid, content FROM {spec.table} WHERE id = ?",
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
                return {"id": memory_id, "type": spec.kind, "restored": True}
        raise NotFound(f"找不到记忆 {memory_id}")

    def purge_candidates(self, *, after_days: float | None = None) -> list[dict[str, Any]]:
        """删除候选 —— **仅报告，不删除**。

        两类来源，判据不同（§8.3 有意分成两个键）：
        1. `archived`：归档超过 `purge_after_days`，期间没再被碰过
        2. `level0`：落到遗忘层超过 `level0_purge_days`，且复活次数没攒够
        """
        days = self.cfg.purge_after_days if after_days is None else after_days
        moment = now_ms()
        cutoff = moment - int(days * DAY_MS)
        level0_days = self.cfg.level0_purge_days
        level0_cutoff = moment - int(level0_days * DAY_MS)
        needed = self.cfg.revive_hits_needed

        out: list[dict[str, Any]] = []
        for db_name, conn in self._all_dbs("workspace"):
            for spec in (EXPERIENCE, EPISODE):
                rows = conn.execute(
                    f"SELECT id, content, archived_at, last_accessed_at, access_count"
                    f" FROM {spec.table}"
                    f" WHERE archived_at IS NOT NULL AND archived_at <= ?"
                    f"   AND (last_accessed_at IS NULL OR last_accessed_at < archived_at)",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    out.append(
                        {
                            "reason": "archived",
                            "type": spec.kind,
                            "db": db_name,
                            "id": row["id"],
                            "content": str(row["content"])[:60],
                            "archived_at": row["archived_at"],
                            "access_count": row["access_count"],
                            "idle_days": round((now_ms() - int(row["archived_at"])) / DAY_MS, 1),
                        }
                    )

                # 遗忘层超期：经历要看复活次数有没有攒够，经验没有这个列就只看停留时长
                if spec is EPISODE:
                    stale = conn.execute(
                        f"SELECT id, content, level0_since, revive_hits"
                        f" FROM {spec.table}"
                        f" WHERE archived_at IS NULL AND forget_level = 0"
                        f"   AND level0_since IS NOT NULL AND level0_since <= ?"
                        f"   AND revive_hits < ?",
                        (level0_cutoff, needed),
                    ).fetchall()
                else:
                    stale = conn.execute(
                        f"SELECT id, content, level0_since, 0 AS revive_hits"
                        f" FROM {spec.table}"
                        f" WHERE archived_at IS NULL AND forget_level = 0"
                        f"   AND level0_since IS NOT NULL AND level0_since <= ?",
                        (level0_cutoff,),
                    ).fetchall()
                for row in stale:
                    out.append(
                        {
                            "reason": "level0",
                            "type": spec.kind,
                            "db": db_name,
                            "id": row["id"],
                            "content": str(row["content"])[:60],
                            "level0_since": row["level0_since"],
                            "revive_hits": int(row["revive_hits"] or 0),
                            "idle_days": round((moment - int(row["level0_since"])) / DAY_MS, 1),
                        }
                    )
        out.sort(key=lambda x: x["idle_days"], reverse=True)
        return out

    def purge_items(self, ids: Sequence[str]) -> dict[str, Any]:
        """物理删除指定记忆（含向量与全文索引）。**不可恢复**，仅由显式调用触发。

        只允许两类：已归档的，或落在遗忘层且停留超期的。
        两道的共同点 —— 它们都已经不在常规视野里了，删掉不会让人「咦，怎么没了」。
        """
        level0_cutoff = now_ms() - int(self.cfg.level0_purge_days * DAY_MS)
        removed: list[str] = []
        missing: list[str] = []
        for memory_id in ids:
            hit = False
            for _db_name, conn in self._all_dbs("all"):
                for spec in (EXPERIENCE, EPISODE):
                    row = conn.execute(
                        f"SELECT rowid, archived_at, forget_level, level0_since"
                        f" FROM {spec.table} WHERE id = ?",
                        (memory_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    in_forget_layer = (
                        int(row["forget_level"] or 0) == 0
                        and row["level0_since"] is not None
                        and int(row["level0_since"]) <= level0_cutoff
                    )
                    if row["archived_at"] is None and not in_forget_layer:
                        raise ValueError(
                            f"{memory_id} 既没归档、也没在遗忘层待够 "
                            f"{self.cfg.level0_purge_days:.0f} 天，拒绝直接删除。"
                            "先 archive，或用 memory_archive 让它退场。"
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
                " COALESCE(SUM(archived_at IS NOT NULL), 0) AS archived"
                f" FROM {EXPERIENCE.table}"
            ).fetchone()
            info["experience"] = int(row["total"] or 0)
            info["experience_live"] = int(row["live"] or 0)
            info["experience_archived"] = int(row["archived"] or 0)

            if db_name == "workspace":
                ep_row = conn.execute(
                    "SELECT COUNT(*) AS n,"
                    " COALESCE(SUM(archived_at IS NOT NULL), 0) AS a"
                    " FROM episodes"
                ).fetchone()
                info["episodes"] = int(ep_row["n"] or 0)
                info["episodes_archived"] = int(ep_row["a"] or 0)
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
                pending_embed += int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM {spec.table} WHERE embedded = 0"
                    ).fetchone()["n"]
                    or 0
                )
            info["pending_embedding"] = pending_embed
            info["size_bytes"] = path.stat().st_size if path.exists() else 0
            result["dbs"][db_name] = info

        return result

    def backfill_embeddings(self, *, limit: int = 500) -> dict[str, Any]:
        if not self.embedder.available:
            return {"embedded": 0, "skipped": "embedding 未配置"}
        done = 0
        for db_name, conn in self._all_dbs("all"):
            for spec in ALL_SPECS:
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
        """把项目库导出成「经验 / 经历 / 日志」Markdown 视图。

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

        # --- 经验：一节，一条一行
        items = self.list_experience(
            scope="workspace", include_invalid=include_invalid, limit=5000
        )
        lines = [
            "# 经验",
            "",
            "> 由 `humanity-memory export` 手动导出，**请勿手改**——下次导出会覆盖。",
            "> 增删改用 `memory_write` / `memory_invalidate`。",
            "",
            f"## 共 {len(items)} 条",
            "",
        ]
        for item in items:
            mark = "" if item.live else "（已失效）"
            tag = f"  <!-- {' '.join(item.tags)} -->" if item.tags else ""
            verified = item.to_dict().get("verified_iso", "未确认")
            lines.append(f"- {item.content}{mark}{tag}  <!-- 确认于 {verified} -->")

        path = root / "经验.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(str(path))

        # --- 经历：一件事一个文件
        episodes = self.list_episodes(limit=2000)
        for ep in episodes:
            name = f"{ep.day}-{_slug(ep.content)}.md"
            body = [
                f"# {ep.day} · {_slug(ep.content, 40)}",
                "",
                f"> 导出视图 · id `{ep.id}` · 类型 `经历` · 复盘 `{ep.digest_id or 'n/a'}`",
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
            "experience": len(items),
            "episodes": len(episodes),
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
