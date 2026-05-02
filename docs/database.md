# Database schema

Reference for the v1 SQLite schema. Every table that ships in v1 is
created by `socialhome/migrations/0001_initial.sql` (1,717 lines).
This doc groups those tables by domain so a contributor can find what
they need without reading the SQL top-to-bottom.

The migration file is the **source of truth** for column types,
indexes, and foreign keys; this page exists to navigate it. When
anything below contradicts the file, the file wins.

## Conventions

- TEXT columns hold ISO-8601 UTC timestamps unless otherwise noted.
- `instance_id` and `user_id` are 32-character base32 strings derived
  from an Ed25519 public key (§4.1.2 / §4.1.3) and never reassigned.
- Foreign keys reference public ids (string keys), never integer
  surrogates, so federation events can carry the same key across
  instance boundaries.
- JSON columns are stored as TEXT and validated at the service layer.
- GPS coordinates are stored already-truncated to 4 decimal places
  (§4 / [`principles.md`](./principles.md)).
- Schema migrations after v1 follow the `0002_*.sql` pattern; the v1
  file collapses what the spec called "migrations 0001–0033".

## Identity, users, auth

| Table | Purpose |
|---|---|
| `instance_identity` | Single-row table (`id='self'`) with the HFS's long-term Ed25519 keypair, optional ML-DSA-65 PQ key, household lat/lon, and routing secret. |
| `instance_config` | Generic key/value store for instance-level settings the platform adapter or services need to persist. |
| `users` | Local household users — username, display name, profile picture hash, admin flag, theme, status, locale, child-protection fields, soft-delete state, `last_seen_at` (most recent WS disconnect — drives "Last seen X" rendering after a server restart, see `docs/protocol/presence.md` § Online status), and `source` (`manual` vs `ha`). |
| `user_profile_pictures` | WebP bytes for household-level profile pictures, keyed by `user_id`. Separate table so `SELECT * FROM users` stays cheap. |
| `remote_users` | Users on paired remote instances. Carries display name, alias, picture hash, public key, `deprovisioned_at`, and `synced_at`. Same `user_id` namespace as `users`. |
| `api_tokens` | Per-user API tokens (HA mode and integrations). Stores `token_hash` only — the plaintext is shown to the user once. |
| `platform_users` | Standalone-mode local accounts (`platform/standalone/`). Empty in HA mode. Stores password hash, email, notify endpoint. |
| `platform_tokens` | Bearer tokens for `platform_users`. Hash-only storage. |

## Federation: peers, pairing, outbox, replay

| Table | Purpose |
|---|---|
| `remote_instances` | One row per paired peer. Holds the peer's `instance_id`, identity public key, KEK-encrypted directional session keys, inbox URL, status (`pending_*` / `confirmed` / `unpairing`), source (`manual` / `space_session`), `proto_version`, and the negotiated `sig_suite` (`ed25519` / `ed25519+mldsa65`). |
| `pending_pairings` | In-flight QR pairings. Stores own DH keypair, peer DH/identity material once received, `verification_code` (SAS), inbox URL, status, and expiry. KEK-encrypted private DH until pairing confirms. |
| `pairing_relay` | Admin-pending `PAIRING_INTRO_RELAY` requests received from a paired peer (§11.9). |
| `federation_outbox` | Pending outbound envelopes — `event_type`, encrypted `payload_json`, `attempts`, `next_attempt_at`, `status`. `expires_at = NULL` for security-critical events; 7-day TTL for ordinary ones (§4.4.7). |
| `federation_replay_cache` | `msg_id` → `received_at` for inbound dedup. Pruned by `infrastructure/replay_cache_scheduler.py`. |
| `network_discovery` | Peer-graph cache for §11.10 BFS path-finding — one row per `(instance_id, discovered_via)` edge. Compound PK so multiple paths to a peer are independent rows. |
| `gfs_connections` | Paired Global Federation Servers — `gfs_instance_id`, public key, inbox URL, status. |
| `gfs_space_publications` | Which public spaces are currently published to which GFS connection. |

## Household feed and content

