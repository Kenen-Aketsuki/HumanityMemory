# HumanityMemory

基于 **SQLite + sqlite-vec** 的本地两类仿生记忆库，以 **MCP server + Skill 集合**的形式提供给智能体使用。

零服务依赖：整个记忆就是几个 `.db` 文件，向量索引、全文索引、元数据全在里面，不上云。

```
┌──────────────────────────────────────────────────────────────┐
│  信条层  CORE_RULES.md  →  静态注入 System Prompt，不进向量库  │
│           全局一份 + 每个项目一份                              │
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
  workspace.db（项目）
  物理隔离，换项目就看不到
  （global.db 已于 2026-11-17 关闭，跨项目走 CORE_RULES）
```

## 为什么这么分层

人类不会把每件事都记成永久结论。当天发生的事先粗略记下（日志），
过一晚沉淀出可复用的经验，细节自然淡出。

**判据只有一条：它是否需要回答「什么时候发生的」。**
需要 → 经历（带时间、会压缩）；不需要 → 经验（可复用的结论、不压缩、可被推翻）。

**为什么不给「易变的结论」单列一类**：版本号、依赖、路径这类东西确实变得快，
但那不是**类型**差异，而是**可信度**差异。给它一个低初始可信度（0.3），
之后靠证据（`memory_verify`）涨落，就够了——
早先那套「单独一类 + 1→3→5→7 天复核梯子」多出一个分类、一张日历、一套状态机，
换来的只是"记得去复核"这件本来就没人做得到的事。

这套结构把过程显式化了，而且用**数据库触发器**保证纪律：

- `logs` 表改正文 → `RAISE(ABORT)`，不靠应用层自觉
- 未复盘的日志删不掉
- 经验正文不可改，只能追加新条目并让旧的失效
- 已失效的经验不能"复活"

## 特性

- **信条不进检索**：硬约束靠静态注入 100% 命中，不赌召回率
- **一类一个物理表**：经验 / 经历 / 日志各有自己的归档 TTL（3 年 / 1 年 / 复盘即清）
- **可信度靠证据**：`memory_verify` 按**间隔**加分（隔半年再验一次，比昨天验过信息量大），
  证伪扣分但**不删行**——「它曾经是个结论」本身就是知识
- **读到低分条目会提醒**：低于 `verify_on_read` 的命中带 `·待验证` 标记
- **物理隔离**：项目记忆在项目目录，删项目即删记忆，零残留
- **只有项目库**：全局库已关闭，跨项目硬约束走 `CORE_RULES.md` 静态注入
- **混合检索**：余弦向量 KNN + FTS5 trigram + bigram 兜底
- **复盘由当前会话的模型执行**：不额外配 LLM；模型只写条目、给初值、报证据，分值等级全归工具
- **优雅降级**：没配 embedding 也能用，退化为关键词检索

## 快速开始

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

然后在项目里：

```bash
cd D:/proj/YourProject
Your/Path/To/HumanityMemory/.venv/Scripts/humanity-memory.exe init
humanity-memory log add "今天排查了 XXX，根因是 YYY"
humanity-memory memory add "ResolveTask 末尾的兜底会被短路" --kind experience --confidence 0.8
humanity-memory memory add "项目是 Unity 2022.3.62f1" --confidence 0.3 --source ProjectVersion.txt
humanity-memory memory add "Bug #10805 引导不指餐厅：三条静默出口" --kind episode
humanity-memory search "引导为什么不触发" --recall
```

> `search` 不加 `--recall` 是**复盘专用**的（只在时间窗内或持通行证时可用），
> 日常请走 `--recall`：它才会把命中项计入活跃度。见「复盘闸门」。

## 配置

