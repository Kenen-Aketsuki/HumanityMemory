# 外部可视化与分析工具 · 开发文档

> 状态：**设计稿（待评审）** · 日期：2026-11-20 · 作者：大肥鱼 🐋
>
> 一句话：把现有的星图、检索分析，以及只有 md/json 的裁定报告，连同全新的「复盘记录」
> 「记忆现状」「检索测试」，收进一个 **本地 HTTP 后端 + 单页 HTML 前端** 的工具里；
> 传一个 token 或库根目录即可启动，前端实时跟进库的当前状态。
>
> v2 变更（2026-11-20 17:50）：原「judge 历史」改名**复盘记录**并扩展为按天记录
> 等级/活跃度变动 + 新增与顶替；「当前全量记忆」从复盘记录里抽离成独立页**记忆现状**，
> 显示实时活跃度、遗忘等级、距下次裁定时间。

---

## 0. 目标与范围

| 项 | 内容 |
| --- | --- |
| 启动方式 | `humanity-memory analyze <token路径 或 库根目录> [--port] [--no-browser] [--heartbeat-timeout] [--hidden-grace] [--startup-grace]` |
| 后端 | Python 标准库 `http.server`，只监听 `127.0.0.1`，全程只读 |
| 前端 | 单页 HTML（统一外壳 + 5 个视图），无 CDN、无外部依赖、断网可用 |
| 实时性 | 前端轮询数据源签名，库/报告一变就重新拉对应视图，不缓存陈旧快照 |
| 进程生命周期 | 前端每 3s 报心跳；可见页 10s / 后台页 60s 无心跳则后端自动退出，不留僵尸进程（§8.1） |
| 五大视图 | ① 星图可视化（现有）② recall 历史分析（现有）③ **复盘记录**（按天，原 judge 历史扩展）④ **记忆现状**（实时，新）⑤ 检索测试（全新） |

**明确不做**：不改记忆本体、不写活跃度、不写检索日志、不触发裁定、不接外部服务。
这个工具是**观测台**，不是第二个写入口。

---

## 1. 现状盘点

| 能力 | 取数实现 | 渲染实现 | 产物 | 启动入口 | 数据源 |
| --- | --- | --- | --- | --- | --- |
| 星图 | `starmap.py`: `resolve_db` → `read_nodes`/`read_vectors` → `build_graph` | `render_html(graph)` 塞自包含 HTML | 单个 `.html` | `starmap.bat` → `humanity-memory starmap <path>` | 库根目录 `workspace.db`（只读 `mode=ro`） |
| recall 历史 | `reports.py`: `read_retrievals` + `read_node_activity` + `latest_judge_snapshot` + `level_thresholds` | `retrieval_view.py`: `render_retrieval_html(records, meta)` | 单个 `.html` | `report.bat` → `humanity-memory retrieval-report` | 工作区 `retrieval/*.jsonl` + 库实时 `activity` |
| **复盘记录**（原 judge 历史） | `reports.py`: `write_judge_report` 落 `.md` + `.json`；`build_judge_report` 可重建快照 | **无 HTML**，只有 `render_judge_markdown` | `reports/judge/<stamp>.{md,json}`，文件名 `YYYYMMDD-HHMMSS` | 由 `lifecycle judge` 旁路生成 | 工作区 `reports/judge/*.json` + 库实时字段（新增/顶替） |
| **记忆现状**（新） | 直接只读扫 `knowledge`/`episodes` 当前行 + `lifecycle.LevelTable.period_ms()` 算倒计时 | 无（本工具新增） | 无（纯实时，不落盘） | 本工具内 | 库根目录 `workspace.db`（只读） |
| 检索测试 | 无 | 无 | 无 | 无 | 需要用 `store.search()` 现算 |

> 注：复盘快照的 `items[]`（本次裁定）与 `nodes[]`（全量节点）是**两个不同时刻**的活跃度 ——
> `items[].activity` 是裁定那一刻的值，`nodes[].activity` 是裁定**落库后**从库里读回的值。
> 根因：`_settle_period()` 会 `activity = 0`（`store.py:1218`），而报告在裁定落库之后才写
> （`store.py:972-983`）。这个区别直接决定复盘记录视图的字段口径，详见 §6.1。

