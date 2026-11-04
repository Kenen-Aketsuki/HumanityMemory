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
| **事实** | `facts` 表 | 简短精确的陈述句 | 长期有效，遗忘极慢；被推翻时**追加新事实并让旧的失效** |
| **日志** | `logs` 表 | 当天发生的原始记录 | **只追加、不可修改**（数据库触发器强制），复盘后清空 |
| **经历** | `episodes` 表 | 复盘压缩出的叙述 | 由日志压缩而来，保留因果与结论 |

信条不需要你操心——它已经在你的系统提示里了。你主要操作另外三层。

## 写事实还是写日志

这是**最容易错的地方**，写错了整条记忆链就乱了。

| 判断 | 写 fact | 写 log |
| --- | --- | --- |
| 时间性 | 长期成立 | 今天发生的 |
| 形态 | 一句陈述句 | 可以有过程、有时间线 |
| 例子 | "这个项目 UI 用 UGUI" | "今天排查了布局错乱，根因是 Canvas 缩放模式" |
| 长度 | 不超过 500 字，越短越好 | 可以长 |

**默认写日志。** 白天对话里绝大多数内容都应该进日志，复盘时再压缩成事实。
过早写事实会导致：记了一堆当天细节当"永久结论"，三个月后全是噪音。

只有明确满足「三个月后还有用 + 一句话能说清 + 不依赖当天上下文」才直接写事实。

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

## 进一步阅读

- 三层的写入粒度与判断 → `references/writing-guide.md`
- 召回时机、query 写法、结果解读 → `references/recall-guide.md`
- 复盘怎么做、颗粒度怎么把握 → `references/consolidate-guide.md`
- 配置、排障、备份、换模型 → `references/operations.md`