配置全走环境变量或 `.env`。**只认仓库根这一份 `<仓库根>/.env`**：`config.py` 相对源码
自身位置定位，不再读 `~/.humanity-memory/.env` 或 cwd 下的 `.env`；无论 MCP server / CLI
从哪个目录启动，读到的都是同一份配置。真实环境变量仍是最高优先级。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HM_HOME` | `~/.humanity-memory` | 全局信条与诊断目录（库已退役） |
| `HM_WORKSPACE` | — | 显式指定工作区，跳过 cwd 查找 |
| `HM_EMBED_BASE_URL` | — | OpenAI 兼容端点 |
| `HM_EMBED_MODEL` | — | 例如 `BAAI/bge-m3` |
| `HM_EMBED_API_KEY` | — | 本地 Ollama 可留空 |
| `HM_EMBED_DIM` | `1024` | **必须与模型实际维度一致** |
| `HM_ARCHIVE_TTL_EXPERIENCE` | `1095` | 经验多久未用就归档 |
| `HM_ARCHIVE_TTL_EPISODE` | `365` | 经历多久未用就归档 |
| `HM_W_RETRIEVAL` | `0.72` | 检索相似度权重 |
| `HM_W_IMPORTANCE` | `0.12` | 重要性权重 |
| `HM_W_LEVEL` | `0.16` | 遗忘等级倍率权重 |

**阈值不走环境变量，走四层 TOML**（后者覆盖前者）：出厂（包内全量）→ 安装（`<安装根>/memory.toml`，`config template` 生成的全量模板）→ 用户（`~/.humanity-memory/memory.toml`，增量）→ 项目（`<项目>/.ai_memory/memory.toml`，增量）。
只改几项就用后两层，别动安装层那份全量——放着全量等于接管所有默认值。
改完要重启 MCP 客户端才生效（server 常驻，配置只在启动时读）。

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

### 多项目：一条配置，令牌钉库

`mcp.json` 里**只有 exe 和 `serve`**，不带任何项目路径：

```json
"humanity-memory": {
  "command": "Your/Path/To/HumanityMemory-Release/current/humanity-memory.exe",
  "args": ["serve"],
  "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
}
```

一个 server 服务所有项目：**打开哪个库由当前目录的令牌决定**，
`<项目>/.ai_memory/token.json` 就是那个项目的接入证明。项目搬家令牌跟着搬。

给一个项目接入记忆库（在项目根执行，四条选一条）：

```bash
humanity-memory bind new                     # 新建独立库（默认用目录名当标签）
humanity-memory bind new --label island      # 新建并指定标签
humanity-memory bind adopt                   # 沿用本目录已有的 .ai_memory/workspace.db
humanity-memory bind existing --library island   # 并入已有库（A、B 共用一个库）
humanity-memory bind decline                 # 本次不接入（记住这个决定，以后不再问）
```

查看与运维：

```bash
humanity-memory bindings list          # registry 里有哪些库、谁在用
humanity-memory --workspace <项目> doctor    # 接入状态 + 配置 + 库自检
humanity-memory revoke --workspace <项目>    # 撤销钥匙（不动库文件）
humanity-memory bind move --from <旧> --to <新>   # 项目搬家后修令牌
```

**没有令牌就什么都不做。** 未接入时不会建目录、不会建库、也不会去翻「上次用过的工作区」——
它会返回一个错误码 + 一份可照做的清单（`memory_bind_status` 能看到同样的内容）。

### 令牌里有什么（v2）

```json
{
  "version": 2,
  "key": "hmk_7f3a9c1e…",
  "workspace": "D:/proj/YourProject",
  "library": "yourproject",
  "label": "yourproject"
}
```

- 只有**身份**：没有 embedding 凭据（凭据走 `.env`），也没有信条路径（信条从项目里探测）。
- `library: null` 表示**明确不接入**（`bind decline` 的结果）。这是被记住的决定，
  不是"还没配"——所以它同样拒绝读写，只是不再反复问你。
- `library` 是**断言，不是寻址**：真正打开哪个库由服务端 registry（`~/.humanity-memory/registry.json`）
  决定。手改令牌里的 `library` 拿不到别人的库，只会让校验失败。
- registry 只存 `sha256(salt + key)`，明文钥匙只在令牌文件里。

### 启动时怎么决定打开哪个库

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--workspace` | 显式指定项目根（运维 / 调试用） |
| 2 | `HM_WORKSPACE` | 环境变量，同上 |
| 3 | cwd 向上查找 | **只认含 `token.json` 的 `.ai_memory`** |
| 4 | 都没有 | 拒绝：`no_workspace` / `no_token` + 清单，不建任何目录 |