关键复用点：三个工具已经有**纯函数级**的取数与渲染逻辑（`build_graph` / `render_html` /
`read_retrievals` / `render_retrieval_html`）。后端要做的是给它们套一层 JSON API，
**不重写取数逻辑** —— 这是保证"可视化与工具本身不漂移"的前提。

---

## 2. 关键约束与现状陷阱

### 2.1 recall 与 search 的活跃度语义（本需求的核心）

这是整个"检索测试"能否成立的地基，必须先讲清：

| 入口 | 强化命中项 | 写检索日志(JSONL) | 说明 |
| --- | --- | --- | --- |
| `store.recall()` | ✅ `_reinforce()` 写库 | ✅ `_log_retrieval()` | 「用进废退」的「进」，活跃度按相关性平方累加 |
| `store.search()` | ❌ | ❌ | 纯只读检索，刻意不改访问强度 |
| `store.deep_recall()` | ⚠️ 会 `revive` 遗忘层（改库） | ❌（本身不写，由 recall 代写） | 不能当只读用 |

> 代码证据：`cli.py:287-343`、`store.py:1550-1646`、`store.py:1971-2017`。
> 记忆库里的结论也一致："search 刻意不改访问强度；日常拿 search 顶替 recall 会让活跃度算成空的。"

**结论：检索测试必须走 `search()`，绝不调 `recall()` / `_reinforce()` / `deep_recall()`。**

### 2.2 复盘闸门（gate）的边界

`gate.py` 目前拦的是 CLI / MCP 的**日常检索入口**（裸 `search` 需复盘时间窗或通行证）。
检索测试是**分析/调参用途**，不该被闸门拦截，理由有三：

1. 它调 `search()` 直连 store，不经过 CLI 的 `cmd_search`、也不经过 MCP 的 `memory_search`；
2. 它不写活跃度、不写 JSONL，对活跃度零影响 —— 不存在"拿 search 顶替 recall 把活跃度算空"的问题；
3. 闸门管的是"使用记忆"的入口，观测台属于"查看记忆"，是两类行为。

文档与 UI 都要显式标注这一点，避免日后被误当成"绕过闸门的后门"。

### 2.3 数据源分裂（库目录 vs 工作区目录）

这是最容易踩的坑：

- **星图**读库根目录的 `workspace.db`（schema、向量、节点）；
- **recall / judge 历史**读工作区目录下的 `retrieval/`、`reports/judge/`；
- 在共享库布局（`libraries/<label>`）里，库目录**同时**承担工作区角色 ——
  实测 `libraries/humanitymemory/`、`libraries/island/`、`libraries/majotavern/` 下都带着
  `reports/` 和 `retrieval/`；
- 但用户给的 `libraries/lilith/` 目前**只有** `workspace.db` + `CORE_RULES.md`，
  还没有这两个目录 → 工具必须优雅处理"暂无历史数据"，给空状态提示，**不能报错退出**。

统一入口用 `bindings.resolve_readonly(target)`：它同时返回 `library_dir` / `workspace_dir` /
`workspace_db`，一次定位解决三个数据源。

### 2.4 只读承诺

沿用星图的规矩：所有 DB 连接 `mode=ro`（路径 `quote(as_posix)` 以吃中文/空格）；
不建表、不 checkpoint、不写任何文件到库目录。唯一的"写"是后端自身不落盘 —— 请求级别现算现读。

### 2.5 版本适配（待实测）

`reports.py` 的 judge 取数读 `knowledge` / `episodes` 表，而 `store.py` 用 `EXPERIENCE.table`
（v4 后为 experience）。接入时对 v4/v5 库各实测一次表名，必要时在 `datasource` 层做表名兼容，
**不要散落到各视图里各写一套**。

---

## 3. 总体架构

