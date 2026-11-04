# HumanityMemory

基于 **SQLite + sqlite-vec** 的本地三层仿生记忆库，以 **MCP server + Skill 集合**的形式提供给智能体使用。

零服务依赖：整个记忆就是几个 `.db` 文件，向量索引、全文索引、元数据全在里面，不上云。

```
┌──────────────────────────────────────────────────────────────┐
│  信条层  CORE_RULES.md  →  静态注入 System Prompt，不进向量库  │
│           全局一份 + 每个项目一份                              │
└──────────────────────────────────────────────────────────────┘
                            │ 不参与检索
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  事实层  facts       简短陈述句 · 遗忘极慢 · 追加式失效         │
│  经历层  episodes    复盘压缩出的叙述 · 慢遗忘                  │
│  日志层  logs        当天原始记录 · append-only · 复盘后清空    │
└──────────────────────────────────────────────────────────────┘
        │                                    │
        ▼                                    ▼
  workspace.db（项目）              global.db（跨项目事实）
  物理隔离，换项目就看不到           永远参与检索
```

## 为什么这么分层

人类不会把每件事都记成永久结论。当天发生的事先粗略记下（日志），
过一晚沉淀出可复用的经验（事实），细节自然淡出。

这套结构把这个过程显式化了，而且用**数据库触发器**保证纪律：

- `logs` 表改正文 → `RAISE(ABORT)`，不靠应用层自觉
- 未复盘的日志删不掉
- `facts` 正文不可改，只能追加新事实并让旧的失效
- 已失效的事实不能"复活"

## 特性

- **信条不进检索**：硬约束靠静态注入 100% 命中，不赌召回率
- **三层分离**：事实/经历/日志各有半衰期（3650 / 180 / 14 天）
- **物理隔离**：项目记忆在项目目录，删项目即删记忆，零残留
- **跨库检索**：全局事实无论当前在哪个项目都参与召回
- **混合检索**：余弦向量 KNN + FTS5 trigram + bigram 兜底
- **复盘由当前会话的模型执行**：不额外配 LLM，两步工具完成
- **优雅降级**：没配 embedding 也能用，退化为关键词检索

## 快速开始

```bash
cd D:/proj/HumanityMemory

python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .        # Windows
# source .venv/bin/activate && pip install -e .       # macOS / Linux

# 配置必须放用户级目录，否则换项目目录就读不到
mkdir -p ~/.humanity-memory
cp .env.example ~/.humanity-memory/.env
# 编辑里面的 embedding 配置

./.venv/Scripts/humanity-memory.exe doctor
```

然后在项目里：

```bash
cd D:/proj/YourProject
D:/proj/HumanityMemory/.venv/Scripts/humanity-memory.exe init
humanity-memory log add "今天排查了 XXX，根因是 YYY"
humanity-memory fact add "这个项目的 UI 用 UGUI" --tags Unity
humanity-memory search "UI 用什么"
```

## 配置

