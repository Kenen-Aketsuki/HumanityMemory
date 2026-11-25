# HumanityMemory

基于 **SQLite + sqlite-vec** 的本地仿生记忆库，以 **MCP server + Skill 集合**的形式提供给智能体使用。

零服务依赖：一个记忆库就是一个 `.db` 文件，向量索引、全文索引、元数据全在里面，不上云。

| 项 | 现状 |
| --- | --- |
| 版本 | `2.0`（库 schema `v5`；本地构建号形如 `2.0-local.N`，不跟 git） |
| 运行环境 | Python ≥ 3.10；运行时依赖只有 `sqlite-vec` / `mcp` / `httpx`（3.10 另加 `tomli`） |
| 记忆类型 | 经验 `experience` / 经历 `episode` / 日志 `log`，外加一层不进库的信条 |
| 对外形态 | 29 个 MCP 工具 + 21 个 CLI 子命令 + 1 个 Skill |
| 库的位置 | 新建（`bind new`）在 `<安装根>/libraries/<label>`；沿用（`bind adopt`）在 `<项目>/.ai_memory` |
| 单元测试 | 392 项 |

```
┌──────────────────────────────────────────────────────────────┐
│  信条  CORE_RULES.md  →  静态注入 System Prompt，不进向量库    │
│        全局一份 + 每个项目一份                                │
└──────────────────────────────────────────────────────────────┘
                            │ 不参与检索
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  经验  knowledge  可复用的判定与结论 · 不压缩 · 可被推翻      │
│                   可信度靠证据涨落，不靠日历复核               │
│  经历  episodes   发生过的事 · 带时间 · 随遗忘等级逐级压缩      │
│  日志  logs       当天原始记录 · append-only · 复盘后清空      │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
  workspace.db（库目录）
  物理隔离，换库就看不到另一个库的内容
```

## 分层判据

**判据只有一条：它是否需要回答「什么时候发生的」。**

需要 → 经历（带时间、会压缩）；不需要 → 经验（可复用的结论、不压缩、可被推翻）。

**易变与否不构成独立类型，只影响录入时的可信度。** 版本号、依赖、路径这类现状，
按低初始可信度（`0.3`）录入，之后靠证据（`memory_verify`）涨落。

结构上的纪律由**数据库触发器**保证，不依赖应用层自觉：

- `logs` 表改正文 → `RAISE(ABORT)`
- 未复盘的日志删不掉
- 经验正文不可改，只能追加新条目并让旧的失效
- 已失效的经验不能"复活"

## 特性

- **信条不进检索**：硬约束靠静态注入 100% 命中，不赌召回率
- **一类一张物理表**：经验 / 经历 / 日志各有自己的归档 TTL（3 年 / 1 年 / 复盘即清）
- **可信度靠证据**：`memory_verify` 按**间隔**加分（隔半年再验一次，比昨天验过信息量大），
  证伪扣分但**不删行**——「被推翻的结论」本身也是知识
- **读到低分条目会提醒**：低于 `verify_on_read` 的命中带 `·待验证` 标记
- **遗忘由等级驱动**：0~10 共 11 档，一个周期裁定一次、一次只动一级，事后能回答「它为什么在这一级」
- **未接入就什么都不做**：没有令牌不建目录、不建库、不翻"上次的工作区"，只返回错误码 + 清单
- **库跟着安装走**：`bind new` 把库放进 `<安装根>/libraries`，项目里只留身份锚点与信条，
  备份 / 提交 / 分享都不会捎带 MB 级二进制
- **混合检索**：余弦向量 KNN + FTS5 trigram + bigram 兜底，严格阈值 + 可配置兜底层
- **复盘由当前会话的模型执行**：不额外配 LLM；模型只写条目、给初值、报证据，分值等级全归工具
- **优雅降级**：没配 embedding 也能用，退化为关键词检索

## 安装

```bash
cd Your/Path/To/HumanityMemory

python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .        # Windows
# source .venv/bin/activate && pip install -e .       # macOS / Linux

# 配置只认仓库根这一份，换目录启动也读得到
cp .env.example .env
# 编辑里面的 embedding 配置

./.venv/Scripts/humanity-memory.exe doctor
```

`doctor` 会打印配置来源、库落点、embedding 是否可用、以及当前目录的接入状态。

## 配置

### 凭据与路径：`.env`

