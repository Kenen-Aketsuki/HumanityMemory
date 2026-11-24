# 运维与排障

## 存储布局

```
<安装根>/libraries/<label>/          # 库本体（2026-11-19 起统一存放）
├── workspace.db        # 记忆库：experience + episodes + logs
├── memory.toml         # 库级配置（可选，最高优先级）
├── reports/ retrieval/ # 派生产物（judge 报告、检索缓存）
└── *.bak-*             # 库自己的备份

<项目>/.ai_memory/token.json         # 身份锚点：只说明"这个项目用哪个库"
<项目>/.workbuddy/memory/信条.md     # 项目信条（跟着项目走，不跟库）
<安装根>/.env                        # 配置唯一来源（含 HM_LIBRARIES）

~/.humanity-memory/
├── registry.json              # 库与钥匙的归属索引
└── CORE_RULES.md              # 全局信条（静态注入，仍然生效）
```

**项目之间物理隔离**：不同项目指向不同 label，互不可见（共享组除外）。
**删库要显式去 `libraries/<label>/`** —— 删项目只删掉那张身份卡，库还在。
换机器 / 重装时先备份 `libraries/`，它不在任何 git 仓库里。

**`.env` 只认仓库根这一份**（`<仓库根>/.env`，由 `config.py` 相对源码自身位置
定位，源码不含硬编码绝对路径）。不再读 `~/.humanity-memory/.env` 或 cwd 下的
`.env`；无论 MCP server / CLI 从哪个目录启动，读到的都是同一份配置，
不会出现「换个目录 embedding 静默失效」。真实环境变量仍是最高优先级
（令牌、`mcp.json` 的 env、命令行覆盖都依赖它）。

工作区查找规则：从 cwd 向上逐级找 `.ai_memory`，**只认含 `token.json` 的那个**。
找不到就是「未接入」——不建目录、不建库、也不沿用上次的工作区。
（2026-11-18 起：自动建库已删除，`last-workspace.txt` 已删除。）

启动上下文（cwd、走了哪条解析路径、命中的库、错误码）写在
`~/.humanity-memory/diagnostics/startup.json`，排查「为什么落到这个库」先看它。

## 令牌：项目的接入证明（v2）

身份在项目里，不在 `mcp.json` 里。客户端配置只剩 `["serve"]`，
打开哪个库由当前目录的 `<项目>/.ai_memory/token.json` 决定：

```json
{
  "version": 2,
  "key": "hmk_7f3a9c1e…",
  "workspace": "D:/proj/YourProject",
  "library": "yourproject",
  "label": "yourproject"
}
```

接入 / 运维（全部有对应 MCP 工具，`memory_bind_*`）：

```bash
humanity-memory bind new [--label 名字]      # 新建独立库 + 发令牌
humanity-memory bind adopt                   # 沿用本目录已有的 .ai_memory/workspace.db
humanity-memory bind existing --library 标签  # 并入已有库（A、B 同库）
humanity-memory bind decline                 # 本次不接入（library=null，记住决定）
humanity-memory bindings list                # registry：有哪些库、谁在用
humanity-memory revoke --workspace <项目>     # 撤销钥匙（不动库文件）
humanity-memory bind move --from <旧> --to <新>   # 搬家后修令牌
```

令牌里**没有凭据**：embedding 只走 `.env`（见下）。registry 只存 `sha256(salt+key)`，
明文钥匙只在令牌文件里；`library` 是断言、真归属在 registry，手改无效。

启动优先级：`--workspace` > `HM_WORKSPACE` > cwd 向上查找（只认带令牌的）。
`--token` 仍可传，但只作「路径覆盖 + workspace 提示」，不再是身份来源。

## 配置

```bash
# 信条与库所在目录（global.db 已退役，这里现在只放信条/诊断/备份）
HM_HOME=~/.humanity-memory

# embedding（OpenAI 兼容）
HM_EMBED_BASE_URL=https://api.siliconflow.cn/v1
HM_EMBED_API_KEY=sk-xxx
HM_EMBED_MODEL=BAAI/bge-m3
HM_EMBED_DIM=1024          # 必须和模型实际维度一致

# 显式指定工作区（跳过 cwd 查找）
# HM_WORKSPACE=D:/proj/SomeProject
```

完整清单见仓库 `.env.example`。

### 阈值配置在哪（四层，后者覆盖前者）

| 层 | 路径 | 定位 |
| --- | --- | --- |
| 出厂 | 包内 `memory.default.toml` | 全量默认值，随版本发布，别改 |
| 安装 | `<安装根>/memory.toml`（与 `.env` 同目录） | **全量模板**，`humanity-memory config template` 整份生成 |
| 用户 | `~/.humanity-memory/memory.toml` | **增量**，只写要改的键，跨项目 |
| 项目 | `<项目>/.ai_memory/memory.toml` | **增量**，只写要改的键，优先级最高 |

```bash
humanity-memory config template        # 生成安装位置的全量模板（已存在会拒绝）
humanity-memory config template --force   # 覆盖重来
```

**只改几项就用用户级 / 项目级**，别动安装层那份全量：全量放着等于接管了所有默认值，
以后出厂调了某个值，你那份的旧值会赢。

**改完必须重启 MCP 客户端**（server 常驻，配置只在进程启动时读一次）。
CLI 和星图脚本每次都是新进程，改完立刻生效。

### 没有 embedding 也能用