```
                          浏览器（单页 HTML）
   ┌────────┬────────┬────────┬────────┬────────┐
   │ 星图   │recall  │复盘记录│记忆现状│检索测试│   ← 5 个 tab
   └───┬────┴───┬────┴───┬────┴───┬────┴───┬────┘
       │        │        │        │        │ POST query
       └────────┴────────┴────────┴────────┘  fetch JSON
                    HTTP / JSON（localhost only）
   ┌─────────────────────────────────────────────────────┐
   │  webtool/server.py   ThreadingHTTPServer + 路由      │
   │  ├─ /api/session      库身份 / schema / 计数          │
   │  ├─ /api/starmap      → starmap.build_graph          │
   │  ├─ /api/recall       → reports.read_* + level_*     │
   │  ├─ /api/review       → 扫 judge/*.json + 库实时字段  │
   │  ├─ /api/memory       → 扫当前节点 + 实时裁定倒计时    │
   │  ├─ /api/search-test  → store.search + 模拟 delta     │
   │  └─ /api/status       数据源签名（供轮询）             │
   ├─────────────────────────────────────────────────────┤
   │  webtool/datasource.py  统一定位 + 只读取数适配层      │
   │  webtool/simulate.py    检索测试（纯算，不落库）        │
   └──────────────────┬──────────────────────────────────┘
                      │ bindings.resolve_readonly(target)
   ┌──────────────────▼──────────────────────────────────┐
   │  数据源：workspace.db（ro） + retrieval/*.jsonl       │
   │          + reports/judge/*.json                      │
   └─────────────────────────────────────────────────────┘
```

**数据流原则**：后端只做「解析目标 → 调现有纯函数 → JSON 序列化」三件事；
前端负责把 JSON 画成人能看的界面。取数逻辑单源，HTML 模板只留一套（前端），
避免现在「starmap 一套 HTML、retrieval 另一套 HTML」的样式分叉。

---

## 4. 启动与库定位

### 4.1 命令形态

```
humanity-memory analyze <target> [--port 8765] [--no-browser]
    [--heartbeat-timeout 10] [--hidden-grace 60] [--startup-grace 60]
```

`--target` 接受（与 `starmap` / 只读工具一致）：

1. 令牌文件 `.ai_memory/token.json`（或令牌所在目录）；
2. 库根目录，如 `<安装根>/libraries/<库名>`；
3. 项目根目录（经令牌追到库）；
4. 直接的 `workspace.db` 文件。

统一交给 `bindings.resolve_readonly()`，拿不到库就按现有拒绝契约给 code + 处置建议，
不静默生成空库。

### 4.2 启动动作

1. 解析 target → layout，校验 `workspace.db` 存在且 schema 可读；
2. 探测 `retrieval/`、`reports/judge/` 是否存在（缺失记为"暂无数据"，非致命）；
3. 起 `ThreadingHTTPServer(("127.0.0.1", port), Handler)`，租约初始为启动宽限期；
4. 打印 `http://127.0.0.1:<port>` 与心跳策略；`--no-browser` 之外则调默认浏览器打开；
5. 起 watchdog 守护线程；Ctrl+C / 心跳超时都会走 `shutdown()` 优雅退出，无临时文件残留。

> 端口被占：默认 8765，失败时提示 `--port <其它>`，不静默换端口。
> `--heartbeat-timeout 0` 关闭自动退出（长时调试用），此时 watchdog 不启动。

---

## 5. HTTP API 设计

全部 `GET` 只读；仅检索测试用 `POST`。错误统一
`{"ok": false, "code": "...", "message": "..."}`。

| 端点 | 方法 | 返回要点 | 复用的现有实现 |
| --- | --- | --- | --- |
| `/api/session` | GET | `{db, dir, schema_version, node_count, has_retrieval, has_judge, levels}` | `starmap.inspect_schema` / `reports.level_thresholds` |
| `/api/starmap` | GET | `{graph:{nodes,edges,meta}}` | `starmap.build_graph` + 等级/活跃度逻辑（从 `run()` 抽出可复用段） |
| `/api/recall` | GET | `{records, meta:{levels,nodes,activity,generated}}` | `reports.read_retrievals` 等四个函数 + `render_retrieval_html` 的 meta 组装 |
| `/api/review` | GET | `{days:[{date, at, changes:[...], activity_deltas:[...], added:[...], replaced:[...], deduped:[...]}]}`，按天倒序；字段见 §6.1 | 扫 `reports/judge/*.json` + 逐份解析 + 库实时 `created_at`/`invalidated_by`/`memory_events` |
| `/api/memory` | GET | `{nodes:[{id,kind,level,activity,next_judge_ms,due,...}], meta:{levels,now}}`，实时算倒计时 | 只读扫 `knowledge`/`episodes` + `LevelTable.period_ms()`；字段见 §6.2 |
| `/api/search-test` | POST | `{hits:[...], simulated:[...], took_ms, note}` | 见 §7 |
| `/api/status` | GET | `{starmap:"<sig>", recall:"<sig>", review:"<sig>", memory:"<sig>", now}` | 见 §8 |
| `/api/heartbeat?visible=1\|0` | GET | `{ok, now, interval_ms, timeout, remaining}`；**只 touch 租约，不读库、不算签名** | 见 §8.1 |

