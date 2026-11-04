-- ============================================================
-- HumanityMemory schema v2 —— 三层仿生记忆
--
--   信条层  CORE_RULES.md   → 不进库，静态注入
--   事实层  facts           → 长期，遗忘极慢，追加式失效
--   经历层  episodes        → 长期，复盘后的压缩摘要
--   日志层  logs            → 当天原始记录，严格 append-only
--
-- 向量表 *_vec 由 db.py 动态创建（维度来自配置，cosine 度量）
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
-- 事实层：简短精确的陈述句。遗忘极慢，不参与压缩。
-- 失效采用「追加式」：置 invalid_at，不删除、不改正文。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    rowid            INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
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
    embedded         INTEGER NOT NULL DEFAULT 0,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_facts_live     ON facts(invalid_at);
CREATE INDEX IF NOT EXISTS idx_facts_created  ON facts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_facts_embedded ON facts(embedded) WHERE embedded = 0;

-- ------------------------------------------------------------
-- 经历层：复盘产出的压缩叙述。按天聚合，保留事件时间。
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
    embedded         INTEGER NOT NULL DEFAULT 0,
    meta             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_episodes_day      ON episodes(day DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_at       ON episodes(at DESC);
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
    fact_count    INTEGER NOT NULL DEFAULT 0,
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

-- 事实失效是单向的，不能被"复活"
CREATE TRIGGER IF NOT EXISTS facts_no_revive
BEFORE UPDATE ON facts
WHEN OLD.invalid_at IS NOT NULL AND NEW.invalid_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'facts 的失效是单向的：已失效的事实不能被复活，请写入新事实');
END;

-- 事实正文一经写入即不可改（篡改历史应改为追加新事实）
CREATE TRIGGER IF NOT EXISTS facts_content_immutable
BEFORE UPDATE OF content ON facts
WHEN OLD.content <> NEW.content
BEGIN
    SELECT RAISE(ABORT, 'facts 正文不可修改：请追加新事实并使旧事实失效');
END;

-- ============================================================
-- 全文索引（trigram，中文子串友好）
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, tags,
    content = 'facts', content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS facts_fts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_au AFTER UPDATE OF content, tags ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
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