`--token <路径>` 仍可传，但它的作用已经降级为「路径覆盖 + 用令牌里的 workspace 当提示」，
**不再是身份来源**——放行与否仍由七步校验决定。

校验顺序（任一失败即拒绝，失败点就是错误码）：
根目录 → 令牌存在且可解析 → `version == 2` → 令牌的 `workspace` 就是本根 →
registry 有这把 key 且未撤销 → registry 的 `workspace` / `library` 与令牌一致 →
`library` 非 null 且库目录存在。

运行中切库用 `memory_use_workspace(path="D:/proj/YourProject")`：它同样先校验令牌，
通过后才重建 store 并**按新库重载配置**（库级 `memory.toml` 跟着库走）。
`persist` 参数已失效——进程间不再共享"上次工作区"。

启动上下文（cwd、解析来源、命中的库、错误码）会写进
`~/.humanity-memory/diagnostics/startup.json`，排查「为什么落到这个库」时先看它。

### 复用项目已有的信条文件

如果项目里已有 Markdown 信条（例如 `.workbuddy/memory/信条.md`），会**自动探测并复用**，
不会另建一份 `CORE_RULES.md`。找不到才用默认位置。

需要指向别处时用 `--rules <路径>` 或环境变量 `HM_RULES_PATH`。

## 导出 Markdown 视图（仅手动）

数据库是唯一事实源，但人是需要翻文件的。导出**只在人主动执行时发生**：

```bash
humanity-memory --workspace D:/proj/YourProject export \
  --out D:/proj/YourProject/.ai_memory/export
```

`--out` 必填，没有默认落点。这是刻意的：一旦允许自动推导目录，就等于给了它一条
「悄悄往项目记忆目录写文件」的路径，手写笔记与渲染产物会混在同一层——而两者
的处置方式完全相反（手写要保留，渲染要能随时整份覆盖）。

同样地，**不提供任何自动/定时/写入后回调的导出**：MCP 侧没有导出工具，
复盘完成也不会自动刷新视图。要最新视图就跑一次上面的命令。

生成：

| 文件 | 内容 |
| --- | --- |
| `知识现状.md` | 知识 / 现状分两节，每条一行，现状带确认时间 |
| `经历/YYYY-MM-DD-<摘要>.md` | 一次复盘产出的经历，一个文件 |
| `日志/YYYY-MM-DD.md` | 按天归档的原始日志 |

**导出是只读视图，不是备份**：不含 id、`access_count`、`verified_at` 与向量，
恢复后 id 全变、引用链会断。要能回滚就复制 `.db`（WAL 下连 `-wal` / `-shm` 一起）。
信条文件不在导出范围内——它本来就是手写的。

想保留一键入口就在项目里放个脚本，但落点必须写死、不能靠默认值：

```bat
@echo off
Your/Path/To/HumanityMemory/.venv/Scripts/humanity-memory.exe ^
  --workspace "D:/proj/YourProject" ^
  export --out "D:/proj/YourProject/.ai_memory/export"
```

## 安装 Skill

```bash
cp -r skills/humanity-memory ~/.workbuddy/skills/
```

```
skills/humanity-memory/
├── SKILL.md                        # 两类分工、写经验还是写日志、工具速查
└── references/
    ├── writing-guide.md            # 写入粒度、事实与日志的判断标准
    ├── recall-guide.md             # 召回时机、query 写法、结果解读
    ├── consolidate-guide.md        # 复盘流程、提炼标准、purge 取舍
    ├── consolidate-task.md         # 定时复盘的任务模板（两处占位，各仓库微调）
    └── operations.md               # 配置、排障、备份、换模型
```

## 工具集

