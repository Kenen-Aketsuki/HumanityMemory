# Log 检索条件 · 设计稿（v5 已发布）

> 状态：**已发布**（schema v5，2026-11-20 全量 pytest 406 通过；已打 release v0.5.4 并切 current，10 库 + 本地库全部就地升至 v5，新版 exe doctor all_ok）· 日期：2026-11-20 · 作者：大肥鱼 🐋
>
> 一句话：log 参与日常 recall，但**不需要给它记一个"热度"字段**——它只要记下"被什么查询命中过"。
> 复盘时按这些条件重放检索，直接算出新条目的活跃度；热度只是检索条件的派生量，不再是独立存储。
> **全程尽量不靠模型判断**：来源映射、继承值、失配判定全由工具算，模型只负责"提炼内容"这一件事。

---

## 0. 一屏速览

| 项 | 内容 |
| --- | --- |
| 需求怎么演化的 | 最初："log 参与搜索，活跃度不能忽视 → 给 log 记活跃度"。后来欧尼酱提出重放思路。现在收敛为：**既然能重放，log 就只记检索条件，不记热度**。 |
| 一句话结论 | logs 加一列 `retrievals`（被哪些 query 命中过），**不加 activity 列**；复盘时从来源 log 的检索条件重放，直接算出新条目的 `activity` 初值。 |
| 为什么能砍掉 activity | ① 它不参与 log 自身的检索排序（`combine_score` 只看 retrieval/importance/level）；② log 无 `forget_level`，不进 judge/压缩；③ 它唯一用途是给新条目当种子，而这个值能从 `retrievals` **完整派生**——activity 本来就是 `(sim-0.35)²` 的累加。存了原料又存成品，冗余。 |
| 收益 | log 表少一个字段、recall 少一次数值累加、迁移少搬一列；新条目的活跃度从"搬运来的热度"变成"验证过的继承"，语义更正。 |
| **LLM 依赖** | **压到最低**：模型只做「提炼日志成经验/经历 + 给 confidence/importance 初值」——这是复盘本职、机器替代不了。来源映射、活跃度计算、重放判定、失配告警**全部工具完成**，不要求模型填 `log_ids`、不算分值、不做相似度判断。（沿用信条"分值、等级、遗忘、查重一律由工具裁定，不替它们判断"） |
| 明确不做 | ① log 不进遗忘等级体系（无 `forget_level`、不参与 judge/压缩/归档）；② 不改检索排序与融合公式；③ 不改 `search()`（仍刻意不强化）；④ 不读临时 JSONL（欧尼酱确认后续会删）。 |
| 版本策略 | 建议定为 **schema v5** 的第一项变更（`EXPECTED_SCHEMA_VERSION` 4→5 + 离线 `v4_to_v5` 迁移器）。 |

**收益示意**

| 场景 | 现在 | 改后 |
| --- | --- | --- |
| 某条 log 一天被 recall 命中 6 次 | 只攒了 access_count，热度信号在复盘时全丢 | 6 次的 query 都留在 `retrievals` 里，复盘时重放/派生，一次不丢 |
| 3 条 log 合并成 1 条经验 | 新经验 activity=0 | 从 3 条的检索条件算出继承值：能接住的 query 计分，接不住的自动失效 |
| 新经验撞上存量重复条目 | 存量 `+0.30` 固定值 | 存量 `+max(0.30, 继承值)`，热 log 提炼出的结论信号更强 |
| 复盘后想查"这条结论当初因为什么被记住" | 无从追溯 | `digests.meta` 记下 num/replay 分账，可审计 |

---

## 1. 现状与证据

### 1.1 log 已经是 recall 的正式候选

`store.py:120-132` 把 `LOG = TableSpec("log","logs","logs_vec","logs_fts","at",0.90,LogEntry)` 放进了 `ALL_SPECS`；
日志恒算"常规视野"（`store.py:1819-1825`），`_level_of` 给日志固定 1.0 倍率（`store.py:1855-1862`）。
**log 每天都在被真实检索到**，只是检索完就什么都没留下。

