---
name: humanity-memory
description: 仿生记忆库的读写规范。需要跨会话记住用户偏好、项目约定、技术决策、踩过的坑，或需要回忆此前做过什么、为什么这么做时使用。覆盖信条/知识/现状/案例/日志的分工与边界、写知识还是写日志的判断、现状怎么续命与换代、召回时机与检索式写法、午夜复盘固化的完整流程，以及新工作区接入（初始化 / 定时复盘 / 老记忆导入）的三步工作流。
---

# HumanityMemory

跑在 SQLite + sqlite-vec 上的本地长期记忆。**刻意模仿人类记忆的分层**，每层各有各的规则。

## 结构：四类记忆 + 一条信条

「真话」有好几种，寿命完全不同。混在一起装，等于让最短命的那种拖垮整个库。

| 类 | kind | 存哪 | 内容 | 规则 |
| --- | --- | --- | --- | --- |
| **信条** | — | `CORE_RULES.md` | 硬约束、工作方式 | 静态注入 System Prompt，**不进向量库、不检索、不遗忘** |
| **知识** | `knowledge` | `knowledge` 表 | 可迁移的机制、约束、接口约定 | 长期有效（半衰期 10 年）；被推翻时**追加新条目并让旧的失效** |
| **现状** | `state` | `knowledge` 表 | 版本、依赖、路径、资源配置 | 200 字内，**必须定期复核**；周期 1→3→5→7 天渐进，到期未复核会被归档 |
| **案例** | `case` | `episodes` 表 | 一次具体事件的「现象 → 根因 → 落点」 | ≤300 字，一个问题一条，权重与近期经历持平 |
| **叙述** | `narrative` | `episodes` 表 | 当天经历的完整叙述 | 刚复盘时接近原文，随后**随时间逐级压缩** |
| **日志** | `log` | `logs` 表 | 当天发生的原始记录 | **只追加、不可修改**（触发器强制），复盘后清空 |

信条不需要你操心——它已经在你的系统提示里了。你主要操作后四类（五类，含日志）。

> **实现注记（schema v3 已落地）**：`knowledge` 与 `state` 同表不同 `kind`，
> `case` 与 `narrative` 同表不同 `kind`。kind 是**行级属性、一级概念**，
> 表只是物理容器；`kind` 写入后不可改（触发器拒绝），所以选错只能新写一条。
> 旧的 `facts` 表已退役，v2 库启动时会自动改名为 `workspace.db.v2-<时间戳>` 留档。

## 落笔之前：先问三句

这一层要收的是**知识**，不是"我看见的东西"。"陈述一句真话"太容易了，
而记忆的价值不在真，在**可迁移**。

写 `memory_write` 之前，先回答三个问题：

1. **换个单号、换个任务 ID、换个资源名，这句话还成立吗？**
   不成立 → 它是**案例**，写 `kind="case"`，而不是知识。
2. **三个月后它还成立吗？**
   不成立 → 它是**现状**，写 `kind="state"`。写进来只是「登记」，之后靠
   `memory_review` 定期复核 —— 仍成立就续命，不成立就删掉。
3. **我能指出它在代码里的落点吗？**（哪个类、哪个方法、成立的条件）
   指不出 → **先别写**。写下来只会是一条听起来可信、拿不出证据的断语，
   比不写更危险：它会成为后续排查的锚点，把人往错方向带。

三问全过，才是知识。

## 五种东西，五个去处

| 类型 | 判据 | 例子 | 怎么落 |
| --- | --- | --- | --- |
| **知识** | 换实例仍成立 + 有代码落点 | "`ResolveTask` 末尾 `UsesScene(taskId,null)==true` 会短路兜底，所以引导卡住时不报错" | `memory_write(kind="knowledge")` |
| **现状** | 当前为真、随版本或方案变化 | "项目是 Unity 2022.3.62f1"、"热更走 HybridCLR" | `memory_write(kind="state", source=...)`；到期用 `memory_review` 复核 |
| **案例** | 绑单号、绑一次事件 | "Bug #10805：引导不指餐厅，根因 StockBread 三条静默出口" | `memory_write(kind="case")` |
| **叙述** | 当天的过程、试错、判断转折 | "今天排查 X，先试 A 不行，再试 B" | `memory_write(kind="narrative")` 或复盘时提交 |
| **日志** | 还没想清楚的原始素材 | 报错栈、时间线、原话 | `log_append` |