| 工具 | 说明 |
| --- | --- |
| `rules_get` / `rules_append` | 读/追加信条（只追加，不改既有内容） |
| `memory_write` | **唯一写入口**，`kind` = `experience` / `episode` |
| `memory_verify` | 给经验落一条证据：`ok=true` 按间隔加分，`ok=false` 扣分（都不删行） |
| `memory_invalidate` | 让经验失效（不删除，保留历史）；对经历会拒绝 |
| `memory_promote` | **已下线**：全局库关闭，调用会明确报错 |
| `memory_list` | 列条目，`kinds` 支持 `all` |
| `log_append` | 追加日志（唯一允许的日志写操作） |
| `log_list` | 按日期列日志 |
| `memory_search` | 混合检索，不改访问强度 |
| `memory_recall` | 检索 + 强化命中项；常规捞不到时自动兜一层深度回忆 |
| `memory_deep_recall` | 只翻遗忘层（0 级）；命中的经历累计复活次数 |
| `memory_judge` | 跑到期裁定：按周期活跃度升降等级，没到期什么都不写 |
| `consolidate_preview` | 取待复盘日志（不返回存量经验，查重交给写入） |
| `consolidate_apply` | 提交复盘结果 |
| `memory_stats` | 统计与健康提示 |
| `memory_maintenance` | 补 embedding |

生命周期与运维工具另有一组：`memory_archive` / `memory_unarchive` / `memory_purge_candidates` /
`memory_purge` / `memory_compress_preview` / `memory_compress_apply` / `memory_use_workspace`。

**已下线**：`memory_review` / `memory_review_list`（复核梯子随 `state` 一起删除）、
`memory_recompress_preview` / `memory_recompress_apply`（按年龄判压缩，已被等级驱动取代）、
`fact_write`。`memory_write` 也没有 `verifies` 参数了 —— 「我看了，还成立」要表达就走 `memory_verify`。

## 复盘

由**当前会话的模型**执行，不额外调 LLM：

```
consolidate_preview(day="today")          → 拿到原始日志 + 现有经验
        ↓  你自己读、提炼
consolidate_apply(experience=[...], episodes=[...], purge=true)
```

复盘产出两块：

- `experience` — 换掉单号、任务 ID 仍然成立的机制与约定。
  `confidence` 是起点：易变 0.3 / 一般 0.5~0.6 / 机制 0.8
- `episodes` — 当天发生了什么，一条一个单一事务，`importance` 决定初始详细程度

`summary` 只写进 digest 留档，**不会变成一条记忆** —— 总结和「发生过的事」是两类东西。

配成定时任务时，**每个工作区各建一个**（只复盘自己那个库，不要跨项目扫）。
直接抄 `skills/humanity-memory/references/consolidate-task.md` 的模板，
把两处 `{{占位}}` 换成这个项目的情况，其余别改 —— 每项目全篇改一遍会漂成七八个版本。
不想清空原始日志就传 `purge=false`。

### 复盘闸门：`memory_search` 为什么默认关着

活跃度只由「真实使用」累加：`memory_recall` 给命中项加，`memory_search` 刻意不加。
如果日常能随手 search，这些检索就不计入活跃度，遗忘曲线随之失真 ——
记忆不老化，或者老化了但不是因为它真的没被用。

所以 `memory_search` 只在复盘时开放。判据全是**代码能核验的事实**，不看模型自觉：

| 途径 | 条件 |
| --- | --- |
| 时间窗 | 本地时间落在 `[review].window`（默认 `00:00-08:00`，可跨午夜）。定时复盘天然落在这里，无需人工动作 |
| 手签通行证 | 在**真终端**跑 `review begin`，默认 30 分钟，到期自动失效 |

```bash
humanity-memory review status                 # 现在能不能搜、下次什么时候开
humanity-memory review begin                  # 签 30 分钟（--minutes 2h / --reason "补复盘"）
humanity-memory review end                    # 提前收回
```

`review begin` 会检查 stdin 是不是 TTY（终端是 TTY，脚本和 agent 的管道不是），
用来挡住「agent 在管道里顺手调一次」。但这是**弱信号**：实测带伪终端（ConPTY）
的环境里，`isatty()` 会偶发误判成 True。

所以真正兜底的是三条不关心「谁在调」的约束：

| 约束 | 强度 |
| --- | --- |
| 时间窗 | 硬 —— 白天就是搜不了，除非改配置 |
| 每日配额（默认 2 张） | 硬 —— 就算绕过 TTY，也只有这么多分钟 |
| 审计（pid / ppid / 时间 / 查询） | 事后可查 |

