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
    "fact_write",
    "fact_invalidate",
    "fact_promote",
    "fact_list",
    "log_append",
    "log_list",
    "memory_search",
    "memory_recall",
    "consolidate_preview",
    "consolidate_apply",
    "memory_stats",
    "memory_maintenance",
    "memory_use_workspace",
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

            fw = payload(
                await s.call_tool(
                    "fact_write",
                    {"content": "本项目用 bge-m3 做向量检索，维度 1024", "tags": ["向量"]},
                )
            )
            fid = fw.get("fact", {}).get("id")
            print(
                f"[{'OK' if fw.get('embedded') else 'WARN'}] fact_write: {fid} "
                f"embedded={fw.get('embedded')}"
            )
            if not fw.get("ok"):
                failures.append("fact_write 失败")

            gw = payload(
                await s.call_tool(
                    "fact_write", {"content": "主人偏好结构化输出与表格", "scope": "global"}
                )
            )
            print(f"[OK] 全局事实: {gw.get('fact', {}).get('id')}")

            fw2 = payload(
                await s.call_tool(
                    "fact_write",
                    {"content": "本项目改用 Qwen3-Embedding", "invalidates": fid},
                )
            )
            inv = fw2.get("invalidated") or {}
            print(
                f"[{'OK' if inv.get('live') is False else 'FAIL'}] 取代旧事实: "
                f"old.live={inv.get('live')}"
            )
            if inv.get("live") is not False:
                failures.append("取代旧事实失败")

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
                        "facts": [{"content": "mcp 2.x 的 FastMCP 更名为 MCPServer"}],
                        "episodes": ["完成了三层记忆重构与复盘流程验证。"],
                        "summary": "三层记忆验证",
                        "purge": True,
                    },
                )
            )
            r = ca.get("result", {})
            print(
                f"[OK] consolidate_apply: 日志 {r.get('log_count')} → 事实 "
                f"{len(r.get('created_facts', []))} 经历 {len(r.get('created_episodes', []))} "
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

            await s.call_tool("fact_write", {"content": "另一个项目专属事实：用 UI Toolkit"})
            scoped = payload(await s.call_tool("memory_search", {"query": "UI 方案", "limit": 5}))
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
                f"[OK] memory_stats: facts={wsinfo.get('facts_live')} "
                f"episodes={wsinfo.get('episodes')} "
                f"global_facts={st.get('stats', {}).get('global_facts')}"
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