| Table | Purpose |
|---|---|
| `household_features` | Single-row toggle table (`id='default'`) for which household-level features are on (feed, pages, tasks, stickies, calendar, bazaar, …) plus per-post-type allow flags. |
| `feed_posts` | Household-level posts. Type enum covers `text`, `image`, `video`, `transcript`, `poll`, `schedule`, `file`, `bazaar`. Holds reactions JSON, comment count, pinned, deleted, edited_at. |
| `post_comments` | Threaded household post comments with `parent_id` self-reference. |
| `saved_posts` / `feed_read_positions` | Per-user saved posts and last-read marker. |
| `post_drafts` | Auto-saved post composer drafts. `context` is `household_feed`, a `space_id`, a `page_id`, or a `conv_id`. GC'd by `post_draft_scheduler`. |
| `polls` / `poll_options` / `poll_votes` | Household-feed polls. `polls` is keyed by the parent `feed_posts.id`. Votes are stored per `(option_id, voter_user_id)`. |
| `schedule_slots` / `schedule_responses` / `schedule_poll_meta` | Household-feed Doodle-style schedule polls. |
| `bazaar_listings` / `bazaar_bids` / `bazaar_offers` | Household-feed marketplace: fixed-price, offer, bid-from, negotiable, and auction modes. Bids vs offers are distinct concepts. |
| `saved_bazaar_listings` | Per-user saved listings. |
| `shopping_list_items` | Single shared household shopping list (§23.120). |
| `household_theme` | Single-row (`id='default'`) household theme settings — primary/accent colour, surface, mode (`light`/`dark`/`auto`), font, density, corner radius. |
| `stickies` | Household + space sticky notes. `space_id IS NULL` means household-level. |

## Spaces

| Table | Purpose |
|---|---|
| `spaces` | Core space row — id (derived from space identity public key), name, owner, `space_type` (`private`/`household`/`public`/`global`), `join_mode`, retention, feature toggles, posts/pages/stickies/calendar/tasks access modes, public-discovery fields (lat/lon/radius), cover hash, `bot_enabled`, `min_age`, `target_audience`, `dissolved`, and `welcome_version`. |
| `space_members` | Local membership: `(space_id, user_id, role, joined_at, history_visible_from, location_share_enabled, space_display_name, picture_hash)`. Roles are `owner` / `admin` / `member` / `subscriber` (use the `SpaceRole` enum, not bare strings). |
| `space_member_profile_pictures` | Per-space profile-picture override bytes, keyed by `(space_id, user_id)`. |
| `space_remote_members` | Cross-household members admitted via `SPACE_PRIVATE_INVITE` (§D1b). Lets fan-out include the invitee's instance. |
| `space_zones` | Per-space labelled circles for the space map (§23.8.7). 4dp lat/lon, 25–50000 m radius. Replicated to remote member instances via sealed `SPACE_ZONE_*` events. |
| `space_covers` | Hero-banner WebP bytes per space. |
| `space_instances` | One row per `(space_id, instance_id)` — which peer instances participate in a space, with `last_seen_at`. |
| `space_keys` | One row per `(space_id, epoch)` holding the KEK-encrypted AES-256 content key. Epoch advances on member removal/ban (§4.3). |
| `space_bans` / `space_instance_bans` | Banned users / banned remote instances. Identity public key is captured for cross-instance ban enforcement. |
| `space_invitations` | Local + cross-household invites. Invitee-side and host-side rows both exist for `type=private` cross-household invites (§D1b). |
| `space_invite_tokens` | Shareable invite-link tokens — `uses_remaining`, optional expiry. |
| `space_join_requests` | Open join requests for `request`-mode and global spaces. Captures local + cross-household applicants (§D2). |
| `space_aliases` | Per-space-and-local-user aliases — what label the **space** sees for a local user. Federated. |
| `user_aliases` | Per-viewer **private** aliases (§4.1.6). Resolution priority: `space_display_name` > personal alias > global display name. Never federated. |
| `pinned_sidebar_spaces` | Per-user space pin-order in the sidebar. |
| `space_themes` / `space_links` / `space_notif_prefs` | Per-space theme overrides, link tray, and per-user notification preferences. |
| `peer_space_directory` | Cached "From friends" tab — type=public spaces hosted by paired peers (§D1a). One row per `(instance_id, space_id)`; replaced atomically per peer on `SPACE_DIRECTORY_SYNC`. |
| `space_bots` | Named bot personas attached to a space (§bot-bridge). Scope is `space` (admin-curated) or `member` (member-created). `token_hash` is sha256 of the bearer token. |