**只认一个 `.env`**：源码态是 `<仓库根>/.env`；冻结态是 exe 所在 `current/` 的上一级
（`<发布根>/.env`）。`config.py` 相对自身位置定位它，无论从哪个目录启动，读到的都是同一份。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HM_HOME` | `~/.humanity-memory` | registry / 信条 / 诊断目录 |
| `HM_LIBRARIES` | `<安装根>/libraries` | 共享库根目录 |
| `HM_WORKSPACE` | — | 显式指定工作区，跳过 cwd 查找 |
| `HM_RULES_PATH` | — | 显式指定信条文件 |
| `HM_EMBED_BASE_URL` | — | OpenAI 兼容端点 |
| `HM_EMBED_MODEL` | `BAAI/bge-m3` | 模型名 |
| `HM_EMBED_API_KEY` | — | 本地 Ollama 可留空 |
| `HM_EMBED_DIM` | `1024` | **必须与模型实际维度一致** |
| `HM_EMBED_BATCH` / `HM_EMBED_TIMEOUT` | `32` / `30` | 批量与超时 |
| `HM_VEC_EXTENSION` | 自动 | `sqlite-vec` 加载失败时的手动路径 |

真实环境变量优先级高于 `.env`（令牌注入、`mcp.json` 的 `env`、命令行覆盖都走这一层）。

### 行为参数：四层 TOML

阈值、等级表、闸门这些**以 TOML 为准**，四层深合并，后者逐键覆盖前者：

| 层 | 位置 | 用途 |
| --- | --- | --- |
| 出厂 | 包内 `memory.default.toml` | 全量定义，随升级替换，不要改 |
| 安装 | `<安装根>/memory.toml` | `humanity-memory config template` 生成的全量模板 |
| 用户 | `~/.humanity-memory/memory.toml` | 跨项目偏好，写增量 |
| 库 | `<库目录>/memory.toml` | 单项目调参，跟着库走 |

只写要改的那几行即可。数组表（`[[forget.levels]]`、`[[initial.bands]]`）语义上是整体替换，
要改等级表就重写整段。改完要重启 MCP 客户端才生效（server 常驻，配置只在启动时读）。

少量键保留环境变量覆盖，便于临时压测：`HM_W_RETRIEVAL` / `HM_W_IMPORTANCE` / `HM_W_LEVEL`、
`HM_MIN_RETRIEVAL` / `HM_FALLBACK_*` / `HM_CANDIDATE_MULTIPLIER` / `HM_MAX_CANDIDATES`、
`HM_ARCHIVE_TTL_EXPERIENCE` / `HM_ARCHIVE_TTL_EPISODE` / `HM_ARCHIVE_MAX_ACCESS` /
`HM_PURGE_AFTER_DAYS` / `HM_REINFORCE_BOOST`。

### 关键参数

| 段 | 键 | 默认 | 含义 |
| --- | --- | --- | --- |
| `retrieval` | `w_retrieval` / `w_importance` / `w_level` | `0.72` / `0.12` / `0.16` | 三项打分权重 |
| | `level_factor_power` | `0.65` | 等级倍率的幂次 |
| | `deep_recall_floor` | `0.35` | 低于此相似度才转深度回忆 |
| `search` | `min_retrieval` | `0.78` | 常规检索相关性下限 |
| | `fallback_mode` | `maxpool` | 严格层没捞到时的兜底方式（`off` / `rrf` / `maxpool`） |
| | `candidate_multiplier` / `max_candidates` | `8` / `240` | 候选池放大与上限 |
| `dedup` | `dup_floor` / `dup_bonus` / `topk` | `0.90` / `0.30` / `3` | 经验录入查重 |
| `activity` | `sim_floor` / `curve_power` | `0.35` / `2.0` | 活跃度累加曲线 |
| `confidence` | `verify_on_read` / `treat_as_fact` | `0.5` / `0.85` | 读时提醒与视为事实的界线 |
| `forget` | `level0_purge_days` / `archived_purge_days` | `180` / `180` | 进删除候选的静默期 |
| | `max_gap_days` | `2` | 两次遗忘计算之间最多承认的流逝 |
| | `revive_hits` / `revive_to_level` | `2` / `1` | 0 级复活条件与落点 |
| `review` | `window` / `flag_minutes` / `daily_flag_limit` | `00:00-08:00` / `30` / `2` | 复盘闸门 |
| `report` | `judge_keep` / `retrieval_keep_days` | `30` / `90` | 旁路报告保留 |

`min_retrieval` 出厂给得偏严格（`0.78`）。换 embedding 模型后按这个办法重新标定：
拿一个与库内容完全无关的查询跑一遍，看它 top1 的 `vector_similarity`，往上留 `0.01~0.03` 余量，
再用两三个真实查询确认目标条目仍能命中。

### 免费 embedding 选项

| 方案 | 花费 | 维度 | 说明 |
| --- | --- | --- | --- |
| 硅基流动 `BAAI/bge-m3` | 0（限速） | 1024 | 需实名，中文效果最好 |
| 智谱 `embedding-3` | 0（送 2000 万 tokens） | 2048 | 无需实名 |
| 本地 Ollama `bge-m3` | 0，永久 | 1024 | 完全离线，`ollama pull bge-m3` |
| Cloudflare Workers AI | 0（1 万次/天） | 1024 | 国内网络一般 |

切换只改三项，**维度变了必须** `humanity-memory reindex --reset-vectors`。

## 接入一个项目

一个 server 服务所有项目。项目侧只做一件事：在项目根登记一份令牌（库本身在别处）。

```bash
cd D:/proj/YourProject

