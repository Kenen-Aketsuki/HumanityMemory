# 运维与排障

## 存储布局

```
<项目>/.ai_memory/
├── workspace.db        # 项目库：facts + episodes + logs
└── CORE_RULES.md       # 项目信条

~/.humanity-memory/
├── global.db           # 全局库：跨项目 facts
├── CORE_RULES.md       # 全局信条
└── .env                # 配置（放这里，任何目录都能读到）
```

**配置必须放在 `~/.humanity-memory/.env`**，不要只放在项目目录。
MCP server 和 CLI 的工作目录不固定，项目级 `.env` 换个目录就读不到，embedding 会静默失效。

工作区查找规则：从 cwd 向上逐级找 `.ai_memory`，命中即用。
都没有时，如果 cwd 处在一个「像项目」的目录树里（含 `.git`、`ProjectSettings`、
`package.json`、`pyproject.toml`、`.workbuddy` 等标记），会在**项目根**自动建立，
所以新项目接入通常不需要手动 `init`。
用户主目录和盘符根永远不建。

启动上下文（cwd、走了哪条解析路径、命中的工作区）写在
`~/.humanity-memory/diagnostics/startup.json`，排查「为什么落到全局库」先看它。

## 令牌：把工作区绑定从 mcp.json 解耦

`mcp.json` 里写死 API key 有泄露风险（它会被同步、备份、分享）。令牌文件把
「工作区 + 信条 + 凭据」收进一处，客户端配置里只留一个路径：

```json
"args": ["-m", "humanity_memory.mcp_server", "--token", "<令牌路径>"]
```

```bash
humanity-memory workspace use <项目根> --token <令牌路径>   # 建/改
humanity-memory --token <路径> workspace show              # 查看（key 脱敏）
```

令牌里的 `embedding` 留空就回落到 `.env`。**带凭据的令牌别放项目里**——
项目会被 git 提交，凭据就跟着走了。放 `~/.humanity-memory/` 下更安全。

优先级：令牌 > `--workspace` > `HM_WORKSPACE` > cwd 向上查找。

## 配置

```bash
# 全局库目录
HM_HOME=C:/Users/you/.humanity-memory

# embedding（OpenAI 兼容）
HM_EMBED_BASE_URL=https://api.siliconflow.cn/v1
HM_EMBED_API_KEY=sk-xxx
HM_EMBED_MODEL=BAAI/bge-m3
HM_EMBED_DIM=1024          # 必须和模型实际维度一致

# 显式指定工作区（跳过 cwd 查找）
# HM_WORKSPACE=D:/proj/SomeProject
```

完整清单见仓库 `.env.example`。

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

### `logs 表是 append-only：禁止修改已写入的历史内容`

**这不是 bug，是设计。** 你想改日志内容，应该：

- 追加一条新日志说明更正
- 或等复盘时把正确结论提炼成事实

日志是原始凭证，改了就不叫凭证了。

### `未复盘的日志不可删除，请先执行 consolidate`

同理。日志在执行 `consolidate_apply` 之前是唯一的原始素材，
删了就没法复盘了。确实要删就先复盘（哪怕只提一条事实）。

### `facts 正文不可修改：请追加新事实并使旧事实失效`

事实的正文是历史记录。要更正就 `fact_write(invalidates=旧id)`。

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

## 全局事实膨胀

`memory_stats` 里 `global_facts_warn: true` 说明全局事实超过阈值（默认 50）。

跨项目事实本该很少。膨胀通常意味着：

- 有些规则类内容该搬到 `CORE_RULES.md`（它们需要 100% 命中，不该靠检索）
- 有些项目细节该留在各自的 workspace 库
- 有些陈旧的该 `fact_invalidate`

## 查看复盘历史

```sql
SELECT day, log_count, fact_count, episode_count, summary, purged
FROM digests ORDER BY at DESC LIMIT 20;
```

`digests.meta` 里存了当次处理掉的 log id 列表，可追溯。
