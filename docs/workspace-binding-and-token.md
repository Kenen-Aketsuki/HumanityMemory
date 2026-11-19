# 工作区绑定与准入令牌（Workspace Binding & Token）

> 状态：**设计定稿 v2（2026-11-18）**，A1–A7 已确认；前置失效复核见 §12，可执行清单见 §13
> 取代：v1（2026-11-12）。v1 的四态、风险框架、否决理由继续有效，**准入语义与令牌落点已整体改写**
> 影响范围：`paths.py` / `token.py` / `config.py` / `mcp_server.py` / `cli.py`，新增 `keys.py`
> 不涉及：`schema.sql`、任何数据迁移。存量库只做一次**令牌补发**，不动内容

---

## 0. 本次修订（v1 → v2）

v1 是在"启动参数为空、靠 cwd 推导、有全局库兜底"的年代写的。这几条前提在 2026-11-17/18 全部变了，
因此 v2 不是打补丁，而是**换掉准入层的地基**。

| 编号 | v1 的做法 | v2 的定案（2026-11-18 确认） | 改写了哪节 |
| --- | --- | --- | --- |
| **A1** | 未决定 / 已拒绝 → 回落「全局库模式」 | **不允许访问任何库**。直接拒绝，返回拒绝原因 + 可执行的修复清单 | §5.2 / §5.6 / §6 |
| **A2** | 令牌放 `~/.humanity-memory/keys/<md5(workspace)>.json`，靠 md5 反查 | 令牌放 **`<workspace_root>/.ai_memory/token.json`**，靠 cwd 自动发现；去掉全部 md5 文件名机制 | §5.3 / §5.6 / §10.3（整节作废） |
| **A3** | 保留 `last-workspace.txt` 作「启动上下文兜底」 | **放弃 `last-workspace.txt`**。拿不到合法的 workspace cwd 就拒绝，并给出 check list | §5.6 / §9 |
| **A4** | 项目级 `memory.toml` 挂在 `.ai_memory/`（写在代码里、当成项目配置） | **改为「库级」**：一层配置跟着库走，A、B 共库则共用同一份，且是最高优先级层 | §5.7 |
| **A5** | （未讨论）其他库怎么升级 | 其他库**改为接入打包好的 exe**，不再依赖源码；发布时一次性迁移 + 自动发令牌 | §10.2 |
| **A6** | 令牌放项目内会泄露 | **定性为低危，不按"凭据泄露"设计**：令牌无凭据、脱离本机 registry 即失效。真正的泄露载体是库内容，防的是"卫生"不是"安全" | §9 R-7 / §9 末节 |

v1 里**继续有效**的部分：令牌不携带库路径（§5.3）、路径前缀匹配而非哈希做身份（§4.1）、
分两层（准入 / 绑定）各自独立上线（§5.1）、风险清单主体（§9）、P1/P2/P3 分期（§10.1）。

---

## 1. 背景

当前一个工作区对应一个记忆库，库的位置由工作区目录**隐式**决定：
`<workspace_root>/.ai_memory/workspace.db`。代码里没有「工作区身份」这个概念，
`.ai_memory` 所在的位置就是身份（`paths.py:87` `find_workspace_dir` 自下而上找目录）。

两个问题：

1. **不能部分共享**。同一个游戏的前端工程、后端工程、Unity 工程属于同一条工作线，
   知识应当互通；但把所有项目并进一个库，无关记忆又互相污染。
2. **没有准入概念**。任何目录只要长得像项目，server 启动时会**自动**建库并开始写入
   （`mcp_server.py:932` `find_project_root()` → `mkdir` → `source: "cwd-project"`）。
   没有「这个工作区要不要接入记忆」的决策点。

v2 追加第三条，来自 2026-11-17 的实测事故：

3. **错绑比不绑定更坏**。一条全局「当前活跃令牌」+ `last-workspace.txt` 兜底，
   会让一个 cwd 在 `HumanityMemory` 的会话，实际读写 `alpha` 的库，
   系统提示注入的也是别人的信条（见 §3.4）。

---

## 2. 需求

| 编号 | 需求 | 来源 |
| --- | --- | --- |
| R1 | 工作区 A、B 共用同一个记忆库 | 用户原始诉求 |
| R2 | 工作区 C、D 各自使用自己的库，彼此隔离，也不与 A、B 共享 | 用户补充 |
| R3 | 绑定关系可配置，允许未来调整（A→B、C→A 等） | 推导 |
| R4 | 新接入工作区必须询问用户：新建库，还是绑定到已有库 | 用户补充 |
| R5 | 保险起见**默认不处理**：未获确认前不建库、不写库、不发令牌 | 用户补充 |
| R6 | 每个工作区分配一个令牌，作为「是否绑定过」的证明 | 用户补充 |
| R7 | 有令牌时以令牌为库的访问钥匙；服务端结合自身配置判定**是否有权限、访问哪个库** | 用户补充 |
| **R8** | **未接入（含已拒绝）时，任何库都不可访问；拒绝必须说明原因并给出可执行的修复清单**（A1） | 本次确认 |

R1 + R2 合起来说明：不是「全项目一个大池」，而是**部分合并**。
R8 决定了实现形态：拒绝不是「退回某个默认库」，而是**能力关闭**（§6）。

---

## 3. 现状核实（2026-11-18 实测）

### 3.1 身份即目录，锚点实际是 **4 处**（v1 漏了一处）

`Layout`（`paths.py:150`）只有 `workspace_dir`。把它当「项目锚点」的地方：

| 位置 | 用途 | 绑定后会怎样 |
| --- | --- | --- |
| `paths.py:200` | 探测 `<项目>/.workbuddy/memory/信条.md` | 库目录一旦外移，信条会指错位置 |
| `mcp_server.py:97` | 工作区标签 = `workspace_dir.parent.name` | 显示成库名而不是项目名 |
| `mcp_server.py:621` | `write_last_workspace(workspace_dir.parent)` | 记录的是库的父目录（A3 后整段删除） |
| **`config.py:148`** | **项目级配置 = `workspace_dir / memory.toml`** | **A/B 共用库会连配置一起共用** |

第四条 v1 没列，但它是**唯一一条会被 A4 转成优点的**：既然配置本来就挂在库目录上，
那就顺水推舟把它定义成「库级配置」（§5.7），不再假装是项目级。

### 3.2 启动参数不是空的，而且只有一条全局配置

`~/.workbuddy/mcp.json` 里 humanity-memory **只有一条**，写死了一个全局活跃令牌：

```json
{ "args": ["--token", "C:/path/to/active.token.json", "serve"] }
```

`startup.log` 全量 1024 条：`argv: []` 854 条、带 `--token` 155 条；
**最近 30 条里 28 条带 `--token`**，且 `active.token.json` 的 `workspace` 指向
`alpha`。

v1 §3.2 的推论（"令牌必须按约定路径自动发现，否则永远不生效"）方向恰好反了：
**现在令牌是主路径，cwd 是被它覆盖的**。这带来两个直接后果：

- v1 §5.4 的校验 1（`token.workspace == 本次 root`）在这套配置下**必然失败** ——
  boot 实例的 cwd 是 `~/.humanity-memory`，令牌说的是 `alpha`。
  按 v1 实施 = 9 个工作区一起拒绝启动。
- 全局活跃令牌**无法表达**"这个工作区绑定没绑定"（R6）。一个文件只能代表一个工作区。

所以 A2 把令牌搬进项目、A3 删掉兜底，这两条是同一个动作的两半：**身份回到 cwd，令牌跟着项目走**。

### 3.3 全局库已关闭（v1 的兜底方案不存在了）

`docs/two-tier-memory.md` §8.2，2026-11-17 执行：`global.db` → `global.db.retired-20261117`；
`scope="global"` 抛 `ScopeError`；`memory_promote` 只留壳恒报下线；
`store.py:294` 明写「**不打开** `layout.global_db`」；`paths.py:156-159` 加注释禁止任何代码打开它。

因此 v1 的四态里「未决定」「已拒绝」两态的落地方式（"全局库模式"）、`access: global-only`、
以及 §4.4「方案四：`global.db` + `memory_promote`」全部**失效**。
A1 正好补上这个空洞：没有全局库，未接入就是**关掉能力**。

### 3.4 `last-workspace.txt` 是错绑来源，不是兜底

2026-11-17 复盘（HumanityMemory 库日志 `l_f3f99c914b61` + 经验 `x_456980915226`）：

- DSH 重启时，应用启动那一刻的 spawn 用的是配置里的静态 cwd（`~/.humanity-memory`），
  会话内的 spawn 才用会话工作区；
- `serverName` 在作用域内唯一，**boot 实例先注册**，会话实例不再 spawn，整个会话跟着它跑；
- 于是 `argv: []` → `source: "last"` → 落到 `last-workspace.txt` → 打开 `alpha`，
  系统提示注入的也是它的信条。

同一个复盘还记下一条通用事实：**MCP 协议不向 server 提供当前工作区**（经验 `k_3a74bf7d8f20`），
能拿到的只有进程 cwd 和启动参数。A3 的"拒绝"必须与这条事实配套设计，
否则会连救援通道一起关掉（§9 R-11）。

### 3.5 存量：9 个库，全部 `dim=1024 / bge-m3`

schema 已全部是 **v4**，表只有 `knowledge` + `episodes` 两类，
v1 的 `state` / `case` / `narrative` 列读法已不存在：

| 库 | 知识 | 经历 | 日志 | 复盘 |
| --- | ---: | ---: | ---: | ---: |
| HumanityMemory | 106 | 59 | 1 | 12 |
| project_island | 60 | 84 | 0 | 7 |
| CivelRev | 41 | 26 | 0 | 2 |
| majotavern | 33 | 26 | 0 | 6 |
| alpha | 22 | 17 | 2 | 3 |
| beta | 14 | 16 | 2 | 3 |
| beverage_town | 6 | 1 | 0 | 1 |
| MiscAI | 0 | 0 | 0 | 0 |
| beto-client | 0 | 0 | 0 | 0 |
| **合计** | **282** | **229** | **5** | **34** |

