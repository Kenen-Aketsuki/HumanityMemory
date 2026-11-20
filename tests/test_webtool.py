"""外部可视化与分析工具（记忆观测台）的契约测试。

守六件事（设计文档 §11）：

1. **只读断言**：检索测试前后，库文件 / WAL / JSONL / activity 一个字节不变 —— 这是整个需求最硬的约束；
2. **空库优雅**：lilith 这种没有 reports/retrieval 目录的库，各视图返回空状态而不是崩；
3. **中文路径**：库目录含中文/空格时，`mode=ro + quote(as_posix)` 取数正常；
4. **复盘 diff 口径**：按天分组、同天取非 dryrun 最新、活跃度差按 id、dryrun 不参与、items=[] 空状态；
5. **倒计时判据**：记忆现状的 due_code 与 `lifecycle.is_due` 完全一致；
6. **API 契约**：HTTP 后端所有端点可达、错误契约统一。

真实库（island / lilith）测试用 skip 保护（别处环境没有就跳过）；
复盘 diff 用 tmp 库 + 手工快照保证可移植。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from humanity_memory import Config, MemoryStore, resolve_layout
from humanity_memory.lifecycle import DAY_MS, period_kind
from humanity_memory.webtool import memory_state, recall_view, review, simulate, starmap_view
from humanity_memory.webtool.datasource import WebSource

# 真实库位置不写死本机绝对路径：优先读环境变量，其次取与本仓库同级的 Release 目录。
# 两处都没有时 ISLAND / LILITH 不存在，下面的 skipif 会自动跳过，测试仍可移植。
_RELEASE_ENV = os.environ.get("HM_TEST_RELEASE_LIBRARIES")
RELEASE = (
    Path(_RELEASE_ENV)
    if _RELEASE_ENV
    else Path(__file__).resolve().parents[1].parent / "HumanityMemory-Release" / "libraries"
)
ISLAND = RELEASE / "island"
LILITH = RELEASE / "lilith"
needs_island = pytest.mark.skipif(not ISLAND.is_dir(), reason="island 真实库不存在")
needs_lilith = pytest.mark.skipif(not LILITH.is_dir(), reason="lilith 真实库不存在")


@pytest.fixture()
def lib(tmp_path: Path):
    """造一个完整 schema 的临时库，返回 (store, library_dir)。"""
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    yield store, layout.library_dir
    store.close()


# ---------------------------------------------------------------- 1. 只读断言（核心）


@needs_island
def test_search_test_never_writes_library() -> None:
    """检索测试跑前跑后，库/JSONL/activity 快照必须完全一致。"""
    lib = ISLAND

    def snap() -> dict:
        d: dict = {}
        for p in list(lib.glob("workspace.db*")) + list(lib.glob("retrieval/*.jsonl")):
            st = p.stat()
            d[p.name] = (st.st_mtime_ns, st.st_size)
        con = sqlite3.connect(
            "file:" + str(lib / "workspace.db").replace("\\", "/") + "?mode=ro", uri=True
        )
        d["activity"] = con.execute(
            "SELECT id,activity,access_count FROM knowledge ORDER BY rowid"
        ).fetchall()
        con.close()
        return d

    before = snap()
    src = WebSource(lib)
    result = simulate.post(src, {"query": "探索派遣倒计时", "limit": 5})
    assert result["ok"] is True
    assert snap() == before, "检索测试改动了库文件、JSONL 或 activity"


@needs_island
def test_retrieval_mode_strict_fallback_on_record_and_simulate(tmp_path: Path) -> None:
    """严格/宽松模式必须在 recall 记录、模拟返回、两个前端页面上都能区分。

    回归：此前 JSONL 只有 deep（去哪找），没有 mode（怎么找/多严格），
    两个页面都无法区分严格命中与宽泛层兜底。
    """
    import json as _json
    import glob as _glob

    from humanity_memory.webtool import live

    # --- 临时库跑一次真实 recall，验证写入的 JSONL 带 mode 与逐条 fallback ---
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8", "HM_FALLBACK_MODE": "maxpool"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    for txt in ("unity-mcp adb-debugger 安装到 Unity 工程", "音效实装工作流程与清单", "农场技能特效制作"):
        store.write_memory(txt, importance=0.5)
    store.recall("unity-mcp 安装", limit=5)
    store.close()

    files = _glob.glob(str(proj / ".ai_memory" / "**" / "*.jsonl"), recursive=True)
    assert files, "recall 没有写出 JSONL"
    lines = [ln for ln in Path(files[0]).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    rec = _json.loads(lines[-1])
    assert rec.get("mode") in ("strict", "fallback"), "记录缺少检索模式 mode"
    assert "deep" in rec, "deep 与 mode 是两个独立维度，都要保留"
    assert rec["hits"], "这次检索应有命中"
    assert "fallback" in rec["hits"][0], "每条命中要带 fallback 标志"

    # --- 模拟返回同语义：严格/宽松都能真实触发 ---
    src = WebSource(ISLAND)
    strict = simulate.post(src, {"query": "unity-mcp", "limit": 5})
    assert strict["mode"] == "strict", f"短词应严格命中，实际 {strict['mode']}"
    assert all(not h["fallback"] for h in strict["hits"])
    long_q = ("安装 unity-mcp adb-debugger UPM 包到目标 Unity 工程，"
              "Tools 安装脚本 install，package 复制，manifest.json 嵌入包")
    broad = simulate.post(src, {"query": long_q, "limit": 8})
    assert broad["mode"] == "fallback", f"长查询应走宽泛兜底，实际 {broad['mode']}"
    assert any(h["fallback"] for h in broad["hits"])

    # --- 两个页面都要显示模式标签 ---
    recall_html = live.retrieval_live_html()
    assert "modeTag(" in recall_html and "mode-fallback" in recall_html
    assert "检索模式" in recall_html
    js = (Path(__file__).resolve().parents[1]
          / "src/humanity_memory/webtool/static/views.js").read_text(encoding="utf-8")
    assert "function modeTag(" in js and "modeTag(d)" in js


@needs_island
def test_search_test_loads_vec_extension() -> None:
    """长查询的纯向量命中必须能复现 recall 历史 —— vec0 扩展必须在只读连接上加载。

    回归：曾因用 __new__ 绕开 open_db() 而漏加载 vec0，导致读 episodes_vec 报
    "no such module: vec0"，长查询（词法兜不住）返回 0 命中，但 recall 历史有记录。
    """
    src = WebSource(ISLAND)
    q = ("安装 unity-mcp adb-debugger UPM 包到目标 Unity 工程，"
         "Tools 安装脚本 install，package 复制，manifest.json 嵌入包")
    result = simulate.post(src, {"query": q, "limit": 8})
    assert result["ok"] is True
    ids = {h["id"] for h in result["hits"]}
    # 这条历史记录靠纯向量命中（vec_sim≈0.79，lex=None），必须能被模拟复现
    assert "e_672c5087c6e4" in ids, f"长查询的向量命中丢失，实际命中：{ids}"
    hit = next(h for h in result["hits"] if h["id"] == "e_672c5087c6e4")
    assert hit["vec_sim"] is not None and hit["vec_sim"] > 0.7


# ---------------------------------------------------------------- 2. 空库优雅


@needs_lilith
def test_empty_library_all_views_return_empty() -> None:
    """没有 reports/retrieval 目录的库，各视图返回空结构而不是抛异常。"""
    src = WebSource(LILITH)
    assert src.has_retrieval() is False
    assert src.has_judge() is False
    assert recall_view.get(src)["ok"] is True
    assert recall_view.get(src)["records"] == []
    assert review.get(src)["days"] == []
    assert memory_state.get(src)["ok"] is True


# ---------------------------------------------------------------- 3. 中文路径


def test_chinese_and_space_path(tmp_path: Path) -> None:
    """库目录含中文与空格时，只读取数正常（现场建库，避免复制漏掉 WAL）。"""
    home = tmp_path / "home 中文"
    home.mkdir()
    proj = tmp_path / "项目 目录"
    ws = proj / ".ai_memory"
    ws.mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    try:
        store.write_memory("中文路径测试记忆", importance=0.5)
    finally:
        store.close()
    src = WebSource(ws)  # 目录名带空格，路径含中文
    s = src.session()
    assert s["ok"] is True
    assert s["node_count"] >= 1
    conn = src.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------- 4. 复盘 diff 口径


def _write_snapshot(judge_dir: Path, day: str, *, nodes: list, items: list,
                    dry: bool = False, gen_ms: int = 0) -> None:
    """造一份 judge 快照 JSON（文件名 YYYYMMDD-HHMMSS）。"""
    judge_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": gen_ms,
        "dry_run": dry,
        "due": len(items),
        "settled": len(items),
        "total_nodes": len(nodes),
        "items": items,
        "nodes": nodes,
    }
    stamp = "20261119" if day == "2026-11-19" else "20261120"
    suffix = "-dryrun" if dry else ""
    (judge_dir / f"{stamp}-100000{suffix}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_review_groups_by_day_and_diffs(lib) -> None:
    """按天分组 + 相邻日 diff 按 id；dryrun 不参与 diff；items=[] 优雅。"""
    store, libdir = lib
    src = WebSource(libdir)
    jd = src.judge_dir

    n19 = [{"id": "a", "kind": "experience", "activity": 1.0},
           {"id": "b", "kind": "episode", "activity": 2.0}]
    n20 = [{"id": "a", "kind": "experience", "activity": 1.5},   # +0.5
           {"id": "c", "kind": "experience", "activity": 3.0}]   # 新增，不进 diff
    _write_snapshot(jd, "2026-11-19", nodes=n19, items=[], gen_ms=1)
    _write_snapshot(jd, "2026-11-20", nodes=n20,
                    items=[{"id": "a", "kind": "experience", "from": 1, "to": 2,
                            "activity": 1.4, "action": "up", "reason": "测试"}],
                    gen_ms=2)

    days = review.get(src)["days"]
    assert [d["date"] for d in days] == ["2026-11-20", "2026-11-19"]
    d20 = days[0]
    assert d20["changes"][0]["activity"] == 1.4      # 裁定那一刻的活跃度
    delta_a = next(x for x in d20["activity_deltas"] if x["id"] == "a")
    assert delta_a["delta"] == 0.5 and delta_a["prev"] == 1.0
    assert all(x["id"] != "c" for x in d20["activity_deltas"])  # 新增不进 diff
    assert days[1]["changes"] == [] and days[1]["activity_deltas"] == []  # items=[] 优雅


def test_review_dryrun_not_baseline(lib) -> None:
    """前一天是 dryrun 时，后一天的 diff 不应拿它当基准。"""
    store, libdir = lib
    src = WebSource(libdir)
    jd = src.judge_dir
    _write_snapshot(jd, "2026-11-19",
                    nodes=[{"id": "a", "activity": 1.0}], items=[], dry=True, gen_ms=1)
    _write_snapshot(jd, "2026-11-20",
                    nodes=[{"id": "a", "activity": 9.0}], items=[], dry=False, gen_ms=2)
    days = review.get(src)["days"]
    assert days[0]["dry"] is False
    assert days[0]["activity_deltas"] == [], "dryrun 被当成了 diff 基准"


@needs_island
def test_review_on_real_library() -> None:
    """真实库上复盘记录能按天翻，结构完整。"""
    d = review.get(WebSource(ISLAND))
    assert d["ok"] is True
    assert len(d["days"]) >= 1
    for day in d["days"]:
        for key in ("date", "changes", "activity_deltas", "added", "replaced", "deduped"):
            assert key in day


# ---------------------------------------------------------------- 5. 倒计时判据


@needs_island
def test_memory_countdown_matches_is_due() -> None:
    """记忆现状的 due_code 与 lifecycle.is_due 全量一致。"""
    src = WebSource(ISLAND)
    d = memory_state.get(src)
    now = d["meta"]["now"]
    conn = src.connect()
    try:
        mismatch = 0
        checked = 0
        for n in d["nodes"]:
            table = "knowledge" if n["kind"] == "experience" else "episodes"
            row = conn.execute(
                f"SELECT forget_level,period_start FROM {table} WHERE id=?", (n["id"],)
            ).fetchone()
            if not row:
                continue
            expected = src.levels.is_due(
                int(row["forget_level"] or 0), row["period_start"], now, period_kind(n["kind"])
            )
            checked += 1
            if (n["due_code"] == "due") != expected:
                mismatch += 1
        assert checked > 0
        assert mismatch == 0, f"{mismatch}/{checked} 条倒计时判据与 is_due 不一致"
    finally:
        conn.close()


# ---------------------------------------------------------------- 6. API 契约


@needs_island
def test_http_all_endpoints() -> None:
    """HTTP 后端所有端点可达，错误契约统一。"""
    import threading
    import urllib.request

    from humanity_memory.webtool.server import WebServer

    src = WebSource(ISLAND)
    srv = WebServer(("127.0.0.1", 0), src)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        def get(path: str) -> dict:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return json.loads(r.read())

        assert get("/api/session")["ok"] is True
        assert "starmap" in get("/api/status")
        assert get("/api/starmap")["ok"] is True
        assert get("/api/recall")["ok"] is True
        assert get("/api/review")["ok"] is True
        assert get("/api/memory")["ok"] is True
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/search-test",
            data=json.dumps({"query": "测试", "limit": 3}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            assert json.loads(r.read())["ok"] is True
        try:
            get("/api/nonexistent")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_source_error_contract(tmp_path: Path) -> None:
    """定位失败返回统一错误契约（code + message + checklist）。"""
    from humanity_memory.webtool.datasource import SourceError

    e = SourceError("no_db", "库文件不存在", checklist=["检查路径"])
    d = e.to_dict()
    assert d == {"ok": False, "code": "no_db", "message": "库文件不存在", "checklist": ["检查路径"]}


# ---------------------------------------------------------------- 7. 老模板复用（实时页面）


def test_live_pages_reuse_full_render_logic() -> None:
    """星图/检索实时页 = 老模板全文 + async 取数，交互逻辑零裁剪。"""
    from humanity_memory.webtool import live

    star = live.starmap_live_html()
    recall = live.retrieval_live_html()

    # 老模板的交互标志必须在 —— 证明不是被简化版替换，而是原样复用
    assert "dragstart" not in star  # 老代码用 mousedown/mousemove，不游移
    for marker in ("addEventListener('mousemove'", "addEventListener('wheel'",
                   "requestAnimationFrame", "glowColor", "visible(e)"):
        assert marker in star, f"星图实时页缺少老交互逻辑：{marker}"
    for marker in ("renderStats()", "renderDiag()", "bindHotSort()",
                   "function detail(i)", "function hot()"):
        assert marker in recall, f"检索实时页缺少老逻辑：{marker}"

    # 数据注入点已换成 async fetch；老的内嵌注入不再出现
    assert 'fetch("/api/starmap"' in star
    assert 'fetch("/api/recall"' in recall
    assert "/*__DATA__*/" not in star and "/*__DATA__*/" not in recall
    # async 包装闭合正确（括号配对由 node --check 在 CI 侧兜底，这里查结构）
    assert star.count("(async()=>{") == 1 and "(async()=>{" in recall


# ---------------------------------------------------------------- 8. 前端选中态样式


def test_review_timeline_selection_highlight() -> None:
    """复盘记录左侧时间轴的选中态必须有可见高亮（曾漏定义 .item.on 样式）。"""
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/humanity_memory/webtool/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/humanity_memory/webtool/static/views.js").read_text(encoding="utf-8")

    # CSS 必须定义 .item.on 的可见样式；仅靠 JS 切类而无样式 => 选中不高亮
    css = html.replace(" ", "")
    assert ".item.on{" in css, "缺少 .item.on 选中态样式"
    assert "box-shadow:inset3px00var(--exp)" in css, "选中态缺少强调色标识"
    assert ".item:hover{" in css, "列表项缺少可点击的 hover 反馈"

    # review 渲染必须用 on 类标记选中项，并在点击时切换
    assert "i===reviewSel?' on':''" in js, "时间轴未按选中下标加 on 类"
    assert "classList.remove('on')" in js and "classList.add('on')" in js, "点击未切换 on 类"


def test_search_tab_initializes_and_api_has_timeout() -> None:
    """检索 tab 必须能初始化表单；api 必须有超时兜底，否则界面卡在"检索中"。"""
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/humanity_memory/webtool/static/index.html").read_text(encoding="utf-8")

    # 曾经的 bug：refreshView 里 `if(name==='search') return;` 导致切 tab 时表单永不初始化
    assert "if(name==='search')return" not in html.replace(" ", ""), "refreshView 不能提前跳过 search"
    # 调度器是动态分发 window['render_'+name]，search 会命中 render_search
    assert "window['render_'+name]" in html, "调度器需动态分发到 render_<name>"

    # api 超时兜底：请求卡住时也要让按钮复位
    assert "AbortController" in html, "api 缺少 AbortController 超时"
    assert "25000" in html and "AbortError" in html, "api 缺少超时错误转换"


# ---------------------------------------------------------------- 9. 心跳探活


def _hb_server(lib, **kw):
    """起一台短租约测试服务，返回 (server, thread, port)。"""
    import threading

    from humanity_memory.webtool.server import WebServer

    _, libdir = lib
    srv = WebServer(("127.0.0.1", 0), WebSource(libdir), **kw)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, srv.server_address[1]


def test_heartbeat_endpoint_refreshes_lease(lib) -> None:
    """心跳端点刷新租约；前台用短超时，后台用长宽限。"""
    import urllib.request

    srv, thread, port = _hb_server(
        lib, heartbeat_timeout=3.0, hidden_grace=9.0, startup_grace=30.0
    )
    try:
        def beat(visible: int) -> dict:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/heartbeat?visible={visible}", timeout=5
            ) as r:
                return json.loads(r.read())

        front = beat(1)
        assert front["ok"] is True
        assert front["timeout"] == 3.0
        assert front["interval_ms"] == 3000
        assert srv.heartbeat_status()["activated"] is True

        back = beat(0)
        assert back["timeout"] == 9.0, "后台页没有放宽租约，会被浏览器节流误杀"
        assert srv.heartbeat_status()["timeout"] == 9.0
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_watchdog_stops_server_when_heartbeat_stops(lib) -> None:
    """启动宽限过后仍没有心跳 → 服务自动 shutdown，不留僵尸进程。"""
    srv, thread, _ = _hb_server(
        lib, heartbeat_timeout=0.4, hidden_grace=0.4, startup_grace=0.4
    )
    try:
        srv.start_watchdog()
        thread.join(timeout=3)
        assert not thread.is_alive(), "心跳超时后服务未自动退出"
    finally:
        srv.shutdown()
        srv.server_close()


def test_heartbeat_keeps_server_alive_then_exits(lib) -> None:
    """持续心跳能续租；一停就按正式超时退出。"""
    import time
    import urllib.request

    srv, thread, port = _hb_server(
        lib, heartbeat_timeout=0.6, hidden_grace=0.6, startup_grace=0.6
    )
    try:
        srv.start_watchdog()
        for _ in range(4):
            time.sleep(0.25)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/heartbeat?visible=1", timeout=5
            ) as r:
                assert json.loads(r.read())["ok"] is True
        assert thread.is_alive(), "持续心跳期间服务被误杀"
        thread.join(timeout=3)
        assert not thread.is_alive(), "心跳停止后服务未自动退出"
    finally:
        srv.shutdown()
        srv.server_close()


def test_heartbeat_timeout_zero_disables_watchdog(lib) -> None:
    """--heartbeat-timeout 0 用于长时调试：不启动 watchdog。"""
    srv, thread, _ = _hb_server(lib, heartbeat_timeout=0, startup_grace=1.0)
    try:
        assert srv.start_watchdog() is None
        assert srv.heartbeat_timeout == 0
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_frontend_reports_heartbeat() -> None:
    """前端必须周期性发心跳，并在可见性变化时补发。"""
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/humanity_memory/webtool/static/index.html").read_text(encoding="utf-8")
    assert "/api/heartbeat?visible=" in html, "前端未上报心跳"
    assert "document.visibilityState" in html, "心跳未区分前台/后台"
    assert "visibilitychange" in html, "切后台时未立即补发心跳"
    assert "setInterval(heartbeat,hbInterval)" in html, "心跳定时器缺失"
    assert 'id="heartbeat"' in html, "缺少心跳状态显示"

    # 失联倒计时：>5s 每 5s 跳、≤5s 每 1s 跳；心跳恢复要能撤销
    compact = html.replace(" ", "")
    assert "left>5?5000:1000" in compact, "失联倒计时缺少 5s/1s 分档节奏"
    assert "前端失联 · 后端将在" in html, "缺少失联倒计时文案"
    assert "stopLostCountdown()" in html, "心跳恢复后未撤销失联倒计时"
