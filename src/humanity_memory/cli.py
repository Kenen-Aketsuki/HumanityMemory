"""命令行工具：信条 / 陈述 / 日志 / 检索 / 复盘 / 自检。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, rules
from .config import Config
from .db import detect_fts_tokenizer, get_meta
from .models import parse_day
from .paths import ensure_rules, resolve_layout
from .store import MemoryStore, NotFound, ScopeError
from .token import DEFAULT_TOKEN, Token, TokenError, build_token, resolve_token_path


def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _short(text: str, width: int = 88) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _layout(args: argparse.Namespace | None = None):
    token_arg = getattr(args, "token", None)
    if token_arg:
        token = Token.load(resolve_token_path(token_arg))
        token.apply_embedding_env()
        return resolve_layout(
            token.workspace, rules_path=str(token.rules) if token.rules else None
        )
    return resolve_layout(
        getattr(args, "workspace", None),
        rules_path=getattr(args, "rules", None),
    )


def _open(args: argparse.Namespace | None = None) -> MemoryStore:
    return MemoryStore(Config.load(), _layout(args))


# ---------------------------------------------------------------- 命令


def cmd_init(args: argparse.Namespace) -> int:
    cfg, layout = Config.load(), _layout(args)
    ensure_rules(layout.global_rules)
    if layout.workspace_rules:
        ensure_rules(layout.workspace_rules)
    with MemoryStore(cfg, layout) as store:
        _dump(
            {
                "ok": True,
                **layout.describe(),
                "global_rules": str(layout.global_rules),
                "workspace_rules": str(layout.workspace_rules) if layout.workspace_rules else None,
                "embedding_available": cfg.embedding_configured,
                "vec_source": store.vec_source,
            }
        )
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    layout = _layout(args)
    ensure_rules(layout.global_rules)
    if args.append:
        target = layout.global_rules if args.scope == "global" else layout.workspace_rules
        if target is None:
            print("当前目录没有 .ai_memory 工作区；写全局信条请加 --scope global", file=sys.stderr)
            return 2
        text = rules.append(target, args.append, section=args.section)
        print(f"已追加到 {target}（{len(text)} 字符）")
        return 0

    _dump(
        {
            "global": rules.describe(layout.global_rules),
            "workspace": rules.describe(layout.workspace_rules) if layout.workspace_rules else None,
        }
    )
    if args.show:
        print()
        print(
            rules.render_instructions(
                global_rules=layout.global_rules, workspace_rules=layout.workspace_rules
            )
        )
    return 0


_KIND_LABEL = {
    "knowledge": "知识",
    "state": "现状",
    "case": "案例",
    "narrative": "经历",
    "log": "日志",
}


def cmd_memory(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.action == "add":
            content = args.text
            if not content and not sys.stdin.isatty():
                content = sys.stdin.read().strip()
            if not content:
                print("错误：需要提供内容（state 纯确认请用 verify 子命令）", file=sys.stderr)
                return 2
            item = store.write_memory(
                content,
                kind=args.kind or "knowledge",
                tags=args.tags,
                importance=args.importance,
                scope=args.scope,
                source=args.source,
                invalidates=args.invalidates,
            )
            out: dict[str, Any] = {
                "ok": True,
                "kind": item.kind,
                "item": item.to_dict(),
                "embedded": item.embedded,
            }
            if args.invalidates:
                out["invalidated"] = args.invalidates
            _dump(out)
            return 0

        if args.action == "list":
            kinds = args.kind
            if kinds in (None, "all"):
                pick = None
            else:
                pick = [kinds]
            if pick is None or set(pick) & {"knowledge", "state"}:
                for item in store.list_knowledge(
                    kinds=pick,
                    scope=args.scope,
                    include_invalid=args.include_invalid,
                    limit=args.limit,
                ):
                    scope = "全局" if item.scope == "global" else "项目"
                    mark = "" if item.live else " [已失效]"
                    label = "知识" if item.kind == "knowledge" else "现状"
                    tag = f" #{' #'.join(item.tags)}" if item.tags else ""
                    print(
                        f"[{item.id}] ({scope}{label}){mark}"
                        f" {_short(item.content, 66)}{tag}"
                    )
            if pick is None or set(pick) & {"case", "narrative"}:
                for ep in store.list_episodes(kinds=pick, limit=args.limit):
                    label = "案例" if ep.is_case else "叙述"
                    print(f"[{ep.id}] ({ep.day}·{label}) {_short(ep.content, 66)}")
            return 0

        if args.action == "verify":
            item = store.verify_state(args.id)
            _dump({"ok": True, "item": item.to_dict()})
            return 0

        if args.action == "invalidate":
            item = store.invalidate(args.id, invalidated_by=args.by)
            _dump({"ok": True, "item": item.to_dict()})
            return 0

        if args.action == "promote":
            old, new_item = store.promote(args.id)
            _dump({"ok": True, "promoted": new_item.to_dict(), "invalidated": old.to_dict()})
            return 0

    return 2


def cmd_log(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.action == "add":
            content = args.text
            if not content and not sys.stdin.isatty():
                content = sys.stdin.read().strip()
            if not content:
                print("错误：需要提供日志内容", file=sys.stderr)
                return 2
            entry = store.append_log(content, tags=args.tags, session_id=args.session)
            _dump({"ok": True, "log": entry.to_dict()})
            return 0

        entries = store.list_logs(
            day=args.day or "today",
            include_consolidated=not args.pending_only,
            limit=args.limit,
        )
        for entry in entries:
            flag = "已复盘" if entry.consolidated else "待复盘"
            print(f"[{entry.id}] {entry.day} ({flag}) {_short(entry.content, 60)}")
        if not entries:
            print("（空）")
        return 0


def cmd_search(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.recall:
            hits = store.recall(args.query, limit=args.limit)
        else:
            hits = store.search(args.query, limit=args.limit, include_invalid=args.history)
        if args.json:
            _dump([h.to_dict() for h in hits])
            return 0
        if not hits:
            print("（无结果）")
            return 0
        for rank, hit in enumerate(hits, 1):
            item = hit.item
            kind = _KIND_LABEL.get(item.kind, item.kind)
            print(f"{rank:2d}. [{item.id}] score={hit.score:.3f} ({kind}) {_short(item.content)}")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.action == "preview":
            preview = store.consolidate_preview(args.day, limit=args.limit)
            if not args.json:
                print(f"日期 {preview['day']}：待复盘日志 {preview['log_count']} 条")
                for log in preview["logs"]:
                    print(f"  [{log['id']}] {log['at_iso']} {_short(log['content'], 60)}")
                existing = preview.get("existing_knowledge") or []
                if existing:
                    print(f"\n现有有效陈述 {len(existing)} 条（避免重复提取）")
                print()
                print(preview["instructions"])
                return 0
            _dump(preview)
            return 0

        payload: dict[str, Any] = {}
        if args.file:
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        elif not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read())
        else:
            print("错误：需要 --file 或从 stdin 提供 JSON", file=sys.stderr)
            return 2

        result = store.consolidate_apply(
            day=args.day,
            knowledge=payload.get("knowledge") or payload.get("facts"),
            episodes=payload.get("episodes"),
            summary=payload.get("summary"),
            purge=not args.keep_raw,
            model=payload.get("model"),
        )
        _dump(result)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with _open(args) as store:
        data = store.stats()
        for name, info in data["dbs"].items():
            print(f"== {name} ==  {info['path']}")
            for key, value in info.items():
                if key == "path":
                    continue
                if isinstance(value, int) and key in ("size_bytes",):
                    value = _human(value)
                print(f"   {key:18s} {value}")
        if data.get("hint"):
            print()
            print(f"提示：{data['hint']}")
        if args.json:
            _dump(data)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load()
    layout = _layout(args)
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "sqlite_lib": sqlite3.sqlite_version,
        "config": cfg.describe(),
        "layout": layout.describe(),
    }

    try:
        store = MemoryStore(cfg, layout)
    except Exception as exc:  # noqa: BLE001
        _dump({"error": str(exc), "all_ok": False})
        return 1

    conn = store.global_conn
    try:
        vec_version = conn.execute("SELECT vec_version()").fetchone()[0]
        checks.append(
            {"check": "sqlite-vec", "ok": True, "detail": f"{vec_version} via {store.vec_source}"}
        )

        tokenizer = detect_fts_tokenizer(conn)
        checks.append(
            {
                "check": "fts5",
                "ok": tokenizer != "none",
                "detail": f"tokenizer={tokenizer}",
            }
        )

        triggers = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        required = {"logs_append_only", "logs_no_delete_unconsolidated", "knowledge_content_immutable"}
        checks.append(
            {
                "check": "append-only",
                "ok": required.issubset(triggers),
                "detail": f"已安装 {len(required & triggers)}/{len(required)} 个约束触发器",
            }
        )

        dims = {t: get_meta(conn, "embedding_dim") for t in ("knowledge_vec",)}
        checks.append(
            {
                "check": "schema",
                "ok": bool(dims["knowledge_vec"]),
                "detail": f"vec_dim={dims['knowledge_vec']} config_dim={cfg.embed_dim} "
                f"version={get_meta(conn, 'schema_version')}",
            }
        )

        probe = store.embedder.probe()
        if not cfg.embedding_configured:
            checks.append(
                {"check": "embedding", "ok": True, "detail": "未配置，退化为关键词检索"}
            )
        else:
            checks.append({"check": "embedding", "ok": bool(probe.get("ok")), "detail": probe})

        rules_info = rules.describe(layout.global_rules)
        checks.append(
            {
                "check": "CORE_RULES",
                "ok": rules_info["exists"],
                "detail": f"全局 {rules_info['items']} 条",
            }
        )

        stats = store.stats()
        for info in stats["dbs"].values():
            info["size"] = _human(info["size_bytes"])
        report["stats"] = stats
    finally:
        store.close()

    report["checks"] = checks
    report["all_ok"] = all(c["ok"] for c in checks)
    _dump(report)
    return 0 if report["all_ok"] else 1


def cmd_reindex(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.reset_vectors:
            store.reset_vectors()
            print("已清空全部向量，准备重建…")
        result = store.backfill_embeddings(limit=10000)
        _dump({"ok": True, **result})
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    path = resolve_token_path(getattr(args, "token", None)) or DEFAULT_TOKEN

    if args.action == "show":
        try:
            token = Token.load(path)
        except TokenError as exc:
            print(f"没有可用的令牌：{exc}", file=sys.stderr)
            return 1
        _dump({"ok": True, "token": token.masked()})
        return 0

    # use：写令牌 + 顺手把工作区建出来
    embedding: dict[str, Any] = {}
    if args.with_embedding:
        cfg = Config.load()
        if cfg.embedding_configured:
            embedding = {
                "base_url": cfg.embed_base_url,
                "api_key": cfg.embed_api_key,
                "model": cfg.embed_model,
                "dim": cfg.embed_dim,
            }
        else:
            print("提示：当前没有可用的 embedding 配置，令牌里不会写入凭据", file=sys.stderr)

    try:
        token = build_token(
            path,
            workspace=args.path,
            rules=getattr(args, "rules", None),
            label=args.label,
            embedding=embedding,
        )
    except TokenError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    token.save()
    layout = resolve_layout(
        token.workspace, rules_path=str(token.rules) if token.rules else None
    )
    _dump(
        {
            "ok": True,
            "token": token.masked(),
            "workspace_db": str(layout.workspace_db),
            "workspace_rules": str(layout.workspace_rules) if layout.workspace_rules else None,
        }
    )
    return 0


def cmd_lifecycle(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.action == "archive":
            report = store.archive_pass(dry_run=args.dry_run)
            if args.json:
                _dump(report)
            else:
                print(f"{'预览' if args.dry_run else '已归档'}：{report['archived_count']} 条"
                      f"（保留 {report['kept']} 条）")
                for item in report["archived"]:
                    print(f"  [{item['type']}] {item['id']} {item['reason']}")
                    print(f"       {item['content']}")
                print(f"\n{report['note']}")
            return 0

        if args.action == "unarchive":
            _dump(store.unarchive(args.id))
            return 0

        if args.action == "candidates":
            items = store.purge_candidates(after_days=args.days)
            if args.json:
                _dump(items)
                return 0
            if not items:
                print("（无删除候选）")
                return 0
            print(f"删除候选 {len(items)} 条（仅报告，未删除）：")
            for it in items:
                print(f"  [{it['type']}] {it['id']} 已归档 {it['idle_days']} 天 · 访问 {it['access_count']} 次")
                print(f"       {it['content']}")
            return 0

        if args.action == "purge":
            if not args.ids:
                print("错误：需要提供至少一个 id", file=sys.stderr)
                return 2
            _dump(store.purge_items(args.ids))
            return 0

        if args.action == "compress":
            preview = store.compress_candidates()
            if args.json:
                _dump(preview)
                return 0
            if not preview["groups"]:
                print("（没有可压缩的经历）")
                return 0
            print(f"可压缩 {preview['total']} 组：")
            for g in preview["groups"]:
                print(f"  {g['day']}  {g['count']} 条（已归档 {g['archived']}）")
                for p in g["preview"]:
                    print(f"     - {p}")
            print(f"\n{preview['note']}")
            return 0

        if args.action == "recompress":
            preview = store.recompress_preview()
            if args.json:
                _dump(preview)
                return 0
            if not preview["groups"]:
                print("（没有需要进一步压缩的经历）")
                return 0
            print(f"需要压缩 {preview['total']} 条：")
            for g in preview["groups"]:
                print(f"  → level {g['target_level']}（超过 {g['age_threshold_days']:.0f} 天）共 {g['count']} 条")
                for it in g["items"][:5]:
                    print(f"     [{it['id']}] level={it['current_level']} {it['age_days']}天  {it['content'][:40]}")
            print(f"\n{preview['note']}")
            return 0

        if args.action == "recompress-apply":
            payload = {}
            if args.file:
                payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            _dump(store.recompress_apply(payload.get("items", []), level=int(payload.get("level", 1))))
            return 0

        if args.action == "compress-apply":
            payload = {}
            if args.file:
                payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            _dump(store.compress_apply(payload.get("ids", []), payload.get("summary", "")))
            return 0

    return 2


def cmd_export(args: argparse.Namespace) -> int:
    """手动导出 Markdown 视图。

    刻意不提供默认输出目录：导出是**人主动要求**的动作，一旦允许自动推导落点，
    就等于给了它一条「悄悄往项目记忆目录写文件」的路径，手写与渲染产物会混在一起。
    """
    if not args.out:
        print(
            "错误：export 是手动命令，必须显式指定 --out 目录（不提供默认落点）。\n"
            "  例：humanity-memory --workspace <项目根> export --out <输出目录>",
            file=sys.stderr,
        )
        return 2
    with _open(args) as store:
        result = store.export_markdown(args.out, include_invalid=args.include_invalid)
        _dump({**result, "files": len(result["files"])})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import mcp_server

    mcp_server.main()
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humanity-memory", description="SQLite + sqlite-vec 三层仿生记忆"
    )
    parser.add_argument("--version", action="version", version=f"humanity-memory {__version__}")
    parser.add_argument("--workspace", default=None, help="项目根目录（跳过 cwd 向上查找）")
    parser.add_argument("--rules", default=None, help="工作区信条文件路径（覆盖默认 CORE_RULES.md）")
    parser.add_argument("--token", default=None, help="令牌文件路径（其优先级高于 --workspace）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化工作区与全局库").set_defaults(func=cmd_init)

    p_rules = sub.add_parser("rules", help="查看/追加信条")
    p_rules.add_argument("--append", default=None, help="追加一条规则")
    p_rules.add_argument("--section", default=None, help="追加到哪个二级标题下")
    p_rules.add_argument("--scope", default="workspace", choices=["workspace", "global"])
    p_rules.add_argument("--show", action="store_true", help="打印渲染后的完整信条")
    p_rules.set_defaults(func=cmd_rules)

    p_mem = sub.add_parser("memory", help="知识 / 现状 / 案例 / 叙述 操作")
    p_mem.add_argument("action", choices=["add", "list", "verify", "invalidate", "promote"])
    p_mem.add_argument("text", nargs="?", default=None, help="add 时的内容")
    p_mem.add_argument(
        "--kind",
        default=None,
        choices=["knowledge", "state", "case", "narrative", "all"],
        help="add 时的类型（默认 knowledge）；list 时不传即全部",
    )
    p_mem.add_argument("--tags", nargs="*", default=None)
    p_mem.add_argument("--importance", type=float, default=None)
    p_mem.add_argument("--scope", default="workspace", choices=["all", "workspace", "global"])
    p_mem.add_argument("--source", default=None, help="来源；写 state 时建议给")
    p_mem.add_argument("--invalidates", default=None, help="add 时指定被取代的旧 id")
    p_mem.add_argument("--include-invalid", action="store_true")
    p_mem.add_argument("--limit", type=int, default=30)
    p_mem.add_argument("--id", default=None, help="verify/invalidate/promote 的目标 id")
    p_mem.add_argument("--by", default=None)
    p_mem.set_defaults(func=cmd_memory)

    p_log = sub.add_parser("log", help="日志操作")
    p_log.add_argument("action", choices=["add", "list"])
    p_log.add_argument("text", nargs="?", default=None)
    p_log.add_argument("--tags", nargs="*", default=None)
    p_log.add_argument("--session", default=None)
    p_log.add_argument("--day", default=None)
    p_log.add_argument("--pending-only", action="store_true", help="只列未复盘的")
    p_log.add_argument("--limit", type=int, default=100)
    p_log.set_defaults(func=cmd_log)

    p_search = sub.add_parser("search", help="检索")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--recall", action="store_true", help="召回并强化命中项")
    p_search.add_argument("--history", action="store_true", help="包含已失效条目")
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_cons = sub.add_parser("consolidate", help="复盘")
    p_cons.add_argument("action", choices=["preview", "apply"])
    p_cons.add_argument("--day", default=None)
    p_cons.add_argument("--limit", type=int, default=300)
    p_cons.add_argument("--json", action="store_true")
    p_cons.add_argument("--file", default=None, help="apply 时读取的 JSON 文件")
    p_cons.add_argument("--keep-raw", action="store_true", help="保留原始日志不清空")
    p_cons.set_defaults(func=cmd_consolidate)

    p_stats = sub.add_parser("stats", help="统计")
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    p_ws = sub.add_parser("workspace", help="令牌：绑定工作区与凭据")
    p_ws.add_argument("action", choices=["show", "use"])
    p_ws.add_argument("path", nargs="?", default=None, help="use 时的项目根目录")
    p_ws.add_argument("--token", default=argparse.SUPPRESS, help="令牌文件路径，默认 ~/.humanity-memory/active.token.json")
    p_ws.add_argument("--rules", default=argparse.SUPPRESS, help="信条文件（默认自动探测）")
    p_ws.add_argument("--label", default=None, help="人类可读名称")
    p_ws.add_argument("--with-embedding", action="store_true", help="把当前 embedding 配置固化进令牌")
    p_ws.set_defaults(func=cmd_workspace)

    p_life = sub.add_parser("lifecycle", help="生命维持：归档 / 删除候选 / 压缩")
    p_life.add_argument(
        "action",
        choices=[
            "archive", "unarchive", "candidates", "purge",
            "compress", "compress-apply", "recompress", "recompress-apply",
        ],
    )
    p_life.add_argument("ids", nargs="*", default=None, help="purge 时的 id 列表")
    p_life.add_argument("--id", default=None, help="unarchive 的目标 id")
    p_life.add_argument("--dry-run", action="store_true", help="archive 时只预览")
    p_life.add_argument("--days", type=float, default=None, help="candidates 的静默天数")
    p_life.add_argument("--file", default=None, help="compress-apply 读取的 JSON")
    p_life.add_argument("--json", action="store_true")
    p_life.set_defaults(func=cmd_lifecycle)

    p_export = sub.add_parser("export", help="手动导出 Markdown 视图（必须指定 --out）")
    p_export.add_argument("--out", default=None, help="输出目录（必填；不提供默认落点，避免自动写入项目记忆目录）")
    p_export.add_argument("--include-invalid", action="store_true")
    p_export.set_defaults(func=cmd_export)

    p_re = sub.add_parser("reindex", help="重建向量")
    p_re.add_argument("--reset-vectors", action="store_true")
    p_re.set_defaults(func=cmd_reindex)

    sub.add_parser("serve", help="以 stdio 启动 MCP server").set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (NotFound, ScopeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