### 1.2 activity 对 log 自己是死字段

- 检索打分 `combine_score(retrieval, importance, level, w_*)`（`store.py:1835-1842`）——没有 activity。
- log 没有 `forget_level` / `period_start`，`memory_judge` 的 `_due_candidates` 只扫 knowledge / episodes。
- log 也不进压缩、不进归档（寿命由复盘 `purge` 管）。

→ **log.activity 目前没有任何消费者**，这是砍掉它的根本依据。

### 1.3 复盘新条目的活跃度恒为 0

`consolidate_apply`（`store.py:2058-2212`）调用 `write_experience` / `write_episode` 时都不写 activity，落 schema 默认 0
（`store.py:559-590` / `:626-648`）。原始素材再热，提炼出的结论也从零起步。

### 1.4 来源关联只有 digest 级

`logs.digest_id` / `episodes.digest_id` / `digests.meta.log_ids` 都只定位"哪一次复盘"，
没有"哪条 log 生成了哪条条目"的条目级关联（全项目无 `source_logs` 字段）。
→ 需要条目级来源映射，但**由工具自动推断**（见 §3.3），不是让模型填。

### 1.5 临时 JSONL 不能当依赖

`store._log_retrieval`（`store.py:1589-1646`）确实往 `retrieval/<day>.jsonl` 写 query 与命中明细，
但欧尼酱明确指出：**那是数据分析用的临时存储，后续会删**。所以检索条件必须落进库里的 log，不能靠它。

### 1.6 append-only 触发器不挡这次改动

`schema.sql:221-229` 的 `logs_append_only` 只锁 `content / at / day / id`；
新增列的更新（以及现有的 `access_count` / `last_accessed_at`）都不被拦截。

---

## 2. 目标 / 非目标

**目标**

1. log 记下"被哪些检索条件命中过"，而不是记一个热度数字。
2. 复盘生成新条目时，从来源 log 的检索条件算出活跃度初值——重放验证 + 历史派生，取大者。
3. 提炼式合并、向量 dedup、精确重复跳过三条路径都不丢继承值。
4. 活跃度能在审计里看到分账（num / replay），但不给 log 增加一个死字段。

**非目标**

- log 的等级 / judge / 压缩 / 归档 —— 一概不碰。
- 检索排序与融合公式；`search()` 仍然不强化。
- 读临时 JSONL；改 `memory_verify`（它历史上就只动 confidence，不借机改）。

---

## 3. 详细设计

### 3.1 数据层：只加 `retrievals` 一列，升 schema v5

```sql
-- schema.sql，logs 表
retrievals TEXT NOT NULL DEFAULT '[]',   -- 被 recall 命中的检索条件流水 [{q,n,sim,heat,at}]
```

| 文件 | 改动 |
| --- | --- |
| `schema.sql:122-137` | logs 增 `retrievals` 一列（**不增 activity**）；注释同步：允许更新 `consolidated / retrievals / access_count / last_accessed_at` |
| `db.py:19` | `EXPECTED_SCHEMA_VERSION = 5` |
| `models.py:LogEntry` | dataclass 加 `retrievals` 字段；`from_row` 读列；`to_dict` **不输出**（避免撑大 preview 上下文） |
| `upgrade/v4_to_v5.py` | 新增离线迁移器：源库只读 → 新库；旧 log 的 `retrievals='[]'`（历史 JSONL 不追溯，见 §5.2）；`meta.schema_version='5'` |

### 3.2 recall：记检索条件，而不是记热度

`_reinforce` 的 log 分支从"只加 access_count"改为"合并检索条件"：