humanity-memory bind new                     # 新建独立库（标签默认取目录名）
humanity-memory bind new --label island      # 新建并指定标签
humanity-memory bind adopt                   # 把本项目已有的 .ai_memory/workspace.db 登记为自有库
humanity-memory bind existing --library island   # 并入已有库（A、B 共用一个库）
humanity-memory bind decline                 # 本次不接入（记住这个决定，以后不再问）
```

| 动作 | 库放在哪 | 适用 |
| --- | --- | --- |
| `bind new` | `<安装根>/libraries/<label>` | 新项目，库跟着安装走 |
| `bind adopt` | `<项目>/.ai_memory/workspace.db` | 这个目录里已经有库，原地登记 |
| `bind existing` | 沿用 registry 里那个库的路径 | 多个项目共享同一份记忆 |
| `bind decline` | — | 明确不接入，写 `library: null` |

未接入时**不会建目录、不会建库、也不会去翻「上次用过的工作区」**——返回错误码 + 一份可照做的清单
（`memory_bind_status` 能看到同样的内容）。

### 令牌（v2）

```json
{
  "version": 2,
  "key": "hmk_7f3a9c1e…",
  "workspace": "D:/proj/YourProject",
  "library": "island",
  "label": "island"
}
```

- 只有**身份**：没有 embedding 凭据（凭据走 `.env`），也没有库路径与信条路径。
- `library: null` 表示**明确不接入**（`bind decline` 的结果）。这是被记住的决定，
  不是"还没配"——它同样拒绝读写。
- `library` 是**断言，不是寻址**：真正打开哪个库由服务端 registry 决定。手改令牌里的 `library`
  拿不到别人的库，只会让校验失败。
- registry（`~/.humanity-memory/registry.json`）只存 `sha256(salt + key)`，明文钥匙只在令牌文件里。

### 启动时决定打开哪个库

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--workspace` / 工具入参 | 显式指定项目根（运维 / 调试） |
| 2 | `HM_WORKSPACE` | 环境变量，同上 |
| 3 | cwd 向上查找 | **只认含 `token.json` 的 `.ai_memory`** |
| 4 | 都没有 | 拒绝：`no_workspace` / `no_token` + 清单，不建任何目录 |

`--token <路径>` 只做路径覆盖（用令牌里的 workspace 当提示），放行与否仍由七步校验决定。

七步校验，任一失败即拒绝，失败点就是错误码：

```
根目录 → 令牌存在且可解析 → version == 2 → 令牌的 workspace 就是本根
      → registry 有这把 key 且未撤销 → registry 的 workspace / library 与令牌一致
      → library 非 null 且库目录存在
```

常见拒绝码：`no_workspace` / `no_token` / `token_bad` / `token_v1` /
`token_workspace_mismatch` / `library_conflict` / `declined` / `REVIEW_REQUIRED`。

运行中切库用 `memory_use_workspace(path="D:/proj/YourProject")`：同样先校验令牌，
通过后才重建 store 并**按新库重载配置**（库级 `memory.toml` 跟着库走）。

启动上下文（cwd、解析来源、命中的库、错误码）写进
`~/.humanity-memory/diagnostics/startup.json`，排查「为什么落到这个库」时先看它。

### 运维

```bash
humanity-memory bindings list                 # registry 里有哪些库、谁在用
humanity-memory --workspace <项目> doctor     # 接入状态 + 配置来源 + 库自检
humanity-memory revoke --workspace <项目>     # 撤销钥匙（不动库文件）
humanity-memory bind move --from <旧> --to <新>   # 项目搬家后修令牌
humanity-memory bind migrate --scan D:/work   # 扫描并迁移存量（默认 dry-run）
```

### 复用项目已有的信条

项目里已有 Markdown 信条（例如 `.workbuddy/memory/信条.md`）时会**自动探测并复用**，
不会另建一份 `CORE_RULES.md`。找不到才用库目录下的默认位置；需要指向别处用 `--rules` 或
`HM_RULES_PATH`。全局信条固定在 `~/.humanity-memory/CORE_RULES.md`。

## 注册为 MCP server

**发布态**（推荐）：`mcp.json` 里只有 exe 和 `serve`，不带任何项目路径。

