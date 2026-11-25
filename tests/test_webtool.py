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
        # 只比「真写入才会变」的量：主库 (mtime, size) + WAL 待落盘字节数。
        # -shm 的 mtime 与 -wal 的存在性会被只读连接自己改动（登记 read mark、
        # 最后一个连接关闭时清理空 WAL），放进来就是假阳性，测不出真写入。
        st = (lib / "workspace.db").stat()
        d["db"] = (st.st_mtime_ns, st.st_size)
        wal = lib / "workspace.db-wal"
        d["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        for p in lib.glob("retrieval/*.jsonl"):
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
        libs = get("/api/libraries")
        assert libs["ok"] is True and libs["current"], "库清单必须自带当前库，前端才能回显下拉"
        assert any(item["label"] == libs["current"] for item in libs["libraries"])
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


# ---------------------------------------------------------------- 10. 状态签名不抖动


def test_signature_stable_across_reads(tmp_path: Path) -> None:
    """只读取数不得改变签名（读 ≠ 数据变了）。

    回归：签名曾把 `workspace.db-shm` 算进去。WAL 的 shm 是共享内存索引，
    没有别的连接常驻时，只读连接每查一次都要重新登记 read mark，文件 mtime
    随之上涨 —— 于是「自己读一次」被判成「数据变了」：前端每 4s 一轮
    「重载 iframe → /api/starmap 读库 → 签名又变 → 再重载」，星图整页重算，
    视图缩放与拖拽位置每次被重置。

    这里必须**先关掉写入端的连接**再建 WebSource：有常驻写连接时 shm 的
    mtime 反而稳定（页已在映射里），那样测等于没测。关掉它复现的正是
    「单开观测台看库」的现场。
    """
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    layout = resolve_layout(proj, home=cfg.home)
    store = MemoryStore(cfg, layout)
    try:
        store.write_memory("签名不抖动的被测记忆", importance=0.5)
    finally:
        store.close()

    src = WebSource(layout.library_dir)
    before = (src.sig_starmap(), src.sig_review())
    for _ in range(3):
        conn = src.connect()
        try:
            conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()
            conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        finally:
            conn.close()
    assert (src.sig_starmap(), src.sig_review()) == before, (
        "只读查询改变了签名：前端会把『自己在读』当成『数据变了』，陷入循环重载"
    )


def test_signature_changes_on_real_write(lib) -> None:
    """真实写入必须改变签名 —— 去掉 shm 不能顺手把「实时刷新」也修掉。"""
    store, libdir = lib
    src = WebSource(libdir)
    before = src.sig_starmap()
    store.write_memory("签名必须跟着真实写入变化", importance=0.5)
    assert src.sig_starmap() != before, "库真的变了签名却没变：前端将不再自动刷新"


# ---------------------------------------------------------------- 8. 库切换（不依赖启动参数）


def _make_lib(home: Path, base: Path, name: str) -> Path:
    """按真实布局造一个库（`<root>/.ai_memory/workspace.db`），返回库目录。"""
    proj = base / name
    (proj / ".ai_memory").mkdir(parents=True)
    cfg = Config.load({"HM_HOME": str(home), "HM_EMBED_DIM": "8"})
    store = MemoryStore(cfg, resolve_layout(proj, home=cfg.home))
    store.close()
    return proj / ".ai_memory"


def test_library_pool_lists_registry_and_switches(tmp_path: Path, monkeypatch) -> None:
    """观测台脱离启动 target：registry 里的库就是下拉清单，切库只换当前观测对象。

    守三件：
    1. 清单按 registry 标签排序，且「登记了但目录不在」的库标成不可用而不是消失；
    2. 切库成功/失败都不破坏当前态（失败时仍是原来那个库）；
    3. 显式 target 只是一种额外条目，标签仍以 registry 为准。
    """
    from humanity_memory.webtool.datasource import SourceError
    from humanity_memory.webtool.libraries import LibraryNotFound, SourcePool

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HM_HOME", str(home))
    monkeypatch.setenv("HM_LIBRARIES", str(tmp_path / "no_libraries"))  # 隔离本机 .env

    alpha = _make_lib(home, tmp_path / "proj_a", "alpha")
    beta = _make_lib(home, tmp_path / "proj_b", "beta")
    ghost = tmp_path / "gone" / "ghost"
    (home / "registry.json").write_text(
        json.dumps({
            "version": 2,
            "salt": "0123456789abcdef",
            "libraries": {
                "alpha": {"path": str(alpha)},
                "beta": {"path": str(beta)},
                "ghost": {"path": str(ghost)},
            },
            "keys": {},
        }),
        encoding="utf-8",
    )

    pool = SourcePool()
    assert pool.current_label == "alpha", "默认应落在第一个可用的库（按标签排序）"
    labels = [e.label for e in pool.entries]
    assert labels == ["alpha", "beta", "ghost"]
    assert [e.ok for e in pool.entries] == [True, True, False]
    assert "库目录不存在" in (pool.entries[2].error or "")

    state = pool.switch(label="beta")
    assert state["current"] == "beta"
    assert pool.source.db == beta / "workspace.db"
    assert pool.source.session()["library"] == "beta"

    for bad in ("nope", "ghost"):  # 不在清单 / 登记了但打不开：都不许把当前库弄没
        with pytest.raises(LibraryNotFound if bad == "nope" else SourceError):
            pool.switch(label=bad)
        assert pool.current_label == "beta"

    # 显式 target：当成额外条目并入，标签按 registry 认（而不是目录名 proj_a）
    single = SourcePool(alpha)
    assert single.current_label == "alpha"
    assert [e.label for e in single.entries] == ["alpha", "beta", "ghost"]
