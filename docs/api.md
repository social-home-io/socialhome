# HTTP API Reference

Social Home exposes HTTP APIs on two distinct surfaces:

- **HFS** (per-household server) — serves the web UI, mobile apps,
  and any third-party integrations the household admin enables.
  Everything under `/api/*` plus `/federation/inbox/{id}` (federation inbound).
- **GFS** (global federation server) — serves the public
  space-directory, RTC signalling relay, and operator admin portal.
  Everything under `/gfs/*`, `/cluster/*`, and `/admin*`.

This file lists every live endpoint. For the *why* behind the
protocol events these routes trigger, see
[protocol/](./protocol/README.md).

## Authentication

| Model | Where | How |
|---|---|---|
| **Bearer token** | HFS `/api/*` | `Authorization: Bearer <token>` or, for WebSocket only, `?token=<token>`. Tokens are minted via `/api/auth/token` (standalone) or via the HA adapter. |
| **Signed envelope** | HFS `/federation/inbox/{id}`, GFS `/gfs/*` | Ed25519 signature inside the posted envelope. No separate auth header — the signature *is* the auth. |
| **Cookie session** | GFS `/admin*` | `admin_auth` middleware. Logged in via `POST /admin/login` with a bcrypt-verified password. |
| **None** | Health, VAPID key, public SSR pages, directory listings | Explicitly public. |

API tokens appear in access logs (because of the WebSocket `?token=`
fallback) and browser history. **Operators must redact tokens from
log aggregation.** Code must never log the full query string of
`/api/ws`.

## Conventions

- Content type is `application/json` unless otherwise stated.
  Multipart is used for avatar / cover / media uploads.
- Responses follow `{"ok": true, …}` on success and
  `{"ok": false, "error": {"code": "...", "message": "..."}}` on
  domain errors. HTTP status codes are standard: 200 / 201 / 204 for
  success, 400 for validation, 401 for missing auth, 403 for
  authorisation failures, 404 for missing resources, 409 for
  conflicts, 429 for rate limits.
- Pagination uses `?limit=N&cursor=…`. Cursors are opaque; don't
  parse them.
- Timestamps are ISO-8601 UTC, serialised via orjson.

## HFS — Authentication & self

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/token` | Issue a bearer token (standalone mode). |
| GET | `/api/me` | Current user profile. |
| PATCH | `/api/me` | Update display name, timezone, language, etc. |
| GET | `/api/me/picture` | Download current user's avatar. |
| POST | `/api/me/picture` | Upload avatar (multipart). |
| DELETE | `/api/me/picture` | Remove avatar. |
| POST | `/api/me/picture/refresh-from-ha` | HA-mode only: re-fetch from HA user profile. |
| GET | `/api/me/export` | Initiate a data-export job. |
| GET | `/api/me/corner` | "My Corner" aggregated feed. |
| GET / POST / DELETE | `/api/me/tokens[/{id}]` | Manage personal API tokens. |

Admins also have:

| Method | Path | Purpose |
|---|---|---|
| GET / DELETE | `/api/admin/tokens[/{id}]` | List / revoke any user's tokens. |
| GET | `/api/admin/ha-users` | HA-mode: list HA users for provisioning. |
| POST | `/api/admin/ha-users/{username}/provision` | Create a Social Home user from an HA user. |

## HFS — Users

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/users` | List all users on this HFS. |
| GET | `/api/users/{user_id}` | Fetch a user profile. |
| PATCH | `/api/users/{user_id}` | Admin-only update (or self). |
| GET | `/api/users/{user_id}/picture` | Fetch another user's avatar. |
| GET | `/api/users/{user_id}/export` | Admin-only export of another user's data. |

### Personal user aliases (§4.1.6)