**重构要求**：`starmap.run()` 目前把「取数 + 等级/活跃度 + 组装 graph + 写文件 + 打印」揉在一起。
需抽出 `build_graph_payload(db, args) -> dict`（纯取数，不写文件），CLI 的 `run()` 与后端
`/api/starmap` 都调它 —— 保证两条路径的星图完全一致。

---

## 6. 前端设计

单个 `index.html`（内联 CSS/JS 或同目录静态文件），深色主题，延续现有两个 HTML 的视觉语言
（同一套 CSS 变量：`--bg:#0b0f16` / `--panel:#121821` 等），五个 tab：

| Tab | 视图内容 | 数据来源 | 渲染方式 |
| --- | --- | --- | --- |
| 星图 | 力导向图 + 图例 + 数据源面板 | `/api/starmap` | **iframe 嵌入老工具页**（见下） |
| recall 历史 | 指标条 + 左记录 / 中详情 / 右热度榜 + 自动诊断 | `/api/recall` | **iframe 嵌入老工具页**（见下） |
| **复盘记录** | 左侧**按天**时间轴 + 右侧四块：① 遗忘等级变动 ② 活跃度变动 ③ 新增与顶替 ④ md 原文 | `/api/review` | `views.js` 自绘 |
| **记忆现状** | 当前全量记忆表：活跃度 / 遗忘等级 / **距下次裁定时间**，可排序筛选 | `/api/memory` | `views.js` 自绘 |
| 检索测试 | 查询输入框 + 参数（limit/kinds） + 结果表（分数/向量·词面/活跃度/**模拟增量**） | `POST /api/search-test` | `views.js` 自绘 |

**星图 / recall 复用老工具全文渲染逻辑（实施期重要修正）**：最初这两个视图用 `views.js`
自写简化版，结果交互全丢（无法拖拽/缩放/悬停）、UI 明显逊于老的 `starmap.bat` /
`report.bat` 产物。实施改为：新增 `webtool/live.py`，在运行时把 `starmap._HTML_TEMPLATE`
和 `retrieval_view._TEMPLATE` 的 `/*__DATA__*/` 注入点换成一段 async loader（fetch API →
DATA → 执行原脚本主体），老模板的 CSS/JS **一行不改**；server 暴露 `/starmap.html` /
`/recall.html`，主界面对应 tab 用 iframe 嵌入。这样老工具成熟的交互（拖拽节点、滚轮缩放、
平移、悬停全文、点击选中、边/节点类型筛选、语义阈值、搜索高亮、热度榜排序、自动诊断）
100% 保留，且渲染逻辑单点维护。`/api/starmap` 取 `graph` 键、`/api/recall` 响应体本身即 DATA，
与老报告结构逐字段一致，故可直接复用。iframe 首屏懒加载，签名变化时重载当前 tab。

两张新界面都不改变报告的生成方式，只给已有数据配可翻、可排序的视图。口径见 §6.1 / §6.2。

### 6.1 复盘记录视图（原 judge 历史）：按天展示口径

**分组**：文件名 `YYYYMMDD-HHMMSS.json` 的前 8 位即日历日；同一天多份报告（如补跑）取
`generated_at` 最新的一份作为该天快照，dry-run（`-dryrun`）单独标注、默认不参与 diff。
时间轴按日期倒序。

**每天展示四块**：

1. **当天遗忘等级变动**（快照 `items[]`，**可能为空**）
   - 空时显示「当天没有到期条目 / 无等级变动」，不留空表；
   - 列：`id` / 类型 / `L(from) → L(to)` / **变动时活跃度**（`items[].activity`，裁定那一刻的值）/
     动作 ↑↓=/ 依据（`reason`）。

2. **当天活跃度变动**（由相邻两天的快照 `nodes[]` 逐 id 算差，见下表口径）
   - 列：`id` / 类型 / 前一天活跃度 / 当天活跃度 / **变动值**（含正负色）；
   - 只显示"两天里都在"的条目；纯新增归到第 3 块，避免重复。

3. **新增与顶替情况**（本视图重点，库实时数据，**不是**快照字段）
   - **新增记忆**：`created_at`（经验）/ `created_at` 或 `day`（经历）落在当天的条目；
     列 `id` / 类型 / 等级 / 初始活跃度 / 内容摘要。
   - **顶掉老记忆**：`knowledge.invalidated_by IS NOT NULL AND invalid_at` 落在当天 —— 即
     "老结论被新结论取代"；列 **被顶掉的旧 id** / **顶替它的新 id**（`invalidated_by`）/
     旧内容摘要 / 顶替时间。实测 island 库有 5 条此类记录。
   - **去重合并（单独列，勿混入"顶掉"）**：`memory_events` 中 `event='dedup'` 且 `at` 落在当天 ——
     这是"新内容太像已有条目 → 不新建、给原条目加活跃度"，**原记忆没被删除**。
     列 命中 id / 原条目 id / `delta`。实测 island 库有 9 条。语义与"顶掉"相反，UI 必须分组区分。
   - 三块都空时显示「当天无新增 / 顶替 / 合并」。

4. **md 原文切换**：可切到该天报告的 Markdown 原文。

**活跃度变动的 diff 口径**（写死，否则会算错）：

| 规则 | 说明 |
| --- | --- |
| 对比基准 | 取**时间轴上前一天**的快照（同库、非 dry-run、按 `generated_at` 排序）；日历日相邻，不是"上一份报告" |
| 公式 | `diff = 当天 nodes[id].activity − 前一天 nodes[id].activity`（按 id 关联，负数显示下降色） |
| 前一天缺失 | 该 id 不出现在第 2 块（首次报告 / 中间几天没跑 judge / 纯新增） |
| 精度 | 保留 2 位小数，与 `items[].activity` 一致 |

> **语义提示（UI 必须显示）**：裁定后活跃度会被清零（`activity = 0`）。所以第 2 块的 diff 反映
> 「当天裁定完之后」相对「前一天裁定完之后」的差，**不是**"这一天被检索了多少次"。
> 检索命中增量看 recall 历史页的 delta 列。两组数字口径不同，混看会误判活跃度崩了。

**后端职责**：`/api/review` 一次性返回按天分组 + 算好 diff + 新增/顶替/合并明细，前端只渲染。
真实库实测：`island` 与 `humanitymemory` 均有 11-17 ~ 11-20 连续报告；某天 `items=[]` 是常态
（11-20 的 island 报告 due=0、100 节点、0 变动），空状态必须优雅处理。

### 6.2 记忆现状视图（新，实时）

与复盘记录**完全分离**：复盘记录看"历史上每天发生了什么"，记忆现状看"此刻每条记忆什么状态"。
数据全部实时从库读，**不读快照**（快照的 `remaining_days` 是裁定那一刻的切片，会过时）。

**表格列**：

| 列 | 数据源 / 算法 |
| --- | --- |
| `id` / 类型 / 内容摘要 | `knowledge` / `episodes` 当前行（同星图的 `read_nodes`） |
| 遗忘等级 | `forget_level`，按 `[[forget.levels]]` 配置显示 L 档 |
| 当前活跃度 | `activity` 实时值（复用 `reports.read_node_activity` 的读法） |
| **距下次裁定** | 见下 |
| 升降线 / 周期 | 来自等级表 `up_threshold` / `down_threshold` / `period_days`（前端可筛"已到期"） |

**距下次裁定时间怎么算**（关键，实时）：

```
span_ms = LevelTable.period_ms(level, dimension)     # lifecycle.py:205，经验/经历各自周期
next_ms = period_start + span_ms
remain_ms = next_ms - now_ms()

