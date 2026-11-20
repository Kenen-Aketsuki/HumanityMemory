-- ============================================================
-- HumanityMemory schema v4 —— 经历 / 经验 两类 + 日志
--
--   信条层   CORE_RULES.md  → 不进库，静态注入
--   经验     knowledge      → 可复用的判定与结论（不会压，会被推翻）
--   经历     episodes       → 发生过的事，一条一个单一事务（会压缩）
--   日志层   logs           → 当天原始记录，严格 append-only
--
-- 「两类」是**表级**的事实：一张表一类，类型由表承载，没有行级 kind。
-- 所以按类型检索就是选表，不存在第二层过滤。
--
-- 相对历史 schema 的改动（旧库一律拒绝，迁移走 `humanity-memory upgrade`）：
--   1. 删掉 knowledge.kind 与 episodes.kind —— 旧的四类
--      （knowledge / state / case / narrative）不再成立。
--   2. 删掉 state 专用的 state_interval_days 与复核梯子：
--      易变性改由**可信度**表达（易变的给低初值，被读到就被要求先验）。
--   3. 表名保持 knowledge / episodes / logs 不变：只删了两列，
--      没有改表、没有搬数据。旧库里多余的 kind / state_interval_days
--      列留着不影响读写（INSERT 省略它们，走 DEFAULT）。
--   4. 补齐评分维度 forget_level / confidence / activity / period_start /
--      revive_hits / level0_since 与 memory_events 审计表。
--
-- 为什么不再分知识的子类：
--   旧的四类里，「知识 vs 现状」的差别本质是「它多容易变」。
--   用离散类别去表达连续属性，判据只能写在 prompt 里靠模型自觉；
--   改成初始可信度后，易变的条目自己会被反复验证纠正，
--   不需要一个永远不会被维护的标签。
--
-- 向量表 *_vec 由 db.py 动态创建（维度与 chunk_size 来自配置）
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- 键值元信息：schema 版本、向量维度、扩展来源等
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ------------------------------------------------------------
-- 经验：可复用的判定与结论。
--   时间戳不是它的必要属性（「项目用 HybridCLR」与哪天写下无关），
--   所以三类时间分开存：created_at（登记）、valid_at（开始成立）、verified_at（最后一次核对）。
-- 失效采用「追加式」：置 invalid_at，不删除、不改正文。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    content          TEXT    NOT NULL,
    tags             TEXT    NOT NULL DEFAULT '[]',
    importance       REAL    NOT NULL DEFAULT 0.75,
    scope            TEXT    NOT NULL DEFAULT 'workspace',
    source           TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    last_accessed_at INTEGER,
    access_count     INTEGER NOT NULL DEFAULT 0,
    valid_at         INTEGER,
    invalid_at       INTEGER,
    invalidated_by   TEXT,
    verified_at      INTEGER,
    archived_at      INTEGER,
    embedded         INTEGER NOT NULL DEFAULT 0,
    -- ---- 三个评分维度 ----
    forget_level     INTEGER NOT NULL DEFAULT 3,
    confidence       REAL    NOT NULL DEFAULT 0.3,
    activity         REAL    NOT NULL DEFAULT 0,
    period_start     INTEGER,
    level0_since     INTEGER,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_knowledge_live     ON knowledge(invalid_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_created  ON knowledge(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedded ON knowledge(embedded) WHERE embedded = 0;
CREATE INDEX IF NOT EXISTS idx_knowledge_archived ON knowledge(archived_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_forget   ON knowledge(forget_level);

-- ------------------------------------------------------------
-- 经历：发生过的事，一条一个单一事务。
--   at / content 分列存储 —— 「什么时候」是它的主键性属性，压缩也不许动 at。
--   可信度不在此表：经历不谈真假，它的可信度是 forget_level 的派生值，
--   落一个没人维护的冗余字段只会和事实脱节。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    content          TEXT    NOT NULL,
    at               INTEGER NOT NULL,
    day              TEXT    NOT NULL,
    tags             TEXT    NOT NULL DEFAULT '[]',
    importance       REAL    NOT NULL DEFAULT 0.4,
    access_count     INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    last_accessed_at INTEGER,
    digest_id        TEXT,
    archived_at      INTEGER,
    compression_level INTEGER NOT NULL DEFAULT 0,
    embedded         INTEGER NOT NULL DEFAULT 0,
    forget_level     INTEGER NOT NULL DEFAULT 3,
    activity         REAL    NOT NULL DEFAULT 0,
    period_start     INTEGER,
    revive_hits      INTEGER NOT NULL DEFAULT 0,
    level0_since     INTEGER,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_episodes_day      ON episodes(day DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_at       ON episodes(at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_embedded ON episodes(embedded) WHERE embedded = 0;
CREATE INDEX IF NOT EXISTS idx_episodes_archived ON episodes(archived_at);
CREATE INDEX IF NOT EXISTS idx_episodes_forget   ON episodes(forget_level);

-- ------------------------------------------------------------
-- 日志层：当天原始记录。**严格 append-only**（内容/时间/日期不可改）。
-- 允许更新：consolidated、retrievals、access_count、last_accessed_at。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs (
    rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    at           INTEGER NOT NULL,
    day          TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    tags         TEXT    NOT NULL DEFAULT '[]',
    session_id   TEXT,
    importance   REAL    NOT NULL DEFAULT 0.3,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at INTEGER,
    consolidated INTEGER NOT NULL DEFAULT 0,
    digest_id    TEXT,
    embedded     INTEGER NOT NULL DEFAULT 0,
    meta         TEXT    NOT NULL DEFAULT '{}',
    -- 被 recall 命中的检索条件流水 [{q,n,sim,heat,at}]，复盘时派生继承值
    retrievals   TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_logs_day      ON logs(day DESC, at);
CREATE INDEX IF NOT EXISTS idx_logs_pending  ON logs(consolidated, day);
CREATE INDEX IF NOT EXISTS idx_logs_embedded ON logs(embedded) WHERE embedded = 0;

-- ------------------------------------------------------------
-- 复盘记录：每次 consolidate 的审计
--   knowledge_count 是历史列名（表名没改，列名跟着没改）——
--   它数的是"这一轮落了几条经验"。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digests (
    id            TEXT PRIMARY KEY,
    day           TEXT    NOT NULL,
    at            INTEGER NOT NULL,
    log_count     INTEGER NOT NULL DEFAULT 0,
    knowledge_count INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    purged        INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    meta          TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_digests_day ON digests(day DESC);

-- ------------------------------------------------------------
-- 记忆事件审计
--
-- 为什么单独一张表而不是塞进 meta：等级与可信度是**随时间反复变动**的量，
-- 塞 JSON 里就只能看到"现在是多少"，答不出"什么时候、因为什么变的"。
-- 而后者才是三个月后回头审视「这条为什么被降级」时真正要的依据。
--
-- 只追加，不做 UPDATE / DELETE（触发器兜底）。
--   event 取值：
--     verify       验证通过（可信度上升）
--     contradict   被证伪（可信度下降）
--     level_up / level_down   周期裁定升降级
--     activity_clear          周期末活跃度清零
--     compress     经历压缩（compression_level 变化）
--     revive       0 级被深度回忆复活
--     archive / unarchive     归档状态变更
--     purge        删除候选被执行
--     write / invalidate      写入与失效
--
-- table_name 存**物理表名**，不写"经验"这种类型名：审计是给三个月后
-- grep 用的，要能直接对上下面的表。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_events (
    rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    table_name TEXT    NOT NULL
               CHECK (table_name IN ('knowledge', 'episodes', 'logs')),
    item_id    TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    from_value TEXT,
    to_value   TEXT,
    delta      REAL,
    reason     TEXT,
    meta       TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_item ON memory_events(table_name, item_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_events_at   ON memory_events(at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON memory_events(event, at DESC);

-- 事件表只追加：审计一旦能被改，就不再是审计
CREATE TRIGGER IF NOT EXISTS memory_events_append_only
BEFORE UPDATE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events 是只追加的审计表：禁止修改历史事件');
END;

CREATE TRIGGER IF NOT EXISTS memory_events_no_delete
BEFORE DELETE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events 是只追加的审计表：禁止删除历史事件');
END;

-- ============================================================
-- 硬约束（数据库层，不依赖应用层自觉）
-- ============================================================

-- 日志只追加：内容/时间/日期一旦写入不可改
CREATE TRIGGER IF NOT EXISTS logs_append_only
BEFORE UPDATE ON logs
WHEN OLD.content <> NEW.content
  OR OLD.at      <> NEW.at
  OR OLD.day     <> NEW.day
  OR OLD.id      <> NEW.id
BEGIN
    SELECT RAISE(ABORT, 'logs 表是 append-only：禁止修改已写入的历史内容');
END;

-- 未复盘的日志不可删除
CREATE TRIGGER IF NOT EXISTS logs_no_delete_unconsolidated
BEFORE DELETE ON logs
WHEN OLD.consolidated = 0
BEGIN
    SELECT RAISE(ABORT, '未复盘的日志不可删除，请先执行 consolidate');
END;

-- 失效是单向的，不能被"复活"（复活指的是遗忘层复活，不是这里）
CREATE TRIGGER IF NOT EXISTS knowledge_no_revive
BEFORE UPDATE ON knowledge
WHEN OLD.invalid_at IS NOT NULL AND NEW.invalid_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'knowledge 的失效是单向的：已失效条目不能被复活，请写入新条目');
END;

-- 正文一经写入即不可改（经验不压缩，篡改历史应改为追加新条目并置旧条失效）
CREATE TRIGGER IF NOT EXISTS knowledge_content_immutable
BEFORE UPDATE OF content ON knowledge
WHEN OLD.content <> NEW.content
BEGIN
    SELECT RAISE(ABORT, 'knowledge 正文不可修改：请追加新条目并使旧条目失效');
END;

-- ============================================================
-- 全文索引（trigram，中文子串友好）
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, tags,
    content = 'knowledge', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE OF content, tags ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO knowledge_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    content, tags,
    content = 'episodes', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE OF content, tags ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO episodes_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
    content, tags,
    content = 'logs', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS logs_fts_ai AFTER INSERT ON logs BEGIN
    INSERT INTO logs_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS logs_fts_ad AFTER DELETE ON logs BEGIN
    INSERT INTO logs_fts(logs_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