### 反例：同样是真话，价值差十倍

```
❌ kind=knowledge: Bug #10805 根因：QuestGuide.StockBread 在任务 40001 开局有三条静默出口……
```
单号和任务 ID 把它钉死在一件事上。三个月后有人问"引导 114514 为什么不触发"，
它进得了候选集，却只能是噪音——甚至会把排查方向锚到 StockBread 上。

```
✅ kind=knowledge: QuestGuide 排查「引导卡住 / 零提示」先看三处：各分支 return 前漏了 Tip、
   ResolveTask 末尾 UsesScene(taskId,null)==true 短路兜底、!MainIdle() 提前 return。
```
换任何单号都成立，还能指出落点——这才是知识。原案例本身不丢，
压成 `kind="case"` 存着即可，检索时权重与近期叙述持平，但不会冒充知识。

**实测差异**：同一模型下，后者对"引导 114514 没有触发 / 引导卡住 / 任务没指路"
四类问法的相似度比前者高 0.09~0.18，与无关内容的区分度接近翻倍。

### 手软的代价是双份的

把案例当知识写，检索时它同时做两件坏事：占掉 top-6 里的一个席位
（实测这批库里 top-6 相似度区间只有 0.07，无关项很容易挤进来），
以及**锚定排查方向**——看到"三条静默出口"就顺着想，而真实根因可能在配表。

**默认写日志。** 白天对话里绝大多数内容都应该进日志，复盘时再压缩成知识。
过早写知识会导致：记了一堆当天细节当"永久结论"，三个月后全是噪音。

> **日志的寿命只到复盘为止。**
> 复盘（`consolidate_apply(purge=true)`）会**物理清空**原始日志，之后只剩压缩后的经历摘要。
> 所以要查精确的报错栈、完整时间线、当时的原始措辞，**必须在复盘前**查。
> 如果某天的原始记录很重要，复盘时改传 `purge=false` —— 日志会保留并标记为已复盘。
>
> 检索很便宜，别吝啬：`memory_search(..., kinds=["log"])` 只查日志层，
> `log_list(day="today")` 按天列原文，`kinds="all"` 才是全类别。

## 现状的生命周期：复核梯子 + 宽限

现状是唯一**不靠"被读到"续命**的类别。知识被检索到就算强化，现状不算——
因为"你查到它"不等于"它现在还是这样"。

复核周期不是固定的 7 天，而是**从 1 天起步逐步放宽**：

| 状态 | 下次复核 | 宽限 |
| --- | --- | --- |
| 刚写入 | 1 天 | 1 天 |
| 确认 1 次 | 3 天 | 3 天 |
| 确认 2 次 | 5 天 | 5 天 |
| 确认 ≥ 3 次 | 7 天（封顶） | 5 天（封顶） |

**为什么新条目要天天验**：刚写下的现状最容易错——写的时候就过时、记错版本号、
把临时方案当定论。熬过几次复核都没变，才有资格拿到更长的间隔。

判定只看 `max(created_at, verified_at)`，**被检索、被召回都不算续命**：
反复被查往往说明它写得模糊，或者已经不准了。

```
# 看一眼，还是这样 → 续命并升一档（1→3→5→7）
memory_review(item_id="s_xxx", matched=true)
# 等价写法：memory_write(kind="state", verifies="s_xxx")

# 看一眼，已经不成立了 → 当场物理删除
memory_review(item_id="s_xxx", matched=false)
```