```json
"humanity-memory": {
  "command": "Your/Path/To/HumanityMemory-Release/current/humanity-memory.exe",
  "args": ["serve"],
  "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
}
```

**源码态**：

```json
{
  "mcpServers": {
    "humanity-memory": {
      "command": "Your/Path/To/HumanityMemory/.venv/Scripts/python.exe",
      "args": ["-m", "humanity_memory.mcp_server"],
      "env": {
        "HM_EMBED_BASE_URL": "https://api.siliconflow.cn/v1",
        "HM_EMBED_API_KEY": "sk-你的key",
        "HM_EMBED_MODEL": "BAAI/bge-m3",
        "HM_EMBED_DIM": "1024"
      }
    }
  }
}
```

改完需要在连接器管理页面点「信任」。

## CLI

| 命令 | 说明 |
| --- | --- |
| `init` | 补齐信条 + 打开已有库自检（**不建库**，建库走 `bind`） |
| `bind new / adopt / existing / decline / move / migrate` | 接入、搬家、存量迁移 |
| `bindings list` / `revoke` | 查 registry / 撤销钥匙 |
| `workspace show / use` | 查看当前令牌 / 签发一份 v2 令牌 |
| `doctor` | 接入状态、配置来源、embedding、库自检 |
| `rules --show / --append` | 读 / 追加信条（`--scope` 取 `workspace` 或 `global`） |
| `memory add / list / verify / invalidate` | 经验的写与证据 |
| `log add / list` | 日志追加与按日查询 |
| `search <query> --recall` | 召回并强化命中项（日常入口） |
| `review status / begin / end` | 复盘闸门：查状态、签通行证、提前收回 |
| `consolidate preview / apply` | 复盘取料与提交 |
| `stats` / `config template` | 统计 / 生成安装层全量配置 |
| `lifecycle archive / judge / relevel / candidates / compress` | 归档、裁定、重算落档、删除候选、压缩 |
| `export --out <目录>` | 导出 Markdown 视图（必填落点） |
| `retrieval-report [--out]` | 检索记录渲染成单文件 HTML（含阈值诊断） |
| `starmap <目录> --out <HTML>` | 只读星图 |
| `reindex [--reset-vectors]` | 重建向量 |
| `upgrade <库> --out <新库> / --in-place` | 离线升级历史库，只产出 / 就地换库 |
| `serve` | 以 stdio 启动 MCP server |

```bash
humanity-memory log add "今天排查了 XXX，根因是 YYY"
humanity-memory memory add "ResolveTask 末尾的兜底会被短路" --kind experience --confidence 0.8
humanity-memory memory add "项目是 Unity 2022.3.62f1" --confidence 0.3 --source ProjectVersion.txt
humanity-memory memory add "Bug #10805 引导不指餐厅：三条静默出口" --kind episode
humanity-memory search "引导为什么不触发" --recall
```

> `search` 不加 `--recall` 是复盘专用的（只在时间窗内或持通行证时可用），
> 日常请走 `--recall`：它才会把命中项计入活跃度。见「复盘闸门」。

## 工具集

| 工具 | 说明 |
| --- | --- |
| `rules_get` / `rules_append` | 读 / 追加信条（只追加，不改既有内容） |
| `memory_write` | **唯一写入口**，`kind` = `experience` / `episode`；经验上限 500 字，录入自动查重 |
| `memory_verify` | 给经验落一条证据：`ok=true` 按间隔加分，`ok=false` 扣分（都不删行）；`level` 决定权重 |
| `memory_invalidate` | 让经验失效（不删除，保留历史）；对经历会拒绝 |
| `memory_promote` | 不提供提升：没有全局库可去，调用返回明确错误 |
| `memory_list` | 列条目，`kinds` 支持 `all` |
| `log_append` | 追加日志（唯一允许的日志写操作） |
| `log_list` | 按日期列日志 |
| `memory_search` | 复盘专用混合检索，不改访问强度（受闸门约束） |
| `memory_recall` | 检索 + 强化命中项；常规捞不到时自动兜一层深度回忆 |
| `memory_deep_recall` | 只翻遗忘层（0 级）；命中的经历累计复活次数 |
| `memory_judge` | 跑到期裁定：按周期活跃度升降等级，没到期什么都不写 |
| `consolidate_preview` | 取待复盘日志（不返回存量经验，查重交给写入） |
| `consolidate_apply` | 提交复盘结果 |
| `memory_stats` | 统计与健康提示 |
| `memory_maintenance` | 补 embedding、返回待复盘与整理提示 |
| `memory_archive` / `memory_unarchive` | 归档 / 恢复（恢复会重新嵌入） |
| `memory_purge_candidates` / `memory_purge` | 删除候选（只报告） / 物理删除（已归档或超期 0 级） |
| `memory_compress_preview` / `memory_compress_apply` | 压缩队列与提交 |
| `memory_use_workspace` | 运行中切库（同样先校验令牌） |
| `memory_bind_status` / `_new` / `_adopt` / `_existing` / `_decline` | 接入与状态 |