```python
# recall(context) 把 query 传下来（store.py:1573 改为 self._reinforce(hits, context)）
def _reinforce(self, hits, query: str = "") -> None:
    ...
    for hit in group:
        if table == "logs":
            retrievals = merge_retrieval(hit.item.retrievals, query,
                                         sim=self._fuse(hit.vector_similarity, hit.lexical_score),
                                         now=now)     # 按 q 去重，n+=1，更新 sim/heat/at
            conn.execute(
                "UPDATE logs SET access_count = access_count + 1, last_accessed_at = ?,"
                " retrievals = ? WHERE rowid = ?", (now, retrievals, hit.item.rowid))
        else:
            conn.execute(
                f"UPDATE {table} SET access_count = access_count + 1, last_accessed_at = ?,"
                " activity = activity + ? WHERE rowid = ?", (now, delta, hit.item.rowid))
```

`merge_retrieval` 的单条结构：

| 字段 | 含义 |
| --- | --- |
| `q` | query 原文（strip + 折叠空白后归一化） |
| `n` | 命中次数（同 q 累加，不新增项） |
| `sim` | 最近一次的相关性 `_fuse(vec, lex)` |
| `heat` | 该 q 的累计活跃度贡献 `Σ activity_contribution(sim_i)` —— **历史热度的落点** |
| `at` | 最近一次命中时刻 |

容量：单条 log 最多 32 个不同 query，超出淘汰 `at` 最旧的（见 §7 决策 8）。

> **关键**：热度没有消失，只是从"log 的独立字段"降格为"检索条件流水里的 `heat` 聚合项"。
> 它随 `retrievals` 一起被记、被备份、被 purge，不再多占一个列。

### 3.3 复盘继承：来源自动映射（模型零参与）

**不让模型填 `log_ids`。** 来源关联由工具在 `consolidate_apply` 内部自动推断——这是"减少 LLM 依赖"的关键一刀：

```python
# 模型照常只交 content + confidence/importance，不碰来源
consolidate_apply(
    experience=[{"content": "…", "confidence": 0.8}],
    episodes=[{"content": "…", "importance": 0.7}],
)
# 工具内部：每条新条目 →
#   1. 用 write_* 已算好的 vec，在当天 pending 的 logs_vec 做 top-k（复用 _dedup_probe 的查询写法）
#   2. 相似度 ≥ source_match_floor 的 log 作为该条目的来源 L
#   3. 无命中 / embedder 不可用 → 回落当天全量 pending（digest 级关联，绝不要求模型补填）
#   4. 用 L 的 retrievals 算 I_num / I_replay（见 §3.4）
```

| 规则 | 设计 |
| --- | --- |
| 自动映射 | 新条目 vec vs 当天 pending logs_vec 的 top-k 匹配，阈值 `source_match_floor`（建议 0.35，与活动度门槛同量级）。这是纯机器计算，跟已有的 `_dedup_probe`（`store.py:462-495`）同一套机制。 |
| 兜底 | 向量不可用 / 一条都没匹配上 → 用当天全部 pending log 作为 L。**不抛错、不中断、不需要模型介入**。 |
| 审计 | `digests.meta.inherited` 记每个新条目的 `num / replay / total` 分账 + 自动匹配到的来源 log_ids，事后可核对映射对不对。 |
| 为什么不用模型填 | 模型判断"这条经验来自哪条 log"要读全部日志正文、容易漏填错填，还吃 token；向量匹配确定性强、零 token、和查重共用一套设施。信条也写着"分值、等级、遗忘、查重一律由工具裁定"。 |
| 逃生口 | 可选保留手动 `log_ids` 作覆盖（模型确有把握时），但**默认路径完全不需要**——加它只是给特殊情况留个后门，不是常规要求。 |

> 匹配时机：`consolidate_apply` 里日志要到最后才标 `consolidated` 并可能 purge，所以写条目时 `logs_vec` 还在，匹配可以直接做。

### 3.4 继承值的两条口径

设新条目 E 的来源 log 集合 L，两条口径都从 `retrievals` 出发：

