---
name: humanity-memory
description: 三层仿生记忆库的读写规范。需要跨会话记住用户偏好、项目约定、技术决策、踩过的坑，或需要回忆此前做过什么、为什么这么做时使用。覆盖信条/事实/日志三层的分工与边界、写事实还是写日志的判断、事实被推翻时如何追加式失效、召回时机与检索式写法、以及午夜复盘固化的完整流程。
---

# HumanityMemory

跑在 SQLite + sqlite-vec 上的本地长期记忆。**刻意模仿人类记忆的分层**，三层各有各的规则。

## 三层结构

| 层 | 存哪 | 内容 | 规则 |
| --- | --- | --- | --- |
| **信条** | `CORE_RULES.md` | 硬约束、工作方式 | 静态注入 System Prompt，**不进向量库、不检索、不遗忘** |
| **知识**（原名「事实」） | `facts` 表，待改名 `knowledge` | 可迁移的机制、约束、接口约定 | 长期有效，遗忘极慢；被推翻时**追加新条目并让旧的失效** |
| **日志** | `logs` 表 | 当天发生的原始记录 | **只追加、不可修改**（数据库触发器强制），复盘后清空 |
| **经历** | `episodes` 表 | 当天经历的完整叙述 | 刚复盘时接近原文，随后**随时间逐级压缩** |

信条不需要你操心——它已经在你的系统提示里了。你主要操作另外三层。

## 落笔之前：先问三句

这一层要收的是**知识**，不是"我看见的东西"。名字叫「事实」本身就是偏差源头——
"陈述一句真话"太容易了，而记忆的价值不在真，在**可迁移**。

写 `fact_write` 之前，先回答三个问题：

1. **换个单号、换个任务 ID、换个资源名，这句话还成立吗？**
   不成立 → 它是**案例**，不是知识。写日志。
2. **三个月后它还成立吗？**
   不成立 → 它是**现状**（版本号、依赖、目录、资源配置）。可以写，但必须
   `tags=["state"]`，并且变更时用 `invalidates` 顶掉它，别让它自己烂在库里。
3. **我能指出它在代码里的落点吗？**（哪个类、哪个方法、成立的条件）
   指不出 → **先别写**。写下来只会是一条听起来可信、拿不出证据的断语，
   比不写更危险：它会成为后续排查的锚点，把人往错方向带。

三问全过，才是知识。

## 三种东西，三个去处

| 类型 | 判据 | 例子 | 写哪 |
| --- | --- | --- | --- |
| **知识** | 换实例仍成立 + 有代码落点 | "`ResolveTask` 末尾 `UsesScene(taskId,null)==true` 会短路兜底，所以引导卡住时不报错" | `fact_write` |
| **现状** | 当前为真、随版本或方案变化 | "项目是 Unity 2022.3.62f1"、"热更走 HybridCLR" | `fact_write` + `tags=["state"]`，变更时 `invalidates` |
| **案例** | 绑单号、绑一次事件 | "Bug #10805 根因是 StockBread 三条静默出口" | `log_append` |
| **日志** | 当天的过程、试错、时间线 | "今天排查 X，先试 A 不行，再试 B" | `log_append` |

### 反例：同样是真话，价值差十倍

```
❌ Bug #10805 根因：QuestGuide.StockBread 在任务 40001 开局有三条静默出口……
```
单号和任务 ID 把它钉死在一件事上。三个月后有人问"引导 114514 为什么不触发"，
它进得了候选集，却只能是噪音——甚至会把排查方向锚到 StockBread 上。

```
✅ QuestGuide 排查「引导卡住 / 零提示」先看三处：各分支 return 前漏了 Tip、
   ResolveTask 末尾 UsesScene(taskId,null)==true 短路兜底、!MainIdle() 提前 return。
```
换任何单号都成立，还能指出落点——这才是知识。

**实测差异**：同一模型下，后者对"引导 114514 没有触发 / 引导卡住 / 任务没指路"
四类问法的相似度比前者高 0.09~0.18，与无关内容的区分度接近翻倍。

### 手软的代价是双份的

把案例当知识写，检索时它同时做两件坏事：占掉 top-6 里的一个席位
（实测这批库里 top-6 相似度区间只有 0.07，无关项很容易挤进来），
以及**锚定排查方向**——看到"三条静默出口"就顺着想，而真实根因可能在配表。

**默认写日志。** 白天对话里绝大多数内容都应该进日志，复盘时再压缩成知识。
过早写知识会导致：记了一堆当天细节当"永久结论"，三个月后全是噪音。

> **实现注记（2026-11）**：当前 `facts` 表混存「知识」与「现状」，案例还没有独立表
> （暂时落在 `logs` / `episodes`）。判据按本文执行；schema 正在拆为
> `knowledge` / `state` / `cases` 三张表，拆完后三类各自有独立的失效与归档策略。

> **日志的寿命只到复盘为止。**
> 复盘（`consolidate_apply(purge=true)`）会**物理清空**原始日志，之后只剩压缩后的经历摘要。
> 所以要查精确的报错栈、完整时间线、当时的原始措辞，**必须在复盘前**查。
> 如果某天的原始记录很重要，复盘时改传 `purge=false` —— 日志会保留并标记为已复盘。
>
> 检索很便宜，别吝啬：`memory_search(..., kinds=["log"])` 只查日志层，
> `log_list(day="today")` 按天列原文。

