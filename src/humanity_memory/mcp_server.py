"""MCP server：三层记忆 + 信条静态注入。

stdio 传输对 stdout 极敏感，任何多余输出都会破坏协议帧。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, rules
from .config import Config
from .paths import (
    Layout,
    ensure_rules,
    find_project_root,
    find_workspace_dir,
    read_last_workspace,
    resolve_layout,
    write_last_workspace,
)
from .store import MemoryStore, NotFound, ScopeError, _norm_kind
from .token import Token, TokenError, resolve_token_path

INSTRUCTIONS_TAIL = """\
记忆使用纪律：
存放纪律（先想 kind，再落笔）：
- knowledge 知识：换个单号/任务 ID 仍然成立的机制与约定，能指出代码落点。
- state 现状：当前为真但会变的东西（版本、依赖、路径、资源配置），200 字内，
  给 source；换代时用 invalidates 顶掉旧的，别让旧现状烂在库里。
- case 案例：一次具体事件的「现象 → 根因 → 落点」，≤300 字，一个问题一条。
  带单号的排查结论放这里 —— 它换个单号就不成立，不是知识。
- narrative 叙述：当天过程、试错与判断转折。
- 日志（log）是当天的原始记录，只追加、不改写。白天先写日志，复盘时再压缩。
- state 不会因为被检索到就续命：必须 memory_write(kind="state", verifies=<id>) 才算确认。
- 复盘时先用 consolidate_preview 取素材，总结后用 consolidate_apply 提交。
- 一个 server 可以服务多个项目：如果在某个具体项目里工作，而 memory_stats 显示的工作区
  不是你预期的那个，先调用 memory_use_workspace 传入项目根目录再继续。
- 记忆有生命周期：长期不用会被归档（默认检索不到但内容还在）。想清理时先
  memory_archive 预览、memory_purge_candidates 看候选，**永远不要自动删**。
