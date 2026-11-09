# 工作区接入：初始化 → 定时复盘 → 老记忆录入

用户说「给 X 项目初始化记忆系统 / 接入这个记忆 / 建个复盘任务」时，走这三步。
**一条任务只服务一个工作区**，不要跨项目扫描、不要一个任务扫多个库。

---

## 步骤 1 · 初始化记忆库

### 先探查，别急着 init

| 查什么 | 为什么 |
| --- | --- |
| git 根在哪、工作区根是不是同一个目录 | 决定库会不会变成 `?? .ai_memory/` 噪声 |
| `.ai_memory` 是否已存在 | **已存在优先核验，不删重来** |
| 已有库的四表行数 / schema 版本 / 信条内容 | 判断是空壳、旧 v2 库，还是正在用的正常库 |
| 是否有活跃连接（`-wal` / `-shm`、`diagnostics/startup.json`） | 有人在用时删库会掀掉连接 |
| 项目性质文档（`CLAUDE.md` / `README` / 规约） | 步骤 2 写复盘 prompt 的项目特有条款要用 |

只有用户**明确说「老库删了先」**才删；删之前仍要逐表核实行数，并备份 `CORE_RULES.md`。

### 幂等初始化

```bash
humanity-memory --workspace "<工作区根>" init
```

`cmd_init` 只做 `ensure_rules`（文件不存在才写模板）+ 打开已有库，**不会重建、不会覆盖数据**，
所以库已经自动建好时跑一次是安全的。MCP server 在含 `.git` / `.workbuddy` 的项目根启动时
会按 `cwd-project` 自动建库，这种「已经初始化好了」的情况很常见——如实告诉用户，不要为了
显得做了事而删掉重建。

### 库放哪

- 默认放**工作区根**（`find_workspace_dir` 从 cwd 逐级向上找，子目录都能命中）。
- git 根在工作区更内层时，把库放外层反而更省事：既覆盖全部子目录，又天然不在版本库里。
- 若库**必然落在 git 仓库内**：先看 `.git/hooks/pre-commit` 是否已拦截 `.ai_memory/`；
  再优先用 `.git/info/exclude`（本地忽略、不影响协作者）消掉 untracked 噪声，
  多并行线共用的仓库尤其不要擅自改 `.gitignore`。

### 必跑的验证

```bash
humanity-memory --workspace "<根>" doctor     # all_ok: true
(cd "<根>/Assets" && humanity-memory stats)   # 不带参数，应命中同一个 workspace.db
```

再核 `CORE_RULES.md == paths.py RULES_TEMPLATE`（避免 v2 旧术语混入）、四表行数、
`resolve_layout` 的 `ws_rules` 有没有被 `.workbuddy/memory/信条.md` 抢走。

---

## 步骤 2 · 编写定时复盘任务（**先向用户确认，再创建**）

### 必须确认的四项配置

**不要替用户拍板。** 用户给了周期（如「每周二到周六凌晨一次」）也仍要确认
「凌晨具体几点」和「跑多久」，这两项最容易被含糊带过。用一次 `AskUserQuestion`
把下面四项问全：

| 项 | 落到哪个字段 | 例子 |
| --- | --- | --- |
| **循环周期** | `rrule` 的 `FREQ` + `BYDAY` / `INTERVAL` | `FREQ=WEEKLY;BYDAY=MO,WE` |
| **执行时刻** | `BYHOUR` / `BYMINUTE` | 凌晨 → 03:00，不要留「凌晨」这种模糊值 |
| **持续时间** | `validFrom` / `validUntil`（不设 = 无限期） | 长期有效 / 试运行到 2027-02-09 |
| **工作目录** | `cwds` | 必须是工作区根或其子目录 |

其它要点：

- **先列出现有任务再定时刻**，错开已有复盘任务，避免多个会话同一时刻启动。
- `cwds` 决定 MCP server 的 cwd 能否找到库；库在父目录时要说明「向上查找会命中」。
- `nextRunAt` 创建后换算成当地日期核对星期，别只信 rrule 字符串。
- 创建完 `view` 回读一次确认持久化。

### 任务 prompt 骨架

以 `references/consolidate-task.md` 为模板，另加三道闸：

1. `memory_use_workspace(path="<工作区>", persist=false)` —— **persist 必须 false**，
   定时任务不能改掉用户白天会话的工作区选择；不要依赖 server 启动时的 cwd 猜。
2. `memory_stats()` 校验 `stats.layout.workspace_db` 必须是预期路径，
   **不对就立刻停机报告、一个字都不写**（防记忆静默分裂写错库）。
3. `logs_pending == 0` 跳到压缩预览 / 归档预览 / 维护，不硬造内容。