Viewer-private renames of other users (local or remote). Aliases never federate — only the requesting user sees them. Resolution priority `space_display_name > personal_alias > display_name` is applied server-side wherever a user reference is rendered (currently the space-members endpoint; other endpoints follow incrementally).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/aliases/users` | List the viewer's personal aliases. |
| PUT | `/api/aliases/users/{user_id}` | Set or update the viewer's alias for a target user. |
| DELETE | `/api/aliases/users/{user_id}` | Clear the viewer's alias for a target user. |

## HFS — Household feed

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/feed` | Summary with latest posts + highlights. |
| GET | `/api/feed/posts` | Paginated post list. |
| POST | `/api/feed/posts` | Create a household post. |
| GET / PATCH / DELETE | `/api/feed/posts/{id}` | Read / edit / delete one post. |
| GET / POST / DELETE | `/api/feed/posts/{id}/reactions[/{emoji}]` | List reactions; add / remove own. |
| GET / POST | `/api/feed/posts/{id}/comments` | List / add comments. |
| PATCH / DELETE | `/api/feed/posts/{id}/comments/{cid}` | Edit / delete own comment. |
| POST | `/api/feed/posts/{id}/save` | Bookmark. |
| GET | `/api/feed/saved` | List bookmarks. |
| GET | `/api/me/feed/read` | Caller's scroll-restoration watermark. Returns `{last_read_post_id, last_read_at}`. |
| POST | `/api/me/feed/read` | Mark a post read. Body: `{"post_id": "..."}` (or `null` to clear). 404 on unknown post id. |
| GET | `/api/me/subscriptions` | Caller's subscribed spaces — `{subscriptions: [{space_id, subscribed_at}, ...]}`, newest first. A subscription = a read-only member row (`role='subscriber'` in `space_members`); the caller receives the same content-delivery stream as real members but is blocked on post / comment / reaction writes. Distinct from the dashboard "Spaces you follow" widget, which pins spaces the user is already a full member of — see `corner_service` + `preferences_json['followed_space_ids']`. |

## HFS — Spaces

See [protocol/spaces.md](./protocol/spaces.md) for the federation
events these routes fire.

**Space CRUD**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/spaces` | List spaces the caller belongs to. |
| POST | `/api/spaces` | Create a new space. |
| GET / PATCH / DELETE | `/api/spaces/{id}` | Read / update / delete. |
| POST | `/api/spaces/join` | Join a space via an invite token. |
| PATCH | `/api/spaces/{id}/ownership` | Transfer ownership. |
| GET | `/api/admin/spaces` | Admin-only: list all spaces on this HFS. |
| GET | `/api/spaces/{id}/feed` | Space feed summary. |
| POST | `/api/spaces/{id}/sync` | Trigger a re-sync with the space hosts. |
| POST / DELETE | `/api/spaces/{id}/subscribe` | Subscribe / unsubscribe to a public or global space. Idempotent. Subscribe adds the caller as `role='subscriber'` in `space_members` (read-only member — receives content, cannot post / comment / react). Private / household spaces return 403. Unsubscribe is a no-op for users who aren't subscribers (won't demote real members). Returns `{subscribed}`. |

**Members**

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/spaces/{id}/members` | List / invite. |
| GET / PATCH / DELETE | `/api/spaces/{id}/members/me` | Self member profile. |
| POST / DELETE | `/api/spaces/{id}/members/me/picture` | Space-specific avatar. |
| GET / PATCH / DELETE | `/api/spaces/{id}/members/{user_id}` | Admin-only ops. |
| GET | `/api/spaces/{id}/members/{user_id}/picture` | Fetch a member's space avatar. |

**Invites / joins / moderation**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/spaces/{id}/invite-tokens` | Mint an invite token. |
| GET | `/api/spaces/{id}/join-requests` | Pending requests. |
| POST | `/api/spaces/{id}/join-requests/{req_id}/{approve\|reject}` | Decide. |
| POST | `/api/spaces/{id}/remote-invites` | Invite a user on another HFS. |
| GET | `/api/remote_invites` | Remote invites pending for this household. |
| POST | `/api/remote_invites/{token}/{accept\|decline}` | Respond. |
| POST | `/api/spaces/{id}/ban` | Ban a user from a space. |
| GET / DELETE | `/api/spaces/{id}/bans[/{user_id}]` | Ban list management. |
| GET | `/api/spaces/{id}/moderation` | Moderation queue. |
| POST | `/api/spaces/{id}/moderation/{item_id}/{approve\|reject}` | Decide. |

**Appearance**

| Method | Path | Purpose |
|---|---|---|
| GET / POST / DELETE | `/api/spaces/{id}/cover` | Space cover image. |
| GET / PATCH | `/api/spaces/{id}/theme` | Space-level theme. |

**Customisation**

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/spaces/{id}/links` | List / create admin-configured sidebar quick-links. Members see; admin/owner writes. Body: `{label, url, position?}`. |
| PATCH / DELETE | `/api/spaces/{id}/links/{link_id}` | Update or remove a link. Admin/owner. |
| GET / PUT | `/api/spaces/{id}/notif-prefs` | Caller's per-space notification level. Body: `{level}` where level ∈ `"all"` \| `"mentions"` \| `"muted"`. Muted suppresses `space_post_created` notifications; `mentions` only fires when the caller appears in the post's `mentions`. |

