# Schema v3：升级说明

> 已经改完代码。**不做数据迁移** —— 旧库整份改名留档，新库从零开始。
> 原因见最后一节「为什么不就地迁移」。

## 一句话版本

**重启客户端即可。** MCP server 重启后，检测到上一代 `facts` 表的库会自动改名成
`workspace.db.v2-<时间戳>`，然后建一个新库。

## 手工核实

重启后看目录里有没有多出 `*.v2-*` 文件：

```bash
ls -la <项目>/.ai_memory/
ls -la ~/.humanity-memory/
```

应当看到：

```
workspace.db                      ← 新的 v3 库（knowledge / episodes / logs）
workspace.db.v2-20261105-162918   ← 旧库留档，不再被读
workspace.db-wal / -shm           ← 新库的 WAL
```

校验新库确实生效：

```bash
sqlite3 <项目>/.ai_memory/workspace.db "select value from meta where key='schema_version'"
# 期望输出 3
sqlite3 <项目>/.ai_memory/workspace.db ".tables"
# 期望看到 knowledge / episodes / logs / digests，没有 facts
```

## 为什么可能没自动改名

改名用的是文件级 `rename`，**当前进程正打开这个库时会失败**（Windows 上就是
`WinError 32`）。代码里对此的处理是：不报错、不阻塞，继续建新 schema ——
于是旧 `facts` 表会作为一个不再被读的孤儿留在同一个文件里。

两种情况都无害：

| 现象 | 说明 |
| --- | --- |
| 目录里多了 `*.v2-*` | 正常，旧库已留档 |
| 没有 `*.v2-*`，但库里有 `facts` 表 | 当时库被占用；停掉客户端后删掉那个 `.db` 再开即可 |

## 想把旧内容捞回来

旧库还在，只是不再被新代码读。两条路：

```bash
# 1) 直接从留档库里查（结构是 v2 的 facts 表）
sqlite3 <项目>/.ai_memory/workspace.db.v2-* "select content from facts limit 20"

# 2) 用已经导出的 Markdown（如果有）
#    .ai_memory/export/事实知识.md —— 内容在，但没有 id / 访问次数 / 向量
```

**不要指望 Markdown 导出能还原成库**：它不含 id、`access_count`、`verified_at` 与向量，
恢复后 id 全变、`episodes.digest_id` 与 `invalidated_by` 的引用链会断。

## 本次 schema 变更摘要

| 变化 | 说明 |
| --- | --- |
| `facts` → `knowledge` | 加 `kind`（knowledge / state）与 `verified_at`；`kind` 有 CHECK 约束且不可 UPDATE |
| `episodes` | 加 `kind`（case / narrative） |
| `digests.fact_count` → `knowledge_count` | 审计字段跟着改名 |
| 新触发器 | `knowledge_no_revive` / `knowledge_content_immutable` / `knowledge_kind_immutable` / `episodes_kind_immutable` |
| 向量表 | `facts_vec` → `knowledge_vec`，并显式 `chunk_size=64`（默认 1024 会让每张表预分配 4MB） |
| FTS | `facts_fts` → `knowledge_fts`（FTS5 改名安全，触发器会跟着改） |

## 为什么不就地迁移

v2 的 `facts` 里混着三类东西：可迁移的机制、会变的现状、以及带单号的一次性案例。
`kind` 的判定只能靠人读内容 —— 那是判断题，不是转换题。自动化只能靠「有没有单号」
这类表面特征去猜，而单号只是痕迹不是判据（没单号的修法依然是案例，
带编号但描述机制的句子依然是知识）。

猜错一半的迁移，比从零开始更糟：错分类的条目会以错误的 TTL 和权重长年留在库里。

所以 v3 的取舍是：**结构先对，内容重新长**。旧库整份留档，需要时能翻。

## 涉及代码

| 文件 | 改动 |
| --- | --- |
| `schema.sql` | 整份重写为 v3 |
| `models.py` | `Fact` → `Knowledge`（带 kind / verified_at）；`Episode` 加 kind |
| `store.py` | `write_memory(kind=)` 统一写入口；检索按 kind 两级过滤；`_type_weight` 给 case 固定 0.90；归档按行级 kind 取 TTL |
| `db.py` | `VEC_TABLES` 改名、`chunk_size`、`retire_legacy_db`、schema_version=3 |
| `lifecycle.py` | `archive_decision` 支持 `access_extends=False` + `verified_ms` |
| `config.py` | 半衰期 / TTL / 默认重要性按 kind 取值 |
| `mcp_server.py` | `fact_*` → `memory_write / memory_invalidate / memory_promote / memory_list`；`kinds` 支持 `all`；检索默认只回 lines |
| `cli.py` | `fact` 子命令 → `memory`，新增 `verify` |
| `tools/verify_mcp_config.py` | 端到端用例跟着改 |
| `tests/` | 85 项通过，新增 kind 语义、state 续命、case 权重、kind 不可改等用例 |