## 事实被推翻时

**不要**改旧事实（改不了，数据库会拒绝），**不要**删。

```
fact_write(content="项目改用 UI Toolkit", invalidates="f_旧的id")
```

旧事实自动变成 `invalid_at` 非空，默认检索不再返回，但 `memory_search(include_invalid=true)` 能查到历史。

这条链是**单向的**，已失效的事实不能被"复活"——写错了就再追加一条新的。

## 工具速查

| 工具 | 用途 |
| --- | --- |
| `log_append` | 追加当天日志。白天的主力工具 |
| `fact_write` | 写长期事实，可带 `invalidates` 取代旧事实 |
| `fact_invalidate` | 让事实失效（过时但暂无替代） |
| `fact_promote` | 把项目事实提升为全局事实 |
| `fact_list` | 列事实 |
| `log_list` | 按日期列日志 |
| `memory_recall` | 按当前话题召回 + 强化命中项 |
| `memory_search` | 只查不改，含跨库全局事实 |
| `consolidate_preview` | 取当天待复盘日志 |
| `consolidate_apply` | 提交复盘结果 |
| `rules_get` / `rules_append` | 读/追加信条 |
| `memory_stats` | 统计与健康提示 |
| `memory_use_workspace` | 切换到另一个项目的工作区（一条配置服务多项目） |
| `memory_archive` | 归档低价值记忆（默认只预览） |
| `memory_unarchive` | 从归档恢复 |
| `memory_purge_candidates` | 列删除候选（只报告） |
| `memory_purge` | 物理删除，需 `confirm=true`，只能删已归档的 |
| `memory_compress_preview` | 列可压缩的经历 |
| `memory_compress_apply` | 合并多条经历（原条目归档） |
| `memory_recompress_preview` | 列出该进一步压缩的旧经历 |
| `memory_recompress_apply` | 改写经历并提升压缩级别 |
| `memory_maintenance` | 补 embedding |

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
~/.humanity-memory/global.db      # 跨项目事实
~/.humanity-memory/CORE_RULES.md  # 全局信条
```

**项目之间物理隔离**，换个项目就看不到上一项目的记忆。全局事实例外，永远参与检索。

所以：**跨项目要用的写 `scope="global"`，但别滥用**。规则类内容应该进 `CORE_RULES.md`，
全局事实超过 50 条 `memory_stats` 会警告——那通常说明该拆成规则或留在项目里。

## 复盘

日志是原始素材，需要定期压成事实和经历。**这不是你的模型之外的事**——
复盘由当前会话的模型执行，你不用调用外部 LLM：

1. `consolidate_preview(day="today")` 拿到当天日志和现有事实
2. 你自己阅读、提炼：哪些是长期事实、当天怎么压缩成一段叙述
3. `consolidate_apply(facts=[...], episodes=[...], purge=true)` 提交
4. 原始日志被清空，事实和经历留存

详见 `references/consolidate-guide.md`。

**配成定时任务**：每个工作区各建一个，只复盘自己那个库。任务 prompt 引用
`references/consolidate-task.md` 即可，别把一个任务写成跨项目扫描。

## 经历：起点由重要性决定，之后随时间变短

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
memory_consolidate_apply(episodes=[
  {"content": "被用户指出自作主张，应该先给方案再动手", "importance": 0.9},
  {"content": "修了个 typo", "importance": 0.2},
])
```

后续压缩：

```
memory_recompress_preview()     # 看哪些到了该压的年龄
memory_recompress_apply(items=[{"id":"e_x","content":"更短的版本"}], level=1)
```

检索权重也按级别走：level 0 的完整叙述参与检索但降权（0.88），压得越短权重越高（0.96）。

## 记忆会淡忘，但不会被悄悄删掉

三层都有生命周期，**信条例外（它根本不在库里，不参与任何淘汰）**：

| 层 | 何时归档 | 会压缩吗 |
| --- | --- | --- |
| 事实 | 3 年没被碰过、且访问 ≤ 1 次 | 不压缩（压缩会丢失精确性） |
| 经历 | 1.5 年没被碰过、且访问 ≤ 1 次 | ✅ 可压缩合并 |
| 日志 | 复盘后清空（按复盘，不按访问） | 已有 |

**归档 ≠ 删除**。归档只是让它在默认检索里消失，正文一个字都没动，`include_archived=true` 随时能查回来。

物理删除必须显式调用 `memory_purge(ids, confirm=true)`，而且**只能删已归档的**——没归档的会被拒绝，这是有意的保护。

清理节奏建议：

```
memory_archive(dry_run=true)        # 先看会动哪些
memory_archive(dry_run=false)       # 确认后执行
memory_purge_candidates()           # 定期看删除候选（只报告，不删）
memory_compress_preview()           # 看可合并的经历
```

要删的时候先问用户，说清楚「这些已经归档 N 天、从未再被访问」。**不要自己决定删**。

## 进一步阅读

- 三层的写入粒度与判断 → `references/writing-guide.md`
- 召回时机、query 写法、结果解读 → `references/recall-guide.md`
- 复盘怎么做、颗粒度怎么把握 → `references/consolidate-guide.md`
- 定时复盘任务的提示词模板 → `references/consolidate-task.md`
- 配置、排障、备份、换模型 → `references/operations.md`