level <= 0        → 显示「遗忘层 · 不裁定」
period_start 为空 → 显示「未开始计时」
remain_ms <= 0    → 显示「已到期」（红色）
remain_ms > 0     → 显示「X 天 X 小时」或「X.X 天」（前端选一种，统一格式）
```

- `dimension` 按类型取经验/经历侧（沿用 `period_kind(spec.kind)`），与 `lifecycle.is_due`
  用的是同一套周期定义，保证"这里显示已到期"与"真正跑 judge 会裁定它"一致；
- 复用 `lifecycle.LevelTable`，不要在 webtool 里另写一份周期换算；
- 这是**实时**视图：每天日期翻页、时间流逝都会让倒计时变化，靠 §8 的轮询签名刷新
  （`/api/status` 里给记忆也加一个签名：库 mtime + 当前时间戳分钟级，保证倒计时会走）。

**只读约束**：本视图只读扫表 + 读配置，不裁定、不写库；"距下次裁定"是**预测**，不是触发。

---

## 7. 检索测试模块（重点）

### 7.1 语义

用户手动发起一次查询，程序**模拟 recall 的检索与打分流程**并展示结果，但**不产生任何写入**。
它回答的问题是："这条查询现在会捞到谁？分数多少？如果真跑 recall，各条会加多少活跃度？"

### 7.2 实现要点

```
POST /api/search-test  {"query": "...", "limit": 8, "kinds": null}
   │
   ├─ store.search(query, limit, kinds)      ← 纯只读，不强化、不写 JSONL
   │     → hits（score / vec_sim / lex / level / activity 现值）
   │
   ├─ 对每条 hit 用纯函数算「假想 delta」：
   │     fused = store._fuse(vec_sim, lex_sim)
   │     delta = activity_contribution(fused,
   │                 sim_floor=cfg.activity_sim_floor,
   │                 curve_power=cfg.activity_curve_power)
   │     ← 只用配置 + 纯函数计算，不 UPDATE、不写库
   │
   └─ 返回 {hits, simulated:[{id, delta, activity_after(假想)}],
            took_ms, note:"仅模拟：未写活跃度、未写检索日志"}