**Bot personas (bot-bridge)**

Named bots that post into a space via the bot-bridge. Each bot has its
own Bearer token; see the "Bot-bridge" section under *Integrations* for
how those tokens are used to post.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/spaces/{id}/bots` | List bots visible to the caller (all members see all bots). |
| POST | `/api/spaces/{id}/bots` | Create a bot. Body: `{scope, slug, name, icon}`. Admin required for `scope="space"`. Returns `{...bot, token}` — token is shown once. |
| PATCH | `/api/spaces/{id}/bots/{bot_id}` | Update `name`/`icon`. Owner/admin for any bot; members for their own `scope="member"` bots. |
| DELETE | `/api/spaces/{id}/bots/{bot_id}` | Delete. Same permissions as PATCH. Existing posts remain (author falls back to "Home Assistant"). |
| POST | `/api/spaces/{id}/bots/{bot_id}/token` | Rotate the Bearer token. Returns the new plaintext token — show once. |

**Space-scoped content** — posts, comments, reactions, pages,
tasks, calendar, stickies, gallery, polls — follow identical
patterns (`GET/POST/PATCH/DELETE`). See the per-feature endpoint
sections below.

## HFS — Content types

### Posts, comments, reactions

Same route shapes as the household feed, prefixed by `/api/spaces/{id}/`:

```
/api/spaces/{id}/posts
/api/spaces/{id}/posts/{pid}
/api/spaces/{id}/posts/{pid}/reactions[/{emoji}]
/api/spaces/{id}/posts/{pid}/comments[/{cid}]
```

### Pages

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/pages` | Household-level pages. |
| GET / PATCH / DELETE | `/api/pages/{id}` | CRUD. |
| POST | `/api/pages/{id}/lock` | Acquire 5-minute edit lock. |
| POST | `/api/pages/{id}/lock/refresh` | Extend lock. |
| GET | `/api/pages/{id}/versions` | Version history. |
| POST | `/api/pages/{id}/revert` | Revert to earlier version. |
| POST | `/api/pages/{id}/{delete-request\|delete-approve\|delete-cancel}` | Two-admin delete. |
| GET / POST / PATCH / DELETE | `/api/spaces/{id}/pages[/{pid}]` | Space-scoped pages. |
| POST | `/api/spaces/{id}/pages/{pid}/resolve-conflict` | Force-pick in a conflict. |