9 个库 `meta.embedding_dim` 全为 1024、`embedding_model` 全为 `BAAI/bge-m3`
→ **共享组的维度冲突（R-3）当前不是风险**，合并技术上无阻碍，剩下的只有语义问题。

两个库是 0 条（`MiscAI` / `beto-client`），由当年的自动建库顺手造出来。A1 + 删除自动建库后，
它们会稳定停在「未接入」，不会再有新库被静默创建。

> **2026-11-18 迁移时复核**：`alpha` / `beta` 是文档早期的**示例条目**，磁盘上已无对应目录。
> 实际库以 `bind migrate --scan <工作区根>` 的结果为准，当时是 10 个：上表 7 个真库
> + `Aketsuki_Unity_MCP`（26/22/0/4）+ `project_island_back`（39/19/21/3，迁移前 `active.token.json`
> 钉住的就是它）+ `HumanityMemory_dev`（空库，`label = hm_dev`，F1 的 dev 隔离库）。

---

## 4. 被否决的方案

### 4.1 用 `MD5(cwd)` 决定打开哪个库 —— 否决理由不变

哈希是 1:1 纯函数，产生不了 R1 要求的多对一；路径规范化（`d:\` / `D:\` / junction / 8.3 短名）
会让同一项目算出两个 hash、**静默分裂**；项目改名后旧库变孤儿，无反查表即失联。

> **边界**：否决的是「用哈希做身份」。v1 还留了个尾巴——用 md5 给 `keys/` 目录里的令牌命名。
> **A2 之后这个尾巴也没了**：令牌按固定路径放在项目内，不需要任何文件名编码机制，
> v1 §10.3 整节作废（见 §10.3）。

### 4.2 所有项目合并为一个库 —— 否决理由不变

违反 R2，且需要在 `store.py` 给所有查询加来源谓词，漏一处即跨项目串数据。

### 4.3 Windows 目录联接（junction）—— 仍只作应急手段

`.ai_memory` 已在多数工程被忽略，`find_workspace_dir` 用 `is_dir()` 认目录、返回未 resolve 的路径，
所以 `ws_dir.parent` 仍是项目根，信条探测不会错。但 Windows 专属、对 git/复制/压缩不可见、
承载不了 R4–R8 的准入语义，两处失败（误删、跨盘复制）都是静默的。

### 4.4 `global.db` + `memory_promote` 做跨项目共享 —— **已失效**

2026-11-17 全局库关闭，`memory_promote` 下线（§3.3）。此路已断，不再是"零改动选项"。

### 4.5 继续用一条全局「当前活跃令牌」服务所有项目 —— 新增，否决

这是**当前实况**（§3.2），也是最省事的路，但：

- 一个文件表达不了 per-workspace 的四态，R6 无从落地；
- 令牌的 `workspace` 与真实 cwd 长期不一致，等于把 R8 的"防误接入"反向做成"必然误接入"；
- 已造成实际事故（§3.4）；
- 它的**唯一优点**（一条 mcp.json、切项目不改配置）已被 A2 完全继承：
  令牌在项目里，cwd 一换就换库，配置条数仍是一条、而且可以不写任何参数。

### 4.6 令牌放 `~/.humanity-memory/keys/<md5>.json`（v1 的 Q1）—— 降级为备选

v1 选它是为了「复制项目目录不带走钥匙」。代价是：路径规范化敏感、项目改名靠扫目录救回（v1 R-9）、
以及 §10.3 那套文件名编码的讨论。A2 用「令牌内 `workspace` 强校验 + 服务端可撤销」换掉了这个收益，
把复杂度从"路径寻址"降到"文件就在那儿"。

---

## 5. 设计

### 5.1 三层，各自独立

```
准入层    <root>/.ai_memory/token.json  +  服务端 registry.json
              │  决定「这个工作区有没有资格用记忆、用哪个库」
              ▼
绑定层    registry.keys[hmk_…].library → libraries/island | <root>/.ai_memory
              │  决定「库文件放在哪」
              ▼
