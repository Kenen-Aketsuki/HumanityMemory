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
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, bindings, gate, keys, rules
from .config import NO_LIBRARY, Config
from .paths import (
    WORKSPACE_DIR,
    Layout,
    empty_layout,
    ensure_rules,
    resolve_layout,
)
from .store import ConsolidationRejected, MemoryStore, NotFound, ScopeError, _norm_kind
from .token import Token, TokenError, build_token, resolve_token_path, same_path, token_path

INSTRUCTIONS_TAIL = """\
记忆使用纪律：
存放纪律（只有一个判据：**它是否需要回答「什么时候发生的」**）：
- 经验（experience）：可复用的判定与结论 —— 机制、约定、坑、根因，换个单号仍然成立。
  上限 500 字。易变的结论（版本、依赖、路径、当前方案）给低 confidence（0.3），
  稳定的机制给高一点；之后能否被信任，由 memory_verify 的证据说话，不由你决定分。
- 经历（episode）：发生过的事，一条一个单一事务。「今天吃饭」「今天洗澡」是两条，
  不是「今天做了一些事」一条。需要时间戳，可以写长 —— 它会随遗忘等级自己压缩。
  带单号的排查过程写这里；其中**可复用的结论**另写一条经验。
- 日志（log）是当天的原始记录，只追加、不改写。白天先写日志，复盘时再总结。
- 没有第三种类型。旧词 knowledge / state / case / narrative 已废弃：
  「现状」不再单独成类 —— 它只是「一条容易变、初始可信度低的经验」。
- 复盘时先用 consolidate_preview 取素材，总结后用 consolidate_apply 提交。
- 一个 server 可以服务多个项目：如果在某个具体项目里工作，而 memory_stats 显示的工作区
  不是你预期的那个，先调用 memory_use_workspace 传入项目根目录再继续。

检索纪律：
- 日常找记忆**一律 memory_recall(context=当前在做什么)**：它按当前话题召回并强化命中项，
  会计入活跃度，遗忘曲线才反映真实使用。
- memory_search 是**复盘专用**：只在复盘时间窗内、或持终端手签的通行证时可用。
  日常调用会返回 code=REVIEW_REQUIRED。**被拒就不要再试**，也不要绕道用
  memory_list / export 去翻全量（那只翻不算用，活跃度一样是空的），改用 recall。

绑定纪律（未接入时）：
- 先调用 memory_bind_status 看 code。code 不为 null 时**先向用户提问**：
  新建独立库 / 并入已有库 / 沿用本目录已有的库 / 本次不接入。
- 拿到答复再调对应的 memory_bind_new / memory_bind_existing / memory_bind_adopt /
  memory_bind_decline。**不要自己选**，不要在拒绝态下反复尝试写入。
- 记忆有生命周期：长期不用会被归档（默认检索不到但内容还在）。想清理时先
  memory_archive 预览、memory_purge_candidates 看候选，**永远不要自动删**。
"""


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _as_list(kinds: list[str] | str | None) -> list[str]:
    """kinds 允许 None / "all" / "experience" / ["experience","episode"] 混着传。"""
    if kinds is None:
        return []
    if isinstance(kinds, str):
        return [kinds]
    return [str(k) for k in kinds]


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _err_code(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """带机器可读 code 的拒绝。模型读得懂原因，就不会原地重试。"""
    return {"ok": False, "code": code, "error": message, **extra}


def build_server(
    cfg: Config | None = None,
    *,
    layout: Layout | None = None,
    session: "bindings.Session | None" = None,
) -> MCPServer:
    """装配 MCP server。

    `session` 决定工具面：接进去了就全量注册；没接进去（拒绝态）**只注册
    `bind_*` 与 `memory_use_workspace`**，其余 24 个工具一个都不注册。

    为什么是"不注册"而不是"注册后报错"：模型看不到工具就不会调，
    也就不会出现"agent 以为写入成功"这类事故（§6.4）。
    """
    # 先定布局再装配置：库级 memory.toml 挂在库目录（自有库 = .ai_memory/）下，
    # 反过来就永远读不到库级覆盖。
    if session is None:
        layout = layout or resolve_layout()
        cfg = cfg or Config.load(library_dir=layout.library_dir)
        session = bindings.Session(
            ok=layout.library_dir is not None,
            workspace_root=layout.workspace_root,
            library_dir=layout.library_dir,
            layout=layout,
        )
    layout = layout or session.layout or empty_layout(root=session.workspace_root)
    cfg = cfg or Config.load(
        library_dir=layout.library_dir if layout.library_dir else NO_LIBRARY
    )

    bound = bool(session.ok and layout.library_dir is not None)

    if bound:
        ensure_rules(layout.global_rules)
        if layout.workspace_rules is not None:
            ensure_rules(layout.workspace_rules)
        store: MemoryStore | None = MemoryStore(cfg, layout)
    else:
        # 拒绝态不碰任何项目文件：连信条都不建（那也是一次写盘）
        ensure_rules(layout.global_rules)
        store = None

    # 工具闭包通过这里取当前 store —— 一条配置服务多个空间的前提：
    # 允许运行中切换到另一个工作区，而不是重建整个进程。
    state: dict[str, Any] = {"store": store, "layout": layout, "session": session}
    state_lock = threading.Lock()

    def _st() -> MemoryStore:
        current = state["store"]
        if current is None:  # 拒绝态不会走到这里：那 24 个工具没注册
            raise RuntimeError("未接入记忆库：当前会话没有可用的 store")
        return current

    def _lay() -> Layout:
        return state["layout"]

    # ---- 切库 / 绑定的公共动作 ----

    def _rebind(new_session: "bindings.Session") -> str | None:
        """按新会话重建 store + 重载配置（F2：配置跟着库走，必须换 cfg）。

        返回错误文本，`None` 表示成功。
        """
        new_layout = new_session.layout
        if new_layout is None or new_layout.library_dir is None:
            return "解析到的布局里没有库目录"
        try:
            if new_layout.workspace_rules is not None:
                ensure_rules(new_layout.workspace_rules)
            new_cfg = Config.load(library_dir=new_layout.library_dir)
            new_store = MemoryStore(new_cfg, new_layout)
        except Exception as exc:  # noqa: BLE001
            return f"切换失败：{type(exc).__name__}: {exc}"
        with state_lock:
            old = state["store"]
            state.update(store=new_store, layout=new_layout, session=new_session)
        if old is not None:
            old.close()
        return None

    def _unbind(root: Path | None = None) -> None:
        """回到拒绝态：关库、清 store，并把 session 换成新的拒绝结论。

        只清 store 不清 session 的话，`memory_bind_status` 会继续报告上一次
        的状态 —— 刚做完 decline 却显示 no_token，等于把用户刚做的决定当没发生。
        """
        refusal = bindings.resolve_session(root) if root is not None else None
        with state_lock:
            old = state["store"]
            state["store"] = None
            if refusal is not None:
                state["session"] = refusal
                state["layout"] = empty_layout(root=refusal.workspace_root)
        if old is not None:
            old.close()

    def _bind_root(workspace: str | None) -> Path | dict[str, Any]:
        """bind_* 的公共入口：确定要动的项目根，拿不到就返回拒绝载荷。"""
        hint = workspace or (
            state["session"].workspace_root if state["session"] else None
        )
        if hint is None:
            return bindings.resolve_session(workspace).to_json()
        root = Path(str(hint)).expanduser()
        if root.name == WORKSPACE_DIR:
            root = root.parent
        if not root.is_dir():
            return {
                "ok": False,
                "error": "memory_unavailable",
                "code": "no_workspace",
                "reason": f"目录不存在：{root}",
            }
        return root

    def _apply_binding(result: Any, note: str) -> dict[str, Any]:
        """把 `bindings.bind_*` 的返回值落到当前会话上。"""
        if isinstance(result, dict):  # 已经是拒绝载荷
            return result
        if not result.ok:  # decline：合法的"不接入"，不挂工具
            _unbind(result.workspace_root)
            payload = result.to_json()
            payload["note"] = note
            return payload
        error = _rebind(result)
        if error is not None:
            return _err(error)
        payload = _ok(
            note=note,
            workspace_root=str(result.workspace_root),
            library=result.record.library if result.record else None,
            library_dir=str(result.library_dir),
        )
        payload["tools_registered"] = activate()
        return payload

    def _candidate_libraries() -> list[dict[str, Any]] | dict[str, str]:
        try:
            registry = keys.read()
        except keys.RegistryError as exc:
            return {"error": str(exc)}
        return [
            {
                "library": label,
                "path": str(lib.path),
                "exists": lib.path.is_dir(),
            }
            for label, lib in sorted(registry.libraries.items())
        ]

    def _legacy_hint(root: Path | None) -> dict[str, str] | None:
        """有库没令牌 = 最需要 adopt 的那种现场，单独指出来。"""
        if root is None:
            return None
        db = root / WORKSPACE_DIR / bindings.DB_NAME
        if db.is_file() and not token_path(root).is_file():
            return {
                "workspace_db": str(db),
                "hint": "本目录已有库但没有令牌 → memory_bind_adopt 可沿用它",
            }
        return None

    instructions = (
        rules.render_instructions(
            global_rules=_lay().global_rules,
            workspace_rules=_lay().workspace_rules,
            workspace_label=(
                _lay().workspace_root.name
                if _lay().workspace_root
                else (_lay().library_dir.parent.name if _lay().library_dir else None)
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

    bound_flag = {"on": bound}
    deferred: list[tuple[Any, dict[str, Any]]] = []

    def tool(**kwargs: Any):
        """条件注册：未接入时把装饰器换成「先记账、晚点注册」。

        记账而不是丢弃，是为了 R-11：boot 实例的 cwd 可能落在
        `~/.humanity-memory`，那时整个会话是拒绝态；等 agent 用
        `memory_bind_*` / `memory_use_workspace` 接进去之后，这些工具要
        **当场出现**，而不是让用户重启客户端。工具名单只有这一份 ——
        写在别处迟早会和实现漂移。
        """
        if bound_flag["on"]:
            return app.tool(**kwargs)

        def _defer(fn: Any) -> Any:
            deferred.append((fn, kwargs))
            return fn

        return _defer

    def activate() -> int:
        """把延迟注册的工具一次性挂上（切库 / 绑定成功后调用）。"""
        if bound_flag["on"]:
            return 0
        for fn, kwargs in deferred:
            app.add_tool(fn, **kwargs)
        count = len(deferred)
        deferred.clear()
        bound_flag["on"] = True
        return count

    # ------------------------------------------------------------- 信条

    @tool(
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

    @tool(
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

    @tool(
        name="memory_write",
        description=(
            "唯一的写入口，按 kind 分派：experience（经验）/ episode（经历）。"
            "判据只有一条 —— 它是否需要回答「什么时候发生的」。"
        ),
    )
    def memory_write(
        kind: str,
        content: str = "",
        tags: list[str] | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        scope: str = "workspace",
        invalidates: str | None = None,
        source: str | None = None,
        day: str | None = None,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            kind:
                experience — 经验。可复用的判定与结论（机制、约定、坑、根因），
                    **换个单号/任务 ID/资源名仍然成立**，能指出代码落点。上限 500 字。
                    易变的结论给低 confidence，稳定的机制给高一点。
                episode — 经历。发生过的事，一条一个单一事务，带发生时间，可长。
                    带单号的排查过程放这里；其中可复用的结论另写一条经验。
            content: 正文。
            tags: 标签，用于过滤。
            importance: 0~1。决定初始遗忘等级（默认经验 0.8 / 经历 0.4）。
            confidence: 0~1，仅经验有效：**初次写下时你有多确信**。
                易变的现状 0.3、一般结论 0.5~0.6、机制性结论 0.8。之后一律由
                memory_verify 按证据调整 —— 这里给的是起点，不是评价。
            scope: workspace（默认）。global 已关闭（§8.2），传了会明确报错。
            invalidates: 被这条取代的旧条目 id（仅经验）。旧条置失效、正文保留。
                带了这个参数就不查重 —— 取代品本来就该和被取代者相似。
            source: 来源标识；写经验时写清「凭什么认为它是这样」。
            day: 仅经历用，默认今天。
            allow_duplicate: 强制写入，跳过查重。只在「我确认这是一条独立的结论，
                虽然措辞相似但说的是另一件事」时才用；被查重拦下后想覆盖，
                请改用 invalidates。
        """
        try:
            item = _st().write_memory(
                content,
                kind=kind,
                tags=tags,
                importance=importance,
                confidence=confidence,
                scope=scope,
                invalidates=invalidates,
                source=source,
                day=day,
                allow_duplicate=allow_duplicate,
            )
        except (ScopeError, ValueError, NotFound) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败：{type(exc).__name__}: {exc}")
        payload: dict[str, Any] = {
            "item": item.to_dict(),
            "kind": item.kind,
            "embedded": item.embedded,
        }
        if getattr(item, "deduped", False):
            # 这一条没新建：返回的就是库里已有的那条，活跃度已加过
            payload["deduped"] = True
            payload["note"] = (
                f"与 {item.id} 高度重复，未新建；已给该条加活跃度、原样返回。"
                " 若确实是新结论要取代它，请带 invalidates 重写。"
            )
        return _ok(**payload)

    @tool(
        name="memory_verify",
        description="给一条经验落一条证据：ok=true 按间隔加分，ok=false 按同一条幂律扣分。"
        "level 决定证据权重：L1/L2 计分，L3 只留痕。",
    )
    def memory_verify(
        item_id: str,
        ok: bool = True,
        level: str = "L1",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """唯一的逐条核验入口。

        可信度不是「AI 觉得它对不对」，而是「有没有证据」。加多少分由**间隔**决定 ——
        隔了半年再看一眼它还是对的，比昨天刚验过今天又验一次，信息量大得多。

        Args:
            item_id: 要核验的经验 id。经历不行 —— 经历的可信度是遗忘等级的派生值。
            ok:
                true  — 核对过现实，结论为真：按间隔加分，封顶 1.0。
                false — 现实或上下文已经与之矛盾：按同一条幂律扣分，封底 0。
                        **不删除条目**：被推翻本身是知识，它会带着低分留下来，
                        被后续检索标上 needs_verify，提醒读的人先核一眼。
            level: 证据级别。
                L1 可观测动作 / 工具调用记录 / 明确执行痕迹 → 全权计分
                L2 复盘模型判定当日确实执行了该流程 → 半权
                L3 仅文本共现（「忘了跑 memory_stats」这类提到）→ 0 分，只记一行事件
            reason: 这次证据是什么，一句话。写进审计事件，三个月后靠它回答
                「这条凭什么 0.9」。不填也放行，但那条事件的 reason 会是空的。

        返回新可信度 `to`、本次变化 `delta`、以及是否需要在被读到时提示核验。
        """
        try:
            with state_lock:
                return _ok(result=_st().verify(item_id, ok=ok, level=level, reason=reason))
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"核验失败：{type(exc).__name__}: {exc}")

    @tool(
        name="memory_invalidate",
        description="让一条经验失效（不删除，保留历史）。被更好的结论取代时用它。",
    )
    def memory_invalidate(item_id: str, invalidated_by: str | None = None) -> dict[str, Any]:
        """经验过时但暂无替代时用它：置 invalid_at，正文不动，默认检索跳过。"""
        try:
            item = _st().invalidate(item_id, invalidated_by=invalidated_by)
        except (NotFound, ValueError) as exc:
            return _err(str(exc))
        return _ok(item=item.to_dict())

    @tool(
        name="memory_promote",
        description="已下线（§8.2）：全局库已关闭，没有可提升的去处。调用会明确报错。",
    )
    def memory_promote(item_id: str) -> dict[str, Any]:
        """保留这个工具壳子只为给出清楚的答复，不做任何写入。

        直接删掉会让调用方看到一个「工具不存在」的模糊错误，而这条约束
        （跨项目共享怎么走）恰恰需要被说清楚。
        """
        try:
            _st().promote(item_id)
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        return _err("memory_promote 已下线（§8.2）")

    @tool(name="memory_list", description="列出条目，默认只列有效的。kinds 不传即全部。")
    def memory_list(
        kinds: list[str] | str | None = None,
        scope: str = "all",
        include_invalid: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        """
        Args:
            kinds: all | experience | episode | log 的子集。
            scope: 只有项目库了，这个参数保留是为了兼容调用方；传 global 会报错。
        """
        try:
            raw = [k.strip().lower() for k in _as_list(kinds) if k.strip()]
            want_all = not raw or "all" in raw
            picked = set() if want_all else {_norm_kind(k, writable=False) for k in raw}
            items: list[dict[str, Any]] = []
            if want_all or "experience" in picked:
                items += [
                    k.to_dict()
                    for k in _st().list_experience(
                        scope=scope,
                        include_invalid=include_invalid,
                        limit=max(1, min(limit, 200)),
                    )
                ]
            if want_all or "episode" in picked:
                items += [
                    e.to_dict()
                    for e in _st().list_episodes(limit=max(1, min(limit, 200)))
                ]
            if want_all or "log" in picked:
                items += [
                    log.to_dict()
                    for log in _st().list_logs(
                        include_consolidated=include_invalid, limit=max(1, min(limit, 500))
                    )
                ]
        except (ScopeError, ValueError) as exc:
            return _err(str(exc))
        return _ok(count=len(items), items=items)

    # ------------------------------------------------------------- 日志

    @tool(
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

    @tool(name="log_list", description="按日期列出日志，默认今天且含已复盘的。")
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

    @tool(
        name="memory_search",
        description=(
            "复盘专用检索（经验 + 经历 + 日志，自动含全局库）。"
            "只在复盘时间窗内、或持手签通行证时可用；日常调用会被拒。"
            "日常找东西请用 memory_recall —— 它按当前话题召回并强化命中项，"
            "会计入活跃度；search 刻意不改访问强度，拿它顶替 recall 会让活跃度失真。"
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
            kinds: all / experience / episode / log 的子集，默认全部。
            scope: 只有项目库了，参数保留兼容；传 global 会报错。
            include_invalid: 是否包含已失效条目（查历史用）。
            format: lines（默认，一行一条，省 token）| full（带分数与元数据）。
        """
        # 闸门先于一切：不满足就硬拒，别把「模型试出来能搜」变成一个可选项。
        # 判据全在 gate 里（时间窗 / 手签 flag），不经模型自述。
        gate_state = gate.state(cfg)
        if not gate_state.open:
            gate.audit(
                cfg.home,
                "search_blocked",
                source=gate_state.source,
                window=gate_state.window,
                next_open=gate_state.next_open,
                query=query[:80],
            )
            return _err_code(
                "REVIEW_REQUIRED",
                gate_state.message(),
                gate=gate_state.to_dict(),
                hint=(
                    "日常用 memory_recall(context=当前在做什么)：按话题召回并强化命中项，"
                    "会计入活跃度。复盘需在终端跑 `humanity-memory review begin` 签发通行证。"
                ),
            )
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
            # strict = 严格阈值命中；fallback = 严格层没捞到，退回宽泛检索，
            # 结果只是"沾边"，别当精确匹配用
            "mode": "fallback" if any(h.fallback for h in hits) else "strict",
            "lines": [h.to_line() for h in hits],
            "degraded": None if _st().embedder.available else "embedding 不可用，本次为关键词检索",
        }
        if format == "full":
            payload["results"] = [h.to_dict() for h in hits]
        return _ok(**payload)

    @tool(
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
            kinds: all / experience / episode / log 的子集。
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
        # 兜底捞上来的遗忘层条目要单独说清楚，不然读的人以为它们还在常规视野里
        forgotten = [h for h in hits if h.forgotten]
        if forgotten:
            payload["from_forget_layer"] = [h.item.id for h in forgotten]
            payload["note"] = (
                "带「已遗忘」标记的条目来自 0 级遗忘层：常规检索本来已经看不到它们，"
                "是这次召回没捞到东西才去翻出来的。经历被深度回忆命中多次会自动复活。"
            )
        revived = [h.item.id for h in hits if h.revived]
        if revived:
            payload["revived"] = revived
        if format == "full":
            payload["results"] = [h.to_dict() for h in hits]
        return _ok(**payload)

    @tool(
        name="memory_deep_recall",
        description=(
            "深度回忆：只翻遗忘层（0 级）。命中的经历记一次 revive_hits，"
            "攒够次数就自动复活到 1 级。"
        ),
    )
    def memory_deep_recall(
        query: str,
        kinds: list[str] | str | None = None,
        limit: int = 5,
        format: str = "lines",
    ) -> dict[str, Any]:
        """想起一件「以为自己已经忘了」的事。

        只在 0 级里找 —— 正文完整但已退出常规视野的条目。相关性门槛与常规检索
        同一条线（`deep_recall_floor`），不够相关不算「想起」。

        Args:
            query: 想找什么。
            kinds: all / experience / episode 的子集。
            format: lines（默认）| full。
        """
        try:
            result = _st().deep_recall(
                query, kinds=kinds, limit=max(1, min(limit, 20))
            )
        except ScopeError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"深度回忆失败：{type(exc).__name__}: {exc}")
        hits = result["hits"]
        payload: dict[str, Any] = {
            "count": result["count"],
            "lines": [h.to_line() for h in hits],
            "revived": result["revived"],
            "note": result.get("note"),
        }
        if format == "full":
            payload["results"] = [h.to_dict() for h in hits]
        return _ok(**payload)

    # ------------------------------------------------------------- 复盘

    @tool(
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

    @tool(
        name="consolidate_apply",
        description="提交复盘结果：写入提炼的经验与经历，标记日志已处理，可选清空原始日志。"
        "整批前置校验，任一项不合法（放错数组、超长、空内容）就整批拒绝、零写入且原始日志保留；"
        "校验通过后所有写入原子提交，中途失败整体回滚、不删日志。",
    )
    def consolidate_apply(
        day: str | None = None,
        experience: list[dict[str, Any]] | None = None,
        episodes: list[dict[str, Any] | str] | None = None,
        summary: str | None = None,
        purge: bool = True,
    ) -> dict[str, Any]:
        """
        Args:
            day: 复盘哪一天。不传时与 consolidate_preview 对齐，取最近一个仍有
                 待复盘日志的日期；**补历史时务必显式传回 preview 返回的 day**。
            experience: 经验数组，每项 {"content": str, "tags": [str],
                   "importance": float, "confidence": float, "invalidates": "旧id",
                   "source": str}。上限 500 字。可复用的判定与结论放这里；
                   带单号的过程不要放这里 —— 那是 episode。
            episodes: 经历数组。每项 {"content": str, "importance": float, "tags": [str]}，
                   也可直接传字符串。一条一个单一事务；importance 决定初始详细程度
                   （≥0.75 完整叙述 / 0.45~0.75 要点 / <0.45 一句话）。
            summary: 当天一句话总结，只写进 digest 留档，不生成任何记忆条目。
            purge: 是否物理清空已复盘的原始日志，默认 true。只有整批提交成功才会删。

        返回回执含 requested_experience / requested_episodes / created_* /
        skipped_experience / warnings：提交后请核对 created_* 条数是否与请求一致，
        不要只看 ok=true。
        """
        try:
            result = _st().consolidate_apply(
                day=day,
                experience=experience,
                episodes=episodes,
                summary=summary,
                purge=purge,
                model=cfg.embed_model or None,
            )
        except ConsolidationRejected as exc:
            return _err(f"复盘提交被拒绝：{exc}。整批未写入，原始日志保持待复盘")
        except (ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"复盘失败：{type(exc).__name__}: {exc}；已回滚，原始日志未改动")
        return _ok(result=result)

    # ------------------------------------------------------------- 运维

    @app.tool(
        name="memory_use_workspace",
        description="切换当前记忆工作区到指定项目目录。先校验令牌；未接入就返回清单，不建库。",
    )
    def memory_use_workspace(
        path: str, rules: str | None = None, persist: bool = False
    ) -> dict[str, Any]:
        """
        Args:
            path: 项目根目录（不是 .ai_memory 目录）。
            rules: 信条文件路径；不传则自动探测 <项目>/.workbuddy/memory/信条.md。
            persist: 已失效（A3 删了 last-workspace.txt），恒为进程内生效。
        """
        new_session = bindings.resolve_session(path, rules_path=rules)
        if not new_session.ok:
            # 拒绝也是正常返回：带上 code 与清单，agent 直接复述给用户（§6.1）
            return new_session.to_json()
        error = _rebind(new_session)
        if error is not None:
            return _err(error)
        return _ok(
            switched_to=(new_session.layout or _lay()).describe(),
            library=new_session.record.library if new_session.record else None,
            rules=(
                str(new_session.layout.workspace_rules)
                if new_session.layout and new_session.layout.workspace_rules
                else None
            ),
            persisted=False,
            tools_registered=activate(),
        )

    # ------------------------------------------------------------- 绑定（§6.4）

    @app.tool(
        name="memory_bind_status",
        description="报告本工作区的接入状态（四态 + 错误码 + 出路清单），可预检任意路径。",
    )
    def memory_bind_status(workspace: str | None = None) -> dict[str, Any]:
        """
        Args:
            workspace: 可选，任意项目根目录；不传就看当前会话。
        """
        session = (
            bindings.resolve_session(workspace) if workspace else state["session"]
        )
        payload = session.to_json()
        payload["current"] = _lay().describe()
        payload["candidates"] = _candidate_libraries()
        payload["legacy"] = _legacy_hint(session.workspace_root)
        return payload

    @app.tool(
        name="memory_bind_new",
        description="给本工作区新建一个独立库并签发令牌（未接入时才需要）。",
    )
    def memory_bind_new(label: str | None = None, workspace: str | None = None) -> dict[str, Any]:
        """
        Args:
            label: 库名（小写字母/数字/下划线）；不传就用目录名。
            workspace: 可选，项目根目录；不传用当前会话的 root。
        """
        root = _bind_root(workspace)
        if isinstance(root, dict):
            return root
        try:
            return _apply_binding(
                bindings.bind_new(root, label), "已新建库并签发令牌"
            )
        except bindings.BindingError as exc:
            return exc.to_json()

    @app.tool(
        name="memory_bind_adopt",
        description="把本目录已有的 .ai_memory/workspace.db 登记为自有库并签发令牌（存量迁移入口）。",
    )
    def memory_bind_adopt(workspace: str | None = None) -> dict[str, Any]:
        root = _bind_root(workspace)
        if isinstance(root, dict):
            return root
        try:
            return _apply_binding(
                bindings.bind_adopt(root), "已把现有库登记为自有库"
            )
        except bindings.BindingError as exc:
            return exc.to_json()

    @app.tool(
        name="memory_bind_existing",
        description="把本工作区绑到 registry 里已有的库（A、B 共用一个库）。",
    )
    def memory_bind_existing(library: str, workspace: str | None = None) -> dict[str, Any]:
        """
        Args:
            library: 已有库的标签（见 memory_bind_status 的 candidates）。
            workspace: 可选，项目根目录。
        """
        root = _bind_root(workspace)
        if isinstance(root, dict):
            return root
        try:
            return _apply_binding(
                bindings.bind_existing(root, library), f"已并入库 `{library}`"
            )
        except bindings.BindingError as exc:
            return exc.to_json()

    @app.tool(
        name="memory_bind_decline",
        description="明确表示本工作区不接入记忆库（写入 library=null 令牌，之后不再问）。",
    )
    def memory_bind_decline(workspace: str | None = None) -> dict[str, Any]:
        root = _bind_root(workspace)
        if isinstance(root, dict):
            return root
        try:
            refusal = bindings.bind_decline(root)
        except bindings.BindingError as exc:
            return exc.to_json()
        # 拒绝态要把记忆工具收回去：这次是「决定不接入」，不是「暂时没接」
        _unbind(root)
        payload = refusal.to_json()
        payload = refusal.to_json()
        payload["note"] = "已记住这个决定；想改主意随时 memory_bind_new / memory_bind_existing。"
        return payload

    # ------------------------------------------------------------- 生命维持

    @tool(
        name="memory_judge",
        description=(
            "跑一遍到期裁定：按周期活跃度升降遗忘等级。纯机器计算，"
            "不调模型、不动正文。没到期就什么都不写。"
        ),
    )
    def memory_judge(
        scope: str = "all",
        dry_run: bool = False,
        now: int | None = None,
    ) -> dict[str, Any]:
        """把「该升降了」的记忆一次处理完（§5.1）。

        只改三列：`forget_level` / `activity` / `period_start`。
        维持原级也要回写 —— 不清零活跃度它就永远处于「已到期」，每轮重算同一个结论。
        压缩与删除都不在这里：前者是生成任务（`memory_compress_*`），后者只出候选。

        Args:
            scope: 默认 all。全局库已关闭（§8.2），这里只有项目库会跑；传 global 会报错。
            dry_run: true 只列判定结果，不动数据。
            now: 毫秒时间戳，仅测试或补算用；不传取当前时刻。
        """
        try:
            with state_lock:
                return _ok(result=_st().judge_due(scope=scope, dry_run=dry_run, now=now))
        except (ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"裁定失败：{type(exc).__name__}: {exc}")

    @tool(
        name="memory_archive",
        description="按「久未触碰 + 访问少」归档低价值记忆。默认只预览，dry_run=false 才执行。",
    )
    def memory_archive(dry_run: bool = True) -> dict[str, Any]:
        """
        归档 = 默认检索不到，但内容一字未改，`include_archived=true` 仍能查回。
        信条不参与；经验与经历各按自己的 TTL 判据（默认 3 年 / 1 年）：
        「久未触碰 + 访问少」。

        Args:
            dry_run: true（默认）只列候选不动数据；false 才真正归档。
        """
        try:
            with state_lock:
                return _ok(report=_st().archive_pass(dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            return _err(f"归档失败：{type(exc).__name__}: {exc}")

    @tool(name="memory_unarchive", description="把已归档的记忆恢复正常（会重新嵌入）。")
    def memory_unarchive(memory_id: str) -> dict[str, Any]:
        try:
            with state_lock:
                return _ok(result=_st().unarchive(memory_id))
        except (NotFound, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"恢复失败：{type(exc).__name__}: {exc}")

    @tool(
        name="memory_purge_candidates",
        description=(
            "删除候选：已归档超期的 + 落在遗忘层（0 级）超期且没复活够的。"
            "只报告，不删除。"
        ),
    )
    def memory_purge_candidates(after_days: float | None = None) -> dict[str, Any]:
        """
        Args:
            after_days: 归档后静默多少天算候选；不传用配置默认（180 天）。
                遗忘层那一类固定按 `[forget] level0_purge_days` 判，不受此参数影响。
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

    @tool(
        name="memory_purge",
        description=(
            "物理删除指定记忆（含向量）。不可恢复；只接受已归档的、"
            "或落在遗忘层（0 级）且停留超期的。"
        ),
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

    @tool(
        name="memory_compress_preview",
        description=(
            "取待压缩队列：按遗忘等级推出「当前压得不够深」的经历（只读）。"
            "队列为空时这一步直接跳过。"
        ),
    )
    def memory_compress_preview(limit: int = 50) -> dict[str, Any]:
        """
        判据只有一条：`compression_level < required[forget_level]`。
        没有「已处理」标记 —— 压完把 compression_level 写到目标值，自然出队。

        Args:
            limit: 最多返回多少条（默认 50）。
        """
        try:
            return _ok(preview=_st().compression_queue(limit=limit))
        except Exception as exc:  # noqa: BLE001
            return _err(f"查询失败：{type(exc).__name__}: {exc}")

    @tool(
        name="memory_compress_apply",
        description=(
            "把压好的内容**定向覆盖**回原经历（id、at、条目数都不变）。"
            "不要新建压缩版再归档旧条。"
        ),
    )
    def memory_compress_apply(items: list[dict[str, Any]]) -> dict[str, Any]:
        """
        压缩是内容压缩，不是条目合并：改的是 content 与 compression_level。

        三条硬校验：必须是未归档的经历；新内容不能比原文长；level 只能落在
        (当前, 目标] 区间 —— 写超目标会假装压得更深，写回当前值等于没压。

        Args:
            items: [{"id": "e_xxx", "content": "压好的更短版本", "level": 1}]
                `level` 可省略，缺省写入该条 forget_level 对应的目标值。
        """
        try:
            with state_lock:
                return _ok(result=_st().compression_apply(items))
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"压缩失败：{type(exc).__name__}: {exc}")

    @tool(
        name="memory_stats",
        description="查看两库统计：经验/经历/日志数量、待复盘、待嵌入、全局库是否膨胀。",
    )
    def memory_stats() -> dict[str, Any]:
        try:
            data = _st().stats()
        except Exception as exc:  # noqa: BLE001
            return _err(f"统计失败：{type(exc).__name__}: {exc}")
        data["layout"] = _lay().describe()
        data["vec_source"] = _st().vec_source
        return _ok(stats=data)

    @tool(
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
    argv: list[str], layout: Layout, source: str, session: "bindings.Session", cfg: Config
) -> None:
    """把启动时的关键上下文写进诊断文件。

    MCP 协议不提供"当前工作区"信息，所以只能靠观测客户端的行为来确定
    cwd 是什么、解析走了哪条路。这个探针就是为此存在的——出问题时
    先看 `~/.humanity-memory/diagnostics/startup.json`。

    拒绝态也要写：那种时候最需要回答"它到底以为自己在哪儿"。
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
            "bound": session.ok,
            "code": session.code,
            "workspace_root": str(session.workspace_root) if session.workspace_root else None,
            "library": session.record.library if session.record else None,
            "library_dir": str(layout.library_dir) if layout.library_dir else None,
            "rules": str(layout.workspace_rules) if layout.workspace_rules else None,
            "token": str(session.token.path) if session.token else None,
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
    source = "none"

    if token_arg:
        # `--token` 在 v2 里只剩两件事：路径覆盖 + 用令牌里的 workspace 当 hint。
        # 它**不再**是身份来源 —— 是否放行仍由 §5.5 七步校验决定。
        try:
            token = Token.load(resolve_token_path(token_arg))
        except TokenError as exc:
            print(
                f"[humanity-memory] 令牌不可用（{exc.code}）：{exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        workspace_arg = str(token.workspace)
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
        source = "cwd"

    # §5.5 七步校验：任一失败就进拒绝态 —— 不是退出进程，
        # 而是注册一张只剩 bind_* 的工具面（§6.4）。boot 实例 cwd 落在
        # `~/.humanity-memory` 时，整个会话还能自救（R-11）。
    session = bindings.resolve_session(workspace_arg, rules_path=rules_arg)

    if session.ok:
        layout = session.layout or empty_layout(root=session.workspace_root)
        cfg = Config.load(library_dir=layout.library_dir)
        print(
            f"[humanity-memory] workspace: {layout.library_dir} "
            f"(来自 {source}, library={session.record.library if session.record else '?'})",
            file=sys.stderr,
        )
        if layout.workspace_rules is not None:
            print(f"[humanity-memory] rules    : {layout.workspace_rules}", file=sys.stderr)
    else:
        layout = empty_layout(root=session.workspace_root)
        # 拒绝态绝不读库级 toml：那会拿别人的阈值去算（F3）
        cfg = Config.load(library_dir=NO_LIBRARY)
        print(
            f"[humanity-memory] 未接入（{session.code}）：{session.reason}",
            file=sys.stderr,
        )
        for item in session.checklist:
            print(f"[humanity-memory]   {item}", file=sys.stderr)

    if not cfg.embedding_configured:
        print(
            "[humanity-memory] embedding 未配置，退化为关键词检索。"
            "设置 HM_EMBED_BASE_URL / HM_EMBED_MODEL 可启用语义检索。",
            file=sys.stderr,
        )

    _write_probe(argv, layout, source, session, cfg)

    app = build_server(cfg, layout=layout, session=session)
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