### Tasks

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/tasks/lists` | List / create task lists. |
| GET / PATCH / DELETE | `/api/tasks/lists/{id}` | CRUD. |
| POST | `/api/tasks/lists/{id}/reorder` | Reorder tasks in a list. |
| GET / POST | `/api/tasks/lists/{id}/tasks` | List / create tasks. |
| GET / PATCH / DELETE | `/api/tasks/{id}` | CRUD for a single task. |
| GET / POST / PATCH / DELETE | `/api/tasks/{id}/comments[/{cid}]` | Task comments. |
| GET / POST / DELETE | `/api/tasks/{id}/attachments[/{aid}]` | Task attachments. |
| …same under `/api/spaces/{id}/tasks/...` | | Space-scoped variants. |

### Calendar

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/calendars` | List / create calendars. |
| GET / PATCH / DELETE | `/api/calendars/{id}` | CRUD. |
| GET / POST | `/api/calendars/{id}/events` | List / create events. |
| GET / PATCH / DELETE | `/api/calendars/events/{id}` | CRUD. |
| GET | `/api/calendars/events/{id}/rsvps` | List RSVPs. `?occurrence_at=<iso>` (URL-encoded) scopes to one occurrence of a recurring event. |
| POST | `/api/calendars/events/{id}/rsvp` | Set own RSVP. Body: `{"status": "going\|maybe\|declined", "occurrence_at": "<iso>"}`. `occurrence_at` required for recurring events; defaults to `event.start` for non-recurring. |
| DELETE | `/api/calendars/events/{id}/rsvp` | Clear own RSVP. `?occurrence_at=<iso>` (URL-encoded) required for recurring. |
| POST | `/api/calendars/events/{id}/approve` | Approve / deny pending request-to-join (capped events, Phase C). Approver = event creator OR space admin. Body: `{"user_id": "<uid>", "action": "approve\|deny", "occurrence_at"?: "<iso>"}`. |
| GET | `/api/calendars/events/{id}/pending` | List pending requests (capped events). Approver-only. `?occurrence_at=<iso>` to scope. |
| GET | `/api/calendars/events/{id}/reminders` | List own reminders (Phase D). Optional `?occurrence_at=<iso>` filter. |
| POST | `/api/calendars/events/{id}/reminders` | Add a reminder for the calling user. Body: `{"minutes_before": <int>, "occurrence_at"?: "<iso>"}`. |
| DELETE | `/api/calendars/events/{id}/reminders` | Remove a reminder. Required `?minutes_before=<int>` and optional `?occurrence_at=<iso>`. |
| POST | `/api/calendars/{id}/import_ics` | Upload iCal. |
| POST | `/api/calendars/{id}/{import_image\|import_prompt}` | AI-assisted import. |
| GET | `/api/calendar/{id}/export.ics` | iCal export. |
| …same under `/api/spaces/{id}/calendar/...` | | Space-scoped variants. |

### Stickies, shopping, bazaar, gallery

Same CRUD shape:

```
/api/stickies[/{id}]
/api/shopping[/{id}]          POST /complete, /uncomplete, /clear-completed
/api/bazaar[/{id}]            /{id}/bids[/{bid_id}]  POST /accept, /reject
/api/gallery/albums[/{id}]    /{id}/items[/{iid}]
```

### Bazaar offers & saved listings (§23.23)

Offers write to a dedicated `bazaar_offers` table — distinct from
auction/bid_from `bazaar_bids`. State machine:
`pending → accepted | rejected | withdrawn` (terminal).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/bazaar/{id}/offers` | List offers. Seller sees all; others see only their own. |
| POST | `/api/bazaar/{id}/offers` | Make an offer on a fixed / negotiable listing. Body: `{amount, message?}`. Returns the new offer row. |
| DELETE | `/api/bazaar/{id}/offers/{offer_id}` | Offerer withdraws a pending offer. |
| POST | `/api/bazaar/{id}/offers/{offer_id}/accept` | Seller accepts → listing flips to `sold` and every other pending offer on the listing is auto-rejected. |
| POST | `/api/bazaar/{id}/offers/{offer_id}/reject` | Seller rejects. Body: `{reason?}`. Listing stays active. |
| GET / POST / DELETE | `/api/bazaar/{id}/save` | Probe / bookmark / un-bookmark. POST returns `{saved: true}` (201). |
| GET | `/api/me/bazaar/saved` | Caller's bookmarked listings — `{saved: [{post_id, saved_at}]}`. Client hydrates each via `/api/bazaar/{post_id}`. |

### Polls & schedule polls

Polls attach to an existing post. Reply polls use `/poll`, schedule
polls (Doodle-style) use `/schedule-poll`. Household variants are
unfederated; space variants (below) fan out `SPACE_POLL_*` /
`SPACE_SCHEDULE_*` federation events to paired peers.

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/posts/{id}/poll` | Fetch summary / attach a new reply poll. |
| POST / DELETE | `/api/posts/{id}/poll/vote` | Cast / retract own vote. |
| POST | `/api/posts/{id}/poll/close` | Close (author only). |
| POST | `/api/posts/{id}/schedule-poll` | Attach a new schedule poll. |
| GET | `/api/schedule-polls/{id}/summary` | Slots + responses. |
| POST | `/api/schedule-polls/{id}/respond` | Respond yes/maybe/no to a slot. |
| DELETE | `/api/schedule-polls/{id}/slots/{slot_id}/response` | Retract own response. |
| POST | `/api/schedule-polls/{id}/finalize` | Author picks winning slot. |
| GET / POST | `/api/spaces/{id}/posts/{pid}/poll` | Space-scoped reply poll. |
| POST / DELETE | `/api/spaces/{id}/posts/{pid}/poll/vote` | Cast / retract space vote. |
| POST | `/api/spaces/{id}/posts/{pid}/poll/close` | Close (author only). |
| POST | `/api/spaces/{id}/posts/{pid}/schedule-poll` | Space-scoped schedule poll. |
| GET | `/api/spaces/{id}/schedule-polls/{pid}/summary` | Space schedule summary. |
| POST | `/api/spaces/{id}/schedule-polls/{pid}/respond` | Respond to a space slot. |
| DELETE | `/api/spaces/{id}/schedule-polls/{pid}/slots/{slot_id}/response` | Retract. |
| POST | `/api/spaces/{id}/schedule-polls/{pid}/finalize` | Author finalizes. |