配置层    <库目录>/memory.toml  （A4：跟着库走，最高优先级）
```

三层可独立上线：先准入（此时所有库都是"自有库"），再加绑定，共享才出现。

### 5.2 令牌四态（重定义）

A1 定案：**没有"全局库模式"这个中间态**。两个非绑定态的行为都是"能力关闭"，区别只在"要不要再问"。

| 状态 | 磁盘表现 | server 行为 | 是否询问用户 |
| --- | --- | --- | --- |
| **未决定** | `<root>/.ai_memory/token.json` 不存在 | **拒绝所有记忆读写**；只放行 §6.4 白名单；`memory_bind_status` 报告需要决策 | **要**，agent 必须问（R4） |
| **已拒绝** | token 存在，`library: null` | **同样拒绝所有记忆读写**，错误码 `declined` | **不要**，永远不再问 |
| **已绑定（自有）** | token 存在，`library` = 该工作区自己 | 正常进入 `<root>/.ai_memory` | — |
| **已绑定（共享）** | token 存在，`library` = 组库 | 正常进入组库，A、B 指向同一文件 | — |

「已拒绝」和「未决定」对**能力**的影响完全一致，差别是**记性**：一个你明确不想接入的工作区，
只会被问一次。v1 的这个判断在 A1 下依然成立，而且更必要——现在"拒绝"是真的不能用。

### 5.3 令牌内容与落点

**落点**：`<workspace_root>/.ai_memory/token.json`（A2）。

为什么是这个路径，而不是新造一个目录：

- `find_workspace_dir`（`paths.py:87`）本来就在向上找 `.ai_memory`，**身份解析逻辑一行不用改**；
- `.ai_memory` 的 git 状态实测（2026-11-18 复核）：**3 个已 gitignore**（`Aketsuki_Unity_MCP` /
  `HumanityMemory` / `beverage_town`）、**4 个由 pre-commit 钩子拦截**（`majotavern` /
  `project_island` / `project_island_back` / `beto-client`，钩子含 `.ai_memory/*`，均实测 exit 1）、
  **1 个两者皆无**（`MiscAI`，内容仅 1 条 log）、`CivelRev` 不是 git 仓库。
  迁移时只需给 `MiscAI` 补钩子或 `.git/info/exclude`（§10.2），不必改各工程的 `.gitignore`；
- 库可以外置：绑定到组库时，`.ai_memory/` 只剩 `token.json`（+ 可选 `CORE_RULES.md`），
  db 在 `libraries/island/`。**锚点与库分离，但锚点仍是项目里的实在目录**。

**内容**（v2：只有身份，不含任何凭据；`library` 是标签而不是路径）：

```json
{
  "version": 2,
  "key": "hmk_7f3a9c1e...",
  "workspace": "D:/work/alpha",
  "library": "alpha",
  "label": "alpha"
}
```

- `library: null` = **已拒绝**（四态里的「已拒绝」，§5.2）。拒绝也是要记住的决定，
  否则三个月后没人知道这里为什么记忆全空（§6.3）。
- `library` 是**断言，不是寻址**：真归属永远在服务端 registry，令牌里的值只用于
  §5.5 第 6 步的比对。所以把令牌改成 `"library": "别人的库"` 拿不到别人的库（R7），
  路径也仍然只由 registry 给 —— 改令牌最多把自己弄坏。
- 字段缺失 ≠ 已拒绝：缺 `library` 直接判 `token_bad`，不猜成 null，
  否则一次手抖就等于替用户做了「以后别再问」的决定。
- `embedding` 字段彻底拆除（v1 Q4 已定；`active.token.json` 实测 `embedding: {}`，迁移零阻力）。
- v1 令牌（`version: 1`，含 `embedding`）→ 拒绝访问 + 错误码 `token_v1` + 迁移指引，
  **不做静默降级**（A1 的立场：拿不准就不放行）。

### 5.4 服务端注册表（唯一真相）

`~/.humanity-memory/registry.json`：

```json
{
  "version": 2,
  "salt": "…",
  "libraries": {
    "island": { "path": "<安装根>/libraries/island", "created": "2026-11-19" },
    "alpha": { "path": "D:/work/alpha/.ai_memory", "created": "2026-11-18" }
  },
  "keys": {
    "b7f2747ae1378b99454e435ab4980888c1ab0e8c2f4abc3ddc7467944b861a3b": {
      "workspace": "D:/work/alpha",
      "library": "alpha",
      "access": "rw",
      "created": "2026-11-18",
      "revoked": null
    }
  }
}
```

- `keys` **的键就是 `sha256(salt+key)`**，条目里不再重复存一份 `hash`（单一来源）；
  令牌文件才是钥匙本身。查表即比对，不存在"取出明文再字符串比较"这一步。
- `keys[hash].library` 是**归属真相**；令牌里的同名 `library` 只是断言，两者必须严格
  相等（含 `null`），否则 `key_library_mismatch`（§5.5 第 6 步）。

- `libraries.path` 是**服务端给的**，令牌里没有路径 → 改令牌指不到别人的库。
- **自有库也登记**（`library = <label>`，`path = <root>/.ai_memory`），这样"A、B 共库"和
  "C 自用"在 registry 里是同一种东西，只是 `path` 不同。

### 5.5 校验流程

启动时**依次**检查，任一失败即拒绝（A1），失败点决定错误码（§6.2）：

| # | 检查 | 失败错误码 |
| --- | --- | --- |
| 1 | 能确定 `workspace_root`（§5.6 解析链路） | `no_workspace` |
| 2 | `<root>/.ai_memory/token.json` 存在且可解析 | `no_token` / `token_bad` |
| 3 | `version == 2` | `token_v1` |
| 4 | `normcase(realpath(token.workspace)) == normcase(realpath(root))` | `token_workspace_mismatch` |
| 5 | registry 有该 `key`、`hash` 对得上、`revoked` 为空 | `key_unknown` / `key_revoked` |
| 6 | registry 条目的 `workspace` / `library` 与 token 一致（防手改令牌） | `key_workspace_mismatch` / `key_library_mismatch` |
| 7 | `library` 非 null，且目标目录存在、可写（`ro` 除外） | `declined` / `library_missing` |

`access` 取值（去掉 v1 的 `global-only`，全局库已不存在）：

| 值 | 含义 | 落地方式 |
| --- | --- | --- |
| `rw` | 读写 | 现状 |
| `ro` | 只读 | 工具层拦截所有写操作，返回明确错误 |
| — | 不接入 | 由令牌 `library: null` 表达（四态里的「已拒绝」），不占用 `access` 取值 |

### 5.6 解析链路（v1 §5.5 整体重写）

**阶段一：确定 workspace root**（只找不建，任何一步都**不再创建目录**）

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--workspace` / `HM_WORKSPACE` | 显式指定根；仍走阶段二校验，不跳过令牌 |
| 2 | cwd 向上找**含 `token.json` 的 `.ai_memory`** | A2 的自动发现；命中即 root = 该 `.ai_memory` 的父目录 |
| 3 | cwd 向上找项目标记（`find_project_root`） | **仅用于报错信息**（"你在 X 项目里，但没接入"），不再建库 |
| 4 | 空 | 拒绝，`no_workspace` |

三条被删掉的东西，每条都有原因：

- **`--token` 不再是身份来源**：它最多是"令牌路径覆盖"，且必须与阶段一算出的 root 一致，
  否则 `token_workspace_mismatch`。目的就是不让一个陈旧参数劫持整个会话（§3.2）。
- **`find_project_root` → `mkdir` 分支删除**（v1 Q5 的 (b)）：拿不准就不建。
- **`last-workspace.txt` 删除**（A3）：`read_last_workspace` / `write_last_workspace`
  （`paths.py:54-80`）、`mcp_server.py:916` 兜底、`mcp_server.py:621` 回写，全部移除。
  `memory_use_workspace(persist=True)` 一并失去意义 → `persist` 参数保留但**恒为进程内生效、不落盘**。

**阶段二：由 root 决定库**

```
读 <root>/.ai_memory/token.json
├─ 有 → 校验 §5.5 第 3-7 步
│        ├─ 全通过 → library 的 path → 打开
│        └─ 任一项失败 → 拒绝 + 错误码 + check list（不静默回落！）
└─ 无 → 拒绝（no_token）+ check list
         └─ 若 <root>/.ai_memory/workspace.db 存在
              → 清单第一项置顶为「沿用本目录已有的库」（bind adopt）
```

v1 在这里有「未命中 → 扫 `keys/` 比内容救回」的兜底，**A2 之后不需要了**：
令牌就在项目里，项目搬家令牌跟着搬，唯一会失配的是令牌内的绝对路径 →
用 `bind move --from <旧> --to <新>` 修，或 `bind adopt` 重发。**不要自动改写令牌**（同 R-10 立场）。

### 5.7 库级配置（A4）

现状是 `config_layers(home, workspace)` 返回四层，第四层叫「项目」、路径是 `workspace / memory.toml`
（`config.py:134-151`）。A4 把它的**语义**改掉，代码几乎不动：

| 层 | 路径 | 改后语义 |
| --- | --- | --- |
| 出厂 | 包内 `memory.default.toml` | 不变 |
| 安装 | `<发布根>/memory.toml`（源码态 = `<仓库根>/memory.toml`） | 不变 |
| 用户 | `~/.humanity-memory/memory.toml` | 不变 |
| ~~项目~~ → **库** | `<库目录>/memory.toml` | **最高优先级；跟着库走** |

由此：

- **自有库**：库目录就是 `.ai_memory`，所以路径仍是 `<root>/.ai_memory/memory.toml` —— 今天的写法继续有效；
- **共享库**：配置在 `<安装根>/libraries/island/memory.toml`，A、B 读到**同一份**。
  这正是 A4 想要的："这个库的最高优先级配置就是它自己的库级配置"；
- 代价（必须写进 README）：**在共享组里没法给某个项目单独调阈值**。
  需要独立参数就说明它该是独立库。这是刻意的，避免"配置层比库层更细"造成隐性分裂；
- 实现细节：`Config.load(library_dir=...)` 的入参语义是"**库目录**"
  （共享时传 `<安装根>/libraries/island`），参数名已从 `workspace` 改掉（T5）；
  拒绝态传 `NO_LIBRARY` 哨兵（只装配前三层），`None` 仍表示"自动猜"；
  `peek_workspace_dir` 仍用于"还没绑定时猜配置"。

### 5.8 磁盘布局（2026-11-19 起：库全部统一存放）

```
① 自有库（A 独占一份）                        ② 共享（A、B → island）
D:/work/A/                                   D:/work/A/
└─ .ai_memory/                                 └─ .ai_memory/
   └─ token.json  {"library":"A"}                 └─ token.json  {"library":"island"}
   └─ .workbuddy/memory/信条.md                   └─ .workbuddy/memory/信条.md
                                              D:/work/B/
<安装根>/libraries/A/                            └─ .ai_memory/
   ├─ memory.toml   （库级配置）                       └─ token.json {"library":"island"}
   ├─ workspace.db  （就是记忆本体）
   ├─ reports/ retrieval/  （派生产物，跟库走）  <安装根>/libraries/island/
   └─ CORE_RULES.bak / *.bak-*  （库自己的备份）     ├─ memory.toml   （A、B 共用）
                                                 ├─ workspace.db  （A、B 共用这个库）
                                                 └─ reports/ retrieval/ archive/

信条永远在项目里：A、B 各自的 .workbuddy/memory/信条.md 各自生效。
```

**统一存放的三条理由**：

1. 库是 MB 级二进制，躺在项目里迟早被 `git add -A` 卷走（R-7b 那一串坑的根源）；
2. 项目备份 / 打包 / 分享不该捎带记忆；
3. 反过来 —— 删项目不该连记忆一起删。**代价要记住**：删库必须显式去
   `<安装根>/libraries/<label>/` 操作，项目里的 `.ai_memory/` 只是一张"身份卡"。

项目里只剩：`.ai_memory/token.json`、`.workbuddy/memory/信条.md`。
（`bind relocate` 留下的 `_moved-<日期>/` 是搬迁时的旧文件暂存，确认后由人手动清。）

### 5.9 库放哪：统一在 `<安装根>/libraries/<label>`

`libraries_home()` 的解析顺序：

1. `HM_LIBRARIES`（`.env` 或环境变量，两种运行模式用它对齐）；
2. `<安装根>/libraries/` —— 安装根 = `.env` 所在目录：冻结态是发布根
   （`…/HumanityMemory-Release`），源码态是仓库根；
3. 兜底 `~/.humanity-memory/libraries`（前两条都拿不到才会用到）。

放置原则：**配置跟用户走、数据跟安装走**。registry / 信条 / 诊断是配置与索引（几十 KB），
留在用户目录；库是实体数据（MB 级），跟着安装。混在一起最直接的后果是 C 盘悄悄攒下
几十 MB，而升级 / 回滚时没人会想到去那儿找。

`bind new` 直接把新库建在 `libraries/*`；遗留的「库在项目里」用 `bind relocate` 搬
（幂等：已在统一目录就返回 `relocated: false`）。`doctor` 输出里的 `libraries_home`
就是这个根，`bind migrate` 不参与决定 —— 库的真实位置永远以 registry 为准。

---

## 6. 拒绝契约（A1 的落地形态）

### 6.1 统一返回

未接入态下，**所有**记忆读写工具返回同一个结构（不是抛异常、不是空结果）：

```json
{
  "ok": false,
  "error": "memory_unavailable",
  "code": "no_token",
  "reason": "D:/work/humanity-memory 下没有 .ai_memory/token.json，该工作区尚未接入记忆库。",
  "workspace_root": "D:/work/humanity-memory",
  "expected_token": "D:/work/humanity-memory/.ai_memory/token.json",
  "checklist": [
    "① 本目录已有 .ai_memory/workspace.db（知识 106 / 经历 59）→ bind adopt：把现有库登记为自有库",
    "② 想并入已有库 → bind existing(library=\"island\")",
    "③ 新建独立库 → bind new(label=\"humanity_memory\")",
    "④ 本次不接入 → bind decline"
  ]
}
```

`reason` 必须是**人话 + 具体路径**：它的读者是 agent，agent 要能直接复述给用户。

### 6.2 错误码

| code | 触发 | checklist 首项 |
| --- | --- | --- |
| `no_workspace` | 解析不到 root（cwd 在 home / 盘符根） | 传入项目根：`bind_status(workspace=...)` |
| `no_token` | root 有 `.ai_memory` 但无令牌 | 有 `workspace.db` → adopt；否则 new / existing |
| `token_bad` | 令牌 JSON 损坏 / 缺 `key` | 重新发令牌（`bind adopt`） |
| `token_v1` | `version == 1` | 迁移：`bind adopt` 签发 v2 令牌（丢弃 embedding） |
| `token_workspace_mismatch` | 令牌属于别的项目（复制 / 搬家） | 旧路径仍在 → "令牌不属于此项目"；旧路径消失 → `bind move` |
| `key_unknown` / `key_revoked` | registry 无此 key / 已撤销 | 重发令牌 |
| `key_workspace_mismatch` | 令牌被手改（workspace 不符） | 重发令牌 |
| `key_library_mismatch` | 令牌被手改（`library` 与 registry 不符，含被改成 `null`） | 重发令牌 |
| `declined` | 令牌与 registry 都是 `library: null` | 无清单，只说明"本工作区已选择不接入" |
| `library_missing` | 组库目录不在（移动硬盘 / 网络盘） | 挂载后重试 / `bind existing` 换库 |

### 6.3 「已拒绝」不能变成哑巴

`declined` 也要给出**一句话出路**（"想改主意：`bind new` 或 `bind existing`"），
否则三个月后没人知道这个工作区为什么记忆全空。

### 6.4 白名单：拒绝态下仍然可用的工具（A1 + A3 的强制配套）

**必须保留**。原因不是"方便"，而是 §3.4 那个坑没消失：boot 实例的 cwd 可能是
`~/.humanity-memory`，而 `serverName` 作用域唯一、会话实例不会再 spawn。
如果未接入态连救援工具都关掉，`no_workspace` 会让**整个会话**没有任何记忆能力，agent 也无法自救。

> **在客户端里的表现**：工具列表只剩 6 个（下面 5 个 `memory_bind_*` + `memory_use_workspace`），
> 记忆读写工具全部消失。这是拒绝态的正常形态，不是安装坏了 ——
> 先 `memory_bind_status` 看 `code`，再按清单处理。实测最常见的是
> `no_workspace`（客户端拿应用安装目录当 cwd 起了 boot 实例），
> 用 `memory_use_workspace(path=…)` 一步恢复。

| 工具 | 未接入态行为 |
| --- | --- |
| `memory_bind_status(workspace?)` | 报告四态 + 候选库 + 遗留库提示；`workspace` 可显式预检任意路径 |
| `memory_bind_new(label, workspace?)` | 建库 + 发令牌 |
| `memory_bind_existing(library, workspace?)` | 绑到 registry 已有库 + 发令牌 |
| `memory_bind_adopt(workspace?)` | 把本目录已有 `.ai_memory` 登记为自有库（**存量迁移入口**） |
| `memory_bind_decline(workspace?)` | 写 `library: null` 令牌 |
| `memory_use_workspace(path)` | **仅进程内**重新解析 + 校验；无合法令牌仍返回拒绝 + 清单 |
| `memory_doctor`（若存在） | 只读诊断，不带任何库内容 |

其余 18 个工具在拒绝态**不注册**（不是注册后报错）——模型看不到，就不会误调，
也就不会出现"agent 以为写入成功"这类事故。`INSTRUCTIONS_TAIL` 追加硬规则：

> 若 `memory_bind_status` 显示未接入（`code != null`）：**先向用户提问**（新建库 / 绑到已有库 /
> 沿用本目录已有的库 / 本次不接入），拿到答复再用对应 `memory_bind_*`。
> 不要自己选，不要跳过，不要在拒绝态下继续尝试写入。

---

## 7. 新增 / 变更的 MCP 工具

| 工具 | 变化 |
| --- | --- |
| `memory_bind_status` | 新增；返回四态 + `code` + checklist + 候选库 + 遗留库提示 |
| `memory_bind_new` | 新增；`label` 必填，`workspace` 可选（默认当前 root） |
| `memory_bind_existing` | 新增；`library` 必填 |
| `memory_bind_adopt` | 新增；**存量迁移唯一入口**，自动识别 `.ai_memory/workspace.db` |
| `memory_bind_decline` | 新增；写 `library: null` 令牌 |
| `memory_use_workspace` | `persist` 失效（不再写 last-workspace）；语义改为"重新解析并校验" |
| `memory_promote` | 保持下线 |
| 其余 18 个 | 拒绝态不注册（当前工具总数 24） |

---

## 8. 改动清单

| 文件 | 内容 | 估计行数 |
| --- | --- | --- |
| **新增 `keys.py`** | registry 读写、key 签发/校验/撤销、`O_EXCL` 锁、临时文件 + `os.replace` 原子落盘；**不含任何 md5 文件名逻辑**（A2 已去掉） | ~160 |
| **新增 `bindings.py`**（T10 加） | 扫描 `<root>/.ai_memory`、体检（令牌状态 / git 拦截）、登记库 + 发令牌、退役旧令牌、改 `mcp.json`；CLI 与将来的 `bind_*` 工具共用这一层，**幂等**（重跑全 `keep`） | ~420 |
| `token.py` | v2（`key` / `workspace` / `label`）；**删除 `embedding` 字段与 `apply_embedding_env()`**；落点改 `<root>/.ai_memory/token.json`；v1 只识别不降级 | ~80 |
| `paths.py` | `Layout.workspace_root` / `library_dir`（`workspace_dir` 留别名属性）；`resolve_workspace_root()` 与 `resolve_library()` 两段式；**删除 `read_last_workspace` / `write_last_workspace` / `LAST_WORKSPACE_FILE`**（`paths.py:54-80`） | ~70（净减） |
| `mcp_server.py` | 启动校验 + 拒绝态工具裁剪；**删除 932 行自动建库分支**；删除 916 行 last 兜底、621 行回写；5 个 bind 工具；`instructions` 追加提问纪律；`_write_probe` 增加 token / library / 错误码 | ~170 |
| `cli.py` | `bind new/existing/adopt/decline/move`、`bindings list`、`revoke`、`doctor`；**删除 `workspace use --with-embedding`**；拒绝态统一返回检查清单 | ~180 |
| `config.py` | 第四层改名「项目」→「库」，语义对齐 A4；参数名 `workspace` → `library_dir`；拒绝态只加载前三层 | ~30 |
| `store.py` | 仅补一个前置断言：`layout.library_dir is None` 时立刻报 `memory_unavailable`（防御 CLI 绕过） | ~10 |
| `tests/` | 四态、令牌跨项目被拒、撤销、A/B 同库、C/D 隔离、遗留库 adopt、**拒绝态工具裁剪**、**库级配置共享**、boot 实例救援 | ~170 |

合计约 870 行，**不修改 `schema.sql`，无 schema 迁移**。

> v1 §7 里的 `db.py` 那 15 行**删掉**：R-2 已经修好了（§9）。

---

## 9. 风险清单

| 编号 | 风险 | 触发条件 | 严重度 | 缓解 |
| --- | --- | --- | --- | --- |
| R-1 | **存量工作区集体变「未接入」，用户以为记忆丢了** | 升级后首次启动 | 高 | A1 让它更痛（不能用 ≠ 只是问一句）。因此**发布必须自带迁移**（§10.2）：9 个库一次性 `adopt` + 发令牌，人工零操作；漏掉的库由 `no_token` 清单指出路径与条数 |
| R-2 | ~~两进程同时启动撞锁 / 库被改名~~ | — | **已消除** | `open_db`（`db.py:303`）现在先 `probe_schema_version`（`mode=ro` 只读）→ `_check_version` → **只有 `fresh` 才 `executescript`**；`retire_legacy_db` 已不存在。验收项"不产生 `.v2-*` 改名文件"已满足；仅新库首次初始化仍有一次竞态，属"刚建库"场景 |
| R-3 | 组内 `embedding` 维度不一致开不了库 | A、B 用不同模型 | 低（当前全 1024 / bge-m3） | `meta.embedding_dim` 只有一个值，`ensure_schema` 会抛 `SchemaError`（失败清晰）；绑定时预检并提示 |
| R-4 | ~~组内 state 混在一起复核被误删~~ | — | **已消除** | 两类化改造后复核梯子随 `state` 一起删除，`memory_review` 已下线；经验证伪只扣分不删行 |
| R-5 | registry 并发写丢更新（吊销记录被覆盖） | 两窗口同时绑定 / 吊销 | 中 | `O_EXCL` 锁文件 + 临时文件 `os.replace` |
| R-6 | 解绑是单向门 | 共享期间在 B 写入的内容留在组库 | 中 | `unbind` 时提示先导出 / 归档，不默认视作回滚 |
| **R-7** | **令牌随项目目录被复制 / 打包 / 提交**（A2 的副作用；v1 曾用"令牌在用户目录"规避） | 拷贝工程、`git add -A`、打包分发 | **低**（A6 降级） | 泄露内容只有一个本地绝对路径 + 一个**离开本机 registry 即无效**的 `key`，无凭据、无网络校验面 → 危害约等于公开一个目录名。仍保留三道成本近乎为零的动作：①令牌内 `workspace` 强校验（§5.5 第 4 步）顺带挡掉"复制后被误用"；②可撤销（`revoke`）；③迁移时核对 4 个仓库的 pre-commit 钩子、给 `MiscAI` 补拦截（§10.2，属卫生而非安全） |
| **R-7b** | **真正的泄露载体是库内容，不是令牌** | `.ai_memory/workspace.db` 被提交/推送/打包 | **低**（A7 降级） | AI 写的日志与经历可能顺手记下 `.env` 值、内网地址、报错栈里的令牌。**A7 复核后降级**：①凭据扫描已扩到 9 个主库 550 条 + 11 个遗留备份库 955 条，共 1505 条，六类正则 0 命中；②git 出口由 pre-commit 钩子实测封住（4 个仓库），云同步通道实测不存在，仅余打包一条人工通道；③开发/生产已隔离，测试期脏数据进不了生产库。保留两条零成本动作：`MiscAI` 补钩子或 exclude（**仅它没有钩子**）+ 发布前跑附录 A.1 扫描。**与令牌无关，即使令牌方案回退也依然存在** |
| R-8 | ~~令牌与 embedding 凭据混在一个文件~~ | — | **已消除** | v1 Q4 拆除；实测 `active.token.json` 的 `embedding` 已是空对象 |
| **R-9** | **项目改名 / 搬家后令牌里的绝对路径失配** | 重命名或移动目录 | 中 | 令牌跟着项目走，只有 `workspace` 字段过期 → `bind move --from <旧> --to <新>`（改 token + registry 两处）。**不自动改写**；`token_workspace_mismatch` 的 checklist 直接给这条命令 |
| R-10 | 误删仍有效的令牌 | 盘符未挂载、移动硬盘拔出、网络盘断开 → `is_dir()` 为 False，与"已废弃"不可区分 | 高 | 清理**只能 dry-run 报告 + 人工确认**，绝不作为启动时自动行为；A3 删掉 last-workspace 后，"目录暂时不在"不会再引发切换，反而少了一条静默失效路径 |
| **R-11** | **boot 实例 cwd 不在项目内 → 整个会话 `no_workspace`** | DSH 重启、客户端静态 cwd | **高** | ①§6.4 白名单必须保留 `bind_*` 与 `use_workspace`；②迁移期所有库已发令牌，`use_workspace(<项目根>)` 一步恢复；③`bind_status(workspace=...)` 支持显式预检，agent 可从自身上下文拿到项目路径 |

### 关于「令牌是不是安全边界」

**不是。** 同机能读到 `libraries/island/workspace.db` 的人，不需要令牌。三条价值不变：

1. 防**误接入**（打开错的库）—— 主要价值，A1 把它从"提醒"升级为"硬拦截"；
2. 防令牌被改指向（库路径在服务端）；
3. 可撤销、可审计。

A2 只改变了令牌**放在哪**，没有改变它的安全等级。

#### A6 的威胁模型（2026-11-18 定性：低危）

有人会觉得"令牌进项目 = 凭据进 git"很可怕。把四件事分开看，这个担心不成立：

| 问题 | 事实 |
| --- | --- |
| 令牌里有凭据吗 | 没有。`embedding` 已拆除（Q4），字段只有 `version / key / workspace / label` |
| `key` 本身能用吗 | 不能。registry 只存 `sha256(salt+key)`，且校验要求 `token.workspace == 本机解析出的 root`。换个项目、换台机器、只拿到字符串，三种情况都过不了 |
| 有远程攻击面吗 | 没有。stdio MCP + 本地 sqlite，无监听端口、无鉴权接口 |
| 泄露半径 | 一个本地绝对路径 + 一个无效 key。约等于公开一个目录名 |

所以 R-7 是**卫生问题**，不是安全问题。仍然做 `.git/info/exclude`，理由不是"防泄露"，而是
"不让无意义的文件干扰 `git status`" —— 成本一行、收益零争议。

**真正值得防的是 R-7b：库内容。** 记忆库是 AI 自己写的，
它在记录排查过程时完全可能把 `.env` 值、内网地址、报错栈里的临时令牌一起抄进 `logs` / `episodes`。
`workspace.db` 一旦被提交或随包分发，泄的是这些，而不是令牌。
2026-11-18 对 **9 个主库 + 11 个遗留备份库（共 1505 条文本）**做过只读扫描（判据见附录 A.1），
`sk-` / `ghp_` / `AKIA` / `Bearer` / 私钥块 / `key=` 明文赋值 **全部 0 命中** —— 目前干净，
但这是"内容质量"问题，靠每次写入时的自觉，靠不了绑定方案。

#### A7 环境隔离改变了什么、没改变什么（2026-11-18 追问后复核）

追问："开发与生产环境已经隔离，是否还要提防 R-7b？"

**要，但降级。而且理由不能写"因为隔离了"** —— 隔离与 R-7b 防的是两件正交的事：
隔离管的是**混淆**（哪个库被谁读、测试数据别进生产），R-7b 管的是**外流**（库内容从哪条路离开本机）。
隔离不产生任何出口管控，所以不能替代它；但隔离确实压缩了 R-7b 的**上界**，因为
"泄露的前提是某个环境里的库内容本身值钱"，而测试期的构造数据进了 dev 库，不会混进生产库往外走。

于是把风险折算成"通道 × 内容 × 下游"，逐条实测（2026-11-18）：

| 通道 | 实测状态 | 隔离是否覆盖 |
| --- | --- | --- |
| **git 提交 / 推送** | **已由 pre-commit 钩子封住**（2026-11-18 实测）。4 个仓库 `majotavern` / `project_island` / `project_island_back` / `beto-client` 的 `.git/hooks/pre-commit` 均含 `.ai_memory/*` 与 `*/.ai_memory/*` 两条 case 分支，命中 `exit 1`；用探针文件 `git add` 后 `git hook run pre-commit` 实测返回退出码 1 并列出路径，清理后暂存区 0 残留。前三个仓库另有 `pre-rebase` / `.bak-hm` 备份，说明是成套维护的；`beto-client` 的钩子是 2026-11-18 自 `majotavern` 移植（Cocos2d-x 工程，已裁掉 Unity 专有规则） | **否**（不靠隔离，靠钩子） |
| **打包 / 复制工程目录** | 人工动作，频率低 | **否** |
| **云同步** | 便携同步目录内实测**无任何 `.db`**，同步的只有 `.workbuddy/memory`（服务端生成的那份），`.ai_memory` 完全不参与 | **是，且这条通道直接不存在** |
| **另一个项目的 AI 读你的库** | 2026-11-17 真实发生（island_back 信条注入 HumanityMemory 会话） | **否，且最容易被"隔离了"掩盖** |

钩子的两个边界（不影响结论，但要写下来）：

- 拦的是**提交**，不是 `git add` —— 位置正确（`git add` 本身不外流）；
- `--no-verify` 可绕过（钩子自己的提示里就写了），且钩子**不进版本库**、不随 clone 传播 —— 对本机单人多仓库场景足够，换机器要重装；
- 钩子里那条"已跟踪文件只警告不阻止"的附加检查是**历史遗留的兜底**，当前 4 个仓库的 `.ai_memory` 都是 `??` untracked、零跟踪文件，所以这条缺口未被触发。

**`beto-client` 补装记录（2026-11-18）**：原模板含 `ExternalCommandSystem` / `com.aketsuki.*` 等 Unity 规则，
而它是 Cocos2d-x C++ 工程（无 `Assets/` `Packages/`），故裁掉这些分支、只留 `.ai_memory` 与 `.workbuddy` 两类。
装完做了三向实测：干净暂存区 `exit 0`、普通源码路径 `Classes/__probe.tmp` `exit 0`（不误伤）、
`.ai_memory/__probe.tmp` `exit 1`（拦得住），探针均已清理、暂存区 0 条。
注意它**没装 `pre-rebase`** —— 那是保护 `release` 分支的另一件事，与 `.ai_memory` 无关，需要时单独议。

**对不上的地方（已修正）**：钩子实际覆盖 **4 个仓库**。`MiscAI` 是唯一仍然零拦截的
（连 `pre-commit.sample` 之外什么都没有），其 `.ai_memory/` 仍是裸 untracked，内容仅 1 条 log。
按用户 2026-11-18 决定"`MiscAI` 先不动"，迁移时**不要**把它算作已覆盖。

结论：**R-7b 中 → 低**。上表第 1 行由钩子封住（实测）、第 2 行人工可控、第 3 行实测不存在；
第 4 行不属于本风险（是准入问题，由 A1/A3 解决），单列提醒——
**R-7b 降级不等于整体放松，跨库误读恰恰是隔离管不到、且已实际发生的那一个。**

同时发现一处**隔离其实没做到的地方**（A2 的额外收益）：
dev 与 prod 的**令牌**当前同处 `~/.humanity-memory/`（`active.token.json` / `dev.token.json`），
仅靠 `workspace` 字段区分 —— 这是配置级隔离，不是物理隔离。A2 把令牌改为跟库走之后，
dev 落在 `HumanityMemory_dev/.ai_memory/token.json`、生产落在 `HumanityMemory/.ai_memory/token.json`，
**隔离从"一个可变字段"升级为"一条固定路径"**，比现状更硬。


---

## 10. 分期实施与发布迁移

### 10.1 分期

| 期 | 内容 | 完成标志 |
| --- | --- | --- |
| **P1 准入层** | 令牌落项目内 + 四态 + registry + 删自动建库 + 删 last-workspace + adopt 存量 + `bind_status` + **拒绝态工具裁剪** | 9 个存量库 adopt 后正常读写；未接入确实什么都读不到、且清单可照做 |
| **P2 共享绑定** | `workspace_root` / `library_dir` 拆开 + A、B 指向同一 library + 信条仍各自生效 + 库级 memory.toml 共享 | A、B 写入互见；C、D 完全不受影响 |
| **P3 合并（可选）** | 已有库合并、原库改名留档（`.merged-<date>`，不删除） | 合并后条目数对得上，旧库文件仍在 |

P1 完成后功能即完整（还没有共享）。P2 才是 R1 的兑现。

> 逐条任务、文件与验收见 §13；其中 **T10（发令牌）必须先于 T3/T4（删兜底）**，
> 否则 A1 + A3 一生效，未发令牌的工作区立刻全部失效。

**P3 的合并范围要显式白名单**：R1 的真实用例是 `project_island` ↔ `alpha`
（同一游戏前后端）。9 个库全并会直接违反 R2，并且必须排除 `HumanityMemory`（工具自身的库）。

### 10.2 发布迁移（A5：其他库接入打包 exe）

其他库不再依赖本仓库源码，因此**改代码不影响它们**——影响只发生在发布时。这与
`two-tier-memory.md` §11 的已定项「历史库升级走 `upgrade` CLI 子命令，主程序只检测、不含适配代码」一致：
**迁移必须是一条命令，不是主路径里的隐式适配。**

`humanity-memory bind migrate --all --dry-run` → 确认后 `--apply`，对每个现存
`<root>/.ai_memory/workspace.db` 做四件事：

| 步骤 | 动作 | 可回滚性 |
| --- | --- | --- |
| 1 | registry 登记 `libraries.<label> = { path: <root>/.ai_memory }` | 纯新增，可删条目 |
| 2 | 签发 v2 令牌写入 `<root>/.ai_memory/token.json`（`library = <label>`） | 删文件即回到"未接入" |
| 3 | **核对/补齐 git 拦截**：`majotavern` / `project_island` / `project_island_back` / `beto-client` 已有 pre-commit 钩子覆盖 `.ai_memory/*`（实测通过，**不动它**）；仅 `MiscAI` 无钩子（内容仅 1 条 log，**用户已决定先不动**）→ 迁移时按现状记账，不补。`CivelRev` 不是 git 仓库，跳过。钩子同时挡住令牌**和**库内容（R-7 / R-7b），属卫生动作 | 删一行或删钩子即可回滚，不动 `.gitignore` |
| 4 | 旧 `active.token.json`（v1）改名 `active.token.json.retired-<date>`；`~/.workbuddy/mcp.json` 的 `--token` 参数删除 | 恢复备份即回滚 |

迁移后 mcp.json 变成**真正的一条 0 参数配置**：

```json
{ "args": ["serve"] }
```

—— 这正是 v1 §3.2 想要的形态，只是现在靠"令牌在项目里 + cwd 自动发现"实现，而不是靠全局令牌。

迁移清单（人工核对一次，不进自动化）：

- [ ] 9 个库的 `label` 命名（建议：目录名小写 + 下划线，`island` 留给将来的组库）
- [ ] `~/.humanity-memory/memory.db`（4.3 MB）归属确认：历史残留则改名留档，不参与迁移
- [ ] `project_island` 与 `alpha` 是否 P1 就并入 `island` 组（建议 P2 再做）
- [ ] git 拦截复核：3 个已有钩子的仓库再跑一次 `git hook run pre-commit`（期望 exit 0，工作区干净时）；2 个无钩子的仓库补完后用 `git check-ignore -v .ai_memory/token.json` 或探针提交验证
- [ ] `upgrade.bat` / `report.bat` / `starmap.bat` 里是否还有 `--token` 或 `workspace use` 调用（§7 删了 `--with-embedding`）

### 10.3 v1 §10.3（令牌文件名 md5 vs base64）—— **整节作废**

A2 把令牌放在固定路径 `<root>/.ai_memory/token.json`，**不存在"给令牌起唯一文件名"这个问题**：

- 不需要 md5 寻址 → 没有路径规范化陷阱、没有"改名后文件名失配"（v1 R-9 随之大幅缩小，只剩令牌内绝对路径）；
- 不需要 base64url → 没有 4/3 长度膨胀、没有 NTFS 大小写撞名；
- 不需要"路径 → 文件名"索引 → registry 只做 `key → workspace/library` 的真相，不做寻址。

保留的两个运维命令：`bind move`（搬家）与 `keys gc --dry-run`（清理孤儿 registry 条目）。

### 10.4 验收标准

- registry 不存在 / 只有出厂配置时，`resolve_layout` 输出与今天**逐字节一致**（回归测试锁死）
- **未接入态：不产生 `.ai_memory`、不产生 db、不产生令牌；18 个读写工具不注册**
- 未接入态：模型侧看不到被裁剪的工具；`bind_status` 返回 `code` + checklist
- 令牌只落在 `<root>/.ai_memory/`，`git status` 在**所有仓库**里都看不到它（`check-ignore` 全绿）
- **发布前**跑一次附录 A.1 的凭据扫描：命中数必须为 0（R-7b 的验收线，与令牌无关）
- 令牌 `workspace` 与当前 root 不一致 → 拒绝，且 checklist 给出 `bind move` 的确切命令
- **A、B 共用库**：`library_dir` 相同、`memory.toml` 同一份、`workspace_rules` 仍分别指向各自信条
- **A、B 同时开**：不报锁错误、不产生 `.v2-*` 改名文件
- `declined` 态：一直拒绝、但只问过一次
- `unbind` 后立刻回到 `<root>/.ai_memory`
- `doctor` / `stats` 显示：root、库、所属组、组成员、令牌状态、`access`
- boot 实例场景复现（cwd = `~/.humanity-memory`）：`no_workspace` + `use_workspace(<项目根>)` 一步恢复

---

## 11. 决策记录

### 2026-11-18（v2）

| 编号 | 问题 | 决定 | 影响 |
| --- | --- | --- | --- |
| A1 | 未接入时能否访问库 | **不能**。拒绝 + 原因 + 清单 | 两个非绑定态都变成"能力关闭"；工具裁剪；§6 成为独立契约 |
| A2 | 令牌放哪 | **`<root>/.ai_memory/token.json`** | 取代 v1 Q1；md5 文件名机制整体作废；`--token` 降级为断言；R-7 / R-9 重写 |
| A3 | `last-workspace.txt` | **删除** | 删 `paths.py:54-80`、`mcp_server.py:621/916`；新增 R-11（boot 实例需救援通道） |
| A4 | `memory.toml` 的"项目级" | **改为库级**，最高优先级层跟着库走 | `config.py` 第四层改名；共享组内不能逐项目调参（刻意） |
| A5 | 其他库如何升级 | 接打包 exe，**发布时一次性迁移 + 自动发令牌** | §10.2 的 `bind migrate`；沿用 `upgrade` 子命令的既定原则 |
| A6 | 令牌进项目的泄露危害 | **低危，不按凭据泄露设计**。令牌无凭据、无远程面、脱离本机 registry 即失效；真正的载体是库内容（R-7b） | R-7 中 → 低；`.git/info/exclude` 保留但定性为卫生；新增 R-7b 与 §9 威胁模型小节 |
| A7 | 开发/生产已隔离，是否还要防库内容外流 | **隔离管"混淆"、R-7b 管"外流"，两者正交，不能互相替代**。按"通道 × 内容 × 下游"折算：git 出口已由 pre-commit 钩子实测封住（**4 个仓库有，1 个没有**）、云同步通道实测不存在，只余打包一条人工通道 → R-7b 中 → 低。保留钩子核对 + 发布前扫描两条零成本动作，**但不建机制** | R-7b 中 → 低；§9 新增 A7 小节（含通道实测表 + 钩子边界）；**纠正"5 个仓库都有钩子"为 4 个**，`beto-client` 于 2026-11-18 补装并三向实测，`MiscAI` 按用户决定暂不动；扫描基线扩到备份库 955 条；同时记录"dev/prod 令牌同目录"这处隔离实际未做到的地方，由 A2 顺带升级为路径级隔离 |

### 2026-11-12（v1，继续有效）

| 编号 | 问题 | 决定 | 备注 |
| --- | --- | --- | --- |
| Q2 | 遗留库识别 | 自动识别并置顶为默认选项 | 执行仍需确认；迁移期由 `bind migrate` 自动完成 |
| Q3 | key 存储 | `sha256(salt + key)`，salt 落 registry | 不变 |
| Q4 | embedding 凭据 | 从令牌彻底拆除 | 不变；`active.token.json` 实测已空，迁移零阻力 |
| Q5 | 零配置自动建库 | 仅 MCP 进程禁止，CLI 人工 `init` 保留 | 与 A1 / A3 一致：MCP 侧彻底不建 |
| Q1 | 令牌放哪 | ~~用户目录 `keys/`~~ | **被 A2 取代** |

### 与其它文档的关系

- `docs/two-tier-memory.md` §8.2（关闭 `global.db`）：本方案**不重开全局库**，A1 就是它的直接后果。
  §11「将来重建共享区：允许，但必须更机械，归属由规则决定，不由模型现场判断」——
  本方案的归属由 registry 里的显式绑定决定，符合该约束。
- §11「参数统一 TOML」：A4 后配置层序为 出厂 → 安装 → 用户 → **库**，凭据仍只走 `.env`。
- `config.py:335-350` 的 `.env` 两态定位（源码态 = 仓库根，冻结态 = 发布根）是已定事实，
  v1 §10.2 建议改读 `~/.humanity-memory/.env` **不再采纳**（README 已与代码对齐）。

---

## 12. 前置失效复核（开工前，2026-11-18）

开工前把"文档与代码的冲突"分了类。分类标准只有一句：
**某个结论 P 依赖前提 Q，Q 不成立 → P 整体作废，必须重定方案，改字没用。** 这叫前置失效。
其余两类不叫失效：**陈旧失效**（更早轮次变更后没同步，与 A1–A6 无关）、**待同步工件**（P 成立，只是落点要改）。

### 12.1 前置失效（本次 A1–A6 造成，必须重定方案）

| 编号 | 失效的结论 | 它依赖的前提 | 现状证据 | 重定后的方案 |
| --- | --- | --- | --- | --- |
| **F1** | 「源码态开发用 `dev.token.json` 钉住 dev 工作区，保证永不碰生产库」 | 令牌的 `workspace` 优先级最高，能覆盖 `--workspace` | `dev.bat:33-36,61` 明确注释 "token's workspace outranks --workspace"；`dev.token.json` 是 v1 | A2 把令牌降级为**断言**，钉住工作区的活改由 `--workspace` / `HM_WORKSPACE` 干（阶段一仍是第 1 优先级）。dev 隔离改为「独立 root + 独立 registry 条目」：`HumanityMemory_dev/.ai_memory/workspace.db` 已存在（262 KB，2026-11-18 14:13），迁移时单独 `adopt`（`label = hm_dev`），`dev.bat` 改为 `set HM_WORKSPACE=%DEV_WS%`，`dev.token.json` 退役。**能力没丢，而且更硬**：旧机制靠"覆盖优先级"，新机制靠"身份强校验 + 两个独立库" |
| **F2** | 「一条 server 配置服务多项目：`memory_use_workspace` 解析路径、换 store 即可」 | 库位置与配置无关，所以切库不用重载 `cfg` | `mcp_server.py:618` `MemoryStore(cfg, new_layout)` 复用的是 boot 时的 `cfg` | A4 让配置跟着库走 → **切库必须重载配置**：解析 → 校验令牌 → `Config.load(library_dir=…)` → 新 `MemoryStore(新cfg)`。附带结论：这条不做，P2 共享库落地后读到的是**原库**的 `memory.toml`，A4 等于没实现。工具闭包只有 `build_server:73` 和 `main:951` 两处持有 `cfg`，重载面很窄，可干净改 |
| **F3** | 「`Config.load(workspace=None)` 表示自动猜库」 | 库必然存在或可猜出来 | `config.py:544` `ws_dir = workspace if workspace is not None else peek_workspace_dir(...)` | A1 让"**没有库**"成为一等合法状态 → `None` 的语义变得有歧义，无法表达"明确不读库级配置"。改为模块级 `_NO_LIBRARY` sentinel：`Config.load(library_dir=_NO_LIBRARY)` 只装配 出厂/安装/用户 三层；`None` 保持"自动猜"（兼容 CLI 与测试） |
| **F4** | 「定时复盘任务必须 `persist=false`，防止改掉用户白天的工作区选择」 | 存在一个"持久化的工作区选择" | `workspace-onboarding.md:79`；`mcp_server.py:622` `write_last_workspace` | A3 删掉 `last-workspace.txt` → 这道闸门**失去对象**（防护手段没了，但目标"不污染他人会话"仍要）。重定为：闸门改为「必须显式传 `path`」，第 2 道闸（`stats` 校验库路径）保留并升级为 `library_db`。目标本身由"无共享可变状态"天然达成 |
| **F5** | 「令牌 = 工作区绑定 + 信条路径 + embedding 凭据，且优先级最高」 | 凭据与绑定同文件、令牌代表身份 | `active.token.json` / `dev.token.json` 均 `version: 1`；README 163-231、`operations.md:32-49` 仍按此描述 | **双重失效**：Q4（2026-11-12 拆 embedding）+ A2（2026-11-18 降优先级）。v2 = 纯身份 + 服务端 registry 定库；v1 一律 `token_v1` 拒绝并给迁移指引。两个现存 v1 令牌由 T10 覆盖 |

### 12.2 陈旧失效（更早轮次遗留，与本次无关，顺手一起收）

| 编号 | 失效的结论 | 何时失效 | 落点 |
| --- | --- | --- | --- |
| **S1** | 「未确定工作区时，当前只能读写全局库」 | 2026-11-17 关闭 `global.db`（§8.2） | `mcp_server.py:959` 的 stderr 提示；改为拒绝契约文案 |
| **S2** | 「`stats` 有两库：workspace / global」 | 同上 | `store.py:2472` `db_name == "workspace" else layout.global_db` |
| **S3** | 「接入新工作区前，先只读检索 **global 库**」 | 同上 | `workspace-onboarding.md` 步骤 3 |

> 这三条属于"上一轮改完没同步"，不是本次设计的问题。它们会被 §13 的任务顺路覆盖，
> 但**验收时不要把它们算进本次的回归范围**，否则会误判"这次改动引入的问题"。

### 12.3 待同步工件（结论成立，只是下游要跟着改）

| 编号 | 文件 | 为什么必须改 |
| --- | --- | --- |
| W1 | `tests/test_config_layers.py:65` | 断言 `["出厂","安装","用户","项目"]`，A4 改名后必红 |
| W2 | `README.md:163-231` | 令牌章节按 v1 写（含 embedding、优先级、一条配置服务多项目） |
| W3 | `skills/humanity-memory/references/operations.md:32-49` | 同上，且写着"带凭据的令牌别放项目里"，与 A2/A6 相反 |
| W4 | `skills/humanity-memory/references/workspace-onboarding.md:79` | `persist=false` 闸门（F4）与 global 库检索（S3） |
| W5 | `MiscAI` 的 git 拦截（**唯一没有钩子的仓库，用户已决定暂不处理**） | R-7 / R-7b 的卫生动作；其余 4 个仓库已有钩子且实测通过（`beto-client` 于 2026-11-18 补装），迁移时只核对不修改 |

---

## 13. 实施 TODO（可勾选）

分批的唯一原则：**任何一步停下来，系统都必须是可用状态**。
因此"发令牌"必须早于"删兜底"——否则 A1 + A3 一生效，还没发令牌的 9 个工作区立刻全部失效。

### P1a · 兼容期（旧行为不变，可随时回滚）

- [x] **T1** `token.py` 升 v2：`key` / `workspace` / `label`；落点 `<root>/.ai_memory/token.json`；删 `embedding` 与 `apply_embedding_env()`；v1 只识别不降级
      → 验收：v1 读到返回 `token_v1`；v2 往返读写一致（2026-11-18 完成，`tests/test_token.py` 22 项）
- [x] **T2** 新增 `keys.py`：registry 读写、key 签发/校验/撤销、`O_EXCL` 锁、临时文件 + `os.replace`；**路径走 `global_home()`**（C8）
      → 验收：并发绑定不丢更新；只存 `sha256(salt+key)`；`HM_HOME` 变了 registry 跟着走
      → 2026-11-18 完成，`tests/test_keys.py` 25 项。实现注记：①`keys` 以哈希为键（唯一真相，不重复存 `hash`）；
        ②`library` 按主人决定写进令牌（`null` = 已拒绝），registry 侧同名记录为归属真相，两者严格相等，
        否则 `key_library_mismatch`；③锁等超时报 `registry_busy`，崩在锁里的进程按 mtime 判死（120s）可被接管；
        ④`Registry.save()` 只允许在 `edit()` 内调用。
- [x] **T5** `config.py`：第四层改名「项目」→「库」；参数 `workspace` → `library_dir`；新增 `_NO_LIBRARY` sentinel（F3）
      → 验收：拒绝态不读库级 toml；`None` 仍表示自动猜（2026-11-18 完成）
      → 实现注记：①哨兵导出名 `NO_LIBRARY`，`_NO_LIBRARY` 留作同对象别名与文档对齐；
        `resolve_library_dir()` 是唯一收敛点（哨兵→None / None→猜 / 路径→绝对化）；
        ②`library_dir` 在 `Config.load` 里是**关键字专用**，三态靠 `is` 比较，不做布尔判断；
        ③`Config` 增 `library_dir` 字段与 `describe()["library_dir"]`（拒绝态显示 `no library`）；
        ④层名"项目"→"库"同步改了 `test_config_layers.py` / `test_settings.py` 的层序断言（W1 的一部分）；
        ⑤顺带修了 `starmap._levels_from_config`：库文件不在 `.ai_memory` 也没有同级 `memory.toml` 时传哨兵，
        不再让"随便拷到临时目录的备份库"去猜 cwd 上面那个项目的配置
- [x] **T10** `bind migrate --all`：登记库 → 发令牌 → git 拦截核对（4 个已有钩子只核对；`MiscAI` 按用户决定暂不处理）→ 退役 `active.token.json` → `mcp.json` 去 `--token`；顺带 adopt `HumanityMemory_dev`（F1）
      → 验收：`--dry-run` 先出清单；9 个库 + dev 全部可读写；旧行为（last-workspace / 自动建库）此刻仍保留
      → **2026-11-18 第一段完成**（登记 + 发令牌 + git 核对）：`bind migrate --scan <工作区根> --apply`，
        registry **10 libraries / 10 keys**、明文 key 未落盘，逐个 `check_key(key, workspace, library)` 全通过；
        重跑（不带 `--label`）全 `keep`。实际库 = 扫描根下所有含 `.ai_memory` 的目录
        （§3.5 里的 `alpha` / `beta` 是示例条目，磁盘上已不存在）。
      → **第二段（退役 + 客户端 + 发包）2026-11-18 最终完成**，中间有一次回退，记录在案：
        · 首次做法（已回退）：升版 v0.5.0 → `--publish` → 退役旧令牌、`mcp.json` 变 `["serve"]`。
          **回退理由**：P1b 尚未落地，发包等于把半成品推上 `current`；而且这一步根本不需要新包
          —— 旧 exe 在没有 `--token` 时同样按 cwd 发现库，T10 只该改 `mcp.json`。
          回退动作：`--rollback --keep-db` + 恢复 `mcp.json` + 令牌改名复位。
        · 重做（P1b 全部完成后）：升版 **v0.5.1** → `--publish --yes`（smoke：bind new → doctor → serve 全过）
          → `bind migrate --apply --mcp-json` 退役 `active.token.json` / `dev.token.json`（改名留档）。
          实测：`current`=v0.5.1、`mcp.json` 只有 `["serve"]`、已接入项目 29 工具 / 未接入目录 6 工具。
      → 顺带修了 `tools/build_release.py`：发版不再回填 `--token`；smoke 改为先 `bind new` 造一个已接入沙箱
        （A1 之后空目录跑 doctor 会被正确拒绝，老 smoke 的假设失效）。
      → 已知残留：`tools/switch_to_v4.py`（v4 一次性脚本）仍写 `--token`，重跑会改指源码入口，**别再跑**。

### P1b · 切换拒绝态（可以停在这里长期观察）

- [x] **T3** `paths.py`：`workspace_root` / `library_dir`（`workspace_dir` 留别名）；解析链改为"cwd 找含 `token.json` 的 `.ai_memory`"；删 `read/write_last_workspace`（`paths.py:54-80`）
      → 验收：无令牌时不产生任何目录（2026-11-18 完成，`tests/test_paths.py` 10 项）
      → 实现注记：①发现链默认**只认带令牌的** `.ai_memory`，`include_tokenless=True` 专供 `bind_status` 给 adopt 建议；
        ②显式路径（`--workspace` / `HM_WORKSPACE`）不要求令牌 —— 那是运维出口，不是猜测；
        ③`find_project_root` 与自动建库分支一并删除；④`config.peek_workspace_dir` 同步去掉 last-workspace 一步。
- [x] **T4** `mcp_server.py`：七步校验 + §6 拒绝契约 + 拒绝态工具裁剪 + 删 `932` 自动建库分支 + 删 `916` last 兜底 + `_write_probe` 容错（C9）
      → 验收：未接入时 18 个工具不注册；`bind_status` 返回 `code` + checklist（完成）
      → 实现注记：校验落在 `bindings.resolve_session()`（七步，失败返回 code+reason+checklist，**不抛异常**）；
        工具裁剪用「条件装饰器 + 延迟注册」：拒绝态只注册 6 个（5 个 bind_* + use_workspace），
        绑定成功后 `activate()` 当场把 29 个挂上（R-11 自救不用重启客户端）；`_write_probe` 增补 bound/code/library。
- [x] **T6** `memory_use_workspace` 重写（F2）：不 `create`、不 `ensure_rules`、先校验令牌、**按新库重载 `Config`**
      → 验收：无令牌 → 拒绝 + 清单；切到共享库后读到该库的 `memory.toml`（完成，见 `test_library_level_config_follows_the_library`）
- [x] **T7** 5 个 `bind_*` 工具（均支持 `workspace` 显式入参，供 boot 实例自救）
      → 验收：`bind_status(workspace=…)` 能预检任意路径（完成）
      → 实现注记：MCP 工具与 CLI 共用 `bindings.bind_new/adopt/existing/decline/move`，两边不再各写一份。
- [x] **T8** `cli.py`：`bind new|existing|adopt|decline|move`、`bindings list`、`revoke`、`doctor`；删 `workspace use --with-embedding`
      → 验收：拒绝态 CLI 返回与 MCP 同一份清单（完成）
      → 实现注记：①`workspace use` 直接退役（它签的钥匙在 registry 里没有记录），只留 `show`；
        ②`doctor` 先过准入，未接入时输出与 MCP 同形；③`init` 不再建库，未接入时给 bind 指引。
- [x] **T9** `store.py`：`library_dir is None` 前置断言；清 `stats` 的 global 分支（S2）
      → 验收：CLI 绕过被挡（断言在 `MemoryStore.__init__`，失败时连空库都不会被创建）

### P1c · 收尾

- [x] **T11** 存量工件：`dev.bat`（F1）、`README.md:163-231`（W2）、`operations.md:32-49`（W3）、`workspace-onboarding.md`（F4/S3/W4）
      → 另同步：`SKILL.md`（一条配置服务多项目 / 拒绝态工具面）、`tools/build_release.py`（不再回填 `--token`；smoke 改为先 `bind new` 再 doctor）
- [x] **T12** 测试：改 `test_config_layers.py` 层名（W1）；新增四态 / 跨项目被拒 / 撤销 / 拒绝态裁剪 / 库级配置共享 / boot 实例救援
      → `tests/test_binding_lifecycle.py` 15 项 + `test_paths.py` 10 项；全量 **347 passed**
- [x] **T13** 终验：§10.4 逐条 + 附录 A.1 凭据扫描 0 命中 + 双进程同开
      → 2026-11-18 结果：布局回归逐字节一致（含"未接入零目录"）；拒绝态 6 工具 / 已接入 29 工具；
        凭据扫描 10 库 595 条 **0 命中**；双进程同开两把 29 工具、A 写 B 立刻可见；
        发布 **v0.5.1**（smoke：bind new → doctor → serve 握手全过）。

### P2 · 共享绑定（2026-11-19 落地：island 组）

**范围**：只有 `project_island` ↔ `project_island_back` 共库，其余 8 个继续自有库。
"集中存放"只针对共享组 —— 自有库留在各自项目里是刻意的（删项目即删记忆、令牌跟着项目走）。

| 项 | 结果 |
| --- | --- |
| 组库 | `<安装根>/libraries/island/workspace.db`（`…/HumanityMemory-Release/libraries/island`；从 `project_island_back` 搬出，**以它为准**） |
| 并入 | `project_island` 令牌 `library=island`，读写都落到组库 |
| 旧库 | `project_island/.ai_memory/workspace.db.merged-20261119`（60 知识 / 84 经历，用户决定**推翻重记**，文件留档不删） |
| 信条 | 两边 `CORE_RULES.md` → `<项目>/.workbuddy/memory/信条.md`（库一共享，信条就不能再跟着库走） |
| registry | 删 `project_island` / `project_island_back` 两个标签，新增 `island`；撤销 2 把旧钥匙 |
| 备份 | `~/.humanity-memory/backups/pre-island-20261119-095639/`（registry + 两个库的活文件） |
| 验收 | 两边 `resolve_session` 均 ok 且 `library_dir` 相同；back 写 → island 读得到；`bind migrate` 重跑 10 个全 `keep`（岛组显示 `shared`） |

**四个坑（下次照做）**：

1. **跨盘 `shutil.move` 实际是 copy + delete**（D: → C:），删除被沙箱转投回收站而失败时，
   源文件会留在原地 —— 看起来像"移动成功但源还在"。迁移应显式做
   `wal_checkpoint(TRUNCATE)` → 复制 → 校验 md5/条数 → 再处理源文件。
2. **残留 MCP server 进程会一直握着旧库**：这次有 4 个 `python -m humanity_memory.mcp_server
   --workspace <项目根>`（旧配置留下）把文件锁住，改名报 `WinError 32`。
   迁移前必须先查 `Win32_Process` 并停掉，否则组库副本会与旧库继续分叉。
3. **"库在共享组"不等于"空壳"**：本地没有 `workspace.db` 有两种含义。`bindings._inspect`
   已加 `shared` 状态，避免迁移把共享组项目当成空壳、进而又给它登记一个本地库。
4. **换落点要"先改 registry，再动文件"**：把组库从 `~/.humanity-memory/libraries` 搬到
   安装根时，客户端顺手把 MCP server 拉起来、按 registry 里的**旧路径**又打开了旧库，
   文件被占用、改名报 `WinError 32`。正确顺序：改 registry → 停相关进程 → 复制/校验 →
   处理旧文件。反序的代价是旧库仍在被写入，组库副本会悄悄落后。

### 顺序依赖（不做就会踩）

```
P1a  T1 → T2 → T5 → T10
P1b  T3 → T4 → T6 → T7 → T8 → T9
P1c  T11 → T12 → T13
```

四条硬约束：

1. **T10 必须先于 T3/T4**：先发令牌，再删兜底。反序 = 9 个工作区同时失效。
2. **T6 依赖 T5**：切库重载要用到 `library_dir` 参数与 `_NO_LIBRARY` sentinel。
3. **T12 的层名断言依赖 T5 改名**，别提前改测试，否则中间态必红。
4. **T3 与 T4 可并行**（解析链与拒绝契约互不依赖），但两者都必须在 T6 之前。

---

## 附录 A：证据（2026-11-18 采集）

```
# 启动参数实况（1024 条）
argv=[]                                             854
--token ~/.humanity-memory/active.token.json serve   145
--workspace <项目>                                    15
source: cwd 791 / token 155 / last 36 / none 19 / workspace 15 / cwd-project 8
最近 30 条：28 条带 --token，cwd 依次为 alpha / beta / gamma
           —— workspace_dir 全部是 alpha\.ai_memory

# 全局库已关闭
~/.humanity-memory/global.db.retired-20261117   （1.0 MB，保留至少一个季度）
store.py:294  「全局库已按 §8.2 关闭：这里**不打开** layout.global_db」
paths.py:156  「全局库已于 2026-11-17 关闭（§8.2）」

# 错绑事故（2026-11-17）
HumanityMemory 库日志 l_f3f99c914b61 + 经验 x_456980915226
「boot 实例先注册，会话实例不再 spawn，整个会话跟着 last-workspace 跑」

# 存量（9 库，schema v4，dim 1024 / bge-m3 全一致）
同 §3.5 表

# .ai_memory 的 git 拦截现状（2026-11-18 复核）
gitignore 已忽略（check-ignore 通过）：Aketsuki_Unity_MCP / HumanityMemory / beverage_town
pre-commit 钩子拦截（含 `.ai_memory/*` + `*/.ai_memory/*`，实测 exit 1）：
        majotavern / project_island / project_island_back
        beto-client（2026-11-18 补装，Cocos2d-x 版裁剪模板，三向实测通过）
两者皆无（untracked `??`，内容仅 1 条 log）：MiscAI（用户决定暂不处理）
CivelRev：不是 git 仓库
→ 迁移时 4 个钩子只核对不修改，MiscAI 按现状记账
```

### 附录 A.1：库内容的凭据扫描（2026-11-18，判据可复用）

只读扫 `knowledge` / `episodes` / `logs` / `digests` 的 `content`，命中即计数（**不打印原文**）：

| 模式 | 意图 |
| --- | --- |
| `\bsk-[A-Za-z0-9_\-]{16,}` | OpenAI 类 API key |
| `\b(ghp_[A-Za-z0-9]{20,}\|AKIA[0-9A-Z]{12,})` | GitHub / AWS 令牌 |
| `Bearer\s+[A-Za-z0-9\.\-_]{16,}` | Authorization 头 |
| `-----BEGIN [A-Z ]*PRIVATE KEY-----` | 私钥块 |
| `(?i)(api[_-]?key\|secret[_-]?key\|access[_-]?token\|password\|passwd)\s*[=:]\s*['"]?[A-Za-z0-9_\-]{12,}` | 明文赋值 |
| `\b[A-Za-z0-9+/]{60,}={0,2}\b` | 长串兜底（需人工判形状：纯 hex 多为哈希，混大小写才是 base64） |

结果：**9 个主库（282 知识 + 229 经历 + 5 日志 + 34 复盘 = 550 条），六类全部 0 命中**；
长串兜底仅 3 条误报，均为正文（`CityCore…` / `Soul/Ide…` / `11494/11…`）。

**2026-11-18 补充扫描：遗留备份库。** 首轮只扫了 `workspace.db`，漏掉了同名`.v2-*` / `.v3-*` 备份
（每次 schema 迁移留下的旧库副本，共 **11 个、955 条文本**）。补扫结果同样 **六类 0 命中**。
三个执行细节，下次复跑要照做，否则会白跑：

| 坑 | 表现 | 处理 |
| --- | --- | --- |
| 备份库是 v2/v3 老 schema | `no such column: content` | 列名要动态取（`PRAGMA table_info` 里找 `content/text/body/summary/title/detail/note`），不能写死 |
| 库里有 `*_vec` 影子表 | `no such module: vec0`，**整库查询直接失败** | 跳过 `_vec` / `_fts_` / `sqlite_` 前缀的表，且每个表独立 try，否则一表失败拖垮整库 |
| glob 会捞到 `-shm` / `-wal` | `file is not a database` | 过滤掉这两个后缀 |

> 这个扫描是 R-7b 的验收手段，不是一次性动作：发布前跑一次，命中即先清洗再打包。
> **扫描范围必须包含备份库**——否则"主库干净"会掩盖旧副本里的历史内容。

## 附录 B：为什么身份必须是「目录」而不是「哈希」

| 失败模式 | 后果 | 是否可观测 |
| --- | --- | --- |
| `d:\` 与 `D:\` 算出不同 hash | 记忆分裂成两份 | 否 |
| 经 junction / 8.3 短名访问同一项目 | 记忆分裂 | 否 |
| 项目改名或移动 | 旧库失联 | 否，除非有反查表 |
| 两个项目需要同一个库 | 表达不了 | — |

A2 之后，"改名"降级为令牌内一个字段过期（`bind move` 一条命令），
另两条由 `normcase(realpath())` 比较消除。哈希从头到尾不参与身份判断。