### Space content

| Table | Purpose |
|---|---|
| `space_posts` | Space-level posts. Mirrors `feed_posts` shape; adds `bot_id` (NULL for human authors) and `linked_event_id` (set when the post is the auto-created surface for a calendar event in Phase B). |
| `space_post_comments` | Threaded comments on `space_posts`. |
| `space_moderation_queue` | Pending mod actions — feature, action, payload, reviewer, status, expiry. |
| `space_polls` / `space_poll_options` / `space_poll_votes` | Space-feed polls. Vote rows are encrypted in transit so the GFS never sees who voted what (§25.8.21). |
| `space_schedule_slots` / `space_schedule_responses` / `space_schedule_poll_meta` | Space-scoped Doodle polls. |
| `space_calendar_events` | Space calendar events. Holds RFC 5545 `rrule`, attendees JSON, `capacity`, `notify_before_minutes`. |
| `space_calendar_rsvps` | Per-occurrence RSVPs — `(event_id, user_id, occurrence_at)` PK. Status: `going` / `maybe` / `declined` / `requested` / `waitlist`. |
| `space_calendar_rsvp_reminders` | Pre-event reminder fan-out — `fire_at` partial index on un-sent + future. Driven by `infrastructure/calendar_reminder_scheduler.py`. |
| `space_calendar_feed_tokens` | Per-`(user, space)` revocable tokens for the iCal `.ics` feed. Separate from API tokens so revoking one doesn't affect the other. |
| `pending_federated_rsvps` | Buffer for RSVP federation events arriving before their event has propagated. Flushed on event arrival. |
| `space_pages` | Wiki-style space pages with edit-lock fields and pending-delete approval workflow. |
| `space_page_snapshots` | Concurrent-edit conflict resolution (§4.4.4.1). Sides are `base` / `mine` / `theirs`; `conflict=1` blocks further edits until resolved. |
| `space_task_lists` / `space_tasks` | Space task lists and tasks; mirror household `task_lists` / `tasks` with `space_id`. |

## Direct messages

| Table | Purpose |
|---|---|
| `conversations` | DM threads. Type is `dm` or `group_dm`. `bot_enabled` opts the conversation into the integration's bot bridge. |
| `conversation_members` / `conversation_remote_members` | Local + remote membership rows; `history_visible_from` controls how far back a new joiner can see. |
| `conversation_messages` | Per-message rows. Plaintext storage (DM federation is transport-only encrypted; the local DB stores plaintext like every other surface — see [`principles.md`](./principles.md)). |
| `message_reactions` | One row per `(message_id, user_id, emoji)`. |
| `conversation_message_gaps` | Detected gaps in `(conversation, sender)` sequence — drives the gap-fill request. |
| `conversation_relay_paths` | Multi-hop DM relay paths (§12.5). `path_index=0` is primary; alternatives promote on relay-offline fallback. `relay_path` is a JSON array of instance ids. |
| `conversation_sender_sequences` | Per-`(conversation, sender)` last-emitted sequence number, used to detect gaps and order out-of-order delivery. |
| `dm_relay_seen` | Dedup table for `DM_RELAY` envelopes (§12.5.3). Pruned by the DM GC scheduler. |
| `conversation_delivery_state` | Per-`(message, user)` delivery + read state. |
| `dm_contact_requests` | Pending DM contact requests (§23.47). |

## Notifications and push

| Table | Purpose |
|---|---|
| `notifications` | In-app notification feed — id, user_id, type, title, optional body (omitted for DMs / location messages / UGC per §25.3), link URL, read_at. |
| `push_subscriptions` | Web Push endpoints — `endpoint`, `p256dh`, `auth_secret` (sensitive). One row per browser/device. |