## HFS — Conversations (DMs)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/conversations` | List. |
| POST | `/api/conversations/dm` | Get-or-create 1:1 DM. |
| POST | `/api/conversations/group` | Create group conversation. |
| GET / POST | `/api/conversations/{id}/messages` | List / send. |
| PATCH / DELETE | `/api/conversations/{id}/messages/{mid}` | Edit / delete own. |
| POST | `/api/conversations/{id}/{read\|unread}` | Unread state. `read` bulk-upserts `conversation_delivery_state` rows to `read` for every non-own message and returns `{ok, marked}`. |
| POST | `/api/conversations/{id}/messages/{mid}/delivered` | Stamp the caller's delivery state for one message — idempotent; `read` supersedes. See [DM reliability](./protocol/dm.md#reliability--read-receipts--delivery-state-125). |
| GET | `/api/conversations/{id}/delivery-states` | Per-message delivery/read rows for the whole conversation. Optional `?message_ids=a,b,c`. |
| GET | `/api/conversations/{id}/gaps` | §12.5 sequence holes detected for this conversation — `{gaps: [{sender_user_id, expected_seq, detected_at}]}`. Members only. |
| GET | `/api/conversations/{id}/calls` | Call history in this conversation. |

## HFS — Presence, notifications, search

| Method | Path | Purpose |
|---|---|---|
| GET / POST / DELETE | `/api/presence` | Own presence. |
| POST | `/api/presence/location` | Location update (rate-limited 10/min). |
| GET | `/api/spaces/{id}/presence` | Presence visible in this space. Carries GPS only — `zone_name` is stripped at the household boundary (§23.8.6). |
| GET / POST | `/api/spaces/{id}/zones` | List or create a per-space display zone (§23.8.7). `GET` open to space members; `POST` admin/owner only. Body: `{name, latitude, longitude, radius_m, color?}`. |
| PATCH / DELETE | `/api/spaces/{id}/zones/{zone_id}` | Update or delete a per-space zone. Admin/owner only. Partial update; `color: null` clears, omitting fields leaves them. |
| PATCH | `/api/spaces/{id}/members/me/location-sharing` | Member-self-service opt in or out of GPS sharing for this space (§23.8.8). Body: `{enabled: bool}`. Returns `{location_share_enabled: bool}`. |
| GET | `/api/notifications` | Paginated list. |
| GET | `/api/notifications/unread-count` | Count. |
| POST | `/api/notifications/{id}/read` | Mark read. |
| POST | `/api/notifications/read-all` | Mark all read. |
| GET | `/api/search` | Full-text search (posts, comments, spaces, users). |