退化为关键词检索（FTS5 trigram + bigram 兜底），中文子串匹配依然有效，
只是缺少语义泛化。恢复配置后跑 `humanity-memory reindex` 补欠账。

## 排障

先跑自检：

```bash
humanity-memory doctor
```

逐项检查扩展、FTS、**append-only 触发器是否安装**、schema 维度、embedding 探活、
信条文件。任何一项失败退出码为 1。

### 调读写工具返回「拒绝 + checklist」

**这不是安装坏了，是拒绝态。** 2026-11-24 起工具面**始终全量注册**（不再裁剪）：
未接入时所有记忆工具都可见，但读写、检索、复盘类调用一律返回
`ok:false` + `code` + `checklist`。`memory_bind_status` / `memory_bind_*` /
`memory_use_workspace` 不在拒绝范围内，未接入时照常可用。

第一步永远是看 `code`，别猜：

```
memory_bind_status()            # 看当前会话
memory_bind_status(workspace="<项目根>")   # 预检任意路径
```

| `code` | 含义 | 出路 |
| --- | --- | --- |
| `no_workspace` | 当前 cwd 不是任何项目 | 传项目根给 `bind_status`，再 `memory_use_workspace(path=…)` |
| `no_token` | 是项目，但没接入 | `bind adopt` / `bind new` / `bind existing` |
| `declined` | 该项目明确选了不接入 | 想改主意就 `bind new` / `bind existing` |
| `library_missing` | 库目录不在（盘没挂载） | 挂载后重试，或 `bind existing` 换库 |
| `token_*` / `key_*` | 令牌或钥匙对不上 | 重发令牌：`bind adopt` |

**最常见的一种：`no_workspace` + cwd 是应用安装目录。**
MCP 客户端偶尔会用「应用所在目录」而不是项目目录起一个 boot 实例
（它的 cwd 与用户看到的会话无关），于是解析不到任何项目。
这种情况下 server 本身没坏 —— 处理方式：

```
memory_use_workspace(path="D:/proj/YourProject")
```

它会**在同一进程内**校验令牌、切换库；工具本来就全部在线，切换后读写
守卫自动放行，不需要重启客户端、也不需要重新挂载工具。
若客户端只是缓存了旧的会话状态，刷新/重开会话也能好。

判断"到底是不是拒绝态"的可靠办法：`memory_stats` 返回 `ok:false` + `code`
就是拒绝态（工具可见但未接入）；能返回统计数字就说明已接入。
工具现在始终全量注册，不存在"调不通（unknown tool）"的情况 ——
遇到 unknown tool 是客户端没连上 server，不是拒绝态。

### `logs 表是 append-only：禁止修改已写入的历史内容`

**这不是 bug，是设计。** 你想改日志内容，应该：

- 追加一条新日志说明更正
- 或等复盘时把正确结论提炼成知识

日志是原始凭证，改了就不叫凭证了。

### `未复盘的日志不可删除，请先执行 consolidate`

同理。日志在执行 `consolidate_apply` 之前是唯一的原始素材，
删了就没法复盘了。确实要删就先复盘（哪怕只提一条知识）。

### `正文不可修改`

条目正文是历史记录。要更正就 `memory_write(kind=..., content=新, invalidates=旧id)`。

### `无法加载 sqlite-vec 扩展`

当前 Python 的 sqlite3 没编译 loadable extension 支持。用官方发行版 Python，
或让 `HM_VEC_EXTENSION` 指向可用的 `vec0.dll`。

### `未启用 FTS5`

SQLite 编译时没带 FTS5，换 Python 发行版。

### 中文搜不到

`doctor` 里看 `fts5` 的 tokenizer，应为 `trigram`。若是 `unicode61`，
说明 SQLite 低于 3.34，中文只能整段匹配。升级 Python。

### `database is locked`

WAL 模式下通常是瞬时冲突，`busy_timeout` 已设 8 秒。
持续出现说明有长事务——检查是不是在事务里做了 embedding 网络调用
（本项目的实现已经把网络调用放在事务外）。

另一个常见原因是**同一进程里先后开了两个连接做 DDL**。要复用连接，不要重复 open。

## 换 embedding 模型

维度变了旧向量直接作废：

```bash
humanity-memory reindex --reset-vectors
```

忘了这一步会在启动时抛 `SchemaError`，不会静默出错。

维度没变（比如 bge-m3 → 另一个 1024 维模型）就不用重置，但建议还是 reindex
以保证向量空间一致。

## 备份与迁移

**整个记忆就是一个目录。** 项目记忆在 `.ai_memory/`，全局在 `~/.humanity-memory/`。
停掉 server 后直接复制。

想导入别的机器：把 `.ai_memory/` 整个拷过去即可，路径无关。

## 全局经验膨胀

`memory_stats` 里 `global_experience_warn: true` 说明全局经验超过阈值（默认 50）。

跨项目经验本该很少。膨胀通常意味着：

- 有些规则类内容该搬到 `CORE_RULES.md`（它们需要 100% 命中，不该靠检索）
- 有些项目细节该留在各自的 workspace 库
- 有些陈旧的该 `memory_invalidate`

## 查看复盘历史

```sql
SELECT day, log_count, knowledge_count, episode_count, summary, purged
FROM digests ORDER BY at DESC LIMIT 20;
```

`digests.meta` 里存了当次处理掉的 log id 列表，可追溯。

> `knowledge_count` 是**历史列名**（列随表名一起没改），现在数的是经验条数。