配置全走环境变量或 `.env`。**务必放在 `~/.humanity-memory/.env`**，
MCP server 的工作目录不固定，项目级 `.env` 换个目录就失效。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HM_HOME` | `~/.humanity-memory` | 全局库目录 |
| `HM_WORKSPACE` | — | 显式指定工作区，跳过 cwd 查找 |
| `HM_EMBED_BASE_URL` | — | OpenAI 兼容端点 |
| `HM_EMBED_MODEL` | — | 例如 `BAAI/bge-m3` |
| `HM_EMBED_API_KEY` | — | 本地 Ollama 可留空 |
| `HM_EMBED_DIM` | `1024` | **必须与模型实际维度一致** |
| `HM_HALFLIFE_FACT` | `3650` | 事实半衰期（天） |
| `HM_HALFLIFE_EPISODE` | `180` | 经历半衰期 |
| `HM_HALFLIFE_LOG` | `14` | 日志半衰期 |
| `HM_GLOBAL_FACTS_WARN` | `50` | 全局事实膨胀阈值 |
| `HM_W_RETRIEVAL` | `0.72` | 检索相似度权重 |
| `HM_W_IMPORTANCE` | `0.12` | 重要性权重 |
| `HM_W_STRENGTH` | `0.16` | 新鲜度/使用频次权重 |

### 免费 embedding 选项

| 方案 | 花费 | 维度 | 说明 |
| --- | --- | --- | --- |
| 硅基流动 `BAAI/bge-m3` | 0（限速） | 1024 | 需实名，中文效果最好 |
| 智谱 `embedding-3` | 0（送 2000 万 tokens） | 2048 | 无需实名 |
| 本地 Ollama `bge-m3` | 0，永久 | 1024 | 完全离线，`ollama pull bge-m3` |
| Cloudflare Workers AI | 0（1 万次/天） | 1024 | 国内网络一般 |

切换只改三项，**维度变了必须** `humanity-memory reindex --reset-vectors`。

## 注册为 MCP server

```json
{
  "mcpServers": {
    "humanity-memory": {
      "command": "D:/proj/HumanityMemory/.venv/Scripts/python.exe",
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

### 多项目部署（推荐）

一个 server 进程只服务一个工作区。要服务多个项目，每个项目加一条，用 `--workspace` 锁定：

```json
"humanity-memory-project-island": {
  "command": "D:/proj/HumanityMemory/.venv/Scripts/python.exe",
  "args": [
    "-m", "humanity_memory.mcp_server",
    "--workspace", "D:/proj/YourProject"
  ],
  "env": { "...": "同上" }
}
```

`--workspace` 指向**项目根**，不是 `.ai_memory` 目录。不传则退化为「从 cwd 向上查找」，
这在客户端 cwd 不固定时会落到「只有全局库」模式。

### 用令牌文件（把凭据从 mcp.json 里拿出来）

`mcp.json` 经常被同步、备份、分享。把 API key 写在里面是一层泄露风险。
令牌把「工作区绑定 + 信条路径 + embedding 凭据」收进一个文件，`mcp.json` 只留一个路径：

```json
"args": [
  "-m", "humanity_memory.mcp_server",
  "--token", "D:/proj/YourProject/.ai_memory/memory.token.json"
]
```

生成令牌：

```bash
# 项目内令牌（不含凭据，可以跟着项目走）
humanity-memory workspace use D:/proj/YourProject \
  --token D:/proj/YourProject/.ai_memory/memory.token.json

# 带凭据的令牌（建议放用户目录，别放项目里被 git 提交）
humanity-memory workspace use D:/proj/YourProject --with-embedding
# 默认写到 ~/.humanity-memory/active.token.json

humanity-memory --token <路径> workspace show   # 查看（key 会脱敏）
```

令牌内容：

```json
{
  "version": 1,
  "label": "YourProject",
  "workspace": "D:/proj/YourProject",
  "rules": null,
  "embedding": {}
}
```

- `workspace` 必填，启动时会校验目录存在；项目挪位置后重新 `workspace use` 即可
- `rules` 留 `null` 就自动探测 `.workbuddy/memory/信条.md`
- `embedding` 留空则回落到环境变量 / `.env`
- 优先级：令牌 > `--workspace` > `HM_WORKSPACE` > cwd 查找

**令牌不减少配置条数**。它解决的是三件事：凭据集中、改配置不用动客户端、连错工作区能立刻发现。

### 一条配置服务多个项目（不写多条 mcp.json）

一个 server 进程可以运行中切换工作区：

```
memory_use_workspace(path="D:/proj/YourProject")
```

切换后读写全部落到该项目自己的 `.ai_memory/workspace.db`，**项目之间仍物理隔离**；
`persist=true` 会记住选择，下次启动自动接上。

启动时工作区按这个顺序确定：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--token` | 令牌文件里写死，最确定 |
| 2 | `--workspace` | 命令行指定 |
| 3 | `HM_WORKSPACE` | 环境变量 |
| 4 | cwd 向上查找 | 只有**已存在** `.ai_memory` 才命中，不会新建 |
| 5 | 上次用过的工作区 | `~/.humanity-memory/last-workspace.txt` |
| 6 | 无 | 只能读写全局库，写日志会报错提示先切换 |

如果客户端启动时 cwd 就是项目目录，第 4 条能直接命中——那就是真正的零配置。
客户端 cwd 不可靠时，靠第 5 条 + agent 调一次 `memory_use_workspace` 兜底。

启动上下文（cwd、解析来源、命中的工作区）会写进
`~/.humanity-memory/diagnostics/startup.json`，排查「为什么落到全局库」时先看它。

### 复用项目已有的信条文件

如果项目里已有 Markdown 信条（例如 `.workbuddy/memory/信条.md`），会**自动探测并复用**，
不会另建一份 `CORE_RULES.md`。找不到才用默认位置。

需要指向别处时用 `--rules <路径>` 或环境变量 `HM_RULES_PATH`。

## 导出 Markdown 视图

数据库是唯一事实源，但人是需要翻文件的。导出成项目约定的 Markdown：

```bash
humanity-memory --workspace D:/proj/YourProject export
# 默认写到 <项目>/.workbuddy/memory/
```

生成：

| 文件 | 内容 |
| --- | --- |
| `事实知识.md` | 每条事实一行，带标签注释 |
| `经历/YYYY-MM-DD-<摘要>.md` | 一次复盘产出的经历，一个文件 |
| `日志/YYYY-MM-DD.md` | 按天归档的原始日志 |

**导出是只读视图**，手改会被下次导出覆盖。信条文件不在导出范围内——它本来就是手写的。

## 安装 Skill

```bash
cp -r skills/humanity-memory ~/.workbuddy/skills/
```

```
skills/humanity-memory/
├── SKILL.md                        # 三层分工、写事实还是写日志、工具速查
└── references/
    ├── writing-guide.md            # 写入粒度、事实与日志的判断标准
    ├── recall-guide.md             # 召回时机、query 写法、结果解读
    ├── consolidate-guide.md        # 复盘流程、提炼标准、purge 取舍
    └── operations.md               # 配置、排障、备份、换模型
```

## 工具集

| 工具 | 说明 |
| --- | --- |
| `rules_get` / `rules_append` | 读/追加信条（只追加，不改既有内容） |
| `fact_write` | 写事实，可带 `invalidates` 取代旧事实 |
| `fact_invalidate` | 让事实失效（不删除） |
| `fact_promote` | 项目事实 → 全局事实 |
| `fact_list` | 列事实 |
| `log_append` | 追加日志（唯一允许的日志写操作） |
| `log_list` | 按日期列日志 |
| `memory_search` | 跨层跨库检索，不改访问强度 |
| `memory_recall` | 检索 + 强化命中项 |
| `consolidate_preview` | 取待复盘日志 |
| `consolidate_apply` | 提交复盘结果 |
| `memory_stats` | 统计与健康提示 |
| `memory_maintenance` | 补 embedding |

## 复盘

由**当前会话的模型**执行，不额外调 LLM：

```
consolidate_preview(day="today")          → 拿到原始日志 + 现有事实
        ↓  你自己读、提炼
consolidate_apply(facts=[...], episodes=[...], purge=true)
```

配成定时任务时，prompt 里写清这三步即可。不想清空原始日志就传 `purge=false`。

## 数据模型

| 表 | 关键字段 |
| --- | --- |
| `facts` | `invalid_at`（失效时间）、`invalidated_by`（被谁取代）、`scope`（workspace/global） |
| `episodes` | `at`（事件时间）、`day`、`digest_id`（哪次复盘产出） |
| `logs` | `at`、`day`、`consolidated`、`digest_id` |
| `digests` | 每次复盘的审计：处理了几条日志、产出了几条事实/经历 |

向量：`facts_vec` / `episodes_vec` / `logs_vec`（vec0，cosine 度量），rowid 与主表对齐。
全文：三张 `_fts` 表，trigram 分词，触发器同步。

### 检索打分

```
final = type_weight × (0.72 × retrieval + 0.12 × importance + 0.16 × strength)

retrieval = (0.70 × vec_sim + 0.30 × lex_sim) / 可用通路权重和
strength  = 0.55 × 新鲜度(按层半衰期) + 0.45 × 使用频次(对数，20 次饱和，底分 0.5)
type_weight: fact 1.00 / episode 0.96 / log 0.90
```

`usage_factor` 给从未被召回的内容 0.5 底分——新写入的不该因为"还没被用过"而吃亏。

## 为什么是两段式检索

`vec0` 的 KNN 只接受 `WHERE embedding MATCH ? AND k = ?`，无法在向量扫描阶段做业务过滤。所以：

1. **召回**：向量 KNN 与 FTS 各自取候选 rowid，不做过滤
2. **融合**：join 主表，在 Python 里做状态/作用域过滤与打分

候选集放大到 `limit × 8`（上限 240）。

## 开发

```bash
./.venv/Scripts/python.exe -m pytest -q                    # 55 项单元测试
./.venv/Scripts/python.exe tools/verify_mcp_config.py      # MCP 端到端（隔离环境）
```

## 已知限制

- **同步 SQLite**：并发写靠 WAL + `busy_timeout`，单机单 Agent 够用，高并发需外部串行化
- **无 ANN 索引**：sqlite-vec 0.1.x 走暴力扫描，十万条内毫秒级
- **trigram 边界**：FTS 只能匹配连续 3 字符以上子串，中文换序靠 bigram LIKE 兜底（全表扫描）
- **换模型需 reindex**：维度变了旧向量无法复用
- **信条不热重载**：改完 `CORE_RULES.md` 要重开会话才生效（这是为了保证 KV Cache 稳定）

## 许可

MIT