**口径 N：历史派生（`I_num`）**——把 L 里所有 log 的 `retrievals[].heat` 累加后封顶：

```
I_num(E) = min( Σ heat(L), cfg.activity_inherit_cap )     # cap 默认 1.0
```

这就是旧方案里 log.activity 的值，只不过**现场从检索条件算出来，不落盘**。

**口径 R：重放验证（`I_replay`）**——按当时的查询条件，问"新条目接不接得住"：

1. 从 L 的 `retrievals` 收集 query（按 `q` 去重，跨 log 合并）→ Q。
2. 对 E 的正文做 embedding——写入时已算好 `vec`，直接复用。
3. 对每个 `q ∈ Q`：`vec_sim = cosine(q_vec, E.vec)`（q_vec 可缓存）；`lex = _lexical_coverage(q, E.content)`（`store.py:84`）；`sim = _fuse(vec_sim, lex)`（`store.py:1959`）；`r_q = activity_contribution(sim, ...)`（`lifecycle.py:28`）。
4. `I_replay(E) = max(r_q for q in Q)`，Q 为空则 0。

| 维度 | N 历史派生 | R 重放验证 |
| --- | --- | --- |
| 回答的问题 | "这些素材当时有多热" | "**新条目接不接得住这些查询**" |
| 多条 log 合并 | 累加后封顶，仍有点"按数量买等级"的味道 | 自动过滤：接不住的 query 得 0 |
| 措辞变化 | 不敏感 | **敏感（双刃剑，见 §3.5）** |
| 成本 | 零 | 每条新条目 × N 个 query 的相似度计算 |

### 3.5 融合规则

```
I_total(E) = max( I_num(E), I_replay(E) )       # 历史保底，重放增益
```

| 场景 | 结果 |
| --- | --- |
| 抽象提炼（重放分低） | `I_num` 保底，不丢分 |
| 措辞贴近来源（重放分高） | 重放涨分，反映"真的接得住那些查询" |
| log 没有 `retrievals`（老库/未被召回）/ embedder 不可用 | `I_replay=0`，退回纯 `I_num`；两者皆无则落默认 0，复盘照常成功 |

**三条合并路径都走同一个 `I_total`**：

| 路径 | 现状 | 改后 |
| --- | --- | --- |
| ① 提炼式合并（新建） | activity 落默认 0 | `write_experience` / `write_episode` 新增可选 `activity` 参数，INSERT 显式写 `I_total` |
| ② 向量 dedup 命中存量 | 存量 `+=0.30` 固定 | 存量 `+= max(dup_bonus, I_total)`；审计事件 `dedup` 的 delta / reason 同步实际值 |
| ③ 精确重复跳过 | 记录后 `continue`，零写入 | 若 `I_total>0`，给被跳过的存量 `+= I_total` 并记 `dedup` 事件 |

> ② 为什么取 max 而不是相加：`dup_bonus` 与继承值是同一份证据的两种表达，相加等于计两次，热 log 的结论会虚高。

**提炼损失**：经验是抽象结论（"引导卡住先看三处"），log 是具体事件（"Bug #10805 根因 StockBread…"）。
拿原始 query 去检索抽象后的经验，相似度会**系统性偏低**——最该继承的反而被判"不匹配"。
这不是 bug，是"用原问题考抽象答案"的固有偏差，所以 `I_num` 保底不能省。

**失配只告警、不派活**（守住 LLM 依赖底线）：重放失配（`I_replay≈0` 但 `I_num>0`）时在回执 warnings 里记一条：

> "经验 E 接不住来源 log 的历史查询（当时 sim=0.83 → 现在 0.21）"

对照值取 `retrievals[].sim`。**这条只是观测信号，不要求模型做任何返工判断**——继承值已经取 max 保底，复盘结果不依赖模型是否理会这条告警。是否展示见 §7 决策 6。

### 3.6 生命周期边界

