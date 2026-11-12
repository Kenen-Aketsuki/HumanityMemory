# 定时复盘任务提示词模板

每个工作区配一条自动化，prompt 直接抄下面这段。**一条任务只复盘一个库**——
跨项目扫描会把别的项目的记忆写进当前库，事后很难理清。

## 模板

```
对当前工作区执行一次记忆复盘，按顺序做，不要跳步：

1. memory_stats —— 确认 working_dir 是你预期的项目。不对就停下来说明，不要继续。
2. consolidate_preview(day="today") —— 不传 day 也行，它会自动补最近一个还有积压的日期。
3. 读 logs 和 existing_knowledge，产出三块：
   - knowledge：换掉单号 / 任务 ID / 资源名仍然成立的机制与约定，能指出代码落点。
     一句话一个结论，能带理由就带。指着不出落点的先别写。
   - state：当前为真但会变的东西（版本、依赖、路径、资源配置）。≤200 字，必须给 source。
     若与 existing_knowledge 里的某条现状冲突，用 invalidates 传旧 id 顶掉它。
   - episodes：带单号的一次性结论写成 kind="case"（现象→根因→落点，≤300 字，一问一条）；
     当天的过程、试错与判断转折写成 kind="narrative"。
4. consolidate_apply(knowledge=[...], episodes=[...], purge=true) 提交。
5. 回报：写了几条 knowledge / state / case / narrative，清掉了多少条日志。

纪律：
- 宁可少写。一天 0~3 条知识是正常的，10 条说明标准太松。
- 带单号的排查结论只能进 case，不许写成 knowledge。
- 日志原文不是压缩，不要把 logs 复制进 narrative。
- 没有素材就提交空数组，不要硬造。
```

## 几个要点

**为什么先 `memory_stats`**：一条 MCP 配置服务多个项目，工作区由 cwd / 上次选择决定。
复盘任务多半在无人值守时跑，工作区错了等于污染另一个库。

**为什么强调 `purge=true`**：日志不清空，下次 preview 还会捞到同一批，
同一段内容会被反复提炼。要留原始凭证（完整报错栈、精确时间线）才改 `purge=false`，
它只标记 `consolidated=1`，不会再进 preview。

**为什么现状要复核而不是 `invalidates`**：现状不靠检索续命，也**不允许**用
`invalidates` 换代（会攒出冗长的失效链，而且中间每一条都曾冒充过真话）。
复盘时若要改现状：先 `memory_review(item_id, matched=false)` 删掉旧的那条，
再独立写一条新的。顺序反了会同时留下两条互相矛盾的现状。

**跑完看什么**：`memory_stats` 里的 `state` 条数应该稳定在一个很小的数字（个位数）。
持续增长说明有人在把会变的东西写成状态却不清理，或者把知识错标成了 state。

## 见

- 复盘的整体流程与颗粒度 → `consolidate-guide.md`
- 各类的写入判据 → `writing-guide.md`
