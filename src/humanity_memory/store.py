"""存储门面：事实 / 经历 / 日志三层 + 跨库检索 + 复盘固化。

检索策略
--------
vec0 的 KNN 不支持任意 WHERE 过滤，因此每张表都是「先召回候选 rowid，
再 join 主表在 Python 侧过滤打分」。跨库检索时对 workspace 与 global
分别执行再统一排序 —— 全局事实无论当前在哪个项目都参与召回。
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import DEFAULT_IMPORTANCE, Config
from .db import get_meta, open_db, transaction
from .embeddings import EmbeddingProvider
from .lifecycle import combine_score, lifecycle_strength, similarity_from_cosine_distance
from .models import Episode, Fact, Hit, LogEntry, day_of, new_id, now_ms, parse_day
from .paths import Layout

_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

W_VECTOR_PASS = 0.70
W_LEXICAL_PASS = 0.30
FACT_MAX_CHARS = 500


@dataclass(frozen=True)
class TableSpec:
    kind: str
    table: str
    vec: str
    fts: str
    time_attr: str
    type_weight: float
    model: type


FACT = TableSpec("fact", "facts", "facts_vec", "facts_fts", "created_at", 1.00, Fact)
EPISODE = TableSpec("episode", "episodes", "episodes_vec", "episodes_fts", "at", 0.96, Episode)
LOG = TableSpec("log", "logs", "logs_vec", "logs_fts", "at", 0.90, LogEntry)

ALL_SPECS: tuple[TableSpec, ...] = (FACT, EPISODE, LOG)
SPEC_BY_KIND = {s.kind: s for s in ALL_SPECS}


class NotFound(LookupError):
    pass


class ScopeError(RuntimeError):
    """需要工作区但当前目录没有。"""


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

    # ================================================================ 事实

    def write_fact(
        self,
        content: str,
        *,
        tags: Iterable[str] | None = None,
        importance: float | None = None,
        scope: str = "workspace",
        source: str | None = None,
        valid_at: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Fact:
        content = (content or "").strip()
        if not content:
            raise ValueError("事实内容不能为空")
        if len(content) > FACT_MAX_CHARS:
            raise ValueError(
                f"事实要简短精确（当前 {len(content)} 字，上限 {FACT_MAX_CHARS}）。"
                "长内容请写日志（log），复盘时再压缩成事实。"
            )

        db_name, conn = self._conn_for(scope)
        now = now_ms()
        imp = importance if importance is not None else DEFAULT_IMPORTANCE["fact"]
        imp = max(0.0, min(1.0, float(imp)))
        fact_id = new_id("f")
        vectors = self.embedder.try_embed([content])
        vec = vectors[0] if vectors else None

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO facts(id, content, tags, importance, scope, source,"
                " created_at, updated_at, valid_at, embedded, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    content,
                    json.dumps(_as_tags(tags), ensure_ascii=False),
                    imp,
                    db_name,
                    source,
                    now,
                    now,
                    valid_at if valid_at is not None else now,
                    1 if vec else 0,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            if vec:
                self._vec_put(conn, FACT, int(cur.lastrowid), vec)

        return self.get_fact(fact_id)

    def get_fact(self, fact_id: str) -> Fact:
        for db_name, conn in self._all_dbs("all"):
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is not None:
                return Fact.from_row(row, db_name)
        raise NotFound(f"找不到事实 {fact_id}")

    def invalidate_fact(self, fact_id: str, *, invalidated_by: str | None = None) -> Fact:
        fact = self.get_fact(fact_id)
        if not fact.live:
            return fact
        _, conn = self._conn_for(fact.db)
        now = now_ms()
        with transaction(conn):
            conn.execute(
                "UPDATE facts SET invalid_at = ?, updated_at = ?, invalidated_by = ?"
                " WHERE id = ?",
                (now, now, invalidated_by, fact_id),
            )
        return self.get_fact(fact_id)

    def promote_fact(self, fact_id: str) -> tuple[Fact, Fact]:
        """项目事实提升为全局事实：新写一条全局事实，旧条目标记失效并指向新条目。"""
        source = self.get_fact(fact_id)
        if source.scope == "global":
            raise ValueError(f"{fact_id} 已经是全局事实")
        promoted = self.write_fact(
            source.content,
            tags=source.tags,
            importance=source.importance,
            scope="global",
            source=f"promoted:{source.id}",
        )
        self.invalidate_fact(source.id, invalidated_by=promoted.id)
        return source, promoted

    def list_facts(
        self,
        *,
        scope: str | None = "all",
        include_invalid: bool = False,
        limit: int = 50,
    ) -> list[Fact]:
        out: list[Fact] = []
        for db_name, conn in self._all_dbs(scope):
            sql = "SELECT * FROM facts"
            clauses: list[str] = []
            params: list[Any] = []
            if not include_invalid:
                clauses.append("invalid_at IS NULL")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            for row in conn.execute(sql, tuple(params)).fetchall():
                out.append(Fact.from_row(row, db_name))
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
        kinds: Sequence[str] | None = None,
        scope: str | None = "all",
        include_invalid: bool = False,
        limit: int = 8,
        min_score: float = 0.0,
    ) -> list[Hit]:
        query = (query or "").strip()
        if not query:
            return []

        wanted = (
            [SPEC_BY_KIND[k] for k in kinds if k in SPEC_BY_KIND]
            if kinds
            else list(ALL_SPECS)
        )
        if not wanted:
            return []

        topk = min(self.cfg.max_candidates, max(limit * self.cfg.candidate_multiplier, 40))
        qvec: list[float] | None = None
        if self.embedder.available:
            vectors = self.embedder.try_embed([query])
            qvec = vectors[0] if vectors else None

        hits: list[Hit] = []
        for db_name, conn in self._all_dbs(scope):
            for spec in wanted:
                if db_name == "global" and spec.kind != "fact":
                    continue  # 全局库只承载跨项目事实
                hits.extend(
                    self._search_spec(conn, db_name, spec, query, qvec, topk, include_invalid)
                )

        hits = [h for h in hits if h.score >= min_score]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def recall(
        self, context: str, *, limit: int = 6, kinds: Sequence[str] | None = None
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
        hl = self.cfg.halflife.get(spec.kind, 180.0)
        out: list[Hit] = []

        for rowid, obj in rows.items():
            if spec.kind == "fact" and not include_invalid and not obj.live:
                continue

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
                    score=base * spec.type_weight,
                    vector_similarity=vec_sim,
                    lexical_score=lex_sim,
                    distance=vec_hits.get(rowid),
                )
            )
        return out

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
            table = {"Fact": "facts", "Episode": "episodes", "LogEntry": "logs"}[
                type(hit.item).__name__
            ]
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

    def consolidate_preview(
        self, day: str | None = None, *, limit: int = 300
    ) -> dict[str, Any]:
        """取出待复盘的原始日志，交给当前会话的模型去总结。"""
        target = parse_day(day)
        logs = self.list_logs(day=target, include_consolidated=False, limit=limit)
        live_facts = self.list_facts(scope="all", limit=20)
        return {
            "day": target,
            "log_count": len(logs),
            "logs": [log.to_dict() for log in logs],
            "existing_facts": [f.to_dict() for f in live_facts],
            "instructions": (
                "阅读 logs 后产出两部分：\n"
                "1) facts：从日志里提炼的**长期有效**的简短陈述句，一条一个事实；"
                "不要包含当天的临时细节；若与 existing_facts 冲突，请在 new_fact 里"
                "给出取代关系（用 invalidates 传旧 id）。\n"
                "2) episodes：把当天发生的事压缩成 1~3 段叙述，保留因果与结论。\n"
                "然后调用 consolidate_apply 提交，purge=true 会清空已复盘的原始日志。"
            ),
        }

    def consolidate_apply(
        self,
        *,
        day: str | None = None,
        facts: Sequence[dict[str, Any]] | None = None,
        episodes: Sequence[str] | None = None,
        summary: str | None = None,
        purge: bool = True,
        model: str | None = None,
    ) -> dict[str, Any]:
        target = parse_day(day)
        db_name, conn = self._conn_for("workspace")
        pending = self.list_logs(day=target, include_consolidated=False, limit=1000)
        if not pending:
            return {
                "day": target,
                "log_count": 0,
                "created_facts": [],
                "created_episodes": [],
                "note": "该日没有待复盘的日志",
            }

        digest_id = new_id("d")
        now = now_ms()
        created_facts: list[Fact] = []
        created_episodes: list[Episode] = []

        existing = {
            f.content.strip()
            for f in self.list_facts(scope="all", include_invalid=False, limit=1000)
        }

        for raw in facts or []:
            content = str(raw.get("content", "")).strip()
            if not content or content in existing:
                continue
            try:
                fact = self.write_fact(
                    content,
                    tags=raw.get("tags"),
                    importance=raw.get("importance"),
                    scope="workspace",
                    source=f"digest:{digest_id}",
                )
            except ValueError:
                continue
            existing.add(content)
            created_facts.append(fact)
            old_id = raw.get("invalidates")
            if old_id:
                try:
                    self.invalidate_fact(str(old_id), invalidated_by=fact.id)
                except NotFound:
                    pass

        ep_texts = [e.strip() for e in (episodes or []) if e and e.strip()]
        if not ep_texts and summary:
            ep_texts = [summary.strip()]

        last_at = max(log.at for log in pending)
        for text in ep_texts:
            created_episodes.append(
                self._insert_episode(conn, db_name, text, last_at, target, digest_id)
            )

        with transaction(conn):
            for log in pending:
                conn.execute(
                    "UPDATE logs SET consolidated = 1, digest_id = ? WHERE rowid = ?",
                    (digest_id, log.rowid),
                )
            conn.execute(
                "INSERT INTO digests(id, day, at, log_count, fact_count, episode_count,"
                " summary, purged, model, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    digest_id,
                    target,
                    now,
                    len(pending),
                    len(created_facts),
                    len(created_episodes),
                    summary,
                    1 if purge else 0,
                    model,
                    json.dumps({"log_ids": [x.id for x in pending]}, ensure_ascii=False),
                ),
            )

        purged = self._purge_logs(digest_id) if purge else 0

        return {
            "day": target,
            "digest_id": digest_id,
            "log_count": len(pending),
            "created_facts": [f.to_dict() for f in created_facts],
            "created_episodes": [e.to_dict() for e in created_episodes],
            "purged": purged,
        }

    def _insert_episode(
        self, conn: Any, db_name: str, text: str, at: int, day: str, digest_id: str
    ) -> Episode:
        ep_id = new_id("e")
        now = now_ms()
        vectors = self.embedder.try_embed([text])
        vec = vectors[0] if vectors else None
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO episodes(id, content, at, day, tags, importance,"
                " created_at, digest_id, embedded) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ep_id,
                    text,
                    at,
                    day,
                    "[]",
                    DEFAULT_IMPORTANCE["episode"],
                    now,
                    digest_id,
                    1 if vec else 0,
                ),
            )
            if vec:
                self._vec_put(conn, EPISODE, int(cur.lastrowid), vec)
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        return Episode.from_row(row, db_name)

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

    def list_episodes(self, *, limit: int = 50) -> list[Episode]:
        if self.workspace_conn is None:
            return []
        rows = self.workspace_conn.execute(
            "SELECT * FROM episodes ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Episode.from_row(r, "workspace") for r in rows]

    def list_digests(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if self.workspace_conn is None:
            return []
        rows = self.workspace_conn.execute(
            "SELECT * FROM digests ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================ 运维

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
                "SELECT COUNT(*) AS total, COALESCE(SUM(invalid_at IS NULL), 0) AS live FROM facts"
            ).fetchone()
            info["facts"] = int(row["total"] or 0)
            info["facts_live"] = int(row["live"] or 0)

            if db_name == "workspace":
                info["episodes"] = int(
                    conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"] or 0
                )
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
                if db_name == "global" and spec.kind != "fact":
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

        global_live = result["dbs"].get("global", {}).get("facts_live", 0)
        result["global_facts"] = global_live
        result["global_facts_warn"] = global_live > self.cfg.global_facts_warn
        if result["global_facts_warn"]:
            result["hint"] = (
                f"全局事实已有 {global_live} 条（阈值 {self.cfg.global_facts_warn}）。"
                "跨项目事实本该很少：规则类应写进 CORE_RULES.md，"
                "项目细节应留在各自的 workspace 库。"
            )
        return result

    def backfill_embeddings(self, *, limit: int = 500) -> dict[str, Any]:
        if not self.embedder.available:
            return {"embedded": 0, "skipped": "embedding 未配置"}
        done = 0
        for db_name, conn in self._all_dbs("all"):
            for spec in ALL_SPECS:
                if db_name == "global" and spec.kind != "fact":
                    continue
                rows = conn.execute(
                    f"SELECT rowid, content FROM {spec.table} WHERE embedded = 0 LIMIT ?",
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
        """把项目库导出成「事实/经历/日志」Markdown 视图。

        数据库是唯一事实源，导出的 Markdown 是**只读视图**——
        给人翻阅、给 git 留痕用，不要手改（改了会被下次导出覆盖）。
        """
        if self.workspace_conn is None:
            raise ScopeError("当前目录没有 .ai_memory 工作区")

        root = Path(out_dir)
        (root / "经历").mkdir(parents=True, exist_ok=True)
        (root / "日志").mkdir(parents=True, exist_ok=True)
        written: list[str] = []

        # --- 事实：一条一行
        facts = self.list_facts(scope="workspace", include_invalid=include_invalid, limit=5000)
        lines = [
            "# 事实 / 知识",
            "",
            "> 由 HumanityMemory 自动导出，**请勿手改**——下次导出会覆盖。",
            "> 增删改用 `fact_write` / `fact_invalidate`。",
            "",
        ]
        for fact in facts:
            mark = "" if fact.live else "（已失效）"
            tag = f"  <!-- {' '.join(fact.tags)} -->" if fact.tags else ""
            lines.append(f"- {fact.content}{mark}{tag}")
        path = root / "事实知识.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(str(path))

        # --- 经历：一件事一个文件
        episodes = self.list_episodes(limit=2000)
        for ep in episodes:
            name = f"{ep.day}-{_slug(ep.content)}.md"
            body = [
                f"# {ep.day} · {_slug(ep.content, 40)}",
                "",
                f"> 导出视图 · id `{ep.id}` · 复盘 `{ep.digest_id or 'n/a'}`",
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
            "facts": len(facts),
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
