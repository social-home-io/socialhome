-- ============================================================================
-- Social Home — v1.0 initial schema
-- ----------------------------------------------------------------------------
-- Greenfield launch. Everything the application needs at v1 is created here.
-- Later versions add incremental migrations on top (e.g. 0002_add_foo.sql).
--
-- Conventions:
--   * ``CREATE TABLE IF NOT EXISTS`` everywhere so re-running is a no-op.
--   * TEXT columns hold ISO-8601 UTC timestamps unless otherwise noted.
--   * GPS coordinates are stored already-truncated to 4dp.
--   * user_id / instance_id are 32-character base32 strings derived from
--     an Ed25519 public key (§4.1.2 / §4.1.3). They are NEVER reassigned.
--   * Foreign keys reference the *public* key (user_id, instance_id, id),
--     never an integer surrogate, so that federation events can carry the
--     same key across instance boundaries.
--   * JSON columns are stored as TEXT and validated at the service layer.
-- ============================================================================

-- ── Identity ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS instance_identity (
    id                    TEXT PRIMARY KEY DEFAULT 'self' CHECK(id = 'self'),
    instance_id           TEXT NOT NULL UNIQUE,
    display_name          TEXT NOT NULL DEFAULT 'My Home',
    identity_private_key  TEXT NOT NULL,       -- Ed25519 seed, KEK-encrypted
    identity_public_key   TEXT NOT NULL,       -- 64 hex chars
    key_format            TEXT NOT NULL DEFAULT 'encrypted'
                          CHECK(key_format IN ('encrypted')),
    -- Post-quantum identity material (§25.8 migration path). NULL when this
    -- instance runs on the classical sig_suite = 'ed25519'. Populated by
    -- identity_bootstrap when federation_sig_suite = 'ed25519+mldsa65'.
    pq_algorithm          TEXT,                -- 'mldsa65' | NULL
    pq_private_key        TEXT,                -- KEK-encrypted ML-DSA-65 seed
    pq_public_key         TEXT,                -- hex
    home_lat              REAL,                -- 4dp-truncated
    home_lon              REAL,
    home_label            TEXT,
    routing_secret        TEXT NOT NULL,       -- 32 random bytes, hex; never transmitted
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instance_config (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- ── Users ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    username                   TEXT PRIMARY KEY,
    user_id                    TEXT NOT NULL UNIQUE,
    display_name               TEXT NOT NULL,
    is_admin                   INTEGER NOT NULL DEFAULT 0,
    -- Short hex digest of the current profile picture (or NULL). The
    -- bytes live in user_profile_pictures; this column exists purely so
    -- client URLs like /api/users/{id}/picture?v=<hash> can cache-bust
    -- without a join on every list query.
    picture_hash               TEXT,
    state                      TEXT NOT NULL DEFAULT 'active'
                               CHECK(state IN ('active','inactive')),
    bio                        TEXT,
    locale                     TEXT,
    theme                      TEXT NOT NULL DEFAULT 'auto'
                               CHECK(theme IN ('light','dark','auto')),
    emoji_skin_tone_default    TEXT,
    status_emoji               TEXT,
    status_text                TEXT,
    status_expires_at          TEXT,
    public_key                 TEXT,
    public_key_version         INTEGER NOT NULL DEFAULT 0,
    is_new_member              INTEGER NOT NULL DEFAULT 1,
    preferences_json           TEXT NOT NULL DEFAULT '{}',
    onboarding_complete        INTEGER NOT NULL DEFAULT 0,
    -- Child Protection (§CP) — never exposed in API responses
    is_minor                   INTEGER NOT NULL DEFAULT 0,
    child_protection_enabled   INTEGER NOT NULL DEFAULT 0,
    date_of_birth              TEXT,
    declared_age               INTEGER,
    -- Sensitive PII — never federated
    email                      TEXT,
    phone                      TEXT,
    -- Soft delete (§23.56)
    deleted_at                 TEXT,
    grace_until                TEXT,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    -- Where the row came from: manually provisioned vs synced from the
    -- Home Assistant person.* registry. Admins manage 'ha' rows via the
    -- HA Users admin panel; 'manual' rows come from standalone mode or
    -- explicit admin creates.
    source                     TEXT NOT NULL DEFAULT 'manual'
                               CHECK(source IN ('manual','ha'))
);
CREATE INDEX IF NOT EXISTS idx_users_user_id     ON users(user_id);
CREATE INDEX IF NOT EXISTS idx_users_source      ON users(source);
CREATE INDEX IF NOT EXISTS idx_users_state       ON users(state);

-- ── Profile pictures (§23) ──────────────────────────────────────────────
-- One row = one household-level profile picture. Bytes are the WebP
-- output of ImageProcessor.generate_thumbnail at ≤256×256 (~20–30 KB).
-- Separate table keeps SELECT * on `users` cheap; the parent row
-- carries only the hash for cache-busting + federation event payloads.
CREATE TABLE IF NOT EXISTS user_profile_pictures (
    user_id      TEXT PRIMARY KEY,
    bytes_webp   BLOB NOT NULL,
    hash         TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS remote_users (
    user_id          TEXT PRIMARY KEY,
    instance_id      TEXT NOT NULL REFERENCES remote_instances(id) ON DELETE CASCADE,
    remote_username  TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    alias            TEXT,
    visible_to       TEXT NOT NULL DEFAULT '"all"',
    -- Same cache-busting semantics as users.picture_hash. Bytes live in
    -- user_profile_pictures (keyed by user_id, shared table).
    picture_hash     TEXT,
    bio              TEXT,
    status_json      TEXT,
    -- Set when a USER_REMOVED federation event lands; downstream queries
    -- (member lists, autocompletion) filter the row out. The row itself is
    -- kept so historical posts can still show the display name.
    deprovisioned_at TEXT,
    public_key       TEXT,
    public_key_version INTEGER NOT NULL DEFAULT 0,
    synced_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(instance_id, remote_username)
);
CREATE INDEX IF NOT EXISTS idx_remote_users_instance ON remote_users(instance_id);

-- ── Auth: API tokens ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT,
    last_used_at  TEXT,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

-- Standalone-mode auth (§platform/standalone.py). Empty in HA mode.
CREATE TABLE IF NOT EXISTS platform_users (
    username         TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    picture_url      TEXT,
    is_admin         INTEGER NOT NULL DEFAULT 0,
    email            TEXT,
    notify_endpoint  TEXT,
    password_hash    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS platform_tokens (
    token_id      TEXT PRIMARY KEY,
    username      TEXT NOT NULL REFERENCES platform_users(username) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT,
    last_used_at  TEXT
);

-- ── Federation: peers, pairing, outbox, replay ──────────────────────────────

CREATE TABLE IF NOT EXISTS remote_instances (
    id                    TEXT PRIMARY KEY,        -- instance_id
    display_name          TEXT NOT NULL,
    remote_identity_pk    TEXT NOT NULL,
    key_self_to_remote    TEXT NOT NULL,           -- KEK-encrypted AES-GCM session key
    key_remote_to_self    TEXT NOT NULL,
    remote_inbox_url      TEXT NOT NULL,
    local_inbox_id        TEXT NOT NULL UNIQUE,
    status                TEXT NOT NULL DEFAULT 'confirmed'
                          CHECK(status IN ('pending_sent','pending_received',
                                           'confirmed','unpairing')),
    source                TEXT NOT NULL DEFAULT 'manual'
                          CHECK(source IN ('manual','space_session')),
    proto_version         INTEGER NOT NULL DEFAULT 1,
    -- Post-quantum identity of the peer, learned during pairing. NULL
    -- when the peer doesn't advertise a PQ key (pair runs classical).
    remote_pq_algorithm   TEXT,                    -- 'mldsa65' | NULL
    remote_pq_identity_pk TEXT,                    -- hex
    -- Per-peer negotiated wire suite. Pairing picks the intersection of
    -- both sides' supported suites; stays fixed for the life of the pair.
    sig_suite             TEXT NOT NULL DEFAULT 'ed25519'
                          CHECK(sig_suite IN ('ed25519','ed25519+mldsa65')),
    intro_relay_enabled   INTEGER NOT NULL DEFAULT 1,
    relay_via             TEXT,                    -- introducer instance_id
    home_lat              REAL,
    home_lon              REAL,
    paired_at             TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    last_reachable_at     TEXT,
    unreachable_since     TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_instances_status ON remote_instances(status);

CREATE TABLE IF NOT EXISTS pending_pairings (
    token               TEXT PRIMARY KEY,
    own_identity_pk     TEXT NOT NULL,
    own_dh_pk           TEXT NOT NULL,
    own_dh_sk           TEXT NOT NULL,             -- KEK-encrypted until confirmed
    peer_identity_pk    TEXT,
    peer_dh_pk          TEXT,
    peer_inbox_url      TEXT,
    inbox_url           TEXT NOT NULL,
    own_local_inbox_id  TEXT NOT NULL,
    verification_code   TEXT,
    intro_note          TEXT,
    relay_via           TEXT,
    status              TEXT NOT NULL DEFAULT 'pending_sent'
                        CHECK(status IN ('pending_sent','pending_received',
                                         'confirmed','unpairing')),
    issued_at           TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT NOT NULL
);

-- §11.9 — admin-pending PAIRING_INTRO_RELAY requests. Persisted so a
-- restart doesn't lose admin queue state.
CREATE TABLE IF NOT EXISTS pairing_relay (
    id                  TEXT PRIMARY KEY,
    from_instance       TEXT NOT NULL,
    target_instance_id  TEXT NOT NULL,
    message             TEXT NOT NULL,
    received_at         TEXT NOT NULL DEFAULT (datetime('now')),
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','declined'))
);
CREATE INDEX IF NOT EXISTS idx_pairing_relay_received_at
    ON pairing_relay(received_at);
CREATE INDEX IF NOT EXISTS idx_pairing_relay_status
    ON pairing_relay(status);

CREATE TABLE IF NOT EXISTS federation_outbox (
    id              TEXT PRIMARY KEY,              -- msg_id
    instance_id     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    authority_json  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','delivered','failed')),
    -- Retention window (§4.4.7):
    --   NULL  = structural / security-critical events: never expire
    --   ISO   = regular events: +7 days from created_at
    expires_at      TEXT,
    delivered_at    TEXT,
    failed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_federation_outbox_due
    ON federation_outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS federation_replay_cache (
    msg_id      TEXT PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_federation_replay_received
    ON federation_replay_cache(received_at);

-- ── Household features + presence + post drafts ────────────────────────────

CREATE TABLE IF NOT EXISTS household_features (
    id                TEXT PRIMARY KEY DEFAULT 'default' CHECK(id = 'default'),
    household_name    TEXT NOT NULL DEFAULT 'Home',
    feat_feed         INTEGER NOT NULL DEFAULT 1,
    feat_pages        INTEGER NOT NULL DEFAULT 1,
    feat_tasks        INTEGER NOT NULL DEFAULT 1,
    feat_stickies     INTEGER NOT NULL DEFAULT 1,
    feat_calendar     INTEGER NOT NULL DEFAULT 1,
    feat_bazaar       INTEGER NOT NULL DEFAULT 1,
    allow_text        INTEGER NOT NULL DEFAULT 1,
    allow_image       INTEGER NOT NULL DEFAULT 1,
    allow_video       INTEGER NOT NULL DEFAULT 1,
    allow_file        INTEGER NOT NULL DEFAULT 1,
    allow_poll        INTEGER NOT NULL DEFAULT 1,
    allow_schedule    INTEGER NOT NULL DEFAULT 1,
    allow_bazaar      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS presence (
    username           TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
    entity_id          TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'unavailable'
                       CHECK(state IN ('home','zone','away','unavailable')),
    zone_name          TEXT,
    latitude           REAL,          -- 4dp-truncated
    longitude          REAL,
    gps_accuracy_m     REAL,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Remote-instance presence — federation PRESENCE_UPDATED events land
-- here. Deliberately FK-free on ``from_instance`` + ``remote_username``
-- so it works even before a USERS_SYNC populates :table:`users`.
CREATE TABLE IF NOT EXISTS remote_presence (
    from_instance   TEXT NOT NULL,
    remote_username TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'unavailable'
                    CHECK(state IN ('home','zone','away','unavailable')),
    zone_name       TEXT,
    latitude        REAL,          -- 4dp-truncated
    longitude       REAL,
    gps_accuracy_m  REAL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (from_instance, remote_username)
);
CREATE INDEX IF NOT EXISTS idx_remote_presence_updated
    ON remote_presence(updated_at DESC);

CREATE TABLE IF NOT EXISTS post_drafts (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    context      TEXT NOT NULL,   -- 'household_feed' | <space_id> | <page_id> | <conv_id>
    type         TEXT NOT NULL DEFAULT 'text',
    content      TEXT NOT NULL DEFAULT '',
    media_url    TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_post_drafts_user_ctx
    ON post_drafts(username, context);

-- ── Feed posts (household) ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS feed_posts (
    id              TEXT PRIMARY KEY,
    author          TEXT NOT NULL,                 -- user_id
    type            TEXT NOT NULL
                    CHECK(type IN ('text','image','video','transcript',
                                   'poll','schedule','file','bazaar')),
    content         TEXT,
    media_url       TEXT,
    reactions       TEXT NOT NULL DEFAULT '{}',    -- JSON {emoji: [user_id...]}
    comment_count   INTEGER NOT NULL DEFAULT 0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    deleted         INTEGER NOT NULL DEFAULT 0,
    edited_at       TEXT,
    no_link_preview INTEGER NOT NULL DEFAULT 0,
    moderated       INTEGER NOT NULL DEFAULT 0,
    file_meta_json  TEXT,                          -- JSON FileMeta when type=file
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feed_posts_created ON feed_posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_posts_author  ON feed_posts(author);

CREATE TABLE IF NOT EXISTS post_comments (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES feed_posts(id) ON DELETE CASCADE,
    parent_id   TEXT REFERENCES post_comments(id) ON DELETE CASCADE,
    author      TEXT NOT NULL,                     -- username (household-local)
    type        TEXT NOT NULL DEFAULT 'text'
                CHECK(type IN ('text','image')),
    content     TEXT,
    media_url   TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0,
    edited_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_post_comments_post ON post_comments(post_id);

CREATE TABLE IF NOT EXISTS saved_posts (
    user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    post_id   TEXT NOT NULL,
    saved_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

CREATE TABLE IF NOT EXISTS feed_read_positions (
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    last_read_post_id TEXT,
    last_read_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id)
);

-- ── Spaces ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS spaces (
    id                     TEXT PRIMARY KEY,           -- derived from space identity pk
    name                   TEXT NOT NULL,
    description            TEXT,
    emoji                  TEXT,
    owner_instance_id      TEXT NOT NULL,
    owner_username         TEXT NOT NULL,
    identity_public_key    TEXT NOT NULL,              -- 64 hex chars
    config_sequence        INTEGER NOT NULL DEFAULT 0,
    space_type             TEXT NOT NULL DEFAULT 'private'
                           CHECK(space_type IN ('private','household','public','global')),
    join_mode              TEXT NOT NULL DEFAULT 'invite_only'
                           CHECK(join_mode IN ('invite_only','open','link','request')),
    join_code              TEXT,
    retention_days         INTEGER,
    retention_exempt_json  TEXT NOT NULL DEFAULT '[]',
    -- Feature toggles (mirror domain/space.SpaceFeatures.to_columns)
    feature_calendar       INTEGER NOT NULL DEFAULT 0,
    feature_todo           INTEGER NOT NULL DEFAULT 1,
    feature_location       INTEGER NOT NULL DEFAULT 0,
    location_mode          TEXT NOT NULL DEFAULT 'gps'
                           CHECK(location_mode IN ('gps', 'zone_only')),
    -- feature_location is the on/off switch for the per-space map (§23.8.6).
    -- location_mode picks the privacy tier when the feature is on:
    --   gps       — opted-in members broadcast 4dp GPS to the space.
    --   zone_only — the originating instance matches the member's GPS to a
    --               space-defined zone (§23.8.7) and broadcasts only the
    --               matched zone label; raw coordinates never leave the
    --               originating household. Outside-zone updates are skipped.
    -- HA-defined zone names never reach a space-bound channel under either
    -- mode — see §23.8.5/§23.8.6.
    feature_stickies       INTEGER NOT NULL DEFAULT 0,
    feature_pages          INTEGER NOT NULL DEFAULT 1,
    posts_access           TEXT NOT NULL DEFAULT 'open'
                           CHECK(posts_access IN ('open','moderated','admin_only')),
    pages_access           TEXT NOT NULL DEFAULT 'open'
                           CHECK(pages_access IN ('open','moderated','admin_only')),
    stickies_access        TEXT NOT NULL DEFAULT 'open'
                           CHECK(stickies_access IN ('open','moderated','admin_only')),
    calendar_access        TEXT NOT NULL DEFAULT 'open'
                           CHECK(calendar_access IN ('open','moderated','admin_only')),
    tasks_access           TEXT NOT NULL DEFAULT 'open'
                           CHECK(tasks_access IN ('open','moderated','admin_only')),
    allow_post_text        INTEGER NOT NULL DEFAULT 1,
    allow_post_image       INTEGER NOT NULL DEFAULT 1,
    allow_post_video       INTEGER NOT NULL DEFAULT 1,
    allow_post_transcript  INTEGER NOT NULL DEFAULT 1,
    allow_post_poll        INTEGER NOT NULL DEFAULT 1,
    allow_post_schedule    INTEGER NOT NULL DEFAULT 1,
    allow_post_file        INTEGER NOT NULL DEFAULT 1,
    allow_post_bazaar      INTEGER NOT NULL DEFAULT 1,
    -- Public / discover fields (populated only when join_mode IN ('public','open'))
    lat                    REAL,
    lon                    REAL,
    radius_km              REAL,
    -- Short hex digest of the current cover WebP (bytes live in
    -- space_covers). NULL → no cover, gradient fallback renders.
    cover_hash             TEXT,
    about_markdown         TEXT,
    feature_gallery        INTEGER NOT NULL DEFAULT 0,
    welcome_version        INTEGER NOT NULL DEFAULT 0,
    -- When 1, the HA integration may post to this space via the bot-bridge
    -- (one named SpaceBot persona per automation). Admin opt-in: bots cannot
    -- appear in the space without this. Toggling to 0 is an admin kill-switch
    -- — existing bot tokens stay valid, posting is blocked until re-enabled.
    bot_enabled            INTEGER NOT NULL DEFAULT 0,
    allow_here_mention     INTEGER NOT NULL DEFAULT 0,
    dissolved              INTEGER NOT NULL DEFAULT 0,
    -- Child protection (§CP)
    min_age                INTEGER NOT NULL DEFAULT 0
                           CHECK(min_age IN (0, 13, 16, 18)),
    target_audience        TEXT NOT NULL DEFAULT 'all'
                           CHECK(target_audience IN ('all','family','teen','adult')),
    created_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Child protection: guardian links + per-minor blocks ────────────────────

CREATE TABLE IF NOT EXISTS cp_guardians (
    minor_user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    guardian_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    granted_by       TEXT NOT NULL,
    granted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (minor_user_id, guardian_user_id)
);
CREATE INDEX IF NOT EXISTS idx_cp_guardian
    ON cp_guardians(guardian_user_id);

CREATE TABLE IF NOT EXISTS cp_minor_blocks (
    minor_user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    blocked_user_id  TEXT NOT NULL,
    blocked_by       TEXT NOT NULL,
    blocked_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (minor_user_id, blocked_user_id)
);
CREATE INDEX IF NOT EXISTS idx_spaces_type ON spaces(space_type);

CREATE TABLE IF NOT EXISTS space_members (
    space_id              TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id               TEXT NOT NULL,
    role                  TEXT NOT NULL DEFAULT 'member'
                          CHECK(role IN ('owner','admin','member','subscriber')),
    joined_at             TEXT NOT NULL DEFAULT (datetime('now')),
    history_visible_from  TEXT,
    location_share_enabled INTEGER NOT NULL DEFAULT 0,
    space_display_name    TEXT,
    -- Per-space profile-picture override (§4.1.6). NULL means the
    -- member inherits their household picture. The bytes live in
    -- space_member_profile_pictures.
    picture_hash          TEXT,
    PRIMARY KEY (space_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_space_members_user ON space_members(user_id);

-- Per-space profile-picture override. When a row exists, the member
-- wants a different avatar inside this space than their household
-- default. Federation of space-scoped mutations carries the bytes.
CREATE TABLE IF NOT EXISTS space_member_profile_pictures (
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    bytes_webp   BLOB NOT NULL,
    hash         TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, user_id)
);

-- Per-space zone catalogue (§23.8.7). Each row is a labelled circle on the
-- space map. Members' GPS positions are matched to zones client-side for
-- display labels — zones never replace coordinates on the wire. The catalogue
-- is owned by space admins and replicated to remote member instances via
-- sealed SPACE_ZONE_UPSERTED / SPACE_ZONE_DELETED federation events.
CREATE TABLE IF NOT EXISTS space_zones (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    latitude     REAL NOT NULL,                       -- 4dp truncated
    longitude    REAL NOT NULL,                       -- 4dp truncated
    radius_m     INTEGER NOT NULL CHECK(radius_m BETWEEN 25 AND 50000),
    color        TEXT,                                -- "#RRGGBB", optional
    created_by   TEXT NOT NULL,                       -- user_id of creating admin
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (space_id, name)
);
CREATE INDEX IF NOT EXISTS idx_space_zones_space ON space_zones(space_id);

-- Space cover image (hero banner on the space feed page). One row per
-- space that has a cover set; WebP bytes transcoded from the admin's
-- original upload via ImageProcessor.generate_thumbnail. Parent-row
-- ``spaces.cover_hash`` mirrors the blob hash for cache-busting URLs.
CREATE TABLE IF NOT EXISTS space_covers (
    space_id     TEXT PRIMARY KEY REFERENCES spaces(id) ON DELETE CASCADE,
    bytes_webp   BLOB NOT NULL,
    hash         TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS space_instances (
    space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id   TEXT NOT NULL,
    joined_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id)
);

CREATE TABLE IF NOT EXISTS space_keys (
    space_id        TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    epoch           INTEGER NOT NULL,
    content_key_hex TEXT NOT NULL,                -- KEK-encrypted AES-256 key
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, epoch)
);

CREATE TABLE IF NOT EXISTS space_bans (
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    identity_pk  TEXT,                            -- pubkey for cross-instance ban checks
    banned_by    TEXT NOT NULL,
    banned_at    TEXT NOT NULL DEFAULT (datetime('now')),
    reason       TEXT,
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS space_instance_bans (
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id  TEXT NOT NULL,
    banned_at    TEXT NOT NULL DEFAULT (datetime('now')),
    reason       TEXT,
    PRIMARY KEY (space_id, instance_id)
);

CREATE TABLE IF NOT EXISTS space_invitations (
    id                      TEXT PRIMARY KEY,
    space_id                TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    invited_user_id         TEXT NOT NULL,
    invited_by              TEXT NOT NULL,
    -- §D1b — cross-household invites for type=private spaces. Both
    -- columns are NULL for local invites. When set, the host-side row
    -- carries the invitee's (instance_id, user_id) so admins can see
    -- the pending cross-household invitation; the invitee-side row
    -- carries the *inviter's* (instance_id, user_id) so the invitee
    -- can accept or decline.
    remote_instance_id      TEXT,
    remote_user_id          TEXT,
    invite_token            TEXT,
    space_display_hint      TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','accepted','declined','expired')),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_invite_tokens (
    token         TEXT PRIMARY KEY,
    space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    created_by    TEXT NOT NULL,
    uses_remaining INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT
);

CREATE TABLE IF NOT EXISTS space_join_requests (
    id                        TEXT PRIMARY KEY,
    space_id                  TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    -- §D2 cross-household applicants (global-space join-request over
    -- federation). NULL for local requests; when set, identifies the
    -- applicant's household.
    remote_applicant_instance_id TEXT,
    remote_applicant_pk       TEXT,
    user_id       TEXT NOT NULL,
    message       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','approved','denied','expired','withdrawn')),
    requested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    expires_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS space_aliases (
    space_id        TEXT NOT NULL,
    local_username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    alias           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, local_username)
);

-- Per-viewer private aliases for users (§4.1.6).
-- ``viewer_user_id`` is the local user setting the alias (their own
-- ``users.user_id``); ``target_user_id`` is the user being renamed
-- and may live in either ``users`` or ``remote_users``. The alias
-- is never federated and applies only to the viewer's view.
-- Resolution priority on read: space_display_name > personal alias >
-- global display_name (see ``DisplayableUser``).
CREATE TABLE IF NOT EXISTS user_aliases (
    viewer_user_id  TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    target_user_id  TEXT NOT NULL,
    alias           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (viewer_user_id, target_user_id),
    CHECK(length(alias) > 0 AND length(alias) <= 80)
);
CREATE INDEX IF NOT EXISTS idx_user_aliases_target
    ON user_aliases(target_user_id);

CREATE TABLE IF NOT EXISTS pinned_sidebar_spaces (
    user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    space_id  TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, space_id)
);

-- ── Bot personas for the bot-bridge ────────────────────────────────────────
-- Named bots that post into a space via POST /api/bot-bridge/spaces/{id}.
-- Must be declared before space_posts because of the bot_id FK.
CREATE TABLE IF NOT EXISTS space_bots (
    bot_id      TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK(scope IN ('space','member')),
    -- 'space'  = admin-curated shared bot (no per-member attribution in feed)
    -- 'member' = member-created personal bot (feed shows "via {member}")
    slug        TEXT NOT NULL,               -- [a-z0-9_-], 1–32 chars
    name        TEXT NOT NULL,               -- 1–48 chars
    icon        TEXT NOT NULL,               -- emoji or HA entity_id
    created_by  TEXT NOT NULL,               -- user_id of owner (space-scope: admin; member-scope: member)
    -- sha256 of the raw Bearer token. The plaintext token is shown to the
    -- caller exactly once (at create + rotate) and never stored.
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- One "doorbell" per (space, scope). Two members can each have a
    -- personal "gym-timer" slug, but a space may have only one shared one.
    UNIQUE (space_id, scope, slug)
);
CREATE INDEX IF NOT EXISTS idx_space_bots_space ON space_bots(space_id);
CREATE INDEX IF NOT EXISTS idx_space_bots_owner ON space_bots(space_id, created_by);

-- ── Space content: posts, comments, moderation ─────────────────────────────

CREATE TABLE IF NOT EXISTS space_posts (
    id              TEXT PRIMARY KEY,
    space_id        TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    author          TEXT NOT NULL,                 -- user_id or 'system-integration'
    -- SpaceBot that authored this post via the bot-bridge. Non-NULL iff
    -- author = 'system-integration'. ON DELETE SET NULL so deleting a bot
    -- leaves its historical posts readable (they fall back to generic
    -- "Home Assistant" rendering) rather than triggering a cascade wipe.
    bot_id          TEXT REFERENCES space_bots(bot_id) ON DELETE SET NULL,
    type            TEXT NOT NULL,
    content         TEXT,
    media_url       TEXT,
    reactions       TEXT NOT NULL DEFAULT '{}',
    comment_count   INTEGER NOT NULL DEFAULT 0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    deleted         INTEGER NOT NULL DEFAULT 0,
    edited_at       TEXT,
    no_link_preview INTEGER NOT NULL DEFAULT 0,
    moderated       INTEGER NOT NULL DEFAULT 0,
    file_meta_json  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_space_posts_created
    ON space_posts(space_id, created_at DESC);
-- Needed for the ON DELETE SET NULL cascade from space_bots to find
-- affected posts without a full scan. Feed reads don't filter by
-- bot_id, so the index exists for the delete path + future analytics.
CREATE INDEX IF NOT EXISTS idx_space_posts_bot
    ON space_posts(bot_id) WHERE bot_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS space_post_comments (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES space_posts(id) ON DELETE CASCADE,
    parent_id   TEXT REFERENCES space_post_comments(id) ON DELETE CASCADE,
    author      TEXT NOT NULL,                     -- user_id
    type        TEXT NOT NULL DEFAULT 'text'
                CHECK(type IN ('text','image')),
    content     TEXT,
    media_url   TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0,
    edited_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_space_post_comments_post ON space_post_comments(post_id);

CREATE TABLE IF NOT EXISTS space_moderation_queue (
    id                TEXT PRIMARY KEY,
    space_id          TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    feature           TEXT NOT NULL,
    action            TEXT NOT NULL,
    submitted_by      TEXT NOT NULL,
    payload_json      TEXT,
    current_snapshot  TEXT,
    submitted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('pending','approved','rejected','expired')),
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    rejection_reason  TEXT
);

-- ── User-filed reports (spam / harassment / misinformation / …) ────────────

CREATE TABLE IF NOT EXISTS content_reports (
    id                    TEXT PRIMARY KEY,
    target_type           TEXT NOT NULL
                          CHECK(target_type IN ('post','comment','user','space')),
    target_id             TEXT NOT NULL,
    reporter_user_id      TEXT NOT NULL,
    reporter_instance_id  TEXT,
    category              TEXT NOT NULL
                          CHECK(category IN ('spam','harassment',
                                              'inappropriate','misinformation',
                                              'other')),
    notes                 TEXT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending','resolved','dismissed')),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_by           TEXT,
    resolved_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_content_reports_status
    ON content_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_reports_reporter
    ON content_reports(reporter_user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_reports_unique_pair
    ON content_reports(reporter_user_id, target_type, target_id);

-- ── Polls (household + space variants) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS polls (
    post_id        TEXT PRIMARY KEY REFERENCES feed_posts(id) ON DELETE CASCADE,
    question       TEXT NOT NULL,
    closes_at      TEXT,
    closed         INTEGER NOT NULL DEFAULT 0,
    allow_multiple INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_options (
    id         TEXT PRIMARY KEY,
    post_id    TEXT NOT NULL REFERENCES polls(post_id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    option_id      TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    voter_user_id  TEXT NOT NULL,
    voted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (option_id, voter_user_id)
);

CREATE TABLE IF NOT EXISTS space_polls (
    post_id        TEXT PRIMARY KEY REFERENCES space_posts(id) ON DELETE CASCADE,
    question       TEXT NOT NULL,
    closes_at      TEXT,
    closed         INTEGER NOT NULL DEFAULT 0,
    allow_multiple INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS space_poll_options (
    id         TEXT PRIMARY KEY,
    post_id    TEXT NOT NULL REFERENCES space_polls(post_id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS space_poll_votes (
    option_id      TEXT NOT NULL REFERENCES space_poll_options(id) ON DELETE CASCADE,
    voter_user_id  TEXT NOT NULL,
    voted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (option_id, voter_user_id)
);

-- ── Schedule polls (Doodle-style) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedule_slots (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES feed_posts(id) ON DELETE CASCADE,
    slot_date   TEXT NOT NULL,
    start_time  TEXT,
    end_time    TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule_responses (
    slot_id       TEXT NOT NULL REFERENCES schedule_slots(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    availability  TEXT NOT NULL CHECK(availability IN ('yes','no','maybe')),
    responded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slot_id, user_id)
);

CREATE TABLE IF NOT EXISTS schedule_poll_meta (
    post_id            TEXT PRIMARY KEY REFERENCES feed_posts(id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    deadline           TEXT,
    finalized_slot_id  TEXT,
    closed             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS space_schedule_slots (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES space_posts(id) ON DELETE CASCADE,
    slot_date   TEXT NOT NULL,
    start_time  TEXT,
    end_time    TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS space_schedule_responses (
    slot_id       TEXT NOT NULL REFERENCES space_schedule_slots(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    availability  TEXT NOT NULL CHECK(availability IN ('yes','no','maybe')),
    responded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slot_id, user_id)
);

CREATE TABLE IF NOT EXISTS space_schedule_poll_meta (
    post_id            TEXT PRIMARY KEY REFERENCES space_posts(id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    deadline           TEXT,
    finalized_slot_id  TEXT,
    closed             INTEGER NOT NULL DEFAULT 0
);

-- ── Bazaar (marketplace) ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bazaar_listings (
    post_id         TEXT PRIMARY KEY REFERENCES feed_posts(id) ON DELETE CASCADE,
    seller_user_id  TEXT NOT NULL,
    mode            TEXT NOT NULL
                    CHECK(mode IN ('fixed','offer','bid_from','negotiable','auction')),
    title           TEXT NOT NULL,
    description     TEXT,
    image_urls_json TEXT NOT NULL DEFAULT '[]',
    end_time        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','sold','expired','cancelled')),
    price           INTEGER,
    start_price     INTEGER,
    step_price      INTEGER,
    winner_user_id  TEXT,
    winning_price   INTEGER,
    sold_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bazaar_bids (
    id                TEXT PRIMARY KEY,
    listing_post_id   TEXT NOT NULL REFERENCES bazaar_listings(post_id) ON DELETE CASCADE,
    bidder_user_id    TEXT NOT NULL,
    amount            INTEGER NOT NULL,
    message           TEXT,
    accepted          INTEGER NOT NULL DEFAULT 0,
    rejected          INTEGER NOT NULL DEFAULT 0,
    rejection_reason  TEXT,
    withdrawn         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bazaar_bids_listing ON bazaar_bids(listing_post_id);

CREATE TABLE IF NOT EXISTS saved_bazaar_listings (
    user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    post_id   TEXT NOT NULL,
    saved_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

-- ── Conversations / DMs ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversations (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL CHECK(type IN ('dm','group_dm')),
    name             TEXT,
    created_by       TEXT,                           -- user_id
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_message_at  TEXT,
    -- When 1, the HA integration may post into this DM via the bot-bridge
    -- using the user's API token. DMs have no named bot personas (the 1:1
    -- context makes "Home Assistant" adequate) — this is a simple on/off.
    bot_enabled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversation_members (
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    username              TEXT NOT NULL REFERENCES users(username),
    joined_at             TEXT NOT NULL DEFAULT (datetime('now')),
    last_read_at          TEXT,
    history_visible_from  TEXT,
    deleted_at            TEXT,
    PRIMARY KEY (conversation_id, username)
);

CREATE TABLE IF NOT EXISTS conversation_remote_members (
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    instance_id           TEXT NOT NULL,
    remote_username       TEXT NOT NULL,
    joined_at             TEXT NOT NULL DEFAULT (datetime('now')),
    history_visible_from  TEXT,
    PRIMARY KEY (conversation_id, instance_id, remote_username)
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_user_id  TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    type            TEXT NOT NULL DEFAULT 'text',
    media_url       TEXT,
    reply_to_id     TEXT REFERENCES conversation_messages(id) ON DELETE SET NULL,
    deleted         INTEGER NOT NULL DEFAULT 0,
    edited_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_created
    ON conversation_messages(conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id  TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    emoji       TEXT NOT NULL,
    reacted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id, emoji)
);

-- ── Tasks ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS task_lists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL,                    -- user_id
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    list_id              TEXT NOT NULL REFERENCES task_lists(id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    description          TEXT,
    due_date             TEXT,
    assignees_json       TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'todo'
                         CHECK(status IN ('todo','in_progress','done')),
    position             INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT NOT NULL,
    rrule                TEXT,
    last_spawned_at      TEXT,
    recurrence_parent_id TEXT,
    archived_at          TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_list        ON tasks(list_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status      ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_archived_at ON tasks(archived_at);

CREATE TABLE IF NOT EXISTS space_task_lists (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS space_tasks (
    id                   TEXT PRIMARY KEY,
    list_id              TEXT NOT NULL REFERENCES space_task_lists(id) ON DELETE CASCADE,
    space_id             TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    description          TEXT,
    due_date             TEXT,
    assignees_json       TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'todo'
                         CHECK(status IN ('todo','in_progress','done')),
    position             INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT NOT NULL,
    rrule                TEXT,
    last_spawned_at      TEXT,
    recurrence_parent_id TEXT,
    archived_at          TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_space_tasks_list        ON space_tasks(list_id);
CREATE INDEX IF NOT EXISTS idx_space_tasks_archived_at ON space_tasks(archived_at);

CREATE TABLE IF NOT EXISTS task_deadline_notifications (
    task_id    TEXT NOT NULL,
    due_date   TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, due_date)
);

-- ── Calendar ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calendars (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT '#4A90E2',
    owner_username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    calendar_type   TEXT NOT NULL DEFAULT 'personal'
                    CHECK(calendar_type IN ('personal','space'))
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id              TEXT PRIMARY KEY,
    calendar_id     TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    summary         TEXT NOT NULL,
    description     TEXT,
    start_dt        TEXT NOT NULL,
    end_dt          TEXT NOT NULL,
    all_day         INTEGER NOT NULL DEFAULT 0,
    attendees_json  TEXT NOT NULL DEFAULT '[]',
    mirrored_from   TEXT,
    rrule           TEXT,                   -- RFC 5545 recurrence rule (§17.2)
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS space_calendar_events (
    id                     TEXT PRIMARY KEY,
    space_id               TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    summary                TEXT NOT NULL,
    description            TEXT,
    start_dt               TEXT NOT NULL,
    end_dt                 TEXT NOT NULL,
    all_day                INTEGER NOT NULL DEFAULT 0,
    attendees_json         TEXT NOT NULL DEFAULT '[]',
    rrule                  TEXT,            -- RFC 5545 recurrence rule (§17.2)
    created_by             TEXT NOT NULL,
    notify_before_minutes  INTEGER,
    notified_at            TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_space_calendar_events_space
    ON space_calendar_events(space_id, start_dt);

CREATE TABLE IF NOT EXISTS space_calendar_rsvps (
    event_id    TEXT NOT NULL REFERENCES space_calendar_events(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK(status IN ('going','maybe','declined')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, user_id)
);


-- ── Pages ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pages (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    content              TEXT NOT NULL DEFAULT '',
    cover_image_url      TEXT,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_editor_user_id  TEXT,
    last_edited_at       TEXT,
    locked_by            TEXT,
    locked_at            TEXT,
    lock_expires_at      TEXT,
    delete_requested_by  TEXT,
    delete_requested_at  TEXT,
    delete_approved_by   TEXT,
    delete_approved_at   TEXT
);

CREATE TABLE IF NOT EXISTS space_pages (
    id                   TEXT PRIMARY KEY,
    space_id             TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    content              TEXT NOT NULL DEFAULT '',
    cover_image_url      TEXT,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_editor_user_id  TEXT,
    last_edited_at       TEXT,
    locked_by            TEXT,
    locked_at            TEXT,
    lock_expires_at      TEXT,
    delete_requested_by  TEXT,
    delete_requested_at  TEXT,
    delete_approved_by   TEXT,
    delete_approved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_space_pages_space ON space_pages(space_id);

CREATE TABLE IF NOT EXISTS page_edit_history (
    id               TEXT PRIMARY KEY,
    page_id          TEXT NOT NULL,
    space_id         TEXT,
    title            TEXT NOT NULL,
    content          TEXT NOT NULL,
    cover_image_url  TEXT,
    edited_by        TEXT NOT NULL,
    edited_at        TEXT NOT NULL DEFAULT (datetime('now')),
    version          INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_page_edit_history_version
    ON page_edit_history(page_id, version);

-- ── Stickies ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS stickies (
    id          TEXT PRIMARY KEY,
    space_id    TEXT REFERENCES spaces(id) ON DELETE CASCADE,   -- NULL = household
    author      TEXT NOT NULL,
    content     TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#FFF9B1',
    position_x  REAL NOT NULL DEFAULT 0,
    position_y  REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Notifications ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,                              -- may be null if redacted
    link_url    TEXT,
    read_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    endpoint     TEXT NOT NULL,                    -- sensitive
    p256dh       TEXT NOT NULL,                    -- sensitive
    auth_secret  TEXT NOT NULL,                    -- sensitive
    device_label TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Public space discovery / moderation ────────────────────────────────────

CREATE TABLE IF NOT EXISTS public_space_cache (
    space_id        TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    emoji           TEXT,
    lat             REAL,
    lon             REAL,
    radius_km       REAL,
    member_count    INTEGER NOT NULL DEFAULT 0,
    min_age         INTEGER NOT NULL DEFAULT 0,
    target_audience TEXT NOT NULL DEFAULT 'all',
    cached_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blocked_discover_instances (
    instance_id  TEXT PRIMARY KEY,
    blocked_by   TEXT NOT NULL,
    blocked_at   TEXT NOT NULL DEFAULT (datetime('now')),
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS hidden_public_spaces (
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    space_id   TEXT NOT NULL,
    hidden_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, space_id)
);

-- ─────────────────────────────────────────────────────────────────────────
-- §D1b  cross-household remote members (private-space federation)
-- Records a remote user accepted into a local space via the zero-leak
-- SPACE_PRIVATE_INVITE flow. Lets the host's space-message fan-out
-- include the invitee's instance when encrypting + routing.
CREATE TABLE IF NOT EXISTS space_remote_members (
    space_id       TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id    TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    user_pk        TEXT,
    display_name   TEXT,
    joined_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, instance_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_space_remote_members_instance
    ON space_remote_members(instance_id);

-- ─────────────────────────────────────────────────────────────────────────
-- §D1a  peer-public-space directory sync
-- Mirrors type=public spaces hosted by paired peers so the space browser's
-- "From friends" tab can list them without polling the GFS.
-- Each row represents a single (peer, space) advertisement; on receipt of
-- a fresh SPACE_DIRECTORY_SYNC envelope we replace all rows for that peer
-- atomically.
CREATE TABLE IF NOT EXISTS peer_space_directory (
    instance_id      TEXT NOT NULL,
    space_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    emoji            TEXT,
    member_count     INTEGER NOT NULL DEFAULT 0,
    join_mode        TEXT NOT NULL DEFAULT 'request',
    min_age          INTEGER NOT NULL DEFAULT 0,
    target_audience  TEXT NOT NULL DEFAULT 'all',
    updated_at       TEXT,
    cached_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (instance_id, space_id)
);
CREATE INDEX IF NOT EXISTS idx_peer_space_directory_instance
    ON peer_space_directory(instance_id);

CREATE TABLE IF NOT EXISTS content_reports (
    id              TEXT PRIMARY KEY,
    reporter_user_id TEXT NOT NULL,
    target_type     TEXT NOT NULL CHECK(target_type IN ('post','comment','user','space')),
    target_id       TEXT NOT NULL,
    category        TEXT NOT NULL,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','actioned','dismissed')),
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── User blocks ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_blocks (
    blocker_user_id  TEXT NOT NULL,
    blocked_user_id  TEXT NOT NULL,
    blocked_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blocker_user_id, blocked_user_id)
);

-- ── Shopping list (§23.120) ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    completed     INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT
);

-- ── Space themes / links / household theme ────────────────────────────────

CREATE TABLE IF NOT EXISTS household_theme (
    id              TEXT PRIMARY KEY DEFAULT 'default' CHECK(id = 'default'),
    primary_color   TEXT NOT NULL DEFAULT '#4A90E2',
    accent_color    TEXT NOT NULL DEFAULT '#F5A623',
    -- §23.125 Household Theme Studio extensions.
    surface_color   TEXT,                                   -- optional light surface
    surface_dark    TEXT,                                   -- optional dark surface
    mode            TEXT NOT NULL DEFAULT 'auto'
                    CHECK(mode IN ('light','dark','auto')),
    font_family     TEXT NOT NULL DEFAULT 'system'
                    CHECK(font_family IN ('system','serif','rounded','mono')),
    density         TEXT NOT NULL DEFAULT 'comfortable'
                    CHECK(density IN ('compact','comfortable','spacious')),
    corner_radius   INTEGER NOT NULL DEFAULT 12
                    CHECK(corner_radius BETWEEN 0 AND 24),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS space_themes (
    space_id          TEXT PRIMARY KEY REFERENCES spaces(id) ON DELETE CASCADE,
    primary_color     TEXT NOT NULL DEFAULT '#4A90E2',
    accent_color      TEXT NOT NULL DEFAULT '#F5A623',
    -- §23.123 per-space override fields.
    header_image_file TEXT,
    background_tint   TEXT,
    mode_override     TEXT
                      CHECK(mode_override IS NULL
                            OR mode_override IN ('light','dark','auto')),
    font_family       TEXT NOT NULL DEFAULT 'system'
                      CHECK(font_family IN ('system','serif','rounded','mono')),
    post_layout       TEXT NOT NULL DEFAULT 'card'
                      CHECK(post_layout IN ('card','compact','magazine')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS space_links (
    id        TEXT PRIMARY KEY,
    space_id  TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    label     TEXT NOT NULL,
    url       TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0
);

-- ── Per-space notification preferences ────────────────────────────────────

CREATE TABLE IF NOT EXISTS space_notif_prefs (
    user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    space_id  TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    level     TEXT NOT NULL DEFAULT 'all'
              CHECK(level IN ('all','mentions','muted')),
    PRIMARY KEY (user_id, space_id)
);

-- ── DM contact requests (§23.47) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dm_contact_requests (
    id           TEXT PRIMARY KEY,
    from_user_id TEXT NOT NULL,
    to_user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','accepted','declined')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dm_contact_requests_to
    ON dm_contact_requests(to_user_id, status);

-- ── Call history ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS call_sessions (
    id                   TEXT PRIMARY KEY,
    conversation_id      TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    initiator_user_id    TEXT NOT NULL,
    callee_user_id       TEXT,
    call_type            TEXT NOT NULL CHECK(call_type IN ('audio','video')),
    status               TEXT NOT NULL DEFAULT 'ringing'
                         CHECK(status IN ('ringing','active','ended','declined','missed')),
    participant_user_ids TEXT NOT NULL DEFAULT '[]',
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    connected_at         TEXT,
    ended_at             TEXT,
    duration_seconds     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_call_sessions_user
    ON call_sessions(initiator_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_sessions_conv
    ON call_sessions(conversation_id, started_at DESC);

-- ── CALL_QUALITY samples (spec §26) ──────────────────────────────────────
-- One row per peer per ~10 s while a call is in-progress. Written both
-- locally (from RtcTransport.getStats()) and via federation (remote
-- participant's CALL_QUALITY event). Used for admin-side diagnosis.

CREATE TABLE IF NOT EXISTS call_quality_samples (
    call_id          TEXT NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
    reporter_user_id TEXT NOT NULL,
    sampled_at       INTEGER NOT NULL,       -- unix epoch
    rtt_ms           INTEGER,
    jitter_ms        INTEGER,
    loss_pct         REAL,
    audio_bitrate    INTEGER,
    video_bitrate    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_call_quality_call
    ON call_quality_samples(call_id, sampled_at DESC);

-- ── Content reports (moderation) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS content_reports (
    id                TEXT PRIMARY KEY,
    space_id          TEXT,
    post_id           TEXT NOT NULL,
    reporter_user_id  TEXT NOT NULL,
    reason            TEXT NOT NULL CHECK(reason IN
        ('spam','harassment','inappropriate','misinformation','other')),
    detail            TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('pending','approved','rejected')),
    reviewed_by       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_reports_status
    ON content_reports(status, created_at DESC);

-- ── §28/§29 supplemental tables ───────────────────────────────────────────

-- Bazaar offers (separate from auction bids)
CREATE TABLE IF NOT EXISTS bazaar_offers (
    id              TEXT PRIMARY KEY,
    listing_post_id TEXT NOT NULL REFERENCES bazaar_listings(post_id) ON DELETE CASCADE,
    offerer_user_id TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    message         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','accepted','rejected','withdrawn')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    responded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_bazaar_offers_listing
    ON bazaar_offers(listing_post_id, status);

-- DM sync gap detection
CREATE TABLE IF NOT EXISTS conversation_message_gaps (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_user_id  TEXT NOT NULL,
    expected_seq    INTEGER NOT NULL,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, sender_user_id, expected_seq)
);

-- Multi-hop DM relay paths (§12.5, §18587).
-- One row per (conversation, sender, path_index). path_index=0 is the
-- primary; 1+ are alternatives, promoted on relay-offline fallback.
-- ``relay_path`` is a JSON array of instance_ids; ``relay_path[0]`` is
-- the next hop (validated on each send by ``get_or_select_path``).
CREATE TABLE IF NOT EXISTS conversation_relay_paths (
    conversation_id TEXT NOT NULL,
    sender_user_id  TEXT NOT NULL,
    path_index      INTEGER NOT NULL,
    target_instance TEXT NOT NULL,
    relay_path      TEXT NOT NULL,
    hop_count       INTEGER NOT NULL DEFAULT 1,
    last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, sender_user_id, path_index)
);

CREATE TABLE IF NOT EXISTS conversation_sender_sequences (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_user_id  TEXT NOT NULL,
    last_seq        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, sender_user_id)
);

-- DM_RELAY dedup (§12.5.3)
CREATE TABLE IF NOT EXISTS dm_relay_seen (
    msg_id     TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dm_relay_seen_at ON dm_relay_seen(seen_at);

-- §11.10 instance peer-graph cache for network discovery. The PK is
-- compound because a target instance may be discovered via multiple
-- different paired peers — each edge is an independent fact, and BFS
-- needs to see all of them to evaluate alternative relay paths.
CREATE TABLE IF NOT EXISTS network_discovery (
    instance_id    TEXT NOT NULL,
    discovered_via TEXT NOT NULL,
    seen_at        TEXT NOT NULL DEFAULT (datetime('now')),
    hop_count      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (instance_id, discovered_via)
);

-- Schedule-poll responses live in ``schedule_responses`` (declared
-- alongside ``schedule_slots`` / ``schedule_poll_meta`` earlier in
-- this migration). The legacy ``schedule_votes`` table was a
-- duplicate that the service never wrote to; removed to keep a
-- single path.

-- DM delivery state for read receipts + delivery confirmations
CREATE TABLE IF NOT EXISTS conversation_delivery_state (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    state           TEXT NOT NULL CHECK(state IN ('delivered','read')),
    state_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, message_id, user_id)
);

-- GFS connections (§24) — paired Global Federation Servers.
CREATE TABLE IF NOT EXISTS gfs_connections (
    id TEXT PRIMARY KEY,
    gfs_instance_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    public_key TEXT NOT NULL,
    inbox_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'active', 'suspended')),
    paired_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gfs_space_publications (
    space_id TEXT NOT NULL,
    gfs_connection_id TEXT NOT NULL REFERENCES gfs_connections(id),
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, gfs_connection_id)
);

-- Page snapshots for resolve-conflict diff3 (§4.4.4.1). When two
-- instances concurrently edit a page the service stores both bodies
-- with ``conflict=1`` and blocks further edits until a user runs
-- resolve-conflict (mine | theirs | merged_content).
CREATE TABLE IF NOT EXISTS space_page_snapshots (
    page_id      TEXT NOT NULL,
    space_id     TEXT,
    snapshot_at  TEXT NOT NULL DEFAULT (datetime('now')),
    body         TEXT NOT NULL,
    snapshot_by  TEXT NOT NULL,
    side         TEXT NOT NULL DEFAULT 'mine' CHECK(side IN ('base','mine','theirs')),
    conflict     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (page_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_space_page_snapshots_conflict
    ON space_page_snapshots(page_id, conflict);

-- Comments on tasks
CREATE TABLE IF NOT EXISTS task_comments (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task
    ON task_comments(task_id, created_at);

-- Task attachments — files attached to a task (spec §23.68).
CREATE TABLE IF NOT EXISTS task_attachments (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    uploaded_by   TEXT NOT NULL,
    url           TEXT NOT NULL,
    filename      TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_attachments_task
    ON task_attachments(task_id, created_at);

-- §CP audit logs
CREATE TABLE IF NOT EXISTS minor_space_memberships_audit (
    id           TEXT PRIMARY KEY,
    minor_user_id TEXT NOT NULL,
    space_id     TEXT NOT NULL,
    action       TEXT NOT NULL CHECK(action IN ('joined','removed','blocked')),
    actor_id     TEXT NOT NULL,
    occurred_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS guardian_audit_log (
    id           TEXT PRIMARY KEY,
    guardian_id  TEXT NOT NULL,
    minor_id     TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail       TEXT,
    occurred_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Gallery (§23.119) ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gallery_albums (
    id              TEXT PRIMARY KEY,
    space_id        TEXT REFERENCES spaces(id) ON DELETE CASCADE,
                    -- NULL = household-level album
    retention_exempt INTEGER NOT NULL DEFAULT 0,
    owner_user_id   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    cover_item_id   TEXT,
    item_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gallery_items (
    id                  TEXT PRIMARY KEY,
    album_id            TEXT NOT NULL REFERENCES gallery_albums(id) ON DELETE CASCADE,
    uploaded_by         TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    item_type           TEXT NOT NULL CHECK(item_type IN ('photo','video')),
    filename            TEXT NOT NULL,
    thumbnail_filename  TEXT NOT NULL,
    width               INTEGER NOT NULL,
    height              INTEGER NOT NULL,
    duration_s          REAL,
    caption             TEXT,
    taken_at            TEXT,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_gallery_albums_space   ON gallery_albums(space_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gallery_items_album    ON gallery_items(album_id, sort_order, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gallery_items_uploader ON gallery_items(uploaded_by);

-- ── Full-text search (FTS5) ────────────────────────────────────────────────
-- One unified contentless index keeps the search code simple. The ``scope``
-- column lets a single query restrict by surface (post / space_post / message
-- / page). ``ref_id`` is the canonical id of the source row; the search
-- service uses it to JOIN back to the source table on hit.

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    scope          UNINDEXED,    -- "post" | "space_post" | "message" | "page"
    ref_id         UNINDEXED,    -- source row id
    space_id       UNINDEXED,    -- nullable, used by space-scoped queries
    title,                       -- usually empty for posts/messages
    body,
    tokenize       = "unicode61 remove_diacritics 2"
);