写入的 `scope` 只有 `workspace`；传 `global` 会明确报错。经验可信度只在**初次写下**时给
（易变 0.3 / 一般 0.5~0.6 / 机制 0.8），之后一律由 `memory_verify` 调整。
「我看了，还成立」要表达就走 `memory_verify`，`memory_write` 没有确认参数。

## 复盘

由**当前会话的模型**执行，不额外调 LLM：

```
consolidate_preview(day="today")          → 拿到原始日志
        ↓  你自己读、提炼
consolidate_apply(experience=[...], episodes=[...], purge=true)
```

- `experience` — 换掉单号、任务 ID 仍然成立的机制与约定
- `episodes` — 当天发生了什么，一条一个单一事务，`importance` 决定初始详细程度

`summary` 只写进 digest 留档，**不会变成一条记忆**。不想清空原始日志就传 `purge=false`。

配成定时任务时，**每个工作区各建一个**（只复盘自己那个库，不要跨项目扫）。
模板在 `skills/humanity-memory/references/consolidate-task.md`，把两处 `{{占位}}` 换掉，其余别改。

### 复盘闸门：`memory_search` 为什么默认关着

活跃度只由「真实使用」累加：`memory_recall` 给命中项加，`memory_search` 刻意不加。
如果日常能随手 search，这些检索就不计入活跃度，遗忘曲线随之失真。

log 是例外：它没有 `activity` 字段，只把「被哪些查询命中过」记进 `retrievals`。
复盘时工具从这些条件重放算出新条目的起始活跃度；来源映射与继承值也都由工具算，
模型只负责提炼内容。

所以 `memory_search` 只在复盘时开放。判据全是**代码能核验的事实**，不看模型自觉：

| 途径 | 条件 |
| --- | --- |
| 时间窗 | 本地时间落在 `[review].window`（默认 `00:00-08:00`，可跨午夜）。定时复盘天然落在这里 |
| 手签通行证 | 在**真终端**跑 `review begin`，默认 30 分钟，到期自动失效 |

```bash
humanity-memory review status                 # 现在能不能搜、下次什么时候开
humanity-memory review begin                  # 签 30 分钟（--minutes 2h / --reason "补复盘"）
humanity-memory review end                    # 提前收回
```

`review begin` 检查 stdin 是不是 TTY（终端是 TTY，脚本与 agent 的管道不是），用来挡住
「agent 在管道里顺手调一次」。这是**弱信号**：带伪终端（ConPTY）的环境里 `isatty()` 会偶发误判。

真正兜底的是三条不关心「谁在调」的约束：

| 约束 | 强度 |
| --- | --- |
| 时间窗 | 硬 —— 窗口外就是搜不了，除非改配置 |
| 每日配额（默认 2 张） | 硬 —— 就算绕过 TTY，也只有这么多分钟 |
| 审计（pid / ppid / 时间 / 查询） | 事后可查 |

被拒时返回 `code=REVIEW_REQUIRED`（CLI 退出码 4），并记一笔到
`~/.humanity-memory/diagnostics/review.log`（只追加）。

**要绝对硬就关掉整条手签通道**：`[review].allow_flag = false`，之后只剩时间窗。
这层挡的是「顺手就搜」，不挡蓄意对抗（伪终端、改配置、直读 sqlite）。

## 数据模型

| 表 | 内容 | 关键字段 |
| --- | --- | --- |
| `knowledge` | **经验** | `confidence`、`forget_level`、`activity`、`period_start`、`invalid_at`、`invalidated_by`、`verified_at`、`scope` |
| `episodes` | **经历** | `at`、`day`、`compression_level`、`forget_level`、`revive_hits`、`digest_id` |
| `logs` | 日志 | `at`、`day`、`consolidated`、`digest_id` |
| `digests` | 每次复盘的审计 | 处理了几条日志、产出了几条经验/经历 |

表名 `knowledge` 存的就是经验，一张表一类，没有 `kind` 列分流。

向量：`knowledge_vec` / `episodes_vec` / `logs_vec`（vec0，cosine 度量，`chunk_size=64`），
rowid 与主表对齐。全文：三张 `_fts` 表，trigram 分词，触发器同步。

数据库层还钉了三条绕不过的约束：经验正文不可改、`invalid_at` 不可逆、已失效的不能复活。

### 生命周期

**常规路径只归档、不自动删。**

