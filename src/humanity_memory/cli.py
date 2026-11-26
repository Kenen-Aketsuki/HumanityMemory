"""命令行工具：信条 / 陈述 / 日志 / 检索 / 复盘 / 自检。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from . import __version__, rules
from . import keys as key_registry
from .config import Config, ConfigError, libraries_home
from .db import SchemaVersionError, detect_fts_tokenizer, get_meta
from .models import parse_day
from .paths import ensure_rules, find_workspace_dir, resolve_layout
from .store import MemoryStore, NotFound, ScopeError
from .token import Token, TokenError, build_token, resolve_token_path, same_path, token_path


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


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格：CJK 占两列，用 len() 拼出来的表必然歪。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _layout(args: argparse.Namespace | None = None):
    """解析库布局：**一律走准入层**（令牌 + registry），不再按路径猜。

    2026-11-19 起库统一放在 `<安装根>/libraries/<label>`，`<项目>/.ai_memory`
    里只剩 `token.json`。如果这里还按路径解析，拿到的是一个"没有库的目录"，
    `MemoryStore` 一打开就在那儿造一个空库 —— 星图 / 报告看到的就是
    「有 token 没库」，而写进去的东西也没人知道跑哪去了。
    """
    from . import bindings

    token_arg = getattr(args, "token", None)
    hint = getattr(args, "workspace", None)
    if token_arg:
        try:
            hint = str(Token.load(resolve_token_path(token_arg)).workspace)
        except TokenError as exc:
            _dump({"ok": False, "code": exc.code, "error": str(exc)})
            raise SystemExit(2) from exc

    session = bindings.resolve_session(hint, rules_path=getattr(args, "rules", None))
    if not session.ok or session.layout is None:
        # 拒绝也是正常结果：把同一份 code + 清单打出来，交给用户照做
        _dump(session.to_json())
        raise SystemExit(2)
    return session.layout


def _open(args: argparse.Namespace | None = None) -> MemoryStore:
    layout = _layout(args)
    return MemoryStore(Config.load(library_dir=layout.workspace_dir), layout)


def _open_readonly(args: argparse.Namespace | None = None) -> MemoryStore:
    """只读工具的打开方式：库目录 / .db / 令牌文件 都认，不要求 registry。

    只读工具不该因为"这台机器还没接入"就打不开一份别人拷过来的库。
    """
    from . import bindings

    target = getattr(args, "workspace", None) or getattr(args, "token", None)
    layout = bindings.resolve_readonly(target)
    return MemoryStore(Config.load(library_dir=layout.library_dir), layout)


def _review_gate(args: argparse.Namespace) -> tuple[Config, Any]:
    """CLI 检索入口的闸门：(cfg, gate.State)。

    与 MCP 侧共用 `gate.state` —— 两边判据必须同一份，否则终端能搜、
    agent 搜不了（或反过来），活跃度照样算不准。
    """
    from . import gate

    layout = _layout(args)
    cfg = Config.load(library_dir=layout.workspace_dir)
    return cfg, gate.state(cfg)


# ---------------------------------------------------------------- 命令


def cmd_init(args: argparse.Namespace) -> int:
    """只做「补齐信条 + 打开已有库自检」。**不建库** —— 建库是 bind 的事。"""
    from . import bindings

    session = bindings.resolve_session(getattr(args, "workspace", None))
    if not session.ok:
        payload = session.to_json()
        payload["hint"] = (
            "init 不再自动建库：接入用 `bind adopt`（沿用已有库）或 `bind new`（新建）。"
        )
        _dump(payload)
        return 1

    layout = session.layout
    assert layout is not None and layout.library_dir is not None
    cfg = Config.load(library_dir=layout.library_dir)
    ensure_rules(layout.global_rules)
    if layout.workspace_rules:
        ensure_rules(layout.workspace_rules)
    with MemoryStore(cfg, layout) as store:
        _dump(
            {
                "ok": True,
                **layout.describe(),
                "library": session.record.library if session.record else None,
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
    # recall / deep 是「使用记忆」（会强化命中项 / 累计 revive_hits），不受闸门限制。
    # 裸 search 刻意不改访问强度 —— 日常拿它顶替 recall，活跃度就会算成空的。
    if not (args.recall or args.deep):
        from . import gate

        cfg, gate_state = _review_gate(args)
        if not gate_state.open:
            gate.audit(
                cfg.home,
                "search_blocked",
                source=gate_state.source,
                window=gate_state.window,
                next_open=gate_state.next_open,
                query=args.query[:80],
                entry="cli",
            )
            if args.json:
                _dump({"ok": False, "code": "REVIEW_REQUIRED", **gate_state.to_dict()})
            else:
                print(f"[REVIEW_REQUIRED] {gate_state.message()}", file=sys.stderr)
                print("  复盘需在真终端签发通行证：humanity-memory review begin", file=sys.stderr)
                print("  日常检索改用：humanity-memory search <query> --recall", file=sys.stderr)
            return 4

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


def cmd_review(args: argparse.Namespace) -> int:
    """复盘闸门：查看状态 / 签发通行证 / 提前收回。

    通行证由**人**签发，不由 agent 签发 —— `begin` 要求 stdin 是 TTY。
    这条限制是整件事的地基：终端是 TTY，agent 的 Bash 管道不是。
    """
    from . import gate

    layout = _layout(args)
    cfg = Config.load(library_dir=layout.workspace_dir)
    action = getattr(args, "action", None) or "status"

    if action == "status":
        state = gate.state(cfg)
        if args.json:
            _dump({**state.to_dict(), "flag": str(gate.flag_path(cfg.home))})
            return 0
        print(f"search 放行：{'是' if state.open else '否'}（来源 {state.source}）")
        print(f"  窗口    ：{state.window or '未启用'}")
        if state.expires_at:
            print(f"  到期    ：{state.expires_at}")
        if not state.open and state.next_open:
            print(f"  下次开放：{state.next_open}")
        print(f"  flag    ：{gate.flag_path(cfg.home)}")
        print(f"  签发时长：{cfg.review_flag_minutes} 分钟（默认）")
        if cfg.review_allow_flag:
            print(
                f"  手签通道：开（今日已签 {gate.count_flag_today(cfg.home)}"
                f" / {cfg.review_daily_limit}）"
            )
        else:
            print("  手签通道：已关闭（只认时间窗）")
        return 0

    if action == "end":
        gate.close_flag(cfg.home)
        gate.audit(cfg.home, "review_end")
        print("通行证已收回。search 回到「只认时间窗」。")
        return 0

    # ---- begin ----
    if not cfg.review_allow_flag:
        gate.audit(cfg.home, "review_begin_denied", reason="flag-disabled")
        print(
            "[REVIEW_DENIED] 手签通道已关闭（[review].allow_flag = false）。\n"
            "  现在只认时间窗；要临时开放请改 [review].window。",
            file=sys.stderr,
        )
        return 4

    used = gate.count_flag_today(cfg.home)
    if used >= cfg.review_daily_limit:
        gate.audit(cfg.home, "review_begin_denied", reason="daily-limit", used=used)
        print(
            f"[REVIEW_DENIED] 今天已签发 {used} 张通行证（上限 {cfg.review_daily_limit}）。\n"
            "  配额是天花板：就算绕过了 TTY 判断，也滥用不了。",
            file=sys.stderr,
        )
        return 4

    if cfg.review_require_tty and not gate.is_interactive():
        gate.audit(cfg.home, "review_begin_denied", reason="no-tty")
        print(
            "[REVIEW_DENIED] review begin 必须在交互终端里执行。\n"
            "  原因：通行证由人签发；管道 stdin（脚本 / agent / 定时任务）没有这个资格。",
            file=sys.stderr,
        )
        return 4

    try:
        minutes = gate.parse_minutes(
            getattr(args, "minutes", None), cfg.review_flag_minutes
        )
    except gate.GateError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = gate.open_flag(
        cfg.home, minutes, reason=getattr(args, "reason", None)
    )
    gate.audit(
        cfg.home,
        "review_begin",
        minutes=minutes,
        expires_at=payload["expires_at"],
        reason=payload.get("reason") or "",
    )
    print(f"复盘通行证已签发：{payload['expires_at']} 前 memory_search 可用（{minutes} 分钟）。")
    print("  到期自动失效；提前收回：humanity-memory review end")
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
    用户级 / 库级那两层是增量，只写要改的键。
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
    print("  库      <库目录>/memory.toml（增量，优先级最高：自有库 = .ai_memory，共享库 = libraries/<名>）")
    print()
    print("改完要重启 MCP 客户端才生效（server 是常驻进程，配置只在启动时读一次）。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """自检。**先过准入**：未接入就把同一份拒绝清单打出来，和 MCP 一字不差。"""
    from . import bindings

    hint = getattr(args, "workspace", None)
    if not hint and getattr(args, "token", None):
        try:
            hint = str(Token.load(resolve_token_path(args.token)).workspace)
        except TokenError as exc:
            _dump({"version": __version__, "ok": False, "code": exc.code, "error": str(exc)})
            return 1

    session = bindings.resolve_session(hint)
    if not session.ok:
        payload = session.to_json()
        payload["version"] = __version__
        payload["all_ok"] = False
        _dump(payload)
        return 1

    layout = session.layout
    assert layout is not None and layout.library_dir is not None
    # 配置校验已通过 —— Config.load 不通过会直接抛错，走不到这里
    cfg = Config.load(library_dir=layout.library_dir)
    checks: list[dict[str, Any]] = [
        {
            "check": "binding",
            "ok": True,
            "detail": (
                f"library={session.record.library} access={session.access} "
                f"root={session.workspace_root}"
            ),
        },
        {
            "check": "config",
            "ok": True,
            "detail": (
                f"{len(cfg.layers)} 层生效（{' > '.join(n for n, _ in reversed(cfg.layers))}）"
                f"，等级 {len(cfg.forget_levels)} 档（顶档 {cfg.max_level}）"
            ),
        },
    ]
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "sqlite_lib": sqlite3.sqlite_version,
        "config": cfg.describe(),
        "settings": cfg.describe_settings(),
        "layout": layout.describe(),
        # 共享组库的落点（自有库仍在项目内）：跟着安装走，不进用户目录
        "libraries_home": str(libraries_home()),
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
    """令牌：show 看当前工作区，use 签一份 v2 令牌。

    落点固定 `<root>/.ai_memory/token.json`（A2）。`--token` 只在需要
    写到别处时才传 —— 它已经不是身份来源，只是路径覆盖。
    """
    explicit = resolve_token_path(getattr(args, "token", None))

    if args.action == "show":
        path = explicit
        if path is None:
            ws_dir = find_workspace_dir()
            if ws_dir is None:
                print(
                    "当前目录不在任何已接入的工作区里（向上没找到 .ai_memory）。",
                    file=sys.stderr,
                )
                return 1
            path = token_path(ws_dir)
        try:
            token = Token.load(path)
        except TokenError as exc:
            print(f"没有可用的令牌（{exc.code}）：{exc}", file=sys.stderr)
            return 1
        _dump({"ok": True, "token": token.masked()})
        return 0

    # use 已于 T8 退役：它只会"签令牌但不登记库"，造出 registry 里没有的孤儿钥匙
    print(
        "`workspace use` 已退役（它签的令牌在 registry 里没有对应记录）。改用：\n"
        "  · 本目录已有库 → humanity-memory bind adopt\n"
        "  · 新建独立库   → humanity-memory bind new --label <名字>\n"
        "  · 并入已有库   → humanity-memory bind existing --library <标签>",
        file=sys.stderr,
    )
    return 2


def _label_args(items: list[str] | None) -> tuple[dict[str, str], str | None]:
    """`--label` 的两种用法：`NAME`（单点绑定）与 `PATH=NAME`（migrate 映射）。

    合成一个参数是为了命令行短一点；分开解析是为了不让两种语义混在一个值里。
    """
    mapping: dict[str, str] = {}
    single: str | None = None
    for item in items or []:
        if "=" in item:
            path, name = item.split("=", 1)
            mapping[path.strip()] = name.strip()
        else:
            single = item.strip() or None
    return mapping, single


def cmd_bind(args: argparse.Namespace) -> int:
    """`bind` 全套：migrate / new / existing / adopt / decline / move。

    与 MCP 的 `memory_bind_*` 共用 `bindings` 的实现 —— 两边各写一份的话，
    迟早会出现"CLI 说成功、MCP 说未接入"这种最难查的分裂。
    """
    from . import bindings

    if args.action == "migrate":
        return _bind_migrate(args)
    if args.action == "relocate":
        return _bind_relocate(args)

    root = Path(args.path).expanduser() if args.path else Path.cwd()
    _, single_label = _label_args(args.label)
    try:
        if args.action == "move":
            if not args.from_path:
                print("错误：bind move 需要 --from <旧路径>", file=sys.stderr)
                return 2
            moved = bindings.bind_move(args.from_path, root)
            _dump({"ok": True, **moved})
            return 0

        if args.action == "new":
            session = bindings.bind_new(root, single_label)
        elif args.action == "adopt":
            session = bindings.bind_adopt(root, single_label)
        elif args.action == "existing":
            if not args.library:
                print("错误：bind existing 需要 --library <标签>", file=sys.stderr)
                return 2
            session = bindings.bind_existing(root, args.library)
        else:  # decline
            session = bindings.bind_decline(root)
    except bindings.BindingError as exc:
        payload = exc.to_json()
        payload["ok"] = False
        _dump(payload)
        return 1

    _dump(session.to_json())
    return 0


def cmd_bindings(args: argparse.Namespace) -> int:
    """`bindings list`：registry 里有哪些库、谁在用、哪些钥匙已撤销。"""
    try:
        registry = key_registry.read()
    except key_registry.RegistryError as exc:
        _dump({"ok": False, "code": exc.code, "error": str(exc)})
        return 1

    libraries = []
    for label, lib in sorted(registry.libraries.items()):
        holders = [
            str(rec.workspace)
            for rec in registry.keys.values()
            if rec.active and rec.library == label
        ]
        libraries.append(
            {
                "library": label,
                "path": str(lib.path),
                "exists": lib.path.is_dir(),
                "workspaces": holders,
            }
        )
    revoked = [
        {"workspace": str(rec.workspace), "library": rec.library, "revoked": rec.revoked}
        for rec in registry.keys.values()
        if not rec.active
    ]
    _dump(
        {
            "ok": True,
            "registry": str(registry.path),
            "libraries": libraries,
            "revoked": revoked,
        }
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """`revoke`：撤销一把钥匙（或某个项目/某个库的全部钥匙）。软删，不动库。"""
    if not args.key and not args.workspace and not args.library:
        print(
            "错误：需要 <key>，或用 --workspace / --library 指定范围", file=sys.stderr
        )
        return 2
    try:
        with key_registry.edit() as registry:
            if args.key:
                changed = registry.revoke(args.key)
                hits = [args.key] if changed else []
            else:
                hits = registry.revoke_where(
                    workspace=args.workspace, library=args.library
                )
    except key_registry.RegistryError as exc:
        _dump({"ok": False, "code": exc.code, "error": str(exc)})
        return 1

    if args.key and not hits:
        _dump({"ok": True, "note": "这把钥匙早就撤销过了", "count": 0})
        return 0
    _dump(
        {
            "ok": True,
            "revoked": len(hits),
            "note": "已撤销的钥匙不能再接入；库里已有的记忆不受影响（解绑不是回滚）。"
            "重发令牌即可继续：bind adopt。",
        }
    )
    return 0 if hits else 1


def _bind_relocate(args: argparse.Namespace) -> int:
    """把库从 `<项目>/.ai_memory` 搬到统一目录（幂等；已是统一目录则跳过）。"""
    from . import bindings

    if args.scan:
        roots = []
        for base in args.scan:          # --scan 是 append 型，可给多个根
            b = Path(base).expanduser()
            if not b.is_dir():
                print(f"错误：扫描根不存在：{b}", file=sys.stderr)
                return 2
            roots += [
                d for d in sorted(b.iterdir())
                if d.is_dir() and (d / ".ai_memory").is_dir()
            ]
    else:
        roots = [Path(args.path).expanduser() if args.path else Path.cwd()]

    results: list[dict[str, Any]] = []
    for root in roots:
        try:
            results.append(bindings.relocate(root, dry_run=not args.apply))
        except bindings.BindingError as exc:
            payload = exc.to_json()
            payload["workspace_root"] = str(root)
            results.append(payload)

    ok = all(r.get("ok") for r in results)
    _dump({"ok": ok, "applied": bool(args.apply), "count": len(results), "results": results})
    return 0 if ok else 1


def _bind_migrate(args: argparse.Namespace) -> int:
    from . import bindings

    if not args.scan:
        print(
            "错误：migrate 需要至少一个 --scan <目录>，例如 `--scan D:/work`",
            file=sys.stderr,
        )
        return 2
    if args.depth < 1:
        print("错误：--depth 至少为 1", file=sys.stderr)
        return 2

    labels, _ = _label_args(args.label)

    # 体检必须读**同一份** registry：读错了会得出「钥匙不在 registry」的假结论，
    # 然后每次重跑都重签一遍令牌（幂等性直接破功）
    plan = bindings.build_plan(
        [Path(p) for p in args.scan],
        labels=labels,
        depth=args.depth,
        registry=key_registry.read(args.registry),
    )
    retire = [] if args.keep_legacy_tokens else bindings.legacy_token_paths()
    report = bindings.apply_plan(
        plan,
        dry_run=not args.apply,
        registry_path=args.registry,
        retire_paths=retire,
        mcp_json=Path(args.mcp_json).expanduser() if args.mcp_json else None,
    )

    if args.json:
        _dump(report)
    else:
        _print_bind_migrate(report)
    return 0 if report.get("ok") else 1


def _print_bind_migrate(report: dict[str, Any]) -> None:
    mode = "dry-run（未写任何文件）" if report["dry_run"] else "apply（已落盘）"
    print(f"模式：{mode}")
    print(f"registry：{report['registry']}")
    print()

    rows = report["libraries"]
    if not rows:
        print("没扫描到任何 .ai_memory —— 检查 --scan 指向的目录。")
        return

    print(
        _pad("项目根", 44) + _pad("标签", 22) + _pad("知识/经历/日志/复盘", 21)
        + _pad("令牌", 10) + "动作"
    )
    for item in rows:
        c = item.get("counts") or {}
        tally = (
            "/".join(str(c.get(t, "-")) for t in ("knowledge", "episodes", "logs", "digests"))
            if c
            else "-"
        )
        print(
            _pad(_short(item["root"], 44), 44)
            + _pad(item["label"], 22)
            + _pad(tally, 21)
            + _pad(item["token"], 10)
            + item["action"]
        )

    counts: dict[str, int] = {}
    for item in rows:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    print()
    print("　".join(f"{name} {n}" for name, n in sorted(counts.items())))

    # 只在"要动它"或"有问题"时展开细节，避免 10 行路径刷屏
    details = [i for i in rows if i["action"] != "keep" or i["token"] != "v2-ok"]
    if details:
        print("\n明细：")
        for item in details:
            print(f"  · {item['root']}")
            print(f"    → {item['action']}：{item['reason']}")
            git = item.get("git") or {}
            if git.get("repo"):
                ignored = "是" if git.get("ignored") else "否"
                print(f"    git：忽略={ignored}  钩子={git.get('hook')}")
            elif git:
                print("    git：不是仓库（跳过钩子核对）")

    retired = [r for r in report["retired"] if r.get("retired")]
    if retired:
        print("\n旧令牌退役：")
        for item in retired:
            print(f"  {Path(item['path']).name} → {Path(item['retired']).name}")
    elif report["retired"]:
        print("\n旧令牌：无可退役文件")

    mcp = report.get("mcp_json")
    if mcp:
        if mcp.get("changed"):
            names = "、".join(mcp.get("servers") or [])
            print(f"\nmcp.json：{mcp['path']}")
            print(f"  删掉 --token（servers：{names}）")
            if mcp.get("backup"):
                print(f"  备份：{mcp['backup']}")
        else:
            print(f"\nmcp.json：无改动（{mcp.get('reason') or '没有 --token'}）")
        if mcp.get("skipped"):
            print(f"  其他 server 带 --token，未改动：{'、'.join(mcp['skipped'])}")
    else:
        print("\nmcp.json：未指定 --mcp-json，客户端配置未改动")

    for warning in report.get("warnings") or []:
        print(f"\n⚠ {warning}")

    if report["dry_run"]:
        print("\n确认无误后加 --apply 落地（令牌与 registry 都可单条回滚）。")


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
            frozen = report.get("frozen_days") or 0
            if frozen > 0:
                print(
                    f"静默冻结 {frozen:g} 天：距上次计算太久了，"
                    f"只按 {report['max_gap_days']:g} 天计入记忆寿命"
                    f"（超出的部分已从周期起点里扣掉）。"
                )
            if report["due"] == 0:
                print("（没有到期的裁定项）")
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


def cmd_retrieval_report(args: argparse.Namespace) -> int:
    """把检索记录渲染成单文件 HTML（只读，不碰库）。

    除了检索明细，还把「当前等级表阈值」和「最新一份裁定快照」一起塞进去 ——
    页面才能自己算出「维持/升级需要几次命中」「多少节点在降级线下」。
    """
    from .reports import (
        latest_judge_snapshot,
        level_thresholds,
        read_item_contents,
        read_node_activity,
        read_retrievals,
    )
    from . import bindings
    from .retrieval_view import render_retrieval_html

    try:
        store = _open_readonly(args)
    except bindings.BindingError as exc:
        _dump(exc.to_json())
        return 2
    with store:
        workspace = store.layout.workspace_dir
        if workspace is None:
            print("错误：没有解析到记忆库", file=sys.stderr)
            return 2

        records = read_retrievals(workspace, store.cfg, days=args.days)
        snapshot = latest_judge_snapshot(workspace, store.cfg)
        nodes = [
            {
                "level": n.get("level"),
                "activity": n.get("activity"),
                "up": n.get("up_threshold"),
                "down": n.get("down_threshold"),
            }
            for n in (snapshot or {}).get("nodes", [])
        ]

        # 单文件报告是离线产物，页面没法向后端懒取正文，只能在生成时内联。
        # 只取日志里真正出现过的 id（记录可能跨几十天，全库内联没必要）。
        hit_ids = [str(h.get("id")) for r in records for h in (r.get("hits") or [])]
        contents = {
            item_id: item["content"]
            for item_id, item in read_item_contents(
                store.layout.workspace_db, [x for x in hit_ids if x]
            ).items()
        }

        html = render_retrieval_html(
            records,
            contents=contents,
            meta={
                "db": str(store.layout.workspace_db),
                "days": args.days,
                "levels": level_thresholds(store.cfg),
                "nodes": nodes,
                # 热度榜的第二个排序维度：库里的实时活跃度（键 = 节点 id）。
                # 不用快照里的 activity —— 那是裁定那一刻的切片，而且没跑过裁定的
                # 工作区压根没有快照，排序维度不能因此消失。
                "activity": read_node_activity(workspace),
                "generated": (snapshot or {}).get("generated_at"),
            },
        )
        out = (
            Path(args.out).expanduser().resolve()
            if args.out
            else workspace / "reports" / "retrieval.html"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")

    print(f"检索分析已生成：{out}")
    print(f"  记录 {len(records)} 次检索 · 节点快照 {len(nodes)} 条")
    if not records:
        print("  提示：还没有检索记录 —— 先跑几次 memory_recall，再回来生成。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import mcp_server

    mcp_server.main()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """启动本地可视化分析工具（HTTP 后端 + 浏览器前端，全程只读）。"""
    from .webtool import serve
    from .webtool.datasource import SourceError

    try:
        return serve(
            args.target,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except SourceError as exc:
        print(f"无法启动观测台：{exc.message}", file=sys.stderr)
        for line in exc.checklist:
            print(f"  - {line}", file=sys.stderr)
        return 2


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

    p_review = sub.add_parser(
        "review", help="复盘闸门：签发 / 查看 / 收回 memory_search 通行证"
    )
    p_review.add_argument(
        "action", nargs="?", default="status", choices=["status", "begin", "end"]
    )
    p_review.add_argument(
        "--minutes", default=None, help="通行证时长：30 / 30m / 2h（默认取配置）"
    )
    p_review.add_argument("--reason", default=None, help="写进审计的原因")
    p_review.add_argument("--json", action="store_true")
    p_review.set_defaults(func=cmd_review)

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

    p_ws = sub.add_parser("workspace", help="令牌：工作区身份（v2：只存 key / workspace / label）")
    p_ws.add_argument("action", choices=["show", "use"])
    p_ws.add_argument("path", nargs="?", default=None, help="use 时的项目根目录")
    p_ws.add_argument(
        "--token",
        default=argparse.SUPPRESS,
        help="令牌落点覆盖（默认 <项目根>/.ai_memory/token.json）",
    )
    p_ws.add_argument("--label", default=None, help="人类可读名称")
    p_ws.add_argument("--key", default=None, help="沿用指定的 key（默认沿用旧令牌或现签一个）")
    p_ws.set_defaults(func=cmd_workspace)

    p_bind = sub.add_parser("bind", help="工作区绑定：new / existing / adopt / decline / move / relocate / migrate")
    p_bind.add_argument(
        "action",
        choices=["migrate", "new", "existing", "adopt", "decline", "move", "relocate"],
    )
    p_bind.add_argument(
        "path", nargs="?", default=None, help="项目根目录（默认 cwd；不要把 .ai_memory 传进来）"
    )
    p_bind.add_argument("--library", default=None, help="bind existing 要并入的库标签")
    p_bind.add_argument("--from", dest="from_path", default=None, help="bind move 的旧项目根")
    p_bind.add_argument(
        "--scan",
        action="append",
        default=[],
        metavar="DIR",
        help="migrate：扫描根目录，可重复（如 --scan D:/work）",
    )
    p_bind.add_argument(
        "--depth", type=int, default=3, help="migrate：项目根相对扫描根的最大层数（默认 3）"
    )
    p_bind.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="LABEL",
        help="库标签：bind new/adopt 用 NAME；migrate 用 PATH=NAME，可重复",
    )
    p_bind.add_argument(
        "--registry", default=None, help="registry.json 覆盖（默认 HM_HOME 下那个）"
    )
    p_bind.add_argument(
        "--mcp-json",
        default=None,
        help="migrate：同时从这份客户端配置里删掉 humanity-memory 的 --token",
    )
    p_bind.add_argument(
        "--keep-legacy-tokens", action="store_true", help="migrate：不退役 active/dev.token.json"
    )
    p_bind.add_argument("--apply", action="store_true", help="migrate：真的写盘（默认 dry-run）")
    p_bind.add_argument("--json", action="store_true", help="输出机器可读报告")
    p_bind.set_defaults(func=cmd_bind)

    p_blist = sub.add_parser("bindings", help="registry：列出已登记的库与持有者")
    p_blist.add_argument("action", choices=["list"])
    p_blist.set_defaults(func=cmd_bindings)

    p_revoke = sub.add_parser("revoke", help="撤销钥匙（软删，不动库文件）")
    p_revoke.add_argument("key", nargs="?", default=None, help="明文 key（从令牌里取）")
    p_revoke.add_argument("--workspace", default=None, help="按项目根批量撤销")
    p_revoke.add_argument("--library", default=None, help="按库标签批量撤销")
    p_revoke.set_defaults(func=cmd_revoke)

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

    p_rr = sub.add_parser(
        "retrieval-report",
        help="把检索记录渲染成单文件 HTML（只读；含阈值诊断，调参看这个）",
    )
    p_rr.add_argument(
        "--out",
        default=None,
        help="HTML 输出路径（默认 <工作区>/reports/retrieval.html）",
    )
    p_rr.add_argument(
        "--days", type=int, default=0, help="只读最近 N 天的记录（0 = 全部）"
    )
    p_rr.set_defaults(func=cmd_retrieval_report)

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
    p_star.add_argument(
        "--activity-mode",
        choices=("ratio", "rank"),
        default="ratio",
        help=(
            "辉光颜色 0~1 的算法：ratio = 活跃度 ÷ 本档阈值（默认，1.0 即升档线）；"
            "rank = 跨节点秩归一（颜色沿节点均匀铺开，只表示排名）"
        ),
    )
    p_star.set_defaults(func=cmd_starmap)

    p_an = sub.add_parser(
        "analyze", help="本地可视化分析工具：HTTP 后端 + 浏览器前端（只读）"
    )
    p_an.add_argument(
        "target",
        nargs="?",
        default=None,
        help=(
            "库根目录 / token 文件 / 项目根 / workspace.db。"
            "省略 = 列出 registry 里全部已登记的库，页面右上角下拉切换"
        ),
    )
    p_an.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_an.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_an.set_defaults(func=cmd_analyze)

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