## HFS — Pairing

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/pairing/initiate` | Generate a QR payload. Empty body; base URL comes from the platform adapter (`[standalone].external_url` or the HA integration's pushed base). Returns 422 `NOT_CONFIGURED` if unset. |
| POST | `/api/pairing/accept` | Scanner posts its side of the DH. |
| POST | `/api/pairing/confirm` | Confirm SAS-verified pair. |
| POST | `/api/pairing/peer-accept` | **Peer-to-peer bootstrap** (§11). Public (Ed25519 body signature is the auth). B → A: delivers B's identity + DH keys so A can materialise its RemoteInstance and surface the SAS. Body: `{token, verification_code, identity_pk, dh_pk, inbox_url, display_name?, sig_suite?, pq_identity_pk?, pq_algorithm?, signature}`. Returns `{ok, instance_id, replay}`. |
| POST | `/api/pairing/peer-confirm` | **Peer-to-peer bootstrap** (§11). Public. A → B after A's admin enters the matching SAS: flips B's local status to CONFIRMED. Body: `{token, instance_id, signature}`. Returns `{ok, instance_id, replay}`. |
| POST | `/api/pairing/introduce` | Introduce self to an intermediary. |
| POST | `/api/pairing/auto-pair-via` | Ask a mutual peer to relay. |
| GET / POST | `/api/pairing/auto-pair-requests[/{id}/{approve\|decline}]` | Auto-pair queue. |
| GET | `/api/pairing/connections` | Paired peers. |
| GET | `/api/connections` | Alias of the above. |
| GET / DELETE | `/api/pairing/connections/{instance_id}` | Read / unpair. |
| GET / POST | `/api/pairing/relay-requests[/{id}/{approve\|decline}]` | Relay-request queue. |

## HFS — Calls & WebRTC

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/webrtc/ice_servers` | STUN/TURN config (alias: `/api/calls/ice-servers`). |
| GET / POST | `/api/calls` | List / initiate. |
| GET | `/api/calls/active` | Current active call. |
| POST | `/api/calls/{id}/{answer\|join\|decline\|hangup}` | Lifecycle. |
| POST | `/api/calls/{id}/ice` | Trickle ICE candidate. |
| POST | `/api/calls/{id}/quality` | Report RTT / jitter / loss. |

## HFS — Push

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/push/vapid_public_key` | Public VAPID key (unauth). |
| POST / PUT / DELETE | `/api/push/subscribe[/{sub_id}]` | Register / update / remove. |
| GET | `/api/push/subscriptions` | List own subscriptions. |

## HFS — GFS connections & public spaces

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/gfs/connections` | List / connect. |
| GET / DELETE | `/api/gfs/connections/{id}` | Inspect / disconnect. |
| POST | `/api/gfs/connections/{id}/appeal` | Appeal a ban. |
| GET | `/api/gfs/publications` | Spaces published to GFS. |
| POST / DELETE | `/api/spaces/{id}/publish/{gfs_id}` | Publish / unpublish. |
| GET | `/api/public_spaces` | Aggregated directory. |
| POST | `/api/public_spaces/refresh` | Force-poll GFS. |
| POST | `/api/public_spaces/{space_id}/join-request` | Ask to join. |
| POST | `/api/public_spaces/{space_id}/hide` | Hide locally. |
| POST / DELETE | `/api/public_spaces/blocked_instances/{id}` | Block a GFS. |
| GET | `/api/peer_spaces` | Spaces advertised by directly-paired peers. |

## HFS — Child protection

`/api/cp/*` — see `socialhome/routes/child_protection.py`.
Guardian-scoped operations: manage guardians, list minor's spaces and
conversations, set age gates, read audit logs. All require the minor
or their guardian (household admins have an override).

Two distinct audit surfaces:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cp/minors/{minor_id}/audit-log` | Guardian-driven actions (enable/disable CP, add/remove guardians, toggle blocks). |
| GET | `/api/cp/minors/{minor_id}/membership-audit` | System-driven space-membership changes affecting the minor — \`joined\` / \`removed\` / \`blocked\`. Written automatically by `SpaceService.add_member` / `remove_member` / `ban` when the target user has child-protection enabled. |

## HFS — Reports

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/reports` | Own reports. |
| GET | `/api/admin/reports` | Admin queue. |
| PATCH | `/api/admin/reports/{id}/resolve` | Resolve a report. |