| 表 | 归档条件 | 压缩 |
| --- | --- | --- |
| 信条 | 不参与（它不在库里） | 不参与 |
| `knowledge`（经验） | 1095 天未触碰且访问 ≤1 次 | 不压缩（压缩会丢精确性） |
| `episodes`（经历） | 365 天未触碰且访问 ≤1 次 | 随遗忘等级压缩 |
| `logs` | 复盘后清空（按复盘，不按访问） | 已有 |

归档 = 置 `archived_at`，正文一字不动，默认检索不到；要查回用 `memory_unarchive` 恢复（会重新嵌入）。
`memory_purge(ids, confirm=true)` **只接受已归档的、或落在 0 级遗忘层且停留超期的**；
落进 0 级满 180 天、或归档满 180 天的，才会出现在删除候选里。

```bash
humanity-memory lifecycle archive --dry-run   # 预览会归档哪些
humanity-memory lifecycle archive             # 执行
humanity-memory lifecycle judge --dry-run     # 预览到期的等级裁定
humanity-memory lifecycle relevel --dry-run   # 等级表改档后，按 importance 重算存量落档
humanity-memory lifecycle candidates          # 删除候选（只报告）
humanity-memory lifecycle compress            # 待压缩队列（按遗忘等级推出）
humanity-memory lifecycle compress-apply --file items.json   # 定向覆盖提交
```

### 遗忘等级

**0~10 共 11 档**，周期全在 TOML 里，改档不碰代码：

| 段位 | 周期（经验 / 经历） | 定位 |
| --- | --- | --- |
| **1~5** | 1~7 天 | 短时记忆：一天到一周裁一次，被检索就往上爬，没人问就往下掉 |
| **6~10** | 30~540 天 | 长期记忆：月级起步，只有连续多个周期都活跃的条目才配留在这段 |

录入时按 `importance` 落档（`≥0.55 → L4`、`≥0.30 → L3`、其余 `L2`），顶档只能靠活跃度挣。
压缩跟着等级走：`L0 → 标题`、`L1 → 一句话`、`L2 及以上不动`（考察期不能先压没）。
0 级不进常规视野，只有 `memory_deep_recall` 能翻到；累计 `revive_hits` 次深度命中回到 `L1`。

**改了等级表的周期就必须跑一次 `relevel`**：等级是「这条记忆在时间轴上的位置」，
换一张表，同一个数字的含义就变了。只改配置不动存量，存量条目会按新周期连锁降级。
`relevel` 按当前 `[[initial.bands]]` 由 importance 重算落档、重置周期起点，
跳过 0 级（它有自己的复活通道）和已归档项。

判据用「多久没碰 + 访问几次」而不是分数阈值 —— 分数里总有加项兜底，
再冷的条目也跌不到 0，拿绝对阈值归档会永远不触发。

**长期静默不计入寿命**：两次遗忘计算的间隔最多按 `[forget] max_gap_days`（默认 2 天）算，
超出的部分当冻结期从周期起点里扣掉。工程停摆三个月回来跑一次 `judge`，
记忆不会因为「过了三个月」被判到期。置 0 关闭冻结。

## 检索

### 打分

```
final = type_weight × (0.72 × retrieval + 0.12 × importance + 0.16 × level_factor)

retrieval    = (0.70 × vec_sim + 0.30 × lex_sim) / 可用通路权重和
level_factor = (forget_level / 最高等级) ^ 0.65    （0 级恒为 0）
type_weight: experience 1.00 / episode 0.96 / log 0.90
             （episode 再乘压缩级别权重 0.88~0.96）
```

第三项是**遗忘等级**而不是「强度」：等级一个周期裁定一次、一次只动一级，
事后能回答「它为什么在这一级」。日志没有等级，恒按 1.0 —— 它靠 `type_weight`
与复盘清空体现「原始素材」的定位，不该被等级再罚一次。

### 两段式

`vec0` 的 KNN 只接受 `WHERE embedding MATCH ? AND k = ?`，无法在向量扫描阶段做业务过滤。所以：

1. **召回**：向量 KNN 与 FTS 各自取候选 rowid，不做过滤
2. **融合**：join 主表，在 Python 里做状态/作用域过滤与打分

候选集放大到 `limit × 8`（最少 40，上限 240）。`retrieval` 低于 `min_retrieval`（`0.78`）的
候选直接不返回 —— 重要度、等级、热度都不参与这个判断。严格层没捞到东西时才启用兜底：
`maxpool` 取各路相似度最大值，`rrf` 拆词后排名融合。

`memory_recall` 在最高相似度低于 `deep_recall_floor`（`0.35`）时自动再翻一次遗忘层。
判定用相似度而不是最终分数 —— 分数里有 importance 与等级加成，一条重要的老记忆分数可能很高，
但其实什么也没匹配上。

## 只读视图

数据库是唯一事实源。下面这些产物都是**只读派生**，不写库、不进记忆：

