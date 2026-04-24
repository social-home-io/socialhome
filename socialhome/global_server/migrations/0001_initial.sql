-- GFS schema — Global Federation Server (spec §24.6).
--
-- Greenfield pre-release: this one file carries the complete schema.
-- Rows in `server_config` override TOML values for admin-editable keys.

CREATE TABLE IF NOT EXISTS server_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Admin-editable policy + branding defaults (rows win over TOML at runtime).
INSERT OR IGNORE INTO server_config(key, value) VALUES('server_name',       'My Global Server');
INSERT OR IGNORE INTO server_config(key, value) VALUES('landing_markdown',  '');
INSERT OR IGNORE INTO server_config(key, value) VALUES('header_image_file', '');
INSERT OR IGNORE INTO server_config(key, value) VALUES('auto_accept_clients','1');
INSERT OR IGNORE INTO server_config(key, value) VALUES('auto_accept_spaces', '0');
INSERT OR IGNORE INTO server_config(key, value) VALUES('fraud_threshold',    '5');
-- admin_password_hash row is created when the operator runs --set-password.

-- ── Client instances (spec §24.6) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS client_instances (
    instance_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    public_key   TEXT NOT NULL,
    inbox_url    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','active','banned')),
    auto_accept  INTEGER NOT NULL DEFAULT 0,
    connected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Global spaces (spec §24.6) ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS global_spaces (
    space_id         TEXT PRIMARY KEY,
    owning_instance  TEXT NOT NULL REFERENCES client_instances(instance_id),
    name             TEXT NOT NULL DEFAULT '',
    description      TEXT,
    about_markdown   TEXT,
    cover_url        TEXT,
    min_age          INTEGER NOT NULL DEFAULT 0,
    target_audience  TEXT NOT NULL DEFAULT 'all',
    accent_color     TEXT NOT NULL DEFAULT '#6366f1',
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','active','banned')),
    subscriber_count INTEGER NOT NULL DEFAULT 0,
    posts_per_week   REAL NOT NULL DEFAULT 0,
    published_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Space ↔ subscriber instance bridge ────────────────────────────────────

CREATE TABLE IF NOT EXISTS space_subscribers (
    space_id    TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_space_subscribers ON space_subscribers(space_id);
CREATE INDEX IF NOT EXISTS idx_instance_spaces   ON space_subscribers(instance_id);

-- ── Transport modes per client instance ───────────────────────────────────

CREATE TABLE IF NOT EXISTS rtc_connections (
    instance_id   TEXT PRIMARY KEY REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    transport     TEXT NOT NULL CHECK(transport IN ('webrtc','https')),
    connected_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_ping_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Per-space instance bans (survives GFS_SPACE_JOIN retries) ─────────────

CREATE TABLE IF NOT EXISTS space_instance_bans (
    space_id    TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL,
    banned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    reason      TEXT,
    PRIMARY KEY (space_id, instance_id)
);

-- ── Fraud reports from household admins (new for GFS moderation) ──────────

CREATE TABLE IF NOT EXISTS gfs_fraud_reports (
    id                   TEXT PRIMARY KEY,
    target_type          TEXT NOT NULL CHECK(target_type IN ('space','instance')),
    target_id            TEXT NOT NULL,
    category             TEXT NOT NULL
                         CHECK(category IN ('spam','harassment',
                                            'inappropriate','misinformation',
                                            'illegal','other')),
    notes                TEXT,
    reporter_instance_id TEXT NOT NULL,
    reporter_user_id     TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','dismissed','acted')),
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    reviewed_by          TEXT,
    reviewed_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gfs_fraud_target
    ON gfs_fraud_reports(target_type, target_id, status);
CREATE INDEX IF NOT EXISTS idx_gfs_fraud_status
    ON gfs_fraud_reports(status, created_at DESC);
-- One reporter / one target — replays collapse.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gfs_fraud_reporter_target
    ON gfs_fraud_reports(reporter_instance_id, target_type, target_id);

-- ── Admin portal: sessions + brute-force tracking (spec §24.9) ────────────

CREATE TABLE IF NOT EXISTS admin_sessions (
    token       TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    ip           TEXT NOT NULL,
    attempted_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
    ON admin_login_attempts(ip, attempted_at DESC);

-- ── Admin audit log (spec §24.9.10) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    admin_ip      TEXT,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created
    ON admin_audit_log(created_at DESC);

-- ── Appeals from banned households (spec §24.9 appeal flow) ───────────────

CREATE TABLE IF NOT EXISTS gfs_appeals (
    id           TEXT PRIMARY KEY,
    target_type  TEXT NOT NULL CHECK(target_type IN ('space','instance')),
    target_id    TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','lifted','dismissed')),
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    decided_at   INTEGER,
    decided_by   TEXT
);

-- ── Public pairing tokens (spec §24.7.4) ──────────────────────────────────

CREATE TABLE IF NOT EXISTS gfs_pair_tokens (
    token        TEXT PRIMARY KEY,
    ip           TEXT NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    consumed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pair_tokens_ip
    ON gfs_pair_tokens(ip, created_at DESC);

-- ── Invite tokens (spec §24.8.5) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gfs_invite_tokens (
    gfs_token           TEXT PRIMARY KEY,
    space_id            TEXT NOT NULL REFERENCES global_spaces(space_id) ON DELETE CASCADE,
    source_instance_id  TEXT NOT NULL,
    max_uses            INTEGER NOT NULL DEFAULT 1,
    uses                INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    expires_at          INTEGER
);

-- ── Cluster nodes (spec §24.10) ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id              TEXT PRIMARY KEY,
    url                  TEXT NOT NULL,
    public_key           TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'unknown'
                         CHECK(status IN ('online','offline','syncing','unknown')),
    last_seen            TEXT,
    added_at             TEXT NOT NULL DEFAULT (datetime('now')),
    active_sync_sessions INTEGER NOT NULL DEFAULT 0
);