| 问题 | 答案 |
| --- | --- |
| log 进 judge 吗？ | ❌ 不进。log 没有 `forget_level` / `period_start`。 |
| log 会被周期清零吗？ | ❌ 不会。`retrievals` 是"当天素材被检索的原始证据"，一路保留到复盘。 |
| log 会被压缩 / 归档吗？ | ❌ 不会。寿命仍由复盘 `purge` 管。 |
| log 有活跃度字段吗？ | ❌ **没有**。热度以 `heat` 形式蕴含在 `retrievals` 里，不占独立列。 |
| starmap 会变吗？ | ❌ 不变。日志继续画固定灰；检索页也不必为 log 加"按活跃度排序"（它没有列，派生排序意义不大）。 |

### 3.7 可见性

| 位置 | 改动 |
| --- | --- |
| `consolidate_preview` 的 `logs[]` | **不加字段**（`retrievals` 不输出，避免撑大上下文和诱导模型做来源判断）；模型专注提炼内容 |
| `digests.meta.inherited` | 记 `num/replay/total` 分账 + 自动匹配到的来源 log_ids，事后可审计 |
| `reports` / starmap | 不改（log 无 activity 列，不强行造派生排序） |
| 回执 warnings | 重放失配时记观测信号，**不写"建议调整措辞"这类要模型返工的话**（决策 6） |

---

## 4. 接口与模板变更清单

| 层 | 文件 | 改动 |
| --- | --- | --- |
| Schema | `src/humanity_memory/schema.sql` | logs 增 `retrievals` 一列 + 注释（**不增 activity**） |
| Schema | `src/humanity_memory/db.py` | `EXPECTED_SCHEMA_VERSION = 5` |
| Model | `src/humanity_memory/models.py` | `LogEntry.retrievals` + `from_row`（`to_dict` 不输出） |
| 检索强化 | `src/humanity_memory/store.py:1573,1971-2017` | `recall` 传 context 给 `_reinforce`；log 分支合并 `retrievals`（新函数 `merge_retrieval`），经验/经历分支照旧写 activity |
| 写入 | `src/humanity_memory/store.py:497+,596+` | `write_experience` / `write_episode` 新增 `activity=None` 参数，INSERT 显式写列 |
| 去重 | `src/humanity_memory/store.py:530-554` | dedup 分支 `max(dup_bonus, I_total)` + 审计取值 |
| 重放 | `src/humanity_memory/store.py`（新函数 `replay_queries`） | 从来源 log 的 `retrievals` 取 query → 复用 `_lexical_coverage` / `_fuse` / `activity_contribution` 算 `I_replay`；无数据降级 0；顺带产出失配观测信号 |
| 来源映射 | `src/humanity_memory/store.py`（新函数 `infer_source_logs`） | 新条目 vec vs 当天 pending logs_vec 做 top-k 自动匹配（复用 `_dedup_probe` 写法），无命中回落全量；**模型不填 log_ids** |
| 复盘 | `src/humanity_memory/store.py:2058-2212` | 入口参数不变（content/confidence/importance）；内部自动推断来源 → 算 `I_num` / `I_replay` → `I_total` 传给 write_*；跳过分支补给存量；digest.meta 分账 |
| 校验 | `_prepare_apply_items` | 不变——不再需要 `log_ids` 校验（模型不传了） |
| 迁移 | `src/humanity_memory/upgrade/v4_to_v5.py`（新） | 离线搬表，旧 log `retrievals='[]'` |
| 迁移注册 | `src/humanity_memory/upgrade/__init__.py` | `CHAIN[4] = v4_to_v5`，`LATEST_VERSION` 跟随 |
| 配置 | `memory.default.toml` | `[activity]` 段补 `inherit_cap = 1.0`（决策 4）、`source_match_floor = 0.35`（决策 2） |
| 提示词 | `~/.workbuddy/skills/humanity-memory/references/consolidate-guide.md` | **简化**：明确"只交 content + confidence/importance，不要判断来源、不要算分"；删掉原计划的 log_ids 填写要求 |
| 工具描述 | `src/humanity_memory/mcp_server.py:755-801` | `consolidate_apply` 描述强调"来源由工具自动推断，模型无需指定" |
| 文档 | `README.md` / `docs/two-tier-memory.md` | 活跃度章节补一句"log 记检索条件而非热度，复盘时派生" |