## Presence

| Table | Purpose |
|---|---|
| `presence` | Local users' presence — `home` / `zone` / `away` / `unavailable`, current zone name, 4dp lat/lon, GPS accuracy. |
| `remote_presence` | Same shape, sourced from `PRESENCE_UPDATED` federation events. FK-free on the source columns so it works before `USERS_SYNC` populates `users`. |

## Calls

| Table | Purpose |
|---|---|
| `call_sessions` | One row per call — type (`audio` / `video`), status (`ringing` / `active` / `ended` / `declined` / `missed`), participant list JSON, started/connected/ended timestamps, duration. |
| `call_quality_samples` | Per-peer ~10 s WebRTC stats samples (RTT, jitter, loss, audio/video bitrate). Used for admin diagnostics (§26). |

## Public discovery and moderation

| Table | Purpose |
|---|---|
| `public_space_cache` | Local cache of GFS-published public spaces — `name`, `description`, lat/lon/radius, member count, `min_age`, `target_audience`. |
| `blocked_discover_instances` | Operator block-list applied to the discovery directory — never show spaces from these instances. |
| `hidden_public_spaces` | Per-user hide list for the discovery view. |
| `content_reports` | User-filed reports on posts / comments / users / spaces. Categories: `spam` / `harassment` / `inappropriate` / `misinformation` / `other`. (Note: the migration file declares this table three times to absorb earlier drift; only the final declaration is authoritative.) |
| `user_blocks` | Per-user block list (`blocker_user_id`, `blocked_user_id`). |

## Tasks (household)

| Table | Purpose |
|---|---|
| `task_lists` / `tasks` | Household task lists and tasks. Tasks have `assignees_json`, status (`todo` / `in_progress` / `done`), `rrule` for recurrence, `last_spawned_at`, `recurrence_parent_id`, `archived_at`. |
| `task_deadline_notifications` | Dedup table for fired deadline notifications, keyed by `(task_id, due_date)`. |
| `task_comments` / `task_attachments` | Comments and file attachments on tasks (§23.68). |

## Calendar (household)

| Table | Purpose |
|---|---|
| `calendars` | Personal + space calendars. Personal calendars are owned by a username; space calendars share lifecycle with their space. |
| `calendar_events` | Personal calendar events with `rrule`, attendees, `mirrored_from` (when a space event is mirrored into a personal calendar). |

## Pages (household)

| Table | Purpose |
|---|---|
| `pages` | Household-level wiki pages — title, content, cover image, lock-by/at/expiry, pending-delete approval. |
| `page_edit_history` | Append-only history of page edits. Unique by `(page_id, version)`. |

## Gallery (§23.119)

| Table | Purpose |
|---|---|
| `gallery_albums` | Album shells. `space_id IS NULL` for household-level albums. `retention_exempt` opts the album out of space retention sweeps. |
| `gallery_items` | Album items — type (`photo` / `video`), filename + thumbnail filename, dimensions, duration, caption, taken_at, sort order. |

## Child protection

| Table | Purpose |
|---|---|
| `cp_guardians` | Guardian → minor links granting view/control rights (§CP). |
| `cp_minor_blocks` | Per-minor block list applied by guardians. |
| `minor_space_memberships_audit` | Append-only audit of minor join/leave/block events on spaces. |
| `guardian_audit_log` | Append-only audit of guardian actions on minors. |

## Search

| Table | Purpose |
|---|---|
| `search_index` (FTS5 virtual) | Unified contentless full-text index across posts, space posts, DM messages, and pages. `scope` discriminates the source table; `ref_id` is the source-row id. Tokenizer is `unicode61 remove_diacritics 2`. |

## Schema source

- **File**: `socialhome/migrations/0001_initial.sql`
- **Spec**: §28 (migrations) and §29 (schema reference).

When adding or changing a table after v1, drop a `0002_*.sql` (or
later) into `socialhome/migrations/` and update both the matching
section above and the `Sqlite*Repo` that owns it. See `CLAUDE.md` →
**"Keep docs in sync"** for the reviewer rule.