"""


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _as_list(kinds: list[str] | str | None) -> list[str]:
    """kinds 允许 None / "all" / "knowledge" / ["knowledge","case"] 混着传。"""
    if kinds is None:
        return []
    if isinstance(kinds, str):
        return [kinds]
    return [str(k) for k in kinds]


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def build_server(cfg: Config | None = None, *, layout: Layout | None = None) -> MCPServer:
    cfg = cfg or Config.load()
    layout = layout or resolve_layout()

    ensure_rules(layout.global_rules)
    if layout.workspace_rules is not None:
        ensure_rules(layout.workspace_rules)

    store = MemoryStore(cfg, layout)

    # 工具闭包通过这里取当前 store —— 一条配置服务多个空间的前提：
    # 允许运行中切换到另一个工作区，而不是重建整个进程。
    state: dict[str, Any] = {"store": store, "layout": layout}
    state_lock = threading.Lock()

    def _st() -> MemoryStore:
        return state["store"]

    def _lay() -> Layout:
        return state["layout"]

    instructions = (
        rules.render_instructions(
            global_rules=_lay().global_rules,
            workspace_rules=_lay().workspace_rules,
            workspace_label=(
                _lay().workspace_dir.parent.name if _lay().workspace_dir else None
            ),
        )
        + INSTRUCTIONS_TAIL
    )

    app = MCPServer(
        name="humanity-memory",
        title="HumanityMemory",
        version=__version__,
        instructions=instructions,
    )

    # ------------------------------------------------------------- 信条

    @app.tool(
        name="rules_get",
        description="读取绝对信条（全局 + 工作区）。信条不进向量库、不参与检索、不会被遗忘。",
    )
    def rules_get() -> dict[str, Any]:
        try:
            return _ok(
                global_rules=rules.describe(_lay().global_rules),
                workspace_rules=(
                    rules.describe(_lay().workspace_rules)
                    if _lay().workspace_rules
                    else None
                ),
                text=rules.render_instructions(
                    global_rules=_lay().global_rules,
                    workspace_rules=_lay().workspace_rules,
                ),
                note="信条在 server 启动时静态注入；改完需重开会话才生效。",
            )
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="rules_append",
        description="向信条文件追加一条硬规则（只追加，不改既有内容）。scope=global 影响所有项目。",
    )
    def rules_append(
        rule: str, section: str | None = None, scope: str = "workspace"
    ) -> dict[str, Any]:
        """
        Args:
            rule: 规则正文，一句话、祈使句，例如"禁止修改自动生成的 .meta 文件"。
            section: 追加到哪个二级标题下；不传则追加到文件末尾。
            scope: workspace（当前项目）| global（所有项目）。
        """
        target = _lay().global_rules if scope == "global" else _lay().workspace_rules
        if target is None:
            return _err("当前目录没有 .ai_memory 工作区；写全局信条请用 scope='global'")
        try:
            text = rules.append(target, rule, section=section)
        except (rules.RulesError, OSError) as exc:
            return _err(str(exc))
        return _ok(
            path=str(target),
            chars=len(text),
            note="已写入。信条为静态注入，下次会话启动才生效。",
        )

    # ------------------------------------------------------------- 陈述

    @app.tool(
        name="memory_write",
        description=(
            "唯一的写入口，按 kind 分派：knowledge（知识）/ state（现状）/ case（案例）"
            "/ narrative（叙述）。名字不精确就是灾难——先看 kind 怎么选。"
        ),
    )
    def memory_write(
        kind: str,
        content: str = "",
        tags: list[str] | None = None,
        importance: float | None = None,
        scope: str = "workspace",
        invalidates: str | None = None,
        verifies: str | None = None,
        source: str | None = None,
        day: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            kind:
                knowledge — 知识。**换个单号/任务 ID/资源名仍然成立**，能指出代码落点。
                    机制、约定、坑。上限 500 字。
                state — 现状。当前为真但会随版本变化（版本号、依赖、路径、资源配置）。
                    上限 200 字，必须给 source。会变的东西别写成 knowledge。
                case — 案例。一次具体事件压成「现象 → 根因 → 落点」，上限 300 字，
                    一个问题一条。带单号的排查结论放这里，不要放 knowledge。
                narrative — 叙述。当天过程、试错、判断转折，可长。
            content: 正文。**空 content 只允许 kind=state 且带 verifies**（纯确认）。
            tags: 标签，用于过滤。
            importance: 0~1。默认 knowledge 0.8 / state 0.85 / case 0.75 / narrative 0.4。
            scope: workspace（项目）| global（跨项目，仅陈述类支持且数量要很少）。
            invalidates: 被这条取代的旧条目 id。state 换代必须用它，别让旧现状烂在库里。
            verifies: 确认某条 state 仍然成立并续命（不写新内容时用它）。
            source: 来源标识；写 state 时建议写清「凭什么认为它是这样」。
            day: 仅 case / narrative 用，默认今天。
        """
        try:
            item = _st().write_memory(
                content,
                kind=kind,
                tags=tags,
                importance=importance,
                scope=scope,
                invalidates=invalidates,
                verifies=verifies,
                source=source,
                day=day,
            )
        except (ScopeError, ValueError, NotFound) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败：{type(exc).__name__}: {exc}")
        return _ok(item=item.to_dict(), kind=item.kind, embedded=item.embedded)

    @app.tool(
        name="memory_invalidate",
        description="让一条条目失效（不删除，保留历史）。现状过时但暂无替代时用它。",
    )
    def memory_invalidate(item_id: str, invalidated_by: str | None = None) -> dict[str, Any]:
        try:
            item = _st().invalidate(item_id, invalidated_by=invalidated_by)
        except NotFound as exc:
            return _err(str(exc))
        return _ok(item=item.to_dict())

    @app.tool(
        name="memory_promote",
        description="把项目条目提升为全局条目（跨项目可用）。旧条目标记失效并指向新条目。",
    )
    def memory_promote(item_id: str) -> dict[str, Any]:
        try:
            old, new = _st().promote(item_id)
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        stats = _st().stats()
        return _ok(
            promoted=new.to_dict(),
            invalidated=old.to_dict(),
            global_knowledge=stats.get("global_knowledge"),
            warning=stats.get("hint"),
        )

    @app.tool(name="memory_list", description="列出条目，默认只列有效的。kinds 不传即全部。")
    def memory_list(
        kinds: list[str] | str | None = None,
        scope: str = "all",
        include_invalid: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        """
        Args:
            kinds: all | knowledge | state | case | narrative | log 的子集。
            scope: all | workspace | global
        """
        try:
            picked = {_norm_kind(k) for k in _as_list(kinds)}
            want_all = not picked or "all" in picked
            items: list[dict[str, Any]] = []
            if want_all or picked & {"knowledge", "state"}:
                items += [
                    k.to_dict()
                    for k in _st().list_knowledge(
                        kinds=None if want_all else sorted(picked & {"knowledge", "state"}),
                        scope=scope,
                        include_invalid=include_invalid,
                        limit=max(1, min(limit, 200)),
                    )
                ]
            if want_all or picked & {"case", "narrative"}:
                items += [
                    e.to_dict()
                    for e in _st().list_episodes(
                        kinds=None if want_all else sorted(picked & {"case", "narrative"}),
                        limit=max(1, min(limit, 200)),
                    )
                ]
            if want_all or "log" in picked:
                items += [
                    log.to_dict()
                    for log in _st().list_logs(
                        include_consolidated=include_invalid, limit=max(1, min(limit, 500))
                    )
                ]
        except ScopeError as exc:
            return _err(str(exc))
        return _ok(count=len(items), items=items)

    # ------------------------------------------------------------- 日志

    @app.tool(
        name="log_append",
        description="追加一条当天日志（只追加、不可改）。白天记录事件与排查过程用它。",
    )
    def log_append(
        content: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
        importance: float | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            content: 发生了什么，可以写长，复盘时会被压缩。
            tags: 标签。
            session_id: 会话标识，便于回溯。
        """
        try:
            entry = _st().append_log(
                content, tags=tags, session_id=session_id, importance=importance
            )
        except (ScopeError, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败：{type(exc).__name__}: {exc}")
        return _ok(log=entry.to_dict())

    @app.tool(name="log_list", description="按日期列出日志，默认今天且含已复盘的。")
    def log_list(
        day: str | None = None, include_consolidated: bool = True, limit: int = 100
    ) -> dict[str, Any]:
        """
        Args:
            day: YYYY-MM-DD / today / yesterday。
        """
        try:
            entries = _st().list_logs(
                day=day or "today",
                include_consolidated=include_consolidated,
                limit=max(1, min(limit, 500)),
            )
        except ValueError as exc:
            return _err(str(exc))
        return _ok(count=len(entries), logs=[e.to_dict() for e in entries])

    # ------------------------------------------------------------- 检索

    @app.tool(
        name="memory_search",
        description=(
            "跨层混合检索（知识 + 现状 + 案例 + 叙述 + 日志，自动含全局库）。"
            "不改访问强度；要续命 state 请用 memory_write(verifies=)。"
        ),
    )
    def memory_search(
        query: str,
        kinds: list[str] | str | None = None,
        scope: str = "all",
        include_invalid: bool = False,
        limit: int = 8,
        format: str = "lines",
    ) -> dict[str, Any]:
        """
        Args:
            query: 自然语言查询。
            kinds: all / knowledge / state / case / narrative / log 的子集，默认全部。
            scope: all | workspace | global
            include_invalid: 是否包含已失效条目（查历史用）。
            format: lines（默认，一行一条，省 token）| full（带分数与元数据）。
        """
        try:
            hits = _st().search(
                query,
                kinds=kinds,
                scope=scope,
                include_invalid=include_invalid,
                limit=max(1, min(limit, 50)),
            )
        except ScopeError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"检索失败：{type(exc).__name__}: {exc}")
        payload: dict[str, Any] = {
            "count": len(hits),
            "lines": [h.to_line() for h in hits],
            "degraded": None if _st().embedder.available else "embedding 不可用，本次为关键词检索",
        }
        if format == "full":
            payload["results"] = [h.to_dict() for h in hits]
        return _ok(**payload)

    @app.tool(
        name="memory_recall",
        description="按当前话题召回并强化命中项。只回 lines（省 token），需要元数据用 memory_search。",
    )
    def memory_recall(
        context: str,
        kinds: list[str] | str | None = None,
        limit: int = 6,
        format: str = "lines",
    ) -> dict[str, Any]:
        """
        Args:
            context: 当前在做什么。写具体情境，别只写关键词。
            kinds: all / knowledge / state / case / narrative / log 的子集。
            format: lines（默认）| full。
        """
        try:
            hits = _st().recall(context, kinds=kinds, limit=max(1, min(limit, 20)))
        except ScopeError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"召回失败：{type(exc).__name__}: {exc}")
        payload: dict[str, Any] = {
            "count": len(hits),
            "lines": [h.to_line() for h in hits],
        }
        if format == "full":
            payload["results"] = [h.to_dict() for h in hits]
        return _ok(**payload)

    # ------------------------------------------------------------- 复盘

    @app.tool(
        name="consolidate_preview",
        description="取出某天待复盘的原始日志供你总结。只读，不改任何数据。",
    )
    def consolidate_preview(day: str | None = None, limit: int = 300) -> dict[str, Any]:
        """
        Args:
            day: YYYY-MM-DD / today / yesterday。**不传时自动找最近一个仍有待复盘
                 日志的日期**，适合定时任务补齐积压。
        """
        try:
            return _ok(
                preview=_st().consolidate_preview(day, limit=max(1, min(limit, 1000)))
            )
        except (ValueError, ScopeError) as exc:
            return _err(str(exc))

    @app.tool(
        name="consolidate_apply",
        description="提交复盘结果：写入提炼的知识/现状与案例/叙述，标记日志已处理，可选清空原始日志。",
    )
    def consolidate_apply(
        day: str | None = None,
        knowledge: list[dict[str, Any]] | None = None,
        episodes: list[dict[str, Any] | str] | None = None,
        summary: str | None = None,
        purge: bool = True,
    ) -> dict[str, Any]:
        """
        Args:
            day: 复盘哪一天，默认今天。
            knowledge: 陈述数组，每项 {"content": str, "kind": "knowledge"|"state",
                   "tags": [str], "importance": float, "invalidates": "旧id", "source": str}。
                   kind 不传按 knowledge。**带单号的排查结论不要放这里**，
                   它换个单号就不成立了 —— 那是 case。
            episodes: 经历数组。每项 {"content": str, "kind": "case"|"narrative",
                   "importance": float, "tags": [str]}。
                   case 只写「现象 → 根因 → 落点」，≤300 字，一个问题一条；
                   narrative 写当天过程与判断转折，importance 决定初始详细程度
                   （≥0.75 完整叙述 / 0.45~0.75 要点 / <0.45 一句话）。
            summary: 当天一句话总结（可选）。
            purge: 是否物理清空已复盘的原始日志，默认 true。
        """
        try:
            result = _st().consolidate_apply(
                day=day,
                knowledge=knowledge,
                episodes=episodes,
                summary=summary,
                purge=purge,
                model=cfg.embed_model or None,
            )
        except (ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"复盘失败：{type(exc).__name__}: {exc}")
        return _ok(result=result)

    # ------------------------------------------------------------- 运维

    @app.tool(
        name="memory_use_workspace",
        description="切换当前记忆工作区到指定项目目录。一条 server 配置即可服务多个项目。",
    )
    def memory_use_workspace(
        path: str, rules: str | None = None, persist: bool = True
    ) -> dict[str, Any]:
        """
        Args:
            path: 项目根目录（不是 .ai_memory 目录）。
            rules: 信条文件路径；不传则自动探测 <项目>/.workbuddy/memory/信条.md。
            persist: 记为「上次工作区」，下次 server 启动自动接上。
        """
        try:
            new_layout = resolve_layout(path, rules_path=rules)
        except Exception as exc:  # noqa: BLE001
            return _err(f"解析工作区失败：{type(exc).__name__}: {exc}")
        if new_layout.workspace_dir is None:
            return _err(f"无法在该路径建立工作区：{path}")

        with state_lock:
            try:
                if new_layout.workspace_rules is not None:
                    ensure_rules(new_layout.workspace_rules)
                new_store = MemoryStore(cfg, new_layout)
            except Exception as exc:  # noqa: BLE001
                return _err(f"切换失败：{type(exc).__name__}: {exc}")
            old_store = state["store"]
            state["store"] = new_store
            state["layout"] = new_layout
            if persist:
                try:
                    write_last_workspace(new_layout.workspace_dir.parent)
                except OSError:
                    pass
            old_store.close()

        return _ok(
            switched_to=new_layout.describe(),
            rules=str(new_layout.workspace_rules) if new_layout.workspace_rules else None,
            persisted=persist,
        )

    # ------------------------------------------------------------- 生命维持

    @app.tool(
        name="memory_archive",
        description="按「久未触碰 + 访问少」归档低价值记忆。默认只预览，dry_run=false 才执行。",
    )
    def memory_archive(dry_run: bool = True) -> dict[str, Any]:
        """
        归档 = 默认检索不到，但内容一字未改，`include_archived=true` 仍能查回。
        信条不参与；陈述与经历按**行级 kind** 各自 TTL 判定：
          knowledge 3 年 / state 30 天 / case 1 年 / narrative 1 年。
        state 的续命不看访问次数，只认 verified_at —— 反复被查往往说明它已经不准了。

        Args:
            dry_run: true（默认）只列候选不动数据；false 才真正归档。
        """
        try:
            with state_lock:
                return _ok(report=_st().archive_pass(dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            return _err(f"归档失败：{type(exc).__name__}: {exc}")

    @app.tool(name="memory_unarchive", description="把已归档的记忆恢复正常（会重新嵌入）。")
    def memory_unarchive(memory_id: str) -> dict[str, Any]:
        try:
            with state_lock:
                return _ok(result=_st().unarchive(memory_id))
        except (NotFound, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"恢复失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_purge_candidates",
        description="列出「已归档且长期未再访问」的删除候选。只报告，不删除。",
    )
    def memory_purge_candidates(after_days: float | None = None) -> dict[str, Any]:
        """
        Args:
            after_days: 归档后静默多少天算候选；不传用配置默认（180 天）。
        """
        try:
            items = _st().purge_candidates(after_days=after_days)
        except Exception as exc:  # noqa: BLE001
            return _err(f"查询失败：{type(exc).__name__}: {exc}")
        return _ok(
            count=len(items),
            candidates=items,
            note="删除不可恢复。确认后调用 memory_purge(ids=[...])；不确认就保留，它们已不参与检索。",
        )

    @app.tool(
        name="memory_purge",
        description="物理删除指定记忆（含向量）。不可恢复，只能删已归档的条目。",
    )
    def memory_purge(ids: list[str], confirm: bool = False) -> dict[str, Any]:
        """
        Args:
            ids: 要删除的记忆 id 列表。
            confirm: 必须显式传 true 才会执行 —— 防止误调用。
        """
        if not confirm:
            return _err("拒绝执行：物理删除不可恢复，请显式传 confirm=true")
        try:
            with state_lock:
                return _ok(result=_st().purge_items(ids))
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"删除失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_compress_preview",
        description="列出可压缩的经历（同一天多条）。只读，实际压缩要你总结后调 compress_apply。",
    )
    def memory_compress_preview() -> dict[str, Any]:
        try:
            return _ok(preview=_st().compress_candidates())
        except Exception as exc:  # noqa: BLE001
            return _err(f"查询失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_compress_apply",
        description="把多条经历合并成一条摘要，原条目转为归档（不删除）。",
    )
    def memory_compress_apply(ids: list[str], summary: str, day: str | None = None) -> dict[str, Any]:
        """
        Args:
            ids: 要合并的经历 id 列表。
            summary: 合并后的叙述，保留因果与结论，不要流水账。
            day: 合并后归属的日期，默认取第一条的日期。
        """
        try:
            with state_lock:
                return _ok(result=_st().compress_apply(ids, summary, day=day))
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"压缩失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_recompress_preview",
        description="列出该被进一步压缩的经历（按年龄分档：30 天→level1，180 天→level2）。",
    )
    def memory_recompress_preview() -> dict[str, Any]:
        try:
            return _ok(preview=_st().recompress_preview())
        except Exception as exc:  # noqa: BLE001
            return _err(f"查询失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_recompress_apply",
        description="把经历改写成更短的版本并提升压缩级别（1=去冗余，2=只留结论）。",
    )
    def memory_recompress_apply(items: list[dict[str, Any]], level: int) -> dict[str, Any]:
        """
        经历的压缩是**渐进**的：刚复盘时是完整叙述（level 0），
        随时间逐级变短。比起步就压缩到位，这样刚发生的细节还能查得到。

        Args:
            items: [{"id": "e_xxx", "content": "改写后的更短版本"}]
            level: 目标级别，1（去冗余保关键步骤）或 2（只留结论与根因）。
        """
        try:
            with state_lock:
                return _ok(result=_st().recompress_apply(items, level=level))
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"压缩失败：{type(exc).__name__}: {exc}")

    @app.tool(
        name="memory_stats",
        description="查看两库统计：知识/现状/案例/经历/日志数量、待复盘、待嵌入、全局库是否膨胀。",
    )
    def memory_stats() -> dict[str, Any]:
        try:
            data = _st().stats()
        except Exception as exc:  # noqa: BLE001
            return _err(f"统计失败：{type(exc).__name__}: {exc}")
        data["layout"] = _lay().describe()
        data["vec_source"] = _st().vec_source
        return _ok(stats=data)

    @app.tool(
        name="memory_maintenance",
        description="补跑缺失的 embedding，并返回待复盘与整理提示。",
    )
    def memory_maintenance() -> dict[str, Any]:
        try:
            report = _st().backfill_embeddings()
            stats = _st().stats()
        except Exception as exc:  # noqa: BLE001
            return _err(f"维护失败：{type(exc).__name__}: {exc}")
        return _ok(
            report=report,
            pending_embedding=sum(
                db.get("pending_embedding", 0) for db in stats["dbs"].values()
            ),
            hint=stats.get("hint"),
        )

    return app


def _parse_args(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    """极简参数解析：只认 --workspace / --rules / --token。

    刻意不用 argparse —— 本进程由 MCP 客户端拉起，参数极少，
    而 argparse 遇到未知参数会直接退出，客户端偶尔会多传东西。
    """
    workspace: str | None = None
    rules: str | None = None
    token: str | None = None
    known = {"--workspace": "workspace", "--rules": "rules", "--token": "token"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in known:
            if i + 1 < len(argv):
                value = argv[i + 1]
                name = known[arg]
                if name == "workspace":
                    workspace = value
                elif name == "rules":
                    rules = value
                else:
                    token = value
            i += 2
            continue
        for flag, name in known.items():
            if arg.startswith(flag + "="):
                value = arg.split("=", 1)[1]
                if name == "workspace":
                    workspace = value
                elif name == "rules":
                    rules = value
                else:
                    token = value
                break
        i += 1
    return workspace, rules, token


def _write_probe(
    argv: list[str], layout: Layout, source: str, token: Token | None, cfg: Config
) -> None:
    """把启动时的关键上下文写进诊断文件。

    MCP 协议不提供"当前工作区"信息，所以只能靠观测客户端的行为来确定
    cwd 是什么、解析走了哪条路。这个探针就是为此存在的——出问题时
    先看 `~/.humanity-memory/diagnostics/startup.json`。
    """
    try:
        diag = layout.global_dir / "diagnostics"
        diag.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "argv": argv,
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "source": source,
            "workspace_dir": str(layout.workspace_dir) if layout.workspace_dir else None,
            "rules": str(layout.workspace_rules) if layout.workspace_rules else None,
            "token": str(token.path) if token else None,
            "embedding_configured": cfg.embedding_configured,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (diag / "startup.json").write_text(text + "\n", encoding="utf-8")
        with open(diag / "startup.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 诊断失败绝不影响主流程
        pass


def main() -> None:
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    argv = sys.argv[1:]
    workspace_arg, rules_arg, token_arg = _parse_args(argv)

    token = None
    source = "none"

    if token_arg:
        token_path = resolve_token_path(token_arg)
        try:
            token = Token.load(token_path)
        except TokenError as exc:
            print(f"[humanity-memory] 令牌加载失败：{exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        # 令牌里的 embedding 配置优先级最高，必须在 Config.load() 之前注入
        token.apply_embedding_env()
        workspace_arg = str(token.workspace)
        if token.rules is not None:
            rules_arg = str(token.rules)
        source = "token"
        print(
            f"[humanity-memory] token    : {token.path} (label={token.display})",
            file=sys.stderr,
        )
    elif workspace_arg:
        source = "workspace"
    elif os.environ.get("HM_WORKSPACE"):
        workspace_arg = os.environ["HM_WORKSPACE"]
        source = "env"
    else:
        # 1) cwd 向上找已存在的 .ai_memory（只找不建）
        found = find_workspace_dir(create=False)
        if found is not None:
            workspace_arg = str(found.parent)
            source = "cwd"
        else:
            # 2) cwd 处在一个「像项目」的目录树里 → 在项目根自动建，
            #    省掉每个项目手动 init 一次
            root = find_project_root()
            if root is not None:
                workspace_arg = str(root)
                source = "cwd-project"
                print(
                    f"[humanity-memory] 首次在 {root} 使用，自动建立记忆库",
                    file=sys.stderr,
                )
            else:
                # 3) 兜底：上次用过的工作区
                last = read_last_workspace()
                if last is not None:
                    workspace_arg = str(last)
                    source = "last"
                    print(f"[humanity-memory] 沿用上次工作区：{last}", file=sys.stderr)

    cfg = Config.load()
    layout = resolve_layout(
        workspace_arg, rules_path=rules_arg, create=workspace_arg is not None
    )

    if not cfg.embedding_configured:
        print(
            "[humanity-memory] embedding 未配置，退化为关键词检索。"
            "设置 HM_EMBED_BASE_URL / HM_EMBED_MODEL 可启用语义检索。",
            file=sys.stderr,
        )
    if layout.workspace_dir is None:
        print(
            "[humanity-memory] 未确定工作区，当前只能读写全局库。"
            "可用 memory_use_workspace 指定项目目录。",
            file=sys.stderr,
        )
    else:
        print(
            f"[humanity-memory] workspace: {layout.workspace_dir} (来自 {source})",
            file=sys.stderr,
        )
        if layout.workspace_rules is not None:
            print(f"[humanity-memory] rules    : {layout.workspace_rules}", file=sys.stderr)

    _write_probe(argv, layout, source, token, cfg)

    app = build_server(cfg, layout=layout)
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