项目特有条款从项目规约文档里提炼，例如「负责人拍定的基调属硬约定，进 knowledge 且
importance 0.9」「带单号结论只进 case」「自动生成文件不作为结论」。禁止项固定包含：
`memory_archive(dry_run=false)`、`memory_purge`、改 `CORE_RULES.md` / 源码 / 配置、
任何 `git add/commit/push`。

流程顺序：`preview → 提炼 → apply(显式 day, purge=true) → 逐日补最多 5 天 →
recompress → archive(dry_run=true) → maintenance`。

---

## 步骤 3 · 把原本的记忆文档以日志录入，等待复盘

### 找全来源，别重复导入

常见来源：`.workbuddy/memory/*.md`（WorkBuddy 自带那套记忆）、项目内旧笔记。
**先只读检索** global 库、当前 workspace 库、目标 workspace 库，确认哪些条目已经存在；
记忆系统自身的运维日志属于「工具」，按任务 prompt 不进业务库。

### 按大条目拆分（重点）

> **推荐把一整个记忆文档按大条目拆开，防止单个日志过大。**

- 按 `## ` 章节 / 一二级标题 / 独立事项切分，**一条日志一个主题**；
  没标题的长文档按空行分段或语义主题拆，别整篇塞成一条巨型日志。
- 单条过大坏处：embedding 质量下降、复盘时一条日志混了多件事、
  `case` 该「一事一条」却被挤成流水账、超长内容还会挤占 preview 的 token 预算。
- **原文保留、不预先提炼**：提炼是复盘的工作，导入只做搬运 + 标注。
- 每条加一行导入头，写清来源路径、原始记录日期、以及
  「勿把它当作导入当天新发生的事件」。
- `tags` 建议：`迁移导入 / <来源系统> / <原日期> / <项目> / <条目名>`；
  `importance` 用中等值（0.5 左右），别让历史素材冒充重大事件。

### 保留原始日期（易踩坑）

CLI 的 `log add --day` **只对 `log list` 生效**，`add` 会忽略它；
MCP 的 `log_append` 也没有 `day` 参数。想让日志归到原始日期，用官方 store API：

```python
store.append_log(content, at=<原始日期的 Unix 毫秒>, tags=..., importance=0.5)
# day 由 models.day_of(at) 推导
```

不做这一步，日志 `day` 会变成导入当天，`consolidate_apply` 会把 digest / episodes 也记到
错误的日子，以后回看「11-09 做了 pre-commit 钩子移植」就是失真的。

`_latest_pending_day()` 取 `ORDER BY day DESC`，所以只有这批历史日志时首次复盘就会命中；
之后即使有新日志，按天补完也会轮到旧日期，不会被饿死。

### 验证

- 每条 `day` 为原日期、`embedded=1`、`consolidated=0`
- `stats` 的 `logs_pending` 等于拆出的条数；`knowledge` / `episodes` **仍为 0 行**
  （证明只录了原始素材，没偷偷复盘）
- `consolidate_preview()` 返回的 `day` 与 `log_count` 能捞到这批
- `doctor` 仍 `all_ok: true`

**不要手动跑复盘**，留给定时任务，这样用户能看到任务真的在工作。

---

## 收尾检查清单

- [ ] 已有库核验过行数 / 活跃连接，没有误删
- [ ] `init` 幂等执行，`doctor all_ok: true`，子目录能自动命中
- [ ] 任务配置四项已向用户确认并复述（周期 / 时刻 / 持续时间 / cwds）
- [ ] 任务 `view` 回读、`nextRunAt` 换算核对星期
- [ ] 老记忆按大条目拆分、标注来源与原日期，`consolidate_preview` 能捞到
- [ ] 全程不 `git add` / `commit` / `push`，改动只留工作区
- [ ] 记一条运维日志，说明做了什么、验证结果、遗留待决事项

## 常见坑

| 坑 | 后果 |
| --- | --- |
| 把已自动建好的库当成空壳删掉 | 掀掉正在使用的连接、丢失数据 |
| 库落在 git 仓库内却没配本地忽略 | `?? .ai_memory/` 噪声、被并行线 `git add` 卷走 |
| 定时任务没确认「持续时间」 | 默认无限期，用户其实只想试运行 |
| 「凌晨」没落到具体小时 | 每次触发时间不可预期 |
| 整篇旧文档塞成一条日志 | 复盘颗粒度失控、embedding 质量差 |
| 以为 `log add --day` 能改日期 | 日志全记到导入当天，digest 日期失真 |
| `cwds` 不在工作区目录树 | server 落到别的库，复盘写错地方 |

## 见

- 定时复盘任务提示词模板 → `consolidate-task.md`
- 复盘颗粒度与判断标准 → `consolidate-guide.md`
- 各类的写入判据 → `writing-guide.md`
- 配置 / 排障 / 备份 → `operations.md`