---

## 5. 迁移与兼容

### 5.1 为什么升 v5

项目纪律：**schema 闸门严格，版本不符直接拒绝服务；旧库走离线 `upgrade`，源库只读**（`db.py:15-26`）。
只改 `schema.sql` 不升版本，老库会缺列炸开；在 `open_db` 里自修补则破坏"不就地修补"边界。
`upgrade/__init__.py` 已预留链式升级路径（注释明确提到 v5），本变更正好落地。

### 5.2 存量数据

- v4 → v5 迁移时，所有现有 log 的 `retrievals` 写 `'[]'`。历史检索记录在临时 JSONL 里（后续会被删，不可依赖），库里没有可靠来源——**不追溯，从升级时刻起算**，迁移报告里写明。
- 升级后第一批 log 开始记录检索条件；当天复盘前若还没被召回，两条口径都是 0，与现状一致，不会凭空拔高。
- 已复盘留档（`consolidated=1`）的 log 也补 `'[]'`，不区分。

### 5.3 上线影响（需要欧尼酱确认）

- 本地库 `<项目根>/.ai_memory/workspace.db` 要跑一次离线升级。
- bindings registry 的 10 个库若是 v4，在新版程序下都会被闸门挡住，需逐库升级或补批量入口。**这步工作量比改代码本身大，建议单独排。**

### 5.4 回退

迁移器源库只读、`--out` 不覆盖；失败直接重跑，或改回 `EXPECTED_SCHEMA_VERSION = 4` + 恢复旧 schema / 库备份即可。改动集中在上述文件，回退面小。

---

## 6. 测试计划

现有测试受影响：

| 文件 | 关注点 |
| --- | --- |
| `tests/test_schema_v4.py` | 新库字段断言；新增 v4→v5 升级用例（v3→v4 段不动） |
| `tests/test_store.py::test_recall_reinforces` | 经验/经历照旧；log 分支改为断言 `retrievals` 落盘 |
| `tests/test_dedup.py` | 覆盖 `max(dup_bonus, I_total) > dup_bonus` 的分支 |
| `tests/test_starmap_activity*.py` / `test_deep_recall.py` | **应保持全绿**——本方案不碰等级体系 |

建议新增用例（最小集）：

1. `test_recall_records_query_into_log_retrievals`：recall 命中 log 后 `retrievals` 多了该 query；同 query 再命中 → `n` 变 2 而非新增项，`heat` 累加。
2. `test_retrievals_cap_evicts_oldest`：塞 33 个不同 query → 保留 32 个、最旧的淘汰（决策 8）。
3. `test_replay_rewards_matching_query`：log 的 `retrievals` 里有 Q，复盘出措辞贴近 Q 的经验 → `I_replay > 0`。
4. `test_replay_degrades_when_no_retrievals`：`retrievals='[]'` → `I_replay=0`，回落 `I_num`。
5. `test_inherit_takes_max_of_num_and_replay`：分别构造 `I_num > I_replay` 与反之，断言取大者。
6. `test_consolidate_merge_uses_sum_of_heat_capped`：3 条 log 的 heat 累加 → `I_num` 等于封顶后的值。
7. `test_consolidate_dedup_uses_max_of_bonus_and_inherited`：热 log 触发 dedup，存量增量等于继承值。
8. `test_consolidate_exact_duplicate_still_reinforces`：精确重复跳过时存量也拿到继承值。
9. `test_invalid_log_ids_rejected_atomically` → 替换为 **`test_source_auto_inferred_from_vec`**：模型不传 log_ids，工具用条目 vec 自动匹配到对应 log；向量不可用 → 回落全量且复盘成功。
10. `test_v4_to_v5_sets_retrievals_empty`：迁移往返 + 闸门通过。
11. `test_judge_ignores_logs`：log 有高 heat 也不进 judge 候选。
12. `test_replay_mismatch_is_observation_only`：重放≈0 但历史派生高 → warnings 有观测信号，**且不影响写入结果、不要求模型返工**。