- 到期后先给一个宽限（= 当前周期，最多 5 天），宽限内只是进待复核清单，不会消失。
- **宽限也过了才会被归档**：`memory_archive` 扫描时收走，正文不动，`include_archived` 能查回。
- 想知道欠了哪些账：`memory_review_list()`，或看 `memory_stats` 里的 `state_review`。
- 必须给 `source`：凭什么认为它是这样（哪个文件、哪次评审、哪句原话）。
- 上限 200 字。写不下说明你其实在写知识，该换 kind。
- 命令行：`humanity-memory memory review` 列到期；带 `--id s_xxx` 续命；加 `--stale` 删除。

### 为什么「不符」是直接删，而不是归档

现状的价值只在「它现在是真的」。一条已经假了的现状，正文长得和真话一模一样，
读的人没法从字面看出它过期了——留着只会污染检索、锚定判断。
知识可以归档，是因为它有过时的余地（机制没变，只是暂不适用）；现状没有这个余地。

所以 state **不参与失效链**：`invalidates` 对它直接报错，`memory_invalidate` 也拒绝。
要换现状就「删旧的 → 独立写新的」，库里永远只有当下真实的那几条。

## 知识被推翻时

⚠️ 这一节**只适用于 `knowledge`**。现状走上面的复核流程，不要给它打失效标记。

**不要**改旧条目（改不了，触发器会拒绝），**不要**删。

```
memory_write(kind="knowledge", content="项目改用 UI Toolkit", invalidates="k_旧的id")
```

旧条目自动变成 `invalid_at` 非空，默认检索不再返回，但 `memory_search(include_invalid=true)` 能查到历史。

这条链是**单向的**，已失效的条目不能被"复活"——写错了就再追加一条新的。

## 工具速查

| 工具 | 用途 |
| --- | --- |
| `log_append` | 追加当天日志。白天的主力工具 |
| `memory_write` | **唯一写入口**，`kind` 必填：knowledge / state / case / narrative |
| `memory_review` | 复核现状：`matched=true` 续命并升档，`matched=false` 直接删除 |
| `memory_review_list` | 列到了复核时间的现状（due 宽限内 / overdue 即将归档） |
| `memory_invalidate` | 让 **knowledge** 失效（过时但暂无替代）；对 state 会拒绝 |
| `memory_promote` | 把项目条目提升为全局条目 |
| `memory_list` | 列条目；`kinds` 不传即全部，可传 `"all"` |
| `log_list` | 按日期列日志 |
| `memory_recall` | 按当前话题召回 + 强化命中项 |
| `memory_search` | 只查不改，含跨库全局条目；`format="full"` 取全文 |
| `consolidate_preview` | 取当天待复盘日志 |
| `consolidate_apply` | 提交复盘结果（`knowledge=[...]` / `episodes=[...]`） |
| `rules_get` / `rules_append` | 读/追加信条 |
| `memory_stats` | 统计与健康提示（按 kind 分列） |
| `memory_use_workspace` | 切换到另一个项目的工作区（一条配置服务多项目） |
| `memory_archive` | 归档低价值记忆（默认只预览，按行级 kind 各自 TTL） |
| `memory_unarchive` | 从归档恢复 |
| `memory_purge_candidates` | 列删除候选（只报告） |
| `memory_purge` | 物理删除，需 `confirm=true`，只能删已归档的 |
| `memory_compress_preview` | 列可压缩的经历 |
| `memory_compress_apply` | 合并多条经历（原条目归档） |
| `memory_recompress_preview` | 列出该进一步压缩的旧经历 |
| `memory_recompress_apply` | 改写经历并提升压缩级别 |
| `memory_maintenance` | 补 embedding |

旧名兼容：`fact_write` 等已移除；`kind="fact"` 会被归一化为 `knowledge`、`"episode"` 归一化为 `narrative`。

## 一条 server 配置服务多个项目

一个 server 进程可以随时切换工作区，不必为每个项目写一条 MCP 配置：

```
memory_use_workspace(path="D:/proj/YourProject")
```

切换后所有读写都落在该项目自己的 `.ai_memory/workspace.db`，**项目之间仍然物理隔离**。
`persist=true`（默认）会记住这次选择，下次 server 启动自动接上。