```

- **不调** `deep_recall()`：它会 revive 遗忘层、改库。检索测试默认只跑常规检索，
  UI 上给一句说明"深度回忆会改写遗忘层，故不在模拟范围内"。
- **不写** JSONL：`_log_retrieval` 不参与，避免污染 recall 历史分析的数据源。
- **不经过 gate**：见 §2.2，在 API 注释和 UI 提示里写明"分析用途，不计入活跃度"。

### 7.3 展示字段（对齐 recall 历史详情页，便于对照）

| # | 类型 | id | 等级 | 分数 | 向量/词面 | 当前活跃度 | 模拟增量 | 假想活跃度 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

---

## 8. 实时刷新机制

**签名（signature）** = 数据源的可变性摘要，后端每次现算：

| 数据源 | 签名输入 |
| --- | --- |
| starmap | `workspace.db` 的 `(mtime_ns, size)` + WAL 文件存在性/大小 |
| recall | `retrieval/` 下所有 `*.jsonl` 的 `(name, mtime_ns, size)` 汇总 |
| review（复盘记录） | `reports/judge/` 下 `*.json` 的最大 mtime + 文件数 + 库 mtime（新增/顶替取自库） |
| memory（记忆现状） | 库 mtime + **当前分钟的 unix 时间戳**（倒计时会随时间走，不能只靠库 mtime） |

前端每 4 秒请求 `/api/status`，与上次签名比对：**哪个变了就只重拉哪个视图**，
并在界面角落显示"最后刷新"时间。这样既不缓存陈旧快照，又避免无谓重建星图
（星图对当前库规模是全量算，没必要每 4 秒重算）。

约定：

- 后端**不做内存缓存**，每个 API 请求现读现算 —— 天然跟进库状态；
- 若某数据源被删空（如检索日志被清理），签名变化触发前端清空该视图并显示空状态；
- 若库正被其他进程写入（WAL 变化），签名会跟着变，前端自动刷新。

### 8.1 心跳探活与自动退出

轮询只证明"后端还活着"，不能证明"前端还在看"。之前关掉页面后后端会一直挂在端口上，
所以补一条反向契约：**前端报活、后端按租约自杀**。

| 项 | 值 | 理由 |
| --- | --- | --- |
| 前端心跳间隔 | 3s（由 `/api/heartbeat` 的 `interval_ms` 回传，前端据此设定时器） | 远小于最短超时，容忍偶发抖动 |
| 可见页超时 | 10s（`--heartbeat-timeout`） | 用户关页面 / 前端崩溃后快速回收端口 |
| 后台页宽限 | 60s（`--hidden-grace`） | 浏览器把后台 `setInterval` 节流到分钟级，按 10s 判会误杀 |
| 启动宽限 | 60s（`--startup-grace`） | 给浏览器启动留时间；期间不按正式超时判死 |
| 可见性切换 | `visibilitychange` 时立即补发一次 | 让后端尽早从 10s 租约切到 60s 租约 |

**前端失联倒计时**：心跳失败时，底部状态从「心跳正常 · …」切成
「前端失联 · 后端将在 Ns 后退出」。N 由最后一次成功心跳回传的 `remaining` 换算成本地截止时间；
**距退出 >5s 每 5s 跳一次，≤5s 每 1s 跳一次**并转警告色，归零显示「后端已退出」且停止定时器。
心跳一恢复就撤销倒计时、复原状态。正常运行时不显示倒计时——否则 3s 一次的心跳会把数字
反复刷回 10s，只剩无意义闪动。

实现位于 `WebServer`：`touch(visible)` 在锁内更新 `_last_seen` 与 `_lease`；
`start_watchdog()` 每 0.25s 比对 `monotonic() - _last_seen > _lease`，超时就从**守护线程**
调 `shutdown()`（`serve_forever` 在主线程，自己 shutdown 自己会死锁）。租约只在内存里，
不落盘、不进库 —— 观测台的只读承诺不变。

只有 `/api/heartbeat` 会 touch；状态轮询和检索请求都不算报活，否则一次慢查询期间
前端看似"还在通信"却实则已失联。心跳是裸 `fetch`，不走带 25s 超时的 `api()`，
避免慢请求把心跳也拖死。设 `--heartbeat-timeout 0` 可整条关掉。

---

## 9. 模块与文件清单

| 文件 | 职责 | 性质 |
| --- | --- | --- |
| `src/humanity_memory/webtool/__init__.py` | 包 | 新增 |
| `src/humanity_memory/webtool/server.py` | HTTP 路由 / 请求处理 / 启动 | 新增 |
| `src/humanity_memory/webtool/datasource.py` | target 定位 + 各视图取数适配（含表名兼容） | 新增 |
| `src/humanity_memory/webtool/simulate.py` | 检索测试（search + 模拟 delta，无写入） | 新增 |
| `src/humanity_memory/webtool/review.py` | 复盘记录取数：按天分组 + 活跃度 diff + 新增/顶替/合并明细 | 新增 |
| `src/humanity_memory/webtool/memory_state.py` | 记忆现状取数：实时节点 + `LevelTable.period_ms` 倒计时 | 新增 |
| `src/humanity_memory/webtool/live.py` | 把老星图/检索模板包成「启动 fetch API」的实时页 | 新增 |
| `src/humanity_memory/webtool/static/index.html` | 单页外壳（5 tab）；星图/recall 为 iframe，其余 views.js 自绘 | 新增 |
| `src/humanity_memory/starmap.py` | 抽出 `build_graph_payload(db, args)`；模板供 `live.py` 复用 | 小改（保持行为不变） |
| `src/humanity_memory/cli.py` | 注册 `analyze` 子命令 | 小改 |
| `analyze.bat`（项目根） | 双击/拖入启动；默认走源码 `.venv`，全英文避免 cmd GBK 乱码 | 新增 |
| `tests/test_webtool.py` | API 契约 + 只读断言 + 模拟不落库 + 老模板复用 | 新增 |

依赖：仅标准库（`http.server` / `sqlite3` / `json` / `threading`）；前端零依赖。

---

## 10. 分阶段实施计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P1 骨架 | `analyze` 命令 + server 起停 + `/api/session` + `/api/status` + 前端外壳空 tab | 传 lilith 库根能起服务、session 显示 schema/计数 |
| P2 现有两视图 | `/api/starmap`（含 `build_graph_payload` 抽取）+ `/api/recall`，接前端 | 与现有 starmap.bat / report.bat 产物**逐字段一致** |
| P3 复盘记录 | `review.py` + `/api/review`：按天分组 + 活跃度 diff + 新增/顶替/合并 + 前端时间轴/md 切换 | 有报告的库（如 island）能按天翻；diff 口径正确（§6.1）；`items=[]` 与 lilith 空库显示空状态 |
| P4 记忆现状 | `memory_state.py` + `/api/memory` + 前端实时表（§6.2） | 倒计时与 `lifecycle.is_due` 判定一致；"已到期"条目与真跑 judge 的 due 对得上 |
| P5 检索测试 | `simulate.py` + `/api/search-test` + 前端表单 | 同查询跑 search-test 前后：库 mtime 不变、JSONL 不增行、activity 不变 |
| P6 实时刷新 + 打包 | 轮询签名（含 memory 分钟签名）、刷新提示、`analyze.bat`、README 回写 | 另一进程写库/产生检索记录，前端 4s 内自动更新；倒计时会走；离线构建可用 |

每阶段结束跑**全量测试**（当前 387 项基线），新增测试必须覆盖"只读断言"。

---

## 11. 测试策略

1. **纯函数契约**：`build_graph_payload` 的输出与旧 `run()` 写出的 graph 结构一致；
   `/api/recall` 的 records/meta 与 `cmd_retrieval_report` 塞进 HTML 的 DATA 一致。
2. **只读断言（硬性）**：search-test 调用前后对库目录做快照
   （`workspace.db` + `-wal` + `-shm` 的 mtime/size、`retrieval/*.jsonl` 行数、
   `activity` 列抽样值），三者全部不变才算通过。
3. **空数据源**：无 `retrieval/`、无 `reports/judge/` 的库（lilith）各端点返回空结构而非 500。
4. **中文路径**：用带中文/空格的库目录验证 `mode=ro + quote(as_posix)` 取数正常。
5. **实时刷新**：模拟新增一条 JSONL 记录，验证 `/api/status` 签名变化、前端触发重拉。
6. **复盘记录按天与 diff（§6.1）**：用 island / humanitymemory 的真实报告做固定夹具，断言
   ① 同一天多份只取最新、dry-run 默认排除；② `diff = 当天 − 前一天` 按 id 关联、保留 2 位、
   纯新增不进 diff 块；③ `items=[]` 时 changes 为空不报错；④ 新增按 `created_at` 归当天；
   ⑤ `invalidated_by` 进"顶替"块、`event='dedup'` 进"合并"块，两者不混（island 实测 5 / 9 条）。
7. **记忆现状倒计时（§6.2）**：断言 `next_ms = period_start + LevelTable.period_ms(level, dimension)`
   与 `lifecycle.is_due` 同判据；level≤0→遗忘层、`period_start` 空→未计时、remain≤0→已到期。
8. **心跳探活（§8.1）**：心跳端点按 visible 返回不同租约；不发心跳时 watchdog 真的让
   `serve_forever` 线程退出；持续心跳期间不被误杀；`timeout=0` 不启动 watchdog；
   前端 HTML 必须含 periodic 心跳 + `visibilitychange` 补发。

---

## 12. 风险与待确认（请欧尼酱拍板）

| # | 决策点 | 推荐 | 影响 |
| --- | --- | --- | --- |
| 1 | 前端形态：统一单页 5 tab vs 保留独立 HTML 加导航页 | **统一单页**（样式单源，实时刷新好做） | 需要把两份 HTML 的渲染逻辑搬到前端 JS |
| 2 | HTTP 栈：标准库 `http.server` vs 引入 FastAPI/Flask | **标准库**（零依赖、符合项目风格、够用） | 并发用 ThreadingHTTPServer；上量再换 |
| 3 | 复盘记录的 md 展示：前端渲染 vs 原文预览 | **表格为主 + md 原文切换** | 需决定 md→html 是否引第三方（倾向纯文本预览，不引依赖） |
| 4 | 检索测试是否展示"深度回忆" | **不展示**（deep_recall 会改库，破坏只读承诺） | UI 加一句说明即可 |
| 5 | 版本适配（v4/v5 表名） | 在 `datasource` 层做兼容，实测后定 | 影响 §2.5，需在 P2 前确认 |
| 6 | 端口默认 8765 是否合适 | 可改；占用时提示不静默换 | 无 |
| 7 | **"顶掉老记忆"的口径**：只认 `invalidated_by` vs 也纳入 `dedup` | **分开两块显示**：`invalidated_by`=顶替（旧条目被取代）、`dedup`=合并（原条目保留、只加活跃度） | 语义相反，混在一起会误判"记忆丢失"。island 实测 5 / 9 条 |
| 8 | **距下次裁定**倒计时的精度 | 建议「X.X 天」或「X 天 X 小时」二选一 | 前端统一格式即可；已到期红色标注 |
| 9 | 记忆现状要不要显示 0 级（遗忘层）条目 | **显示但标注「遗忘层·不裁定」**，可筛选隐藏 | 与星图"复盘恒星"口径一致，不凭空丢数据 |

> 评审通过后按 P1→P6 实施；任何阶段出现活跃度被写、JSONL 增行的证据，立即停手回查 §7.2。