| 产物 | 命令 | 内容 |
| --- | --- | --- |
| Markdown 导出 | `export --out <目录>` | `知识现状.md`、`经历/YYYY-MM-DD-<摘要>.md`、`日志/YYYY-MM-DD.md` |
| 星图 | `starmap <目录> --out <HTML>` | 单文件 HTML，节点 = 条目，边 = 语义相似，辉光 = 活跃度 |
| 检索报告 | `retrieval-report` | 单文件 HTML，含阈值诊断，调参看这个 |
| **记忆观测台** | `analyze [目录] [--port 8765]` | 本地 HTTP 后端 + 浏览器前端，五个视图合一，右上角下拉切库（见下） |

`starmap` / `retrieval-report` 是 CLI 产物；日常看库走观测台即可 —— 它内嵌的是同一套渲染逻辑，
不用先生成 HTML 文件。原来的 `starmap.bat` / `report.bat` 两个入口已删除（2026-11-25）。

### 记忆观测台（analyze）

一个把只读视图整合起来的 Web 工具：起一个只监听 `127.0.0.1` 的本地后端，浏览器打开
`http://127.0.0.1:8765` 即可。双击 `analyze.bat`（**不用拖任何东西**）或把库目录拖上去都能启动。

```
humanity-memory analyze                            # 列出 registry 里所有库，右上角下拉切换
humanity-memory analyze "D:/.../libraries/lilith"   # 直接看某个库目录
humanity-memory analyze "<项目根>"                   # 按 .ai_memory/token.json 解析库
humanity-memory analyze workspace.db --port 9999     # 换端口
```

不给 target 时**不需要 token**：清单来自 `~/.humanity-memory/registry.json`（已登记的库），
registry 缺失时兜底扫 `<安装根>/libraries/*`；页面右上角的「观测库」下拉即切，
选择记在浏览器本地，重新打开还是上次那个库。给了 target 也只是清单里的额外一条，随时能切走。
盘没挂上的库会标成「不可用」并禁用，不会从列表里消失。

五个 tab：

| 视图 | 内容 |
| --- | --- |
| 星图 | 力导向图，节点 = 条目，边 = 语义相似，辉光 = 活跃度 |
| recall 历史 | 检索记录 + 热度榜 + 阈值诊断 |
| 复盘记录 | 按天：遗忘等级变动、活跃度较前一天 diff、新增/顶替/合并明细、md 原文 |
| 记忆现状 | 当前活跃度 / 遗忘等级 / 距下次裁定倒计时（实时） |
| 检索测试 | 手输查询跑一次检索，展示分数与**模拟**活跃度增量 |

**全程只读**：`analyze` 不写库、不改活跃度、不写检索日志。特别是「检索测试」——
它走 `search()`（纯只读）而非 `recall()`，模拟增量只是算出来展示，绝不落库；
也不会像深度回忆那样复活遗忘层。前端每 4 秒轮询数据源签名，库或报告一变就增量重拉对应视图。

> 观测台是新代码，旧发布版 exe 里没有。`analyze.bat` 默认走源码 `.venv`；
> 只有重新 `build_release --publish` 之后，才可设 `HM_ANALYZE_USE_PUBLISHED=1` 走发布版。
> Windows 下可直接用 `python -m humanity_memory.cli analyze [目录]` 免装 exe。

`export` 的 `--out` 必填，没有默认落点：一旦允许自动推导目录，就等于给了它一条
「悄悄往项目记忆目录写文件」的路径，手写笔记与渲染产物会混在同一层。
MCP 侧没有导出工具，复盘完成也不会自动刷新视图 —— 要最新视图就跑一次命令。

`starmap` / `retrieval-report` 只在 CLI 使用，不对 AI 暴露；两者都用 `mode=ro` 连接，
不建表、不写列。输入可以是项目根（按令牌解析库）、`token.json`、库目录或 `workspace.db`。

**导出是只读视图，不是备份**：不含 id、`access_count`、`verified_at` 与向量，
恢复后 id 全变、引用链会断。要能回滚就复制 `.db`（WAL 下连 `-wal` / `-shm` 一起）。
信条不在导出范围内 —— 它本来就是手写的。

## 安装 Skill

```bash
cp -r skills/humanity-memory ~/.workbuddy/skills/
```

```
skills/humanity-memory/
├── SKILL.md                        # 两类分工、写经验还是写日志、工具速查
└── references/
    ├── writing-guide.md            # 写入粒度、经验与日志的判断标准
    ├── recall-guide.md             # 召回时机、query 写法、结果解读
    ├── consolidate-guide.md        # 复盘流程、提炼标准、purge 取舍
    ├── consolidate-task.md         # 定时复盘的任务模板（两处占位，各仓库微调）
    ├── workspace-onboarding.md     # 新工作区接入三步：初始化 → 定时复盘 → 旧记忆导入
    └── operations.md               # 配置、排障、备份、换模型
```

