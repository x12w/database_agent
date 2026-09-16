-- 0001_init.sql —— 初始 schema。
--
-- 取代三处散落的持久化：进程内 sessions dict（app/main.py）、
-- data/profiles.json、data/clarifications/*.json、data/explorations/*.json。
--
-- 时间戳统一用 UTC 的 'YYYY-MM-DD HH:MM:SS' 文本：与历史 JSON 完全同格式，
-- 迁移时不需要转换，且字典序 == 时间序，可以直接 ORDER BY。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ---- 会话 ----
-- 历史实现只有一个内存 dict，超过 100 个会话就按"历史最短"淘汰，
-- 且每个会话的历史被砍到 20 条。现在会话是持久对象：可列举、可重开、消息不丢。
CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    profile_id   TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- JSON 数组或 NULL；限制本次会话只能访问哪些表。
    table_filter TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_profile
    ON conversations (profile_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations (updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    -- 会话内序号，从 1 开始；顺序不依赖自增 id，导入时也能保序。
    seq             INTEGER NOT NULL,
    UNIQUE (conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, seq);

-- ---- 连接配置 ----
-- 镜像 DatabaseProfile；kind 存 DialectKind 的值。
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '127.0.0.1',
    port        INTEGER NOT NULL DEFAULT 3306,
    user        TEXT NOT NULL DEFAULT 'root',
    password    TEXT NOT NULL DEFAULT '',
    database    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'mysql',
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ---- 管理员问答 ----
-- 主键是 (profile_id, question_id) 而不是 question_id：历史数据按 profile
-- 分文件存放，同一个 question_id 理论上可以出现在两个文件里，用单列主键
-- 会让其中一条在迁移时被静默吞掉 —— 那正是我们要避免的降级。
CREATE TABLE IF NOT EXISTS clarifications (
    question_id TEXT NOT NULL,
    profile_id  TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    column_name TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL,
    -- agent 提问时给出的猜测，管理员确认/纠正它。
    agent_guess TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | answered | ignored
    -- JSON 数组：抽样值。
    samples     TEXT,
    -- JSON 对象：其它上下文（列类型、命中原因等），历史数据可能为空。
    context     TEXT,
    created_at  TEXT NOT NULL,
    answered_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (profile_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_clarifications_status
    ON clarifications (profile_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clarifications_table
    ON clarifications (profile_id, table_name);

-- ---- 探索报告 ----
-- 一个 profile 一份（upsert），对应历史上的 data/explorations/{id}.json。
CREATE TABLE IF NOT EXISTS explorations (
    profile_id TEXT PRIMARY KEY,
    report     TEXT NOT NULL DEFAULT '',
    candidates TEXT,  -- JSON 数组：待确认的编码列候选
    created_at TEXT NOT NULL
);