## HFS — Bot-bridge (Home Assistant → Social Home)

Lets HA automations post into spaces and DMs via HTTP. See
[protocol/bot-bridge.md](./protocol/bot-bridge.md) if present; the
CRUD surface for the bot personas themselves is under
**Spaces → Bot personas** above.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/bot-bridge/spaces/{space_id}` | **Per-bot** Bearer token (from `POST /api/spaces/{id}/bots`). User API tokens are rejected. | Post as the SpaceBot the token was issued to. Body: `{title?, message}`. Fails 403 when `space.bot_enabled=false`. |
| POST | `/api/bot-bridge/conversations/{conversation_id}` | User Bearer token. | Post a system message into a DM. Fails 403 when `conversation.bot_enabled=false`. |

Both endpoints reject requests carrying `X-Ingress-User` (403) so a
UI-authenticated user cannot impersonate the integration.

## HFS — HA integration bridge

Pushed to by the separate `ha-integration` HACS package. The integration
resolves the externally-reachable URL inside HA (`external_url` or
Nabu Casa Remote UI) and mirrors it here so the addon can stamp it into
new pairing QRs + fan out `URL_UPDATED` to already-paired peers. Admin
Bearer auth (the integration holds the auto-provisioned token).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ha/integration/federation-base` | Current base the addon advertises. Returns `{"base": string \| null}`. |
| PUT | `/api/ha/integration/federation-base` | Upsert `{"base": "https://..."}`. Validates scheme (http/https) and strips trailing slash. On value change, fans out `URL_UPDATED` to every confirmed peer. Returns `{ok, base, changed, peers_notified}`. |

## HFS — Storage, backup, misc

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/storage/usage` | Own usage. |
| GET / PATCH | `/api/admin/storage/quota` | Admin quota config. |
| POST | `/api/backup/pre_backup` | HA snapshot hook. |
| POST | `/api/backup/post_backup` | HA snapshot hook. |
| GET / POST | `/api/backup/{export\|import}` | Full archive round-trip. |
| GET / PATCH | `/api/theme` | Household theme. |
| GET | `/api/household/features` | Feature toggles. |
| POST | `/api/media/upload` | Upload a blob. |
| GET | `/api/media/{filename}` | Download a blob. |
| GET | `/healthz` | Liveness (public). |

## HFS — WebSockets

| Path | Purpose |
|---|---|
| `GET /api/ws` | Realtime event stream — posts, comments, presence, typing, calls, notifications. Auth via `Authorization: Bearer` or `?token=`. Frames: `"ping"` → `"pong"`; JSON `{"type":"typing","conversation_id":"..."}`. |
| `GET /api/stt/stream` | Streaming speech-to-text (binary audio frames → `{"type":"final","text":"..."}`). |

> Speech-to-text and AI data generation are HA-adapter-only in v1 — the
> standalone adapter raises `NotImplementedError` on
> `transcribe_audio` / `stream_transcribe_audio` / `generate_ai_data`.
> The HA adapter requires `[homeassistant].stt_entity_id` to be set
> (e.g. `stt.home_assistant_cloud`) before STT routes return data.

## HFS — federation inbox

| Method | Path | Purpose |
|---|---|---|
| POST | `/federation/inbox/{inbox_id}` | Inbound federation envelope. Runs the §24.11 validation pipeline before dispatch. See [protocol/README.md](./protocol/README.md). |

## GFS — Public relay (HTTPS REST, SH → GFS)

| Method | Path | Purpose |
|---|---|---|
| POST | `/gfs/register` | Register instance. |
| POST | `/gfs/publish` | Publish a space. |
| POST | `/gfs/subscribe` | Subscribe to directory updates. |
| POST | `/gfs/report` | File a fraud / abuse report. |
| POST | `/gfs/appeal` | Appeal a ban. |
| GET | `/gfs/spaces` | Public directory listing. |
| GET | `/healthz` | Liveness. |

## GFS — Push WebSocket (GFS → SH)

| Method | Path | Purpose |
|---|---|---|
| GET (Upgrade) | `/gfs/ws` | Persistent push channel. SH opens this once paired; the GFS pushes `{type:"relay", space_id, event_type, payload, from_instance}` frames as space events fan out. First client frame must be a signed hello `{type:"hello", instance_id, ts, sig}` within 5 s — see spec §24.12. WebSocket close codes: 4400 protocol violation, 4401 auth failure, 4408 hello timeout, 4409 replaced. Heartbeat is the WS-protocol-level ping (30 s). |

## GFS — SH↔SH RTC signalling rendezvous (§4.2.3)

These endpoints are an in-memory bulletin board where two paired Social
Home instances drop SDP offer / answer / ICE candidates so they can
bring up a direct WebRTC DataChannel between themselves for §4.2.3
sync. The GFS holds no PeerConnection.

| Method | Path | Purpose |
|---|---|---|
| POST | `/gfs/rtc/offer` | Store an SDP offer; return a session id. |
| POST | `/gfs/rtc/answer` | Attach an SDP answer to a session. |
| POST | `/gfs/rtc/ice` | Trickle ICE candidate. |
| POST | `/gfs/rtc/ping` | HTTPS-fallback keepalive (sets `rtc_connections.transport`). |
| GET | `/gfs/rtc/session/{session_id}` | Read session state (poll). |

## GFS — Cluster

| Method | Path | Purpose |
|---|---|---|
| POST | `/cluster/sync` | Cluster-node state sync. |
| GET | `/cluster/health` | Node health. |
| POST | `/cluster/signaling-session` | Pick a least-loaded signaling node for a sync session (spec §24.10.7). |
| POST | `/cluster/signaling-session/release` | Release a signaling session on `SPACE_SYNC_DIRECT_READY` / `DIRECT_FAILED`. |

## GFS — Admin portal

**Portal**

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin` | SPA entrypoint. |
| GET | `/admin/static/{path}` | SPA assets. |
| POST | `/admin/login` | Bcrypt-verified login. |
| POST | `/admin/logout` | End session. |

