"""命令行工具：信条 / 陈述 / 日志 / 检索 / 复盘 / 自检。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, rules
from .config import Config, ConfigError
from .db import SchemaVersionError, detect_fts_tokenizer, get_meta
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
    layout = _layout(args)
    return MemoryStore(Config.load(workspace=layout.workspace_dir), layout)


# ---------------------------------------------------------------- 命令


def cmd_init(args: argparse.Namespace) -> int:
    layout = _layout(args)
    cfg = Config.load(workspace=layout.workspace_dir)
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
    "experience": "经验",
    "episode": "经历",
    "log": "日志",
}


def cmd_memory(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.action == "add":
            content = args.text
            if not content and not sys.stdin.isatty():
                content = sys.stdin.read().strip()
            if not content:
                print("错误：需要提供内容", file=sys.stderr)
                return 2
            item = store.write_memory(
                content,
                kind=args.kind or "experience",
                tags=args.tags,
                importance=args.importance,
                confidence=args.confidence,
                scope=args.scope,
                source=args.source,
                invalidates=args.invalidates,
                allow_duplicate=getattr(args, "allow_duplicate", False),
            )
            out: dict[str, Any] = {
                "ok": True,
                "kind": item.kind,
                "item": item.to_dict(),
                "embedded": item.embedded,
            }
            if getattr(item, "deduped", False):
                out["deduped"] = True
                out["note"] = "与既有条目重复，未新建；已给它加活跃度。要覆盖请带 --invalidates"
            if args.invalidates:
                out["invalidated"] = args.invalidates
            _dump(out)
            return 0

        if args.action == "list":
            pick = None if args.kind in (None, "all") else args.kind
            if pick in (None, "experience"):
                for item in store.list_experience(
                    scope=args.scope,
                    include_invalid=args.include_invalid,
                    limit=args.limit,
                ):
                    scope = "全局" if item.scope == "global" else "项目"
                    mark = "" if item.live else " [已失效]"
                    tag = f" #{' #'.join(item.tags)}" if item.tags else ""
                    print(
                        f"[{item.id}] ({scope}经验){mark}"
                        f" {_short(item.content, 66)}{tag}"
                    )
            if pick in (None, "episode"):
                for ep in store.list_episodes(limit=args.limit):
                    print(f"[{ep.id}] ({ep.day}·经历) {_short(ep.content, 66)}")
            return 0

        if args.action == "verify":
            result = store.verify(
                args.id, ok=not args.stale, level=args.level or "L1"
            )
            _dump({"ok": True, "result": result})
            return 0

        if args.action == "invalidate":
            item = store.invalidate(args.id, invalidated_by=args.by)
            _dump({"ok": True, "item": item.to_dict()})
            return 0

        if args.action == "promote":
            try:
                store.promote(args.id)
            except ValueError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
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
        if args.deep:
            result = store.deep_recall(args.query, limit=args.limit)
            hits = result["hits"]
            if args.json:
                _dump({"hits": [h.to_dict() for h in hits], "revived": result["revived"]})
                return 0
            if not hits:
                print("（遗忘层里也没有相关记忆）")
                return 0
            print(f"遗忘层命中 {result['count']} 条：")
            for rank, hit in enumerate(hits, 1):
                kind = _KIND_LABEL.get(hit.item.kind, hit.item.kind)
                rev = " ← 已复活" if hit.revived else ""
                print(f"{rank:2d}. [{hit.item.id}] ({kind}·已遗忘) {_short(hit.item.content)}{rev}")
            return 0

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
            experience=payload.get("experience"),
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


def cmd_config(args: argparse.Namespace) -> int:
    """生成**安装位置**的全量配置模板（`<安装根>/memory.toml`）。

    这一层与 `.env` 同目录、跨版本共享，定位是「这套安装的完整基线」：
    整份生成，让人不用翻进包目录就能看到所有可调项。
    用户级 / 项目级那两层是增量，只写要改的键。
    """
    from .config import DEFAULT_CONFIG_NAME, install_config_file
    from .resources import resource

    target = install_config_file()
    source = resource(DEFAULT_CONFIG_NAME)
    if not source.is_file():
        print(f"错误：找不到出厂配置 {source}", file=sys.stderr)
        return 2

    if target.exists() and not args.force:
        print(
            f"已存在：{target}\n"
            "这份是全量基线，覆盖会丢掉你改过的值。确认要重来就加 --force。",
            file=sys.stderr,
        )
        return 2

    text = source.read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"已生成全量配置模板：{target}")
    print(f"  {len(text.splitlines())} 行，含全部可调项与注释")
    print()
    print("配置层（低 → 高，后者覆盖前者）：")
    print("  出厂    包内 memory.default.toml（全量，随版本更新）")
    print("  安装    上一行那个路径（全量，你现在这份）")
    print("  用户    ~/.humanity-memory/memory.toml（增量：只写要改的键）")
    print("  项目    <项目>/.ai_memory/memory.toml（增量，优先级最高）")
    print()
    print("改完要重启 MCP 客户端才生效（server 是常驻进程，配置只在启动时读一次）。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    layout = _layout(args)
    # 配置校验已通过 —— Config.load 不通过会直接抛错，走不到这里
    cfg = Config.load(workspace=layout.workspace_dir)
    checks: list[dict[str, Any]] = [
        {
            "check": "config",
            "ok": True,
            "detail": (
                f"{len(cfg.layers)} 层生效（{' > '.join(n for n, _ in reversed(cfg.layers))}）"
                f"，等级 {len(cfg.forget_levels)} 档（顶档 {cfg.max_level}）"
            ),
        }
    ]
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "sqlite_lib": sqlite3.sqlite_version,
        "config": cfg.describe(),
        "settings": cfg.describe_settings(),
        "layout": layout.describe(),
    }

    try:
        store = MemoryStore(cfg, layout)
    except Exception as exc:  # noqa: BLE001
        _dump({"error": str(exc), "all_ok": False})
        return 1

    # 自检全部落在项目库上：全局库已关闭（§8.2），不再有第二个库可查
    conn = store.workspace_conn
    if conn is None:
        _dump({"error": "当前目录没有 .ai_memory 工作区，doctor 需要项目库", "all_ok": False})
        return 1
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

        if args.action == "judge":
            report = store.judge_due(
                scope=args.scope, dry_run=args.dry_run, now=args.now
            )
            if args.json:
                _dump(report)
                return 0
            if report["due"] == 0:
                print("（没有到期的裁定项，一个字节都没写）")
                return 0
            head = "预览" if report["dry_run"] else "已裁定"
            print(f"{head} {report['due']} 条：")
            for it in report["items"]:
                arrow = {"up": "↑", "down": "↓", "hold": "="}.get(it["action"], "?")
                print(f"  {arrow} [{it['id']}] {it['kind']} {it['from']}→{it['to']}  {it['reason']}")
            if not report["dry_run"]:
                print(f"\n落盘 {report['settled']} 条，活跃度已清零、周期起点推到现在。")
            return 0

        if args.action == "relevel":
            report = store.relevel_by_importance(
                now=args.now, dry_run=args.dry_run, include_level0=args.include_level0
            )
            if args.json:
                _dump(report)
                return 0
            head = "预览" if report["dry_run"] else "已重算"
            print(
                f"{head} {report['touched']} 条，其中 {report['changed']} 条等级变化"
                f"（按 bands {[(b['min_importance'], b['level']) for b in report['bands']]}）"
            )
            for mv in report["moves"]:
                print(
                    f"  [{mv['id']}] {mv['table']} L{mv['from']}→L{mv['to']}"
                    f"  importance={mv['importance']}"
                )
            print(f"\n{report['note']}")
            if report["dry_run"]:
                print("落地请去掉 --dry-run。")
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
                if it["reason"] == "archived":
                    print(
                        f"  [{it['type']}] {it['id']} 归档 {it['idle_days']} 天"
                        f" · 访问 {it['access_count']} 次"
                    )
                else:
                    print(
                        f"  [{it['type']}] {it['id']} 遗忘层 {it['idle_days']} 天"
                        f" · 深度命中 {it['revive_hits']} 次"
                    )
                print(f"       {it['content']}")
            return 0

        if args.action == "purge":
            if not args.ids:
                print("错误：需要提供至少一个 id", file=sys.stderr)
                return 2
            _dump(store.purge_items(args.ids))
            return 0

        if args.action == "compress":
            queue = store.compression_queue()
            if args.json:
                _dump(queue)
                return 0
            if not queue["items"]:
                print("（队列为空：没有压得不够深的经历）")
                return 0
            print(f"待压缩 {queue['total']} 条（按目标级别：{queue['by_target']}）：")
            for it in queue["items"]:
                print(
                    f"  [{it['id']}] L{it['forget_level']} "
                    f"压缩 {it['current_level']}→{it['target_level']}  "
                    f"{it['length']} 字  {it['content'][:34]}"
                )
            print(f"\n{queue['note']}")
            return 0

        if args.action == "compress-apply":
            payload = {}
            if args.file:
                payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            _dump(store.compression_apply(items))
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


def cmd_starmap(args: argparse.Namespace) -> int:
    """CLI 专用的只读星图；延迟 import，未调用时零开销、零副作用。"""
    from .starmap import run

    return run(args)


def cmd_upgrade(args: argparse.Namespace) -> int:
    """离线升级历史库。CLI 专用，不注册 MCP tool（§8.4 边界 1）。"""
    from .upgrade import UpgradeError, run

    try:
        if getattr(args, "in_place", False):
            # 就地升级是叠加在转换器之上的一层：转换仍只读，只有校验全过才换文件
            from .upgrade.inplace import run_inplace

            report = run_inplace(args.path)
        else:
            report = run(args.path, args.out)
    except UpgradeError as exc:
        print(f"升级失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        _dump(report)
    else:
        _print_upgrade(report)
    return 0 if report.get("ok") else 1


def _print_upgrade(report: dict[str, Any]) -> None:
    print(f"源库：{report['source']}")
    print(f"版本：v{report.get('source_version')} → v{report.get('target_version', '?')}")

    if report.get("noop"):
        print(f"\n· {report.get('reason')}")
        return

    chain = report.get("chain") or []
    if chain:
        print(f"路径：{' → '.join(chain)}")

    plans = report.get("plans") or ([{"plan": report.get("plan")}] if report.get("plan") else [])
    for step in plans:
        plan = step.get("plan") or {}
        if step.get("from") is not None:
            print(f"\n· v{step['from']} → v{step['to']} 计划迁移：")
        counts = plan.get("counts") or {}
        for table, n in counts.items():
            print(f"  {table:<12} {n:>5} 条")
        if plan.get("target_counts"):
            print(
                "  目标条数："
                + ", ".join(f"{k}={v}" for k, v in plan["target_counts"].items())
            )
        if plan.get("by_kind"):
            print("  按旧 kind：" + ", ".join(f"{k}={v}" for k, v in plan["by_kind"].items()))
        if plan.get("level_plan"):
            print("  遗忘等级：" + ", ".join(f"{k}={v}" for k, v in plan["level_plan"].items()))
        if plan.get("confidence_plan"):
            print(
                "  可信度："
                + ", ".join(f"{k}={v}" for k, v in plan["confidence_plan"].items())
            )
        if plan.get("vectors"):
            print(
                "  向量："
                + ", ".join(
                    f"{k}={v}"
                    + (f"(dim {plan['vec_dims'].get(k)})" if plan.get("vec_dims", {}).get(k) else "")
                    for k, v in plan["vectors"].items()
                )
            )

    if report.get("dry_run"):
        print(f"\n{report.get('next')}")
        return

    if not report.get("ok"):
        print(f"\n✗ {report.get('reason', '未完成')}")
        return

    written = report.get("written") or {}
    if not report.get("in_place"):
        # 就地模式下产物就是源库本身，单列一行"产物"会让人以为写出了第二个文件
        print(f"\n产物：{report['out']}")
    else:
        print()
    for table in ("knowledge", "episodes", "logs", "digests", "memory_events"):
        info = written.get(table) or {}
        if not info:
            continue
        extra = f"，跳过 {len(info['skipped'])}" if info.get("skipped") else ""
        if info.get("moved_to_experience"):
            extra += f"，其中 {info['moved_to_experience']} 条 case 搬入经验"
        print(
            f"  {table:<13} 写入 {info.get('written', 0)}/{info.get('read', 0)}{extra}"
        )
    if written.get("vectors"):
        print("  向量写入：" + ", ".join(f"{k}={v}" for k, v in written["vectors"].items()))

    checks = report.get("checks") or {}
    print("\n校验：")
    per_step = checks.get("per_step")
    if per_step and len(per_step) > 1:
        print(f"  逐段通过     {per_step}")
    print(f"  条数对齐     {'✓' if checks.get('counts_ok') else '✗'}")
    print(f"  时间戳未漂移 {'✓' if checks.get('timestamps_ok') else '✗'}")
    print(f"  向量可搜     {'✓' if (checks.get('vectors') or {}).get('ok') else '✗'}")
    for note in checks.get("notes") or []:
        print(f"  · {note}")
    if report.get("ok") and report.get("swapped"):
        print(f"\n全部通过，已就地升级：{report['source']}")
        print(f"原件留档：{report['archived']}（要回退就把它改回原名）")
    elif report.get("ok"):
        print("\n全部通过。源库原样保留，确认无误后自行替换：")
        print(f'  mcp 指向的文件 → "{report["out"]}"')



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

    p_mem = sub.add_parser("memory", help="经验 / 经历 操作")
    p_mem.add_argument(
        "action", choices=["add", "list", "verify", "invalidate", "promote"],
        # promote 已下线（§8.2），保留这个动作只为给出明确报错而不是「未知命令」
    )
    p_mem.add_argument("text", nargs="?", default=None, help="add 时的内容")
    p_mem.add_argument(
        "--kind",
        default=None,
        choices=["experience", "episode", "all"],
        help="add 时的类型（默认 experience）；list 时不传即两类都列",
    )
    p_mem.add_argument("--tags", nargs="*", default=None)
    p_mem.add_argument("--importance", type=float, default=None)
    p_mem.add_argument(
        "--confidence", type=float, default=None,
        help="add 写经验时的初始可信度（0~1）；易变的结论给低值",
    )
    p_mem.add_argument("--scope", default="workspace", choices=["all", "workspace", "global"])
    p_mem.add_argument("--source", default=None, help="来源，写经验时建议给")
    p_mem.add_argument("--invalidates", default=None, help="add 时指定被取代的旧 id")
    p_mem.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="add 写经验时跳过查重：措辞相似但确实是另一条结论才用",
    )
    p_mem.add_argument("--include-invalid", action="store_true")
    p_mem.add_argument("--limit", type=int, default=30)
    p_mem.add_argument("--id", default=None, help="verify/invalidate/promote 的目标 id")
    p_mem.add_argument(
        "--stale",
        action="store_true",
        help="verify 时表示「这条已经不成立了」：按同一条幂律扣分；不传表示证实，按间隔加分",
    )
    p_mem.add_argument(
        "--level", default="L1", choices=["L1", "L2", "L3"],
        help="verify 的证据级别：L1 可观测动作 / L2 复盘判定 / L3 听说（恒 0 分）",
    )
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
    p_search.add_argument(
        "--deep", action="store_true",
        help="深度回忆：只翻遗忘层（0 级），命中的经历会累计复活次数",
    )
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

    p_cfg = sub.add_parser("config", help="生成安装位置的全量配置模板")
    p_cfg.add_argument(
        "action", choices=["template"], nargs="?", default="template",
        help="template：把出厂默认整份写到 <安装根>/memory.toml",
    )
    p_cfg.add_argument("--force", action="store_true", help="已存在也覆盖（会丢掉你的修改）")
    p_cfg.set_defaults(func=cmd_config)

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
            "judge", "archive", "unarchive", "candidates", "purge",
            "compress", "compress-apply", "relevel",
        ],
    )
    p_life.add_argument("ids", nargs="*", default=None, help="purge 时的 id 列表")
    p_life.add_argument("--id", default=None, help="unarchive 的目标 id")
    p_life.add_argument(
        "--dry-run", action="store_true", help="archive / judge / relevel 时只预览"
    )
    p_life.add_argument(
        "--include-level0",
        action="store_true",
        help="relevel 时把 0 级遗忘层也按 importance 拉回（默认跳过它的复活通道）",
    )
    p_life.add_argument("--days", type=float, default=None, help="candidates 的静默天数")
    p_life.add_argument(
        "--now", type=int, default=None, help="judge 的判定时刻（毫秒），补算或复现用"
    )
    p_life.add_argument(
        "--scope", default="all", choices=["all", "workspace", "global"],
        help="judge 的作用范围（默认两个库都跑）",
    )
    p_life.add_argument("--file", default=None, help="compress-apply 读取的 JSON：[{id,content,level?}]")
    p_life.add_argument("--json", action="store_true")
    p_life.set_defaults(func=cmd_lifecycle)

    p_export = sub.add_parser("export", help="手动导出 Markdown 视图（必须指定 --out）")
    p_export.add_argument("--out", default=None, help="输出目录（必填；不提供默认落点，避免自动写入项目记忆目录）")
    p_export.add_argument("--include-invalid", action="store_true")
    p_export.set_defaults(func=cmd_export)

    p_re = sub.add_parser("reindex", help="重建向量")
    p_re.add_argument("--reset-vectors", action="store_true")
    p_re.set_defaults(func=cmd_reindex)

    p_star = sub.add_parser(
        "starmap", help="只读星图：分析目录里的记忆库，生成单文件 HTML（CLI 专用）"
    )
    p_star.add_argument("path", help="拖入的目录（.ai_memory / 项目根），或直接给 workspace.db")
    p_star.add_argument("--out", required=True, help="HTML 输出路径（必填，不提供默认落点）")
    p_star.add_argument("--min-score", type=float, default=0.55, help="语义边最低相似度")
    p_star.add_argument("--top-k", type=int, default=6, help="每个节点最多保留几条语义边")
    p_star.add_argument("--max-nodes", type=int, default=400, help="全对相似度计算的节点上限")
    p_star.set_defaults(func=cmd_starmap)

    p_up = sub.add_parser(
        "upgrade",
        help="升级历史记忆库（离线；--out 只产出、--in-place 就地换库；不对 AI 开放）",
    )
    p_up.add_argument("path", help="旧库路径，或 .ai_memory / 项目根目录")
    mode = p_up.add_mutually_exclusive_group()
    mode.add_argument(
        "--out",
        default=None,
        help="新库输出路径（不提供默认值：不给就只预览，不产出任何文件）",
    )
    mode.add_argument(
        "--in-place",
        dest="in_place",
        action="store_true",
        help="就地升级：产出 → 校验 → 原件留档 → 替换，一步完成（拖拽 bat 用这条）",
    )
    p_up.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    p_up.set_defaults(func=cmd_upgrade)

    sub.add_parser("serve", help="以 stdio 启动 MCP server").set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        # 配置写错要早失败：退出码 2 与「运行期错误 1」分开，脚本能区分
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except SchemaVersionError as exc:
        # 库版本不符是「需要人拿扳手」的状态，用独立退出码，避免被当成普通崩溃
        print(f"库版本不符，已拒绝服务（未修改任何文件）：\n{exc}", file=sys.stderr)
        return 3
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