被拒时返回 `code=REVIEW_REQUIRED`（CLI 退出码 4），并记一笔到
`~/.humanity-memory/diagnostics/review.log`（只追加）。

**要绝对硬就关掉整条手签通道**：`[review].allow_flag = false`。之后只剩时间窗，
白天一次都搜不了；要补复盘就改 `window` —— 一次显式配置修改，看得见。

这层挡的是「顺手就搜」，不挡蓄意对抗（伪终端、改配置、直读 sqlite）。
没有任何纯软件手段能区分「用户按的回车」和「agent 调用同一个 exe」——
前两者会留在 diff 与审计里，后者靠事后核对。

## 数据模型

| 表 | 关键字段 |
| --- | --- |
| `knowledge` | **经验**。`confidence`、`forget_level`、`activity`、`period_start`、`invalid_at`、`invalidated_by`、`verified_at`、`scope` |
| `episodes` | **经历**。`at`、`day`、`compression_level`、`forget_level`、`revive_hits`、`digest_id` |
| `logs` | `at`、`day`、`consolidated`、`digest_id` |
| `digests` | 每次复盘的审计：处理了几条日志、产出了几条经验/经历 |

向量：`knowledge_vec` / `episodes_vec` / `logs_vec`（vec0，cosine 度量，`chunk_size=64`），
rowid 与主表对齐。全文：三张 `_fts` 表，trigram 分词，触发器同步。

**表名沿用 v3 的 `knowledge`，但它的含义已经是「经验」**——v4 起一类一张表，
不再靠 `kind` 列分流。老库里那列 `kind` 留着当历史线索（`state` / `case` 读出来即经验 / 经历），
新库直接不建它，代码里一个 `kind` 列都不碰。

数据库层还钉了三条绕不过的约束：经验正文不可改、`invalid_at` 不可逆、已失效的不能复活。

### 记忆的生命周期

两层都有淘汰机制，**常规路径只归档、不自动删**。

| 表 | 归档条件 | 压缩 |
| --- | --- | --- |
| 信条 | 不参与（它不在库里） | 不参与 |
| `knowledge`（经验） | 3 年未触碰且访问 ≤1 次 | 不压缩（压缩会丢精确性） |
| `episodes`（经历） | 1 年未触碰且访问 ≤1 次 | ✅ 随遗忘等级压缩 |
| `logs` | 复盘后清空（按复盘，不按访问） | 已有 |

归档 = 置 `archived_at`，正文不动，默认检索不到，`include_archived=true` 可查回。
通用删除入口 `memory_purge(ids, confirm=true)` **只能删已归档的**；
落进 0 级遗忘层满 180 天、或归档满 180 天的，才会出现在删除候选里。

用 CLI 操作：

```bash
humanity-memory lifecycle archive --dry-run   # 预览会归档哪些
humanity-memory lifecycle archive             # 执行
humanity-memory lifecycle judge --dry-run     # 预览到期的等级裁定
humanity-memory lifecycle relevel --dry-run   # 等级表改档后，按 importance 重算存量落档
humanity-memory lifecycle candidates          # 删除候选（只报告）
humanity-memory lifecycle compress            # 待压缩队列（按遗忘等级推出）
humanity-memory lifecycle compress-apply --file items.json   # 定向覆盖提交
```

遗忘等级是 **0~10 共 11 档**，周期全在 TOML 里，改档不碰代码：

| 段位 | 周期（经验 / 经历） | 定位 |
| --- | --- | --- |
| **1~5** | 1~7 天 | 短时记忆：一天到一周裁一次，被检索就往上爬，没人问就往下掉 |
| **6~10** | 30~540 天 | 长期记忆：月级起步，只有连续多个周期都活跃的条目才配留在这段 |

**改了等级表的周期就必须跑一次 `relevel`**：等级是「这条记忆在时间轴上的位置」，
换一张表，同一个数字的含义就变了（旧 L4 是 180 天，新 L4 是 5 天）。
只改配置不动存量，老条目会按新周期连锁降级、几周内集体掉进遗忘层 ——
而它们并不是变旧了，只是被换了一把尺子量。`relevel` 按当前 `[[initial.bands]]`
由 importance 重算落档、重置周期起点，跳过 0 级（它有自己的复活通道）和已归档项。