跑法：现有 pytest 套件（`pytest -q`），迁移器用例按 `test_schema_v4.py` 的 fixture 风格造 v4 库。

---

## 7. 待评审决策（请欧尼酱拍板）

| # | 决策点 | 推荐 | 理由 / 替代 |
| --- | --- | --- | --- |
| 1 | log 要不要独立的 activity 列 | **不要，只存 `retrievals`** | activity 对 log 无消费者，且是检索条件的派生量；存原料不存成品。替代：保留 activity（冗余，多一列多一次累加多一段迁移）。 |
| 2 | 来源映射阈值 | **自动向量匹配，`source_match_floor=0.35`，无命中回落全量** | 模型零参与、零 token、确定性强；复用 `_dedup_probe`。替代：让模型填 log_ids（引入 LLM 判断与出错点，违背本次目标）。 |
| 3 | 继承融合 | **max(I_num, I_replay)** | 纯派生按数量买等级；纯重放被提炼损失低估。取大互补。两者都是机器算，不经过模型。 |
| 4 | 继承值上限 | **cap=1.0** | 异常高频累加会产生离谱种子值；写死常量亦可。 |
| 5 | 多条 log 的 heat 聚合 | **Σ heat 后封顶** | 保留"当时有多热"的信号；cap 防高频召回买等级。 |
| 6 | 重放失配信号 | **只记观测，不给模型派活** | 继承已取 max 保底，结果不依赖模型是否理会。杜绝"提示模型返工"这类隐性 LLM 依赖。 |
| 7 | 版本 | **v5 + 离线迁移** | 符合闸门纪律；不搞启动自修补。 |
| 8 | `retrievals` 容量上限 | **32 条/log，淘汰最旧** | 防 JSON 膨胀；重放只需代表 query。 |
| 9 | 已复盘留档 log 是否继续记录条件 | **继续** | 保持 recall 语义一致，实现最简单；替代 `WHERE consolidated=0`（更干净但要改分支）。 |

---

## 8. 落地顺序（评审通过后）

1. schema + 版本闸门 + 迁移器 + LogEntry 字段（库能带 `retrievals` 跑起来）。
2. `_reinforce` 传 query + log 分支合并 `retrievals` + recall 测试。
3. `write_experience` / `write_episode` 的 `activity` 参数 + dedup 分支改造。
4. `infer_source_logs` 自动来源映射 + `replay_queries` + `I_num` 派生 + 取 max + 失配观测信号 + 复盘测试（**全程模型零参与**）。
5. `consolidate_apply` 跳过分支补给存量 + digest 分账。
6. 简化 preview 提示词 / 工具描述：明确"模型只交内容与初值，来源和分值交给工具"。
7. 全量 pytest → 本地库离线升级 → 批量升级 registry 里的其他 v4 库。

---

## 9. 一句话结论

**log 只记被什么问过，不记有多热；热度从检索条件里现算，复盘重放后继承给新条目。**
log 全程不进等级体系，活动信号只在提炼那一刻流进经验/经历——
既砍掉了 log 身上的死字段，又不让"当天日志多"或"抽象提炼后措辞不像"污染继承值。

**模型只做它不可替代的那件事**：把日志提炼成经验/经历、给初值。
来源映射（向量自动匹配）、活跃度计算（派生 + 重放）、失配判定（纯观测）全部由工具完成——
复盘的输出不再依赖模型是否填对了 `log_ids`、是否理会了告警。这套机制，机器说了算。