## 开发

```bash
./.venv/Scripts/python.exe -m pytest -q                    # 392 项单元测试
./.venv/Scripts/python.exe tools/verify_mcp_config.py      # MCP 端到端（隔离环境）
sh tools/hooks/install.sh                                  # 装 pre-commit 钩子（每台机器一次）
```

**提交钩子**拦两件事，都是"进了历史就得改写历史才能清掉"的那类：

1. **本地专用文件**：`ExternalCommandSystem/`、`.workbuddy/`、`.ai_memory/`；
2. **内容里的本机真实路径**：用户目录、开发机的安装目录、工程根这类绝对路径。
   只扫**新增行**，且只认"绝对路径 + 本机特征" —— `D:/proj/YourProject`、
   `<安装根>/libraries` 这类占位符照常放行。

误报时在那一行末尾加 `# pathguard:allow`，并在 review 时说明理由。

### 源码态与发布态

两套入口互不影响：源码态是 `.venv`，发布态是 `<发布根>/current/humanity-memory.exe`
（`current` 是指向版本目录的 junction，`mcp.json` 只认它）。

```bash
python tools/build_release.py --status      # 当前版本、历史、备份、mcp 配置
python tools/build_release.py --publish     # 备份库 → 打包 → smoke
python tools/build_release.py --promote     # 把 current 切到最新构建版本
python tools/build_release.py --rollback    # 回上一版（exe + 库快照）
python tools/build_release.py --prune --keep 2   # 清理旧版本，保留最近 2 个 + current
```

**版本号分两层，打包不再改源码**：

| 层 | 写在哪 | 谁改 | 进 git 吗 |
| --- | --- | --- | --- |
| 大版本（基线） | `pyproject.toml` 的 `version` 与 `__init__.py` 的 `_BASE_VERSION` | 只在源码大版本变化时手改 | 是 |
| 本地构建序号 | `<发布根>/local_build.json` | `--publish` 每次自动 +1 | 否（发布根在项目树外） |

所以日常微调直接 `--publish`，产物版本是 `2.0-local.3` 这种形式，`--version` / `doctor` /
MCP initialize 报的都是它；源码一个字节都不动，工作区不会冒出「版本号没提交」的脏文件。
版本号是打包时随包写进 `version.txt` 的（`__init__._packaged_version()` 读它，源码态读不到就回落基线）。

基线变了（比如改成 `2.0`），序号自动从 `local.1` 重新数。要打一个不带本地后缀的纯净版本号
（正式发版），显式指定：`--publish --target 2.0`。同版本重复构建用 `--force`，
旧目录会被改名留档成 `v<版本>.stale-<时间戳>`。

滚动发布**不会覆盖**旧目录，每个版本约 50MB（onedir 打包带了 Python 运行时和 sqlite-vec）。
攒多了用 `--prune --keep N` 收拾：保留最近 N 个 + `current`，同时把对应条目从 `history.json`
摘掉、配套库快照一起删；被进程占用的目录只 warn 跳过，不会因为一个删不掉就中途失败。

`analyze.bat` / `upgrade.bat` 优先调用**发布版**，只有发布版不存在时才退回 `.venv`，
所以改完源码这两个 bat 不会自动跟着变 —— 要么先 `--publish`，要么删掉发布根让它退回源码态。
bat 第二行会打印 `Using: published (...)` 或 `source (...)`。

`dev.bat` 把工作区钉在隔离目录（`local.env` 里的 `HM_DEV_ROOT`），保证源码态碰不到生产库。
发布根位置走 `HM_RELEASE_ROOT`（`local.env` 或环境变量），源码里不出现本机路径。

## 已知限制

- **同步 SQLite**：并发写靠 WAL + `busy_timeout`，单机单 Agent 够用，高并发需外部串行化
- **无 ANN 索引**：sqlite-vec 0.1.x 走暴力扫描，十万条内毫秒级
- **trigram 边界**：FTS 只能匹配连续 3 字符以上子串，中文换序靠 bigram LIKE 兜底（全表扫描）
- **换模型需 reindex**：维度变了旧向量无法复用
- **信条不热重载**：改完 `CORE_RULES.md` 要重开会话才生效（为了保证 KV Cache 稳定）

## 设计文档

- `docs/two-tier-memory.md` — 两类记忆、等级阶梯、打分与闸门的取舍
- `docs/workspace-binding-and-token.md` — 工作区绑定、令牌与 registry 的准入语义
- `docs/external-visualization-tool.md` — 记忆观测台（HTTP 后端 + Web 前端）的设计与只读口径

## 许可

MIT