**什么时候要调**：
- 会话开始，`memory_stats` 显示的工作区不是你正在做的项目
- 用户提到了另一个项目的记忆

**工作区来自哪里**（优先级从高到低）：

1. `--token` 令牌文件
2. `--workspace` 命令行
3. `HM_WORKSPACE` 环境变量
4. cwd 向上查找**已存在**的 `.ai_memory`
5. cwd 处在一个像项目的目录里（有 `.git` / `ProjectSettings` / `package.json` 等）
   → **在项目根自动建立**，不用手动 init
6. 上次用过的工作区（`~/.humanity-memory/last-workspace.txt`）
7. 无 → 仅全局库

在子目录（如 `<项目>/Assets/Scripts`）里启动时，会自动归位到项目根，不会在子目录建库。
用户主目录和盘符根永远不建。

## 存储布局

```
<项目>/.ai_memory/workspace.db    # 项目记忆（物理隔离）
<项目>/.ai_memory/CORE_RULES.md   # 项目信条
~/.humanity-memory/global.db      # 跨项目知识（global.db 只有 knowledge 类）
~/.humanity-memory/CORE_RULES.md  # 全局信条
```

**项目之间物理隔离**，换个项目就看不到上一项目的记忆。全局条目例外，永远参与检索。

所以：**跨项目要用的写 `scope="global"`，但别滥用**。规则类内容应该进 `CORE_RULES.md`，
全局知识超过 50 条 `memory_stats` 会警告——那通常说明该拆成规则或留在项目里。
`scope="global"` 只接受 knowledge / state，案例和叙述留在项目内。

## 复盘

日志是原始素材，需要定期压成知识和经历。**这不是你的模型之外的事**——
复盘由当前会话的模型执行，你不用调用外部 LLM：

1. `consolidate_preview(day="today")` 拿到当天日志和现有条目
2. 你自己阅读、提炼：哪几条够格当知识、哪条现状变了、当天怎么压成一段叙述
3. `consolidate_apply(knowledge=[...], episodes=[...], purge=true)` 提交
4. 原始日志被清空，知识和经历留存

`knowledge` 每项可带 `"kind": "state"` 覆盖默认的知识归类，带单号的结论写成
`episodes` 里的 `"kind": "case"`。

详见 `references/consolidate-guide.md`。

**配成定时任务**：每个工作区各建一个，只复盘自己那个库。任务 prompt 引用
`references/consolidate-task.md` 即可，别把一个任务写成跨项目扫描。

### 接入一个新工作区（三步，固定顺序）

用户说「给 X 项目初始化记忆系统」时按这三步走完，细节与检查清单见
`references/workspace-onboarding.md`：

1. **初始化记忆库** —— 先探查 git 根与已有 `.ai_memory`；**已存在就核验、不删重来**
   （除非用户明确说「删了先」）。`init` 是幂等的，跑完用 `doctor` 和子目录 `stats` 验证。
2. **编写定时复盘任务** —— **先向用户确认配置再创建**：循环周期、执行时刻、
   **持续时间**（`validUntil`，不设即无限期）、工作目录 `cwds`。别把「凌晨」这类模糊词
   自己拍成某个时间点，也别默认无限期；prompt 必须显式切库并校验 `workspace_db`。
3. **把原本的记忆文档以日志录入，等待复盘** —— 只读盘点旧来源，
   **按大条目（`##` 章节 / 独立事项）拆成多条日志，防止单条过大**；原文保留、不预先提炼，
   用 `store.append_log(at=...)` 保留原始日期（`log add --day` 对 add 无效）。
   只验证 `consolidate_preview` 能捞到，**不要手动复盘**，留给定时任务。

## 叙述：起点由重要性决定，之后随时间变短

仿生记忆是**两个维度**，只做一个都不对。

### 维度一：重要性决定一开始写多细