判据用「多久没碰 + 访问几次」而不是分数阈值 —— 分数里总有加项兜底，
再冷的条目也跌不到 0，拿绝对阈值归档会永远不触发。

**长期静默不计入寿命**：两次遗忘计算的间隔最多按 `[forget] max_gap_days`（默认 2 天）算，
超出的部分当冻结期从周期起点里扣掉。冻结后一次计算最多承认 2 天流逝，
所以周期 > 2 天的 L3 及以上在静默后仍然不到期，L2/L1 照常掉下去。
工程停摆三个月回来跑一次 `judge`，记忆不会因为「过了三个月」被判到期。
置 0 关闭冻结，回到「静默多久算多久」。

### 检索打分

```
final = type_weight × (0.72 × retrieval + 0.12 × importance + 0.16 × level_factor)

retrieval    = (0.70 × vec_sim + 0.30 × lex_sim) / 可用通路权重和
level_factor = (forget_level / 最高等级) ^ 0.65    （0 级恒为 0）
type_weight: experience 1.00 / episode 0.96 / log 0.90
             （episode 再乘压缩级别权重 0.88~0.96）
```

第三项为什么是等级而不是「强度」：旧方案把「多久没碰」和「碰过几次」揉成一个数，
再乘半衰期衰减，结果是分值的形成过程无法解释。等级一个周期裁定一次、一次只动一级，
三个月后还能回答「它为什么在这一级」。日志没有等级，恒按 1.0——
它靠 `type_weight` 与复盘清空体现「原始素材」的定位，不该被等级再罚一次。

## 为什么是两段式检索

`vec0` 的 KNN 只接受 `WHERE embedding MATCH ? AND k = ?`，无法在向量扫描阶段做业务过滤。所以：

1. **召回**：向量 KNN 与 FTS 各自取候选 rowid，不做过滤
2. **融合**：join 主表，在 Python 里做状态/作用域过滤与打分

候选集放大到 `limit × 8`（上限 240）。

## 开发

```bash
./.venv/Scripts/python.exe -m pytest -q                    # 349 项单元测试
./.venv/Scripts/python.exe tools/verify_mcp_config.py      # MCP 端到端（隔离环境）
sh tools/hooks/install.sh                                  # 装 pre-commit 钩子（每台机器一次）
```

**提交钩子**拦两件事，都是"进了历史就得改写历史才能清掉"的那类：

1. **本地专用文件**：`ExternalCommandSystem/`、`.workbuddy/`、`.ai_memory/`；
2. **内容里的本机真实路径**：用户目录、开发机的安装目录、工程根这类绝对路径。
   只扫**新增行**，且只认"绝对路径 + 本机特征" —— `D:/proj/YourProject`、
   `<安装根>/libraries` 这类占位符照常放行。

误报时在那一行末尾加 `# pathguard:allow`，并在 review 时说明理由。

> `starmap.bat` / `report.bat` 优先调用**发布版**（`<发布根>/current`），
> 只有发布版不存在时才退回 `.venv`。所以改完源码这两个 bat **不会自动跟着变** ——
> 要么先 `tools/build_release.py --publish`，要么删掉发布根让它退回源码态。
> 判断当前跑的是哪一版：bat 第二行会打印 `Using: published (...)` 或 `source (...)`。

## 已知限制

- **同步 SQLite**：并发写靠 WAL + `busy_timeout`，单机单 Agent 够用，高并发需外部串行化
- **无 ANN 索引**：sqlite-vec 0.1.x 走暴力扫描，十万条内毫秒级
- **trigram 边界**：FTS 只能匹配连续 3 字符以上子串，中文换序靠 bigram LIKE 兜底（全表扫描）
- **换模型需 reindex**：维度变了旧向量无法复用
- **信条不热重载**：改完 `CORE_RULES.md` 要重开会话才生效（这是为了保证 KV Cache 稳定）

## 许可

MIT
