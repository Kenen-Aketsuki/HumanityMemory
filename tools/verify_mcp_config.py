"""端到端：用 mcp.json 的 command/env 跑通三层记忆与多工作区切换。

为什么不直接用 mcp.json 里的 args：
    真实条目靠 cwd 自动分辨工作区，测试进程的 cwd 无法可靠模拟。
    所以 command 和 env（含凭据）取自 mcp.json —— 这验证了配置本身可用；
    args 换成临时项目，保证测试无副作用。

覆盖：
    1. instructions 里注入信条
    2. 15 个工具都在
    3. 事实 / 日志 / 取代 / 失效 / 复盘全流程
    4. memory_use_workspace 切换工作区，且两个项目数据物理隔离
    5. 重启后能接上「上次工作区」
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_JSON = pathlib.Path.home() / ".workbuddy" / "mcp.json"
ENTRY = "humanity-memory"

EXPECTED_TOOLS = {
    "rules_get",
    "rules_append",
    "memory_write",
    "memory_invalidate",
    "memory_promote",
    "memory_list",
    "log_append",
    "log_list",
    "memory_search",
    "memory_recall",
    "consolidate_preview",
    "consolidate_apply",
    "memory_stats",
    "memory_maintenance",
    "memory_use_workspace",
    "memory_archive",
    "memory_unarchive",
    "memory_purge_candidates",
    "memory_purge",
    "memory_compress_preview",
    "memory_compress_apply",
    "memory_recompress_preview",
    "memory_recompress_apply",
}


def payload(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {"raw": str(result)}


async def run_main_flow(work: dict, env: dict, proj: pathlib.Path) -> list[str]:
    """完整工具链 + 多工作区切换。"""
    failures: list[str] = []
    params = StdioServerParameters(
        command=work["command"],
        args=["-m", "humanity_memory.mcp_server", "--workspace", str(proj)],
        env=env,
        cwd=str(work["cwd"]),
    )
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            init = await s.initialize()
            instr = getattr(init, "instructions", "") or ""
            has_rules = "绝对信条" in instr or "底线规则" in instr
            print(f"[{'OK' if has_rules else 'FAIL'}] instructions 注入信条: {len(instr)} 字符")
            if not has_rules:
                failures.append("instructions 缺信条")

            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            missing = EXPECTED_TOOLS - names
            print(
                f"[{'OK' if not missing else 'FAIL'}] tools: {len(names)} 个"
                f"{' 缺 ' + str(missing) if missing else ''}"
            )
            if missing:
                failures.append(f"缺工具 {missing}")

            rg = payload(await s.call_tool("rules_get", {}))
            print(f"[OK] rules_get: 全局 {rg.get('global_rules', {}).get('items')} 条")

            st0 = payload(
                await s.call_tool(
                    "memory_write",
                    {
                        "kind": "state",
                        "content": "本项目用 bge-m3 做向量检索，维度 1024",
                        "tags": ["向量"],
                        "source": "e2e",
                    },
                )
            )
            sid = st0.get("item", {}).get("id")
            print(
                f"[{'OK' if st0.get('embedded') else 'WARN'}] memory_write(state): {sid} "
                f"embedded={st0.get('embedded')}"
            )
            if not st0.get("ok"):
                failures.append("memory_write(state) 失败")

            kn = payload(
                await s.call_tool(
                    "memory_write",
                    {
                        "kind": "knowledge",
                        "content": "跨项目约定：回复用中文并用表格",
                        "scope": "global",
                    },
                )
            )
            print(f"[OK] 全局知识: {kn.get('item', {}).get('id')}")

            st2 = payload(
                await s.call_tool(
                    "memory_write",
                    {
                        "kind": "state",
                        "content": "本项目改用 Qwen3-Embedding",
                        "invalidates": sid,
                    },
                )
            )
            inv = st2.get("item") or {}
            listed = payload(
                await s.call_tool("memory_list", {"kinds": "state"})
            )
            ids = {x.get("id") for x in listed.get("items", [])}
            old_gone = sid not in ids
            print(
                f"[{'OK' if old_gone else 'FAIL'}] 现状换代: 旧条目仍 live={not old_gone} "
                f"新条目={inv.get('id')}"
            )
            if not old_gone:
                failures.append("取代旧现状失败")

            for text in ["排查 mcp 2.3 的 MCPServer 改名", "doctor 双连接导致 database is locked"]:
                lg = payload(await s.call_tool("log_append", {"content": text}))
                if not lg.get("ok"):
                    failures.append("log_append 失败")
            ll = payload(await s.call_tool("log_list", {"day": "today"}))
            print(f"[OK] log_append / log_list: {ll.get('count')} 条")

            sr = payload(await s.call_tool("memory_search", {"query": "向量用的什么模型", "limit": 5}))
            print(f"[OK] memory_search: {sr.get('count')} 条")

            rc = payload(await s.call_tool("memory_recall", {"context": "继续做记忆库向量部分"}))
            print(f"[OK] memory_recall: {rc.get('count')} 条")

            pv = payload(await s.call_tool("consolidate_preview", {"day": "today"}))
            print(f"[OK] consolidate_preview: 待复盘 {pv.get('preview', {}).get('log_count')} 条")

            ca = payload(
                await s.call_tool(
                    "consolidate_apply",
                    {
                        "day": "today",
                        "knowledge": [
                            {"content": "mcp 2.x 的 FastMCP 更名为 MCPServer", "kind": "knowledge"}
                        ],
                        "episodes": [
                            {
                                "content": "MCP 2.x 把 FastMCP 改名为 MCPServer，导入路径随之调整",
                                "kind": "case",
                            }
                        ],
                        "summary": "三层记忆验证",
                        "purge": True,
                    },
                )
            )
            r = ca.get("result", {})
            print(
                f"[OK] consolidate_apply: 日志 {r.get('log_count')} → 陈述 "
                f"{len(r.get('created_knowledge', []))} 经历 {len(r.get('created_episodes', []))} "
                f"清空 {r.get('purged')}"
            )
            if not r.get("digest_id"):
                failures.append("consolidate_apply 未产出 digest")

            # ---- 多工作区切换
            other = proj.parent / "another"
            other.mkdir(exist_ok=True)
            sw = payload(
                await s.call_tool(
                    "memory_use_workspace", {"path": str(other), "persist": True}
                )
            )
            print(
                f"[{'OK' if sw.get('ok') else 'FAIL'}] memory_use_workspace → "
                f"{sw.get('switched_to', {}).get('workspace_dir')}"
            )
            if not sw.get("ok"):
                failures.append("切换工作区失败")

            await s.call_tool(
                "memory_write", {"kind": "state", "content": "另一个项目专属：用 UI Toolkit"}
            )
            scoped = payload(
                await s.call_tool(
                    "memory_search", {"query": "UI 方案", "limit": 5, "format": "full"}
                )
            )
            got = [x.get("content", "") for x in scoped.get("results", [])]
            leaked = any("bge-m3" in c for c in got)
            print(
                f"[{'OK' if not leaked else 'FAIL'}] 切换后隔离: 命中 {len(got)} 条，"
                f"{'✗ 串味' if leaked else '✓ 无跨项目泄漏'}"
            )
            if leaked:
                failures.append("切换工作区后数据串味")

            st = payload(await s.call_tool("memory_stats", {}))
            wsinfo = st.get("stats", {}).get("dbs", {}).get("workspace", {})
            print(
                f"[OK] memory_stats: knowledge={wsinfo.get('knowledge_live')} "
                f"state={wsinfo.get('state')} cases={wsinfo.get('cases')} "
                f"episodes={wsinfo.get('episodes')} "
                f"global={st.get('stats', {}).get('global_knowledge')}"
            )

            mt = payload(await s.call_tool("memory_maintenance", {}))
            print(f"[OK] memory_maintenance: {mt.get('report')}")
    return failures


async def run_resume_check(work: dict, env: dict) -> list[str]:
    """无参数启动时应接上 last-workspace。"""
    failures: list[str] = []
    params = StdioServerParameters(
        command=work["command"],
        args=["-m", "humanity_memory.mcp_server"],
        env=env,
        cwd=str(work["cwd"]),
    )
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            st = payload(await s.call_tool("memory_stats", {}))
            ws = str(st.get("stats", {}).get("layout", {}).get("workspace_dir") or "")
            hit = "another" in ws
            print(f"[{'OK' if hit else 'FAIL'}] 无参数启动接续上次工作区: {ws}")
            if not hit:
                failures.append("未能接续 last-workspace")
    return failures


async def main() -> int:
    work = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"][ENTRY]
    root = pathlib.Path(tempfile.mkdtemp(prefix="hm-e2e-"))
    fake_home = root / "home"
    fake_home.mkdir(parents=True)
    cwd = root / "client_cwd"
    cwd.mkdir()
    proj = root / "demo_project"
    proj.mkdir()

    env = {
        **os.environ,
        **work.get("env", {}),
        "HM_HOME": str(fake_home),
        "PYTHONUTF8": "1",
    }
    env.pop("HM_WORKSPACE", None)
    ctx = {"command": work["command"], "cwd": cwd}

    print(f"临时项目  : {proj}")
    print(f"临时全局库: {fake_home}")
    print(f"command   : {work['command']}")
    print()

    failures: list[str] = []
    try:
        failures += await run_main_flow(ctx, env, proj)
        failures += await run_resume_check(ctx, env)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print("E2E FAIL:", "; ".join(failures))
        return 1
    print("E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
