-- ============================================================
-- HumanityMemory schema v3 —— 陈述 / 经历 / 日志 三层
--
--   信条层   CORE_RULES.md  → 不进库，静态注入
--   陈述层   knowledge      → kind = knowledge（知识）| state（现状）
--   经历层   episodes       → kind = case（案例）| narrative（叙述）
--   日志层   logs           → 当天原始记录，严格 append-only
--
-- 为什么陈述层要分 kind：
--   「知识」离开具体实例仍成立，靠被使用保值，不该因为一次修复而失效；
--   「现状」当前为真但会随版本变化，必须能失效、且 TTL 很短。
--   两者的遗忘曲线差一个数量级，混在一起时失效机制永远等不到触发条件。
--
-- 同理，经历层的 case 是压到三句话的定论（短、高优先级），
-- narrative 是保留过程的叙述（可随时间压缩）。
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
-- 陈述层：一句话陈述。
--   kind='knowledge' 知识：可迁移的机制/约定，遗忘极慢，不参与失效
--   kind='state'     现状：版本、依赖、路径、资源配置，30 天不确认即归档
-- 失效采用「追加式」：置 invalid_at，不删除、不改正文。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    kind             TEXT    NOT NULL DEFAULT 'knowledge'
                     CHECK (kind IN ('knowledge', 'state')),
    content          TEXT    NOT NULL,
    tags             TEXT    NOT NULL DEFAULT '[]',
    importance       REAL    NOT NULL DEFAULT 0.8,
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
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_knowledge_live     ON knowledge(invalid_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind     ON knowledge(kind);
CREATE INDEX IF NOT EXISTS idx_knowledge_created  ON knowledge(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedded ON knowledge(embedded) WHERE embedded = 0;

-- ------------------------------------------------------------
-- 经历层：一次具体事件，按天聚合。
--   kind='case'      案例：现象→根因→落点，压到 300 字内，检索权重高于叙述
--   kind='narrative' 叙述：保留试错与判断转折，随时间逐级压缩
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    kind             TEXT    NOT NULL DEFAULT 'narrative'
                     CHECK (kind IN ('case', 'narrative')),
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
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_episodes_day      ON episodes(day DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_at       ON episodes(at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_kind     ON episodes(kind);
CREATE INDEX IF NOT EXISTS idx_episodes_embedded ON episodes(embedded) WHERE embedded = 0;

-- ------------------------------------------------------------
-- 日志层：当天原始记录。**严格 append-only**。
-- 唯一允许的更新是把 consolidated 从 0 置 1。
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
    meta         TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_logs_day      ON logs(day DESC, at);
CREATE INDEX IF NOT EXISTS idx_logs_pending  ON logs(consolidated, day);
CREATE INDEX IF NOT EXISTS idx_logs_embedded ON logs(embedded) WHERE embedded = 0;

-- ------------------------------------------------------------
-- 复盘记录：每次 consolidate 的审计
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

-- 失效是单向的，不能被"复活"
CREATE TRIGGER IF NOT EXISTS knowledge_no_revive
BEFORE UPDATE ON knowledge
WHEN OLD.invalid_at IS NOT NULL AND NEW.invalid_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'knowledge 的失效是单向的：已失效条目不能被复活，请写入新条目');
END;

-- 正文一经写入即不可改（篡改历史应改为追加新条目）
CREATE TRIGGER IF NOT EXISTS knowledge_content_immutable
BEFORE UPDATE OF content ON knowledge
WHEN OLD.content <> NEW.content
BEGIN
    SELECT RAISE(ABORT, 'knowledge 正文不可修改：请追加新条目并使旧条目失效');
END;

-- 知识不该被"失效"——它只会过时，不会变假。
-- state 才是需要 invalidates 的那一类，这条约束把语义钉在数据库层。
CREATE TRIGGER IF NOT EXISTS knowledge_kind_immutable
BEFORE UPDATE OF kind ON knowledge
WHEN OLD.kind <> NEW.kind
BEGIN
    SELECT RAISE(ABORT, 'kind 不可修改：knowledge 与 state 的生命周期不同，请写入新条目');
END;

-- 案例与叙述的性质不同，同理不允许改 kind
CREATE TRIGGER IF NOT EXISTS episodes_kind_immutable
BEFORE UPDATE OF kind ON episodes
WHEN OLD.kind <> NEW.kind
BEGIN
    SELECT RAISE(ABORT, 'kind 不可修改：请新建条目');
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
