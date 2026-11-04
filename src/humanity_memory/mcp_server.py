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
    find_project_root,
    find_workspace_dir,
    read_last_workspace,
    resolve_layout,
    write_last_workspace,
)
from .store import MemoryStore, NotFound, ScopeError
from .token import Token, TokenError, resolve_token_path

INSTRUCTIONS_TAIL = """\
记忆使用纪律：
- 事实（fact）是长期结论，写简短陈述句；被推翻时追加新事实并用 invalidates 指向旧事实，
  禁止改写或删除历史。
- 日志（log）是当天的原始记录，只追加、不改写。白天对话里写日志，别急着写事实。
- 复盘时先用 consolidate_preview 取素材，总结后用 consolidate_apply 提交。
- 一个 server 可以服务多个项目：如果在某个具体项目里工作，而 memory_stats 显示的工作区
  不是你预期的那个，先调用 memory_use_workspace 传入项目根目录再继续。
"""


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


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

    # ------------------------------------------------------------- 事实

    @app.tool(
        name="fact_write",
        description="写入长期事实（简短陈述句）。推翻旧事实时用 invalidates 传旧 id，不要改旧记录。",
    )
    def fact_write(
        content: str,
        tags: list[str] | None = None,
        importance: float | None = None,
        scope: str = "workspace",
        invalidates: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            content: 一句话事实。必须简短精确，不要写事件过程；超过 500 字会被拒。
            tags: 标签，用于过滤。
            importance: 0~1，默认 0.8。
            scope: workspace（项目）| global（跨项目，数量要很少）。
            invalidates: 被这条事实推翻的旧事实 id。
            source: 来源标识。
        """
        try:
            fact = _st().write_fact(
                content, tags=tags, importance=importance, scope=scope, source=source
            )
        except (ScopeError, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败：{type(exc).__name__}: {exc}")

        invalidated = None
        warning = None
        if invalidates:
            try:
                invalidated = _st().invalidate_fact(
                    invalidates, invalidated_by=fact.id
                ).to_dict()
            except NotFound as exc:
                warning = f"新事实已写入，但旧事实取消失败：{exc}"

        return _ok(
            fact=fact.to_dict(),
            invalidated=invalidated,
            embedded=fact.embedded,
            warning=warning,
            hint=None if fact.embedded else "embedding 不可用，当前仅能被关键词检索",
        )

    @app.tool(
        name="fact_invalidate",
        description="让一条事实失效（不删除，保留历史）。用于事实过时但暂无替代的情况。",
    )
    def fact_invalidate(fact_id: str, invalidated_by: str | None = None) -> dict[str, Any]:
        try:
            fact = _st().invalidate_fact(fact_id, invalidated_by=invalidated_by)
        except NotFound as exc:
            return _err(str(exc))
        return _ok(fact=fact.to_dict())

    @app.tool(
        name="fact_promote",
        description="把项目事实提升为全局事实（跨项目可用）。旧条目标记失效并指向新条目。",
    )
    def fact_promote(fact_id: str) -> dict[str, Any]:
        try:
            old, new = _st().promote_fact(fact_id)
        except (NotFound, ValueError, ScopeError) as exc:
            return _err(str(exc))
        stats = _st().stats()
        return _ok(
            promoted=new.to_dict(),
            invalidated=old.to_dict(),
            global_facts=stats.get("global_facts"),
            warning=stats.get("hint"),
        )

    @app.tool(name="fact_list", description="列出事实，默认只列有效的。")
    def fact_list(
        scope: str = "all", include_invalid: bool = False, limit: int = 30
    ) -> dict[str, Any]:
        """
        Args:
            scope: all | workspace | global
        """
        try:
            facts = _st().list_facts(
                scope=scope, include_invalid=include_invalid, limit=max(1, min(limit, 200))
            )
        except ScopeError as exc:
            return _err(str(exc))
        return _ok(count=len(facts), facts=[f.to_dict() for f in facts])

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
        description="跨层混合检索（事实 + 经历 + 日志，自动含全局事实）。不改变访问强度。",
    )
    def memory_search(
        query: str,
        kinds: list[str] | None = None,
        scope: str = "all",
        include_invalid: bool = False,
        limit: int = 8,
    ) -> dict[str, Any]:
        """
        Args:
            query: 自然语言查询。
            kinds: fact / episode / log 的子集，默认全部。
            scope: all | workspace | global
            include_invalid: 是否包含已失效事实（查历史用）。
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
        return _ok(
            count=len(hits),
            results=[h.to_dict() for h in hits],
            degraded=None if _st().embedder.available else "embedding 不可用，本次为关键词检索",
        )

    @app.tool(
        name="memory_recall",
        description="按当前话题召回并强化命中项。返回可直接拼进上下文的 lines。",
    )
    def memory_recall(
        context: str, kinds: list[str] | None = None, limit: int = 6
    ) -> dict[str, Any]:
        """
        Args:
            context: 当前在做什么。写具体情境，别只写关键词。
        """
        try:
            hits = _st().recall(context, kinds=kinds, limit=max(1, min(limit, 20)))
        except ScopeError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"召回失败：{type(exc).__name__}: {exc}")
        return _ok(
            count=len(hits),
            lines=[h.to_line() for h in hits],
            results=[h.to_dict() for h in hits],
        )

    # ------------------------------------------------------------- 复盘

    @app.tool(
        name="consolidate_preview",
        description="取出某天待复盘的原始日志供你总结。只读，不改任何数据。",
    )
    def consolidate_preview(day: str | None = None, limit: int = 300) -> dict[str, Any]:
        """
        Args:
            day: YYYY-MM-DD / today / yesterday，默认今天。
        """
        try:
            return _ok(
                preview=_st().consolidate_preview(day, limit=max(1, min(limit, 1000)))
            )
        except (ValueError, ScopeError) as exc:
            return _err(str(exc))

    @app.tool(
        name="consolidate_apply",
        description="提交复盘结果：写入提炼的事实与压缩经历，标记日志已处理，可选清空原始日志。",
    )
    def consolidate_apply(
        day: str | None = None,
        facts: list[dict[str, Any]] | None = None,
        episodes: list[str] | None = None,
        summary: str | None = None,
        purge: bool = True,
    ) -> dict[str, Any]:
        """
        Args:
            day: 复盘哪一天，默认今天。
            facts: 事实数组，每项 {"content": str, "tags": [str],
                   "importance": float, "invalidates": "旧fact_id"}。
            episodes: 压缩后的经历叙述，1~3 段。
            summary: 当天一句话总结（可选）。
            purge: 是否物理清空已复盘的原始日志，默认 true。
        """
        try:
            result = _st().consolidate_apply(
                day=day,
                facts=facts,
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

    @app.tool(
        name="memory_stats",
        description="查看两库统计：事实/经历/日志数量、待复盘、待嵌入、全局事实是否膨胀。",
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