**Admin API** (all require an active admin cookie session)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/overview` | Dashboard stats. |
| GET | `/admin/api/clients` | Registered instances. |
| POST | `/admin/api/clients/{instance_id}/{accept\|reject\|ban}` | Moderate instances. |
| GET | `/admin/api/spaces` | Published spaces. |
| POST | `/admin/api/spaces/{space_id}/{accept\|reject\|ban}` | Moderate spaces. |
| GET / PATCH | `/admin/api/policy` | Operator policy. |
| GET / PATCH | `/admin/api/branding` | Branding text. |
| POST / DELETE | `/admin/api/branding/header-image` | Header image. |
| GET | `/admin/api/reports` | Report queue. |
| PATCH | `/admin/api/reports/{id}/review` | Decide. |
| GET | `/admin/api/appeals` | Appeal queue. |
| PATCH | `/admin/api/appeals/{id}/decide` | Decide. |
| GET | `/admin/api/audit` | Audit log. |
| GET | `/admin/api/cluster` | Cluster status. |
| GET | `/admin/api/cluster/peers[/{node_id}]` | Peer list / detail. |
| POST | `/admin/api/cluster/peers/{node_id}/ping` | Healthcheck a peer. |

## GFS — Public SSR pages

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Operator landing page. |
| GET | `/spaces/{slug}` | Public space detail. |
| GET | `/join/{gfs_token}` | Landing page for an invitation link. |

These pages are server-rendered HTML and require no auth.

## Rate limits

| Endpoint | Limit |
|---|---|
| `POST /api/presence/location` | 10 / min / user |
| `POST /api/calls` | 10 / min / user |
| `POST /api/calls/{id}/decline` | 10 / min / user |
| `POST /api/calls/{id}/hangup` | 30 / min / user |
| `POST /cluster/signaling-session{,/release}` | 60 / min / paired instance |
| Federation inbound (per signing instance) | Rolling window; see §24.11. |

Rate-limit responses return HTTP 429 with a `Retry-After` header.

## Version & compatibility

API responses include `X-Social-Home-Version` when running in
standalone mode (derived from `pyproject.toml`). Breaking changes
bump the major version. Endpoints added in minor versions are
announced in `CHANGELOG.md`.