| importance | 初始级别 | 写多细 |
| --- | --- | --- |
| **≥ 0.75** | level 0 | 完整叙述：背景、当时的判断、试错过程、精确报错、最终结论 |
| **0.45~0.75** | level 1 | 要点：关键步骤 + 结论，不铺陈过程 |
| **< 0.45** | level 2 | 一句话说清是什么 |

**琐事不该有 level 0。**「昨天吃了什么」生下来就该是一句话——
先写成完整叙述再等它压缩，等于给琐事录纪录片。

**典型高重要性**：被用户明确批评、重大失误、关键架构决策、排查很久的疑难、
有情绪张力的对话。这些要保留原始措辞，几年后回看才有价值。

### 维度二：时间让它变短

| 压缩 | 门槛 | 内容变成 |
| --- | --- | --- |
| level 0 → 1 | 30 天 × (0.6 + importance) | 去冗余，保留关键步骤 |
| level 1 → 2 | 180 天 × (0.6 + importance) | 只留结论与根因 |
| level 2 → 3 | 归档时 | 一句话 |

门槛随重要性放大：importance 0.9 要 46 天才压，0.2 的 24 天就压了。
重要的事不仅起点更详细，也保持得更久。

### 怎么用

复盘时只给 importance，级别自动定：

```
consolidate_apply(episodes=[
  {"content": "被用户指出自作主张，应该先给方案再动手", "importance": 0.9},
  {"content": "修了个 typo", "importance": 0.2},
  {"content": "Bug #10805：引导不指餐厅 → StockBread 三条静默出口", "kind": "case"},
])
```

后续压缩：

```
memory_recompress_preview()     # 看哪些到了该压的年龄
memory_recompress_apply(items=[{"id":"e_x","content":"更短的版本"}], level=1)
```

检索权重也按级别走：level 0 的完整叙述参与检索但降权（0.88），压得越短权重越高（0.96）。
**案例固定 0.90**——不按级别衰减，因为它本来就是压过的，且要跟近期叙述抢得到席位。

## 记忆会淡忘，但不会被悄悄删掉

各类都有自己的生命周期，**信条例外（它根本不在库里，不参与任何淘汰）**：

| 类 | 归档条件 | 会压缩吗 |
| --- | --- | --- |
| 知识 | 3 年没被碰过、且访问 ≤ 1 次 | 不压缩（压缩会丢失精确性） |
| 现状 | **复核梯子到期 + 宽限都过了**（1→3→5→7 天，宽限 ≤5 天） | 不压缩 |
| 案例 | 1 年没被碰过、且访问 ≤ 1 次 | 可随经历一起压缩 |
| 叙述 | 1 年没被碰过、且访问 ≤ 1 次（反复读取可续命） | ✅ 可压缩合并 |
| 日志 | 复盘后清空（按复盘，不按访问） | 已有 |

**归档 ≠ 删除**。归档只是让它在默认检索里消失，正文一个字都没动，`include_archived=true` 随时能查回来。

物理删除必须显式调用 `memory_purge(ids, confirm=true)`，而且**只能删已归档的**——没归档的会被拒绝，这是有意的保护。

清理节奏建议：

```
memory_archive(dry_run=true)        # 先看会动哪些（会按 kind 分组报）
memory_archive(dry_run=false)       # 确认后执行
memory_purge_candidates()           # 定期看删除候选（只报告，不删）
memory_compress_preview()           # 看可合并的经历
```

要删的时候先问用户，说清楚「这些已经归档 N 天、从未再被访问」。**不要自己决定删**。

## 进一步阅读

- 各类的写入粒度与判断 → `references/writing-guide.md`
- 召回时机、query 写法、结果解读 → `references/recall-guide.md`
- 复盘怎么做、颗粒度怎么把握 → `references/consolidate-guide.md`
- 定时复盘任务的提示词模板 → `references/consolidate-task.md`
- **新工作区接入三步工作流（初始化 / 定时复盘 / 老记忆导入）** → `references/workspace-onboarding.md`
- 配置、排障、备份、换模型 → `references/operations.md`
