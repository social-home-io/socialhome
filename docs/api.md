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
[protocol/](./protocol/README.md). For the high-level shape — HFS ↔
GFS topology, identity model, sync tiers — see
[architecture.md](./architecture.md).

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
| POST | `/api/auth/redeem-password-reset` | Public: redeem a one-time admin-issued reset token to set a new password. Body: `{token, new_password}`. 204 on success; 410 when expired / already used; 422 for short password / missing fields. Same per-IP rate limit as `/api/auth/token` (5 / 15 min). |
| POST | `/api/admin/users/{username}/issue-password-reset` | Admin: mint a single-use, 1h-TTL reset token for a user. Returns `{token, expires_at, username}` once — admin hands the resulting `/reset-password?token=…` URL to the user out-of-band. Standalone mode has no SMTP, so this is the only recovery path. |
| GET | `/api/admin/auth-audit` | Admin: read the auth audit log — append-only trail of login attempts (success + failure), reset issues, and reset redeems. Query `?limit=N` (default 100, max 500). |
| GET | `/api/me` | Current user profile. |
| PATCH | `/api/me` | Update profile fields. Body: any of `{"display_name", "bio", "preferences", "tz"}`. `tz` is validated against the IANA database (unknown name → 422); the SPA's cold-start probe sends it once on first login so personal calendar events default to the user's local wall clock. |
| POST | `/api/me/username` | Rename the caller's login username. Body: `{"username"}`. The cryptographic `user_id` is unaffected. Invalid / reserved / taken → 422; HA-managed (HA-source) accounts → 403. |
| POST | `/api/me/handle` | Set the caller's public `@handle`. Body: `{"handle"}`. Returns `{handle}` on success. Per-household case-insensitive uniqueness enforced. Invalid / reserved / taken → 422. Unlike `/api/me/username`, **all** users (including HA-managed) may set their own handle. |
| GET | `/api/me/picture` | Download current user's avatar. |
| POST | `/api/me/picture` | Upload avatar (multipart). |
| DELETE | `/api/me/picture` | Remove avatar. |
| POST | `/api/me/picture/refresh-from-ha` | HA-mode only: re-fetch from HA user profile. |
| GET | `/api/me/notify-targets` | Selectable push notify targets for the notification-settings dropdown. HA mode lists the household's `notify.*` entities (`[{entity_id, name}]`); other platforms return `[]`. |
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

### Personal user blocks (§Privacy)

Voluntary adult-to-adult block list — the viewer hides another user's highlights, household-feed posts, presence, notifications, friends-list entry and DMs. Distinct from the parent-driven CP block (`/api/cp/minors/{minor_id}/blocks`). The block stays local to the viewer's instance — the inbound DM gate runs on the receive side, so a remote sender is also rejected without exporting the block list.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/blocks` | List the caller's blocks `[{user_id, blocked_at}]`, newest first. |
| POST | `/api/blocks` | Add a block. Body: `{user_id}`. Self-block → 422. |
| DELETE | `/api/blocks/{user_id}` | Remove a block. Idempotent. |

### Momentum (§Momentum)

Household-broadcast posts that fan to a 3-hop peer mesh. Replies are themselves moments, linked via `parent_moment_id`. Rate-limited at one top-level moment per author per 15 minutes; replies and reactions are exempt. Default visibility: 24h; 7d for moments authored by anyone the viewer follows.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/moments` | List visible moments (block-aware, follow-aware). |
| POST | `/api/moments` | Create a moment. Body: `{content, media_url?, media_type?, duration_ms?, parent_moment_id?}`. |
| GET | `/api/moments/archive` | Full retention-window list. Optional `?tag=<name>` filters to moments tagged with that hashtag (lowercase, no leading `#`). |
| GET | `/api/moments/hashtags` | Trending hashtags inside the viewer's visibility window. Returns `{"hashtags": [{"tag", "count"}, …]}`; `?limit=N` (default 20, capped at 50). |
| GET | `/api/moments/{id}` | Detail incl. replies + reactions. |
| DELETE | `/api/moments/{id}` | Author or admin delete. |
| PUT | `/api/moments/{id}/reaction` | Set / change reaction. Body: `{emoji}`. |
| DELETE | `/api/moments/{id}/reaction` | Clear own reaction. |
| POST | `/api/moments/{id}/report` | Report a moment. Body: `{category, notes?}`. |
| GET | `/api/moments/follows` | List who I follow. |
| POST | `/api/moments/follows` | Follow a user. Body: `{user_id}`. |
| DELETE | `/api/moments/follows/{user_id}` | Unfollow. |
| POST | `/api/highlights/{id}/report` | Report a highlight (same `content_reports` queue). |

## HFS — Household feed

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/feed` | Newest-first list of household posts. Each entry carries the post fields (`id`, `author`, `type`, `content`, …) plus a `latest_comment` field — the most recent non-deleted comment on the post (or `null` when there are none). Used by the SPA to render an inline preview line under each card without an N+1 fetch. |
| GET | `/api/feed/posts` | Paginated post list. |
| POST | `/api/feed/posts` | Create a household post. Body: `{type, content?, media_url?, location?, pinned?, no_link_preview?}`. `type` ∈ `text\|image\|video\|file\|poll\|schedule\|location`. When `type='location'` the body **must** include `location: {lat, lon, label?}` — server truncates `lat`/`lon` to 4 decimal places (~11 m) at the boundary, `label` is optional and capped at 80 characters. `bazaar` posts are space-scoped; `event` posts are auto-created by the calendar bridge. |
| GET / PATCH / DELETE | `/api/feed/posts/{id}` | Read / edit / delete one post. |
| GET / POST / DELETE | `/api/feed/posts/{id}/reactions[/{emoji}]` | List reactions; add / remove own. |
| GET / POST | `/api/feed/posts/{id}/comments` | List / add comments. |
| PATCH / DELETE | `/api/feed/posts/{id}/comments/{cid}` | Edit / delete own comment. |
| POST | `/api/feed/posts/{id}/save` | Bookmark. |
| GET | `/api/feed/saved` | List bookmarks. |
| GET | `/api/me/feed/read` | Caller's scroll-restoration watermark. Returns `{last_read_post_id, last_read_at}`. |
| POST | `/api/me/feed/read` | Mark a post read. Body: `{"post_id": "..."}` (or `null` to clear). 404 on unknown post id. |
| GET | `/api/me/subscriptions` | Caller's subscribed spaces — `{subscriptions: [{space_id, subscribed_at}, ...]}`, newest first. A subscription = a read-only member row (`role='subscriber'` in `space_members`); the caller receives the same content-delivery stream as real members but is blocked on post / comment / reaction writes. Distinct from the dashboard "Spaces you follow" widget, which pins spaces the user is already a full member of — see `corner_service` + `preferences_json['followed_space_ids']`. |
| GET | `/api/me/join-requests` | Caller's outstanding (`pending`) outbound join-requests — `{pending_space_ids: [...]}`. The space browser merges these so a "Request to join" stays "Request pending" across reloads instead of reverting. Scoped to the caller's own `space_join_requests` rows. |

## HFS — Spaces

See [protocol/spaces.md](./protocol/spaces.md) for the federation
events these routes fire.

**Space CRUD**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/spaces` | List spaces the caller belongs to. |
| POST | `/api/spaces` | Create a new space. Body: `{name}` (required), `description?`, `emoji?`, `space_type` (`private`\|`household`\|`public`\|`global`, default `private`; the API accepts any of the four, the SPA only offers `global` in the create dialog when a global server is connected), `join_mode` (default `invite_only`), `lat?`/`lon?` (public/global tiers only and now **optional** — a public space without coordinates is listed in discovery but not map-pinned), `category?` (one of the 10 discovery values — `general`, `hobby_crafts`, `sports_outdoors`, `gaming`, `music_arts`, `food_drink`, `tech`, `local`, `family_parenting`, `learning`; applied for public/global), `min_age?` (`0`\|`13`\|`16`\|`18`; applied only for public/global tiers, ignored otherwise). |
| GET / PATCH / DELETE | `/api/spaces/{id}` | Read / update / **dissolve**. GET response includes `category` (the §23.50 discovery taxonomy, normalized to `general` when unset). PATCH accepts `category` (plus the usual `name`/`description`/`emoji`/`join_mode`/feature toggles); PATCH ignores `space_type` (publication tier is proposal-gated — see below). DELETE opens a `dissolve` **approval proposal** (v_16) — a permanent hard delete that needs a majority of admins to approve (executes immediately for a solo-admin space). |
| GET / POST | `/api/spaces/{id}/proposals` | Multi-admin approval (quorum) for critical actions (v_16). GET lists open proposals + tally. POST `{action, space_type?}` opens a `dissolve` or `set_public_tier` proposal. Any admin may propose; executes once a majority of admins approve. |
| POST | `/api/spaces/{id}/proposals/{pid}/vote` | Admin approves / rejects an open proposal (`{approve}`). A reject cancels it; a majority of approvals executes it. |
| POST / DELETE | `/api/spaces/{id}/archive` | Archive (read-only, reversible) / unarchive. Owner or admin. |
| GET | `/api/spaces/{id}/compat` | Owner / admin only. Per-space protocol-version compatibility of member households (#319 ¶5). Returns `{"ours": <int>, "min_member_proto_version": <int\|null>, "lagging_features": [...], "behind_members": [{instance_id, display_name, proto_version, lacking_features}]}`. `lagging_features` are the shared-space features unavailable because the weakest known member household lacks them; `behind_members` lists each lagging household and the space features it's missing. Member households that have never advertised capabilities (mid-handshake) are excluded — they aren't genuinely behind. `min_member_proto_version` is `null` when there are no known remote members. |
| POST | `/api/spaces/join` | Join a space via an invite token. |
| PATCH | `/api/spaces/{id}/ownership` | Transfer ownership. |
| GET | `/api/admin/spaces` | Admin-only: list all spaces on this HFS. |
| GET | `/api/spaces/{id}/feed` | Space feed summary. |
| POST | `/api/spaces/{id}/sync` | Trigger a re-sync with the space hosts. |
| POST / DELETE | `/api/spaces/{id}/subscribe` | Subscribe / unsubscribe to a public or global space. Idempotent. Subscribe adds the caller as `role='subscriber'` in `space_members` (read-only member — receives content, cannot post / comment / react). For an id with **no local row**, subscribe first mirrors the listing from the first paired GFS that serves it (`GET {gfs}/gfs/spaces/{id}`) and seats a `space_type=global` stub with the GFS-served authority key (TOFU-pinned) before registering on the GFS relay — no GFS knows it, or the listing fails validation → unchanged 404; a paired GFS that errors → **502 `GFS_UNAVAILABLE`**. Private / household spaces return 403. Unsubscribe is a no-op for users who aren't subscribers (won't demote real members); when it removes the **last** local member of a provable GFS mirror it also unsubscribes from every paired GFS and purges the stub with its content. Returns `{subscribed}`. |

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
| POST | `/api/spaces/{id}/remote-invites` | Invite a user on another HFS. Returns `201 {token}` when minted directly (owner, or a seed-holding delegated admin on a delegation-ON space); returns `202 {"status":"pending_owner_approval"}` when a non-owner admin invites on a delegation-OFF space — the invite is forwarded to the host for owner approval (rides `SPACE_REMOTE_ADMIN_ACTION`, action `invite`). |
| PATCH | `/api/spaces/{id}/remote-members/{instance_id}/{user_id}` | Owner-only: promote/demote a remote member (`{"role":"admin"\|"member"}`). Federates as `SPACE_MEMBER_ROLE_CHANGED`. |
| DELETE | `/api/spaces/{id}/remote-members/{instance_id}/{user_id}` | Admin/owner: kick a remote member from the host's side. Routes through `SpaceService.remove_remote_member` — federates `SPACE_REMOTE_MEMBER_REMOVED` and rotates the epoch. |
| GET | `/api/remote_invites` | Remote invites pending for this household. |
| POST | `/api/remote_invites/{token}/{accept\|decline}` | Respond. |
| POST | `/api/spaces/{id}/ban` | Ban a user from a space. |
| GET / DELETE | `/api/spaces/{id}/bans[/{user_id}]` | Ban list management. |
| GET | `/api/spaces/{id}/moderation` | Moderation queue. |
| POST | `/api/spaces/{id}/moderation/{item_id}/{approve\|reject}` | Decide. |

**Appearance**

| Method | Path | Purpose |
|---|---|---|
| GET / POST / DELETE | `/api/spaces/{id}/cover` | Space cover image (hero banner). |
| GET / POST / DELETE | `/api/spaces/{id}/icon` | Space icon (avatar), distinct from the cover; owner/admin upload. Falls back to the emoji when unset. |
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

`POST /api/spaces/{id}/posts` accepts the same body as the household
endpoint, including `{type: "location", location: {lat, lon, label?}}`.
Space-scoped location posts ride on the existing
`SPACE_POST_CREATED` federation event — peers receive the location
inside the encrypted payload and render the same map card.

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

**Embedded media URLs.** Page `content` is a markdown body. Any
`/api/media/{filename}` reference inside it (typically pasted from the
gallery's "Copy reference" button) is **re-signed by the server on
every read** with a fresh 1h-TTL signature, so the SPA's `<img src>`
loads without an `Authorization` header. Storage stays canonical: the
PATCH/POST handlers strip any `?exp=&sig=…` the editor might have
echoed back before the body lands in the DB. Same treatment applies
to the scalar `cover_image_url`. Clients should paste canonical
`/api/media/{filename}` paths and let the server handle signing —
saving a stale signed URL is safe (the strip is idempotent) but
unnecessary.

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
| GET | `/api/calendars/invitees` | List cross-household invitees for the calendar event dialog (§23.60). Returns members of confirmed paired peer instances grouped by instance: `{"instances": [{"instance_id", "instance_name", "members": [{user_id, instance_id, remote_username, display_name, picture_hash, picture_url}]}]}`. **Local household members are never returned** — coordinating with a household member is done via the calendar selector, not the invite picker. Empty list when no instances are paired. |
| GET / POST | `/api/calendars` | List / create calendars. |
| GET / PATCH / DELETE | `/api/calendars/{id}` | CRUD. |
| GET / POST | `/api/calendars/{id}/events` | List / create events. Body fields: `summary`, `start`, `end`, `all_day`, `description`, `attendees`, `rrule`, `rsvp_enabled`, `cover_url`, `tz`. `start` / `end` are UTC ISO 8601 — the SPA converts the local-time form input via `Intl` before submitting. `tz` is the optional IANA name the event anchors to (e.g. `"Europe/Berlin"`); when absent the server resolves to the creator's `users.tz`, then the household `preferences.tz` row, then `"UTC"`. `attendees` accepts only confirmed-paired-instance user_ids — local household member user_ids are rejected with 422 (coordinate via the calendar selector instead). Authorization: any active household member can create / edit events on any household member's personal calendar. Response carries the resolved `tz` so the SPA can render the event in the host's wall clock with an "≈ HH:MM your time" hint when the viewer's browser zone differs. **All-day events keep their day bounds in the event's `tz`, not in UTC** — the composer submits `00:00` / `23:59` of the authored day converted through `tz`, so an all-day "1 May" authored in `Europe/Zurich` is on the wire as `2026-04-30T22:00:00Z → 2026-05-01T21:59:00Z`. A client MUST read an all-day event's day back in `tz` (falling back to `"UTC"` when absent); reading its UTC components instead shifts the day for every household east or west of UTC and makes a single-day event look like a two-day span. Every event in the response carries `copies` — the **server-authoritative** sibling set of the household fan-out, resolved from `client_event_uuid`: `[{"event_id", "calendar_id", "owner_username"}, …]`, one entry per member calendar the event was shared to. It is **independent of which calendars the caller currently has visible** — that independence is the point: a client that infers the sibling set from its loaded agenda will duplicate rows when editing a shared event. `copies` is `[]` for an event with no `client_event_uuid` (legacy / ICS-imported rows) and for space events (those live in `space_calendar_events` and have no household fan-out); it is `[{self}]` for a uuid'd event that was only written to one calendar. It never contains `remote_invite` mirrors — those carry the *peer's* `client_event_uuid` and are not ours to edit. For a **recurring** event the ids in `copies` are the **stored** row ids, never the synthetic `{id}@{iso}` occurrence ids the range query expands to. `POST` is idempotent per `(calendar_id, client_event_uuid)`: when a local row already exists on that calendar with the same client-minted uuid, the request **updates that row in place** (preserving its `id`, `created_by` and provenance, emitting an *updated* rather than a *created* domain / federation event) instead of minting a second copy of the same event on the same calendar. The status code stays `201`, and the response body is the updated row. This makes a retried fan-out POST — and an edit the client sent as a create — safe; the partial unique index `ux_calendar_events_fanout` is the on-disk backstop. |
| GET / PATCH / DELETE | `/api/calendars/events/{id}` | CRUD. PATCH treats `cover_url` as tri-state: omitted = leave unchanged, explicit `null` = clear, string = set. `tz` is validated against the IANA database; an unknown name returns 422. PATCH also accepts `client_event_uuid` to attach the event to a shared household group; absent leaves any existing group id untouched. Promoting an event into a group that another local row on the **same calendar** already occupies returns **422** (the partial unique index `ux_calendar_events_fanout` allows one local copy per `(calendar_id, client_event_uuid)`, and two indistinguishable copies of one shared event on one calendar is a client error). GET and PATCH responses carry `copies` with the same shape and guarantees as on `/api/calendars/{id}/events` — the authoritative, visibility-independent fan-out sibling set (`[]` for a uuid-less row; `[]` for a space event, which this route also serves; never `remote_invite` mirrors). |
| GET | `/api/calendars/events/{id}/rsvps` | List RSVPs. `?occurrence_at=<iso>` (URL-encoded) scopes to one occurrence of a recurring event. |
| POST | `/api/calendars/events/{id}/rsvp` | Set own RSVP. Body: `{"status": "going\|maybe\|declined", "occurrence_at": "<iso>"}`. `occurrence_at` required for recurring events; defaults to `event.start` for non-recurring. |
| DELETE | `/api/calendars/events/{id}/rsvp` | Clear own RSVP. `?occurrence_at=<iso>` (URL-encoded) required for recurring. |
| POST | `/api/calendars/events/{id}/approve` | Approve / deny pending request-to-join (capped events, Phase C). Approver = event creator OR space admin. Body: `{"user_id": "<uid>", "action": "approve\|deny", "occurrence_at"?: "<iso>"}`. |
| GET | `/api/calendars/events/{id}/pending` | List pending requests (capped events). Approver-only. `?occurrence_at=<iso>` to scope. |
| GET | `/api/calendars/events/{id}/reminders` | List own reminders (Phase D). Optional `?occurrence_at=<iso>` filter. |
| POST | `/api/calendars/events/{id}/reminders` | Add a reminder for the calling user. Body: `{"minutes_before": <int>, "occurrence_at"?: "<iso>"}`. |
| DELETE | `/api/calendars/events/{id}/reminders` | Remove a reminder. Required `?minutes_before=<int>` and optional `?occurrence_at=<iso>`. |
| GET | `/api/calendars/events/{id}/export.ics` | iCal export of one event (Phase F). Member-only. Includes the caller's reminders as VALARM blocks. |
| GET | `/api/spaces/{id}/calendar/export.ics` | Subscribable iCal feed for the next 90 days. Auth via `?token=<feed-token>` (no Bearer required — public path). Honours `If-None-Match` for conditional GET. |
| POST | `/api/spaces/{id}/calendar/feed-token` | Mint / regenerate a per-(user, space) feed token. Returns `{token, url}`. |
| DELETE | `/api/spaces/{id}/calendar/feed-token` | Revoke the current feed token. Future fetches return 401. |
| POST | `/api/calendars/{id}/import_ics` | Upload iCal. |
| POST | `/api/calendars/{id}/{import_image\|import_prompt}` | AI-assisted import. |
| GET | `/api/calendar/{id}/export.ics` | iCal export. |
| …same under `/api/spaces/{id}/calendar/...` | | Space-scoped variants. Space event create/list also accepts/returns `announce_in_feed` (§23.15, default **false**): when true the event also mirrors to the space feed as a `PostType.EVENT` post; otherwise it lives only in the Calendar tab. |

### Stickies, shopping, bazaar, gallery

Same CRUD shape:

```
/api/stickies[/{id}]
/api/shopping[/{id}]          POST /complete, /uncomplete, /clear-completed
                              PATCH /{id} (text, store)
                              GET  /stores                store catalogue
                              POST /stores                add a store directly
                              PATCH/DELETE /stores/{name} rename-or-merge / remove
                              PUT  /stores/order          drag-defined trip order
/api/bazaar[/{id}]            /{id}/bids[/{bid_id}]  POST /accept, /reject
/api/gallery/albums[/{id}]    /{id}/items[/{iid}]
```

`/api/shopping`: items optionally carry a free-form `store` name; the
server auto-upserts a `shopping_stores` row on first sighting so the
SPA can render the list grouped by store in the household's
drag-defined trip order. `PATCH /api/shopping/{id}` is tri-state on
`store` — omitted = keep, `null` = clear, string = set. `PUT
/api/shopping/stores/order` accepts `{"order": ["Bakery", "Aldi", …]}`;
unknown names are dropped, missing names retain their relative order
past the explicitly-ordered tail.

Store names are unique **case-insensitively** (`ux_shopping_stores_name_nocase`,
migration `0048`), and every store-scoped lookup resolves `COLLATE NOCASE`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/shopping/stores` | Add a store without assigning it to an item. Body `{"name": "Bakery"}` → `201` `{name, sort_order}`. Idempotent case-insensitively: an existing store comes back unchanged (same casing, same `sort_order`), never a conflict. Blank name → 422, as is `.` or `..`: the store routes carry the name in the URL path and a dot segment is normalised away before routing, so such a row could never be renamed or deleted. |
| PATCH | `/api/shopping/stores/{name}` | Rename — **or merge**. If another store already holds the new name (case-insensitively), the old store's items fold onto it and the old row is dropped; the survivor keeps its own `sort_order`. Returns `{old_name, new_name, merged, moved_items}`. `new_name` is the spelling that *survived*, which on a merge is the target's existing casing, not the casing sent. Unknown store → 404. (There is no 409; collapsing a duplicate is a household's only way out of a pre-`0048` case fork.) |
| DELETE | `/api/shopping/stores/{name}` | Remove the row and clear `store` on every item that referenced it (matched `COLLATE NOCASE`). Returns `{name, cleared}`; a missing row is `200` with `cleared: 0`, not a 4xx. |

### Highlights (§Highlights)

Personal "highlights" pillar — per-author per-day frame bag, federated to
peers based on the author's audience kind. Retention is per-user
(`preferences_json.highlights.retention_days` and `.max_count`); the
in-process retention scheduler prunes expired and over-quota rows.

| Method | Path | Purpose |
|---|---|---|
| POST   | `/api/highlights/frames` | Create or append today's frame. Body: `{frame_type, media_url, caption_text?, caption_emoji?, duration_ms?, audience_kind?, audience?[]}`. Returns `{highlight, frame}`. |
| GET    | `/api/highlights` | List highlights visible to the caller (mine + peers'). Returns `[{highlight, frames, unseen_count}]`. |
| GET    | `/api/highlights/{id}` | Highlight detail with frames. Authors get per-frame `views` and `reactions` keyed by frame id inline. |
| DELETE | `/api/highlights/{id}` | Author removes the whole highlight. Cascades to frames / views / reactions. |
| DELETE | `/api/highlights/frames/{id}` | Author removes one frame. |
| POST   | `/api/highlights/frames/{id}/view` | Mark frame seen. Authors' own views are silently ignored. |
| PUT    | `/api/highlights/frames/{id}/reaction` | Body: `{emoji}`. Upsert — one reaction per viewer per frame. |
| DELETE | `/api/highlights/frames/{id}/reaction` | Clear the caller's reaction on this frame. |
| POST   | `/api/highlights/{id}/share` | Author shares the highlight into a feed. Body: `{scope: 'household' \| 'space', space_id?, note?}`. Creates a `highlight_share` post; returns 201 `{post_id, highlight_id}` or 202 `{queued: true}` for moderated spaces. |
| POST   | `/api/highlights/frames/{id}/dm-reply` | Send a DM that quotes a frame. Body: `{conversation_id, content}`. The frame snapshot is frozen on the message so the reply survives retention. |
| POST   | `/api/highlights/{id}/publish` | Author mints a public share token via a paired GFS. Body: `{gfs_id, label?}`. Returns 201 `{token, url, label}` — the URL is `https://{gfs}/highlight/{instance}/{highlight}/{token}`. Mint additional tokens with repeat calls. |
| GET    | `/api/highlights/{id}/publish` | Local publication snapshot: `{published, gfs_id, published_at}`. Token list lives on the GFS. |
| DELETE | `/api/highlights/{id}/publish` | Drop the publication; CASCADE on the GFS revokes every token. |
| DELETE | `/api/highlights/{id}/publish/{token}` | Revoke a single share token; other tokens under the same publication keep working. |

Audience kinds:

* `all_paired` — default; every confirmed peer instance.
* `households` — author-picked subset of peer instances.
* `users` — author-picked subset of individual user ids; the
  receiving instance enforces the per-user allow-list before
  surfacing to local viewers.

Each `/api/gallery/albums` row carries an `is_system: bool` flag. The
auto-managed "Posts" album (one per household, one per space; pinned
to the top of the list) returns `is_system: true` and `owner_user_id:
null`. `DELETE /api/gallery/albums/{id}`, `PATCH …`, `POST
…/items`, `DELETE …/items/{iid}`, and the retention-exempt route
return **HTTP 403** with code `system album cannot be …` for any
system album — items appear and disappear strictly with their source
feed post. Each item also carries `source_post_id: string | null`;
non-null means the item was mirrored from a feed post.

### Space Bazaar tab (§23.15)

Bazaar is a first-class space tab alongside Calendar / Gallery. Listings
are space-scoped (`bazaar_listings.space_id`); the tab browses one space.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/spaces/{id}/bazaar` | List every listing in this space (any status), newest-first. Member-only; 403 `FEATURE_DISABLED` (`space:bazaar`) when the space's Bazaar feature is off. |

`POST /api/bazaar` accepts an optional `announce_in_feed: bool` (default
**false**). The listing always appears in the space Bazaar tab; the
wrapper post only surfaces in the space feed when `announce_in_feed` is
true (otherwise it carries `space_posts.hidden_from_feed = 1`). Creating a
listing in a space whose `bazaar` feature is off returns 403.

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
| GET | `/api/conversations` | List the caller's conversations. Each row carries `members[]` (other participants only — caller is filtered out) and `member_count` so the inbox renders avatar stacks + peer-name fallbacks (`Anna · Bob`) without N+1 follow-up fetches. Also returns the caller's own `last_read_at` (ISO 8601 or `null`) — the SPA uses it to find the first-unread message in the loaded thread window and anchor the entry scroll on a "New messages" divider. |
| POST | `/api/conversations/dm` | Get-or-create 1:1 DM. Body `{username}`. |
| POST | `/api/conversations/group` | Create group conversation (≥3 participants total — creator + ≥2 others). Body `{members: [username, ...], name?: string}`. |
| GET / POST | `/api/conversations/{id}/messages` | List / send. |
| PATCH / DELETE | `/api/conversations/{id}/messages/{mid}` | Edit / delete own. |
| POST | `/api/conversations/{id}/{read\|unread}` | Unread state. `read` bulk-upserts `conversation_delivery_state` rows to `read` for every non-own message and returns `{ok, marked}`. |
| POST | `/api/conversations/{id}/messages/{mid}/delivered` | Stamp the caller's delivery state for one message — idempotent; `read` supersedes. See [DM reliability](./protocol/dm.md#reliability--read-receipts--delivery-state-125). |
| PUT / DELETE | `/api/conversations/{id}/messages/{mid}/reactions/{emoji}` | Add / remove the caller's emoji reaction on a DM message. `{emoji}` is URL-encoded so multi-byte glyphs survive routing. Membership-gated; fans out a `dm.message_reaction` WS frame to every conversation member and federates as `DM_MESSAGE_REACTION` to remote peers. |
| GET | `/api/conversations/{id}/messages/{mid}/reactions` | Full reaction roster for one message — `{reactions: [{user_id, emoji}]}`. Membership-gated. |
| GET | `/api/conversations/{id}/delivery-states` | Per-message delivery/read rows for the whole conversation. Optional `?message_ids=a,b,c`. |
| GET | `/api/conversations/{id}/gaps` | §12.5 sequence holes detected for this conversation — `{gaps: [{sender_user_id, expected_seq, detected_at}]}`. Members only. |
| GET | `/api/conversations/{id}/calls` | Call history in this conversation. |

## HFS — Presence, notifications, search

| Method | Path | Purpose |
|---|---|---|
| GET / POST / DELETE | `/api/presence` | Own presence. `GET` rows now also carry session-presence fields `is_online`, `is_idle`, `last_seen_at` so the UI can render the green / amber dot without a separate fetch. |
| POST | `/api/presence/location` | Location update (rate-limited 10/min). |
| GET | `/api/spaces/{id}/presence` | Presence visible in this space. Carries GPS only — `zone_name` is stripped at the household boundary (§23.8.6). |
| GET | `/api/spaces/{id}/members` | Roster for this space. Each row carries `is_online`, `is_idle`, `last_seen_at` alongside the member metadata so the SPA renders the same dot on space pages. |
| GET | `/api/conversations/{id}/members` | Roster for one DM / group DM. Each row carries `user_id`, `username`, `display_name`, `picture_url` (signed avatar URL or `null`), `is_self`, `is_online`, `is_idle`, `last_seen_at` so the thread header can render a WhatsApp-style "Online" / "Last seen 2 h ago" line AND surface the peer's avatar next to the TopBar title without a follow-up fetch. |
| POST | `/api/conversations/{id}/messages` | Send a message into a DM / group DM. Body: `{ content, type?, media_url?, file_name?, mime_type?, file_size_bytes?, reply_to_id? }`. `type` ∈ {`text`, `image`, `video`, `file`, `transcript`, `location`} — defaults to `text`. Media types (`image` / `video` / `file`) require `media_url` (typically obtained from `POST /api/media/upload` immediately before) and may carry the file metadata triple. **Rejected with 422 `MEDIA_REQUIRES_DIRECT_PAIRING`** when the conversation requires the multi-hop `DM_RELAY` route — media only flows over directly-paired federation, see [DM media](./protocol/dm-media.md). |
| GET | `/api/conversations/{id}/messages` | List messages in a conversation. Each row carries the same shape as the POST body above, plus `id`, `sender_user_id`, `created_at`, `edited_at`, `deleted`, and the cross-household `media_sync_status` (`null` / `'pending'` / `'failed'`) so the SPA knows when to render the preview-spinner overlay on a media bubble. |
| GET / POST | `/api/spaces/{id}/zones` | List or create a per-space display zone (§23.8.7). `GET` open to space members; `POST` admin/owner only. Body: `{name, latitude, longitude, radius_m, color?}`. |
| PATCH / DELETE | `/api/spaces/{id}/zones/{zone_id}` | Update or delete a per-space zone. Admin/owner only. Partial update; `color: null` clears, omitting fields leaves them. |
| PATCH | `/api/spaces/{id}/members/me/location-sharing` | Member-self-service opt in or out of GPS sharing for this space (§23.8.8). Body: `{enabled: bool}`. Returns `{location_share_enabled: bool}`. |
| GET | `/api/notifications` | Paginated list. Each row carries an optional `link_url` deep-link target — the bell renders unread items as anchors. `dm_message` rows are **collapsed per conversation** — a burst of N inbound DMs from the same peer bumps one bell row rather than stacking N entries; dedupe is scoped to currently-unread rows, so once the recipient opens the thread (`POST /api/conversations/{id}/read` clears the row) the next DM starts a fresh one. `calendar_event_created` rows are **scoped to the event's audience**: personal-calendar events notify the calendar's owner only (and not when the owner created the event themselves); space-calendar events notify space members except the creator. The household-wide fanout from earlier builds is gone. |
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
| POST | `/api/pairing/introduce` | Introduce self to an intermediary. |
| POST | `/api/pairing/auto-pair-via` | Ask a mutual peer to relay. |
| GET / POST | `/api/pairing/auto-pair-requests[/{id}/{approve\|decline}]` | Auto-pair queue. |
| GET | `/api/pairing/connections` | Paired peers (admin/ops view). Now also carries `home_lat` / `home_lon` per row (4dp-truncated, `null` when unset) so the SPA can render a household map without a follow-up fetch. Each row carries `instance_id`, `display_name`, `status`, `reachable`, **`transport`** (`"rtc"` when the WebRTC DataChannel is open, `"https"` when running on the HTTPS-inbox fallback, `null` when the peer is unreachable or pending), **`share_home`** (`true` / `false` — whether this household's home coordinates are shared with the peer; defaults to `true`), **`queued_envelopes`** (integer — federation envelopes still `pending` in the outbox for that peer, i.e. waiting to be sent and retried automatically once the peer is reachable; `0` when there is no backlog, which is what tells an admin a long-dark household apart from a momentary drop), and **`dropped_envelopes`** (integer — envelopes in the terminal `failed` state: a PERMANENT rejection or an exhausted retry budget. These are **not** retried and are purged 24 h after going terminal, so a non-zero value is delivery loss, not a queue). The two counts are deliberately separate: reporting only the pending one renders dropped messages as "queued for delivery". Whitelisted fields only. |
| GET | `/api/connections` | Alias of the above. Returns the same shape including `share_home`, `queued_envelopes`, `dropped_envelopes` and `local_alias` per row. |
| GET / DELETE | `/api/pairing/connections/{instance_id}` | Read / unpair. |
| GET | `/api/pairing/connections/{instance_id}/transport-detail` | Admin-only. Returns `{"last_relay": {"via": <iid>, "ts": <iso>} \| null}` — the most recent DM that relayed via a third household within the last 24h. Powers the SPA's Manage detail panel. |
| PATCH | `/api/pairing/connections/{instance_id}` | Admin-only. Accepts `{"share_home": bool}` — flip whether this household's home coordinates are shared with the peer. Setting `false` immediately fires a one-shot `LOCAL_HOME_LOCATION_CHANGED` with null coords to revoke the peer's pin; setting `true` fires the current coords to restore it. Idempotent. |
| PATCH | `/api/pairing/connections/{instance_id}/alias` | Admin-only. Body `{alias: string\|null}` — set or clear the local-only rename of the peer (cap 80 chars; whitespace-only clears). Returns `{instance_id, display_name, local_alias, effective_display_name}`. Never federated. |
| GET / POST | `/api/pairing/relay-requests[/{id}/{approve\|decline}]` | Relay-request queue. |
| GET | `/api/friends` | Connected-people dashboard payload (non-admin). Returns `{instance, households[], totals}` — the local household block + every confirmed remote household with its member list (joining `remote_instances` × `remote_users`) plus household coordinates. Whitelisted fields only — `routing_secret` / `key_self_to_remote` / `remote_inbox_url` / identity public keys never appear. |
| GET | `/api/admin/federation/compat` | Admin-only. Federation-compatibility panel. Returns `{"ours": <int>, "peers": [...]}` where `ours` is this build's advertised `proto_version` and each peer carries `instance_id`, `display_name`, `proto_version`, `status`, `last_reachable_at`, `capabilities_known` (bool — `false` ⇒ peer is paired but has never advertised capabilities, so its `proto_version` is the conservative default rather than a confirmed value), and `lacking_features` (human-readable labels of features the peer's version is below). Confirmed peers only, ordered by display name. |
| POST | `/api/admin/federation/resync` | Admin-only. Ask a peer to re-broadcast state for a named scope (§319.6). Body `{instance_id, scope}` where `scope` is `"capabilities"`, `"space:<id>"`, or `"calendar:<id>"` (the latter two replay membership-gated content via the §4.4 resume path). Returns `{"status": "ok", "instance_id", "scope"}`. 400 `UNPROCESSABLE` on a missing `instance_id` / unrecognised scope; 409 `PEER_TOO_OLD` when the peer's advertised `proto_version` is below v_19 (it has no resync handler). |
| GET | `/api/admin/federation/external-url` | Admin-only. The admin-set federation inbox base URL. Returns `{base, effective, source}` — `base` is the stored value (`null` when unset), `effective` is what `PlatformAdapter.get_federation_base()` actually resolves (the peer-facing URL, i.e. `base` + `/federation/inbox`), and `source` is `"manual"` / `"auto"` / `null`. The two differ whenever an automatic source is also present, so the UI can say which one is in effect rather than leave an admin guessing. |
| PUT | `/api/admin/federation/external-url` | Admin-only. Upsert `{"base": "https://..."}`. Validates the scheme (http/https), strips a trailing slash, and strips a trailing `/federation/inbox` so pasting a full inbox URL doesn't double it. `{"base": null}` (or an empty string) deletes the row, handing control back to the deployment's automatic source. On a value change, fans out `URL_UPDATED` to every confirmed peer so their cached `remote_inbox_url` tracks the move. Returns `{ok, base, changed, peers_notified}`. 422 `UNPROCESSABLE` on a non-http(s) value. |
| GET | `/api/admin/federation/ice-servers` | Admin-only, read-only. The ICE servers the **federation transport** is currently using, **with secrets removed**: each entry is `{urls, kinds, has_credentials}` — `credential` is an HMAC of `webrtc_turn_secret` under the recommended coturn setup, so it is never returned, and `username` (an `expiry:user_id` pair) is reduced to the `has_credentials` flag. Also returns `has_turn`, `turn_usable` (the same conclusions the boot diagnostics warn about) and `pulls_from_home_assistant`. Purely diagnostic — whether RTC can traverse a network is otherwise visible only in a log warning. |
| GET | `/api/admin/diagnostics` | Admin-only. Support bundle an operator can attach to a bug report: build/version, deployment mode, whether a federation base resolves (host only), the peer table with reachability timestamps (`last_reachable_at` / `unreachable_since` / `capabilities_seen_at`), outbox backlog grouped by peer and status with `max_attempts`, redacted ICE servers, and the schema migration version. Built from an **allow-list** of fields — never by subtracting `SENSITIVE_FIELDS` from rows — so a column added later is absent by default rather than leaked. Excludes peer display names, peer inbox paths (they embed a per-pair secret; only scheme+host is kept), home coordinates, TURN credentials and all user content. `?download=1` adds a `Content-Disposition` filename. |
| PATCH | `/api/admin/instance` | Admin-only. Rename the household — set the federated instance display name. Body `{display_name}` (1–80 chars after trimming). Persists `instance_identity.display_name` and re-broadcasts it to every confirmed peer via `INSTANCE_CAPABILITIES_UPDATED`, so paired households see the new name without re-pairing. Returns `{"display_name"}`. 422 `UNPROCESSABLE` on a missing/blank/over-length name. |

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
| GET | `/api/spaces/{id}/publications` | This space's GFS publications, each with `status` (`active`/`pending`/`banned`). Admin. |
| POST / DELETE | `/api/spaces/{id}/publish/{gfs_id}` | Publish (returns the publication object: `{space_id, gfs_connection_id, published_at, status}`; `422` if the GFS is unreachable or rejects) / unpublish. |
| GET | `/api/public_spaces` | Aggregated directory. |
| POST | `/api/public_spaces/refresh` | Force-poll GFS. |
| POST | `/api/public_spaces/{space_id}/join-request` | Ask to join. |
| POST | `/api/public_spaces/{space_id}/hide` | Hide locally. |
| POST / DELETE | `/api/public_spaces/blocked_instances/{id}` | Block a GFS. |
| GET | `/api/peer_spaces` | Spaces advertised by directly-paired peers. |

**Public-Momentum (§Momentum-public)**

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/moments/public/registrations` | List / opt this user into the directory on a GFS. |
| DELETE / PATCH | `/api/moments/public/registrations/{gfs_id}` | Deregister / flip `default_share`. |
| GET / POST | `/api/moments/public/follows` | List / follow public author. |
| DELETE | `/api/moments/public/follows/{gfs_id}/{user_id}` | Unfollow. |
| GET | `/api/gfs/{gfs_id}/moments/users` | Proxy GFS directory; passes `?q=<substr>` through. |
| GET | `/api/gfs/{gfs_id}/moments/users/{user_id}/picture` | Proxy GFS-mirrored avatar bytes (used by Discover cards). |

## HFS — Child protection

`/api/cp/*` — see `socialhome/routes/child_protection.py`.
Guardian-scoped operations: manage guardians, list minor's spaces and
conversations, set age gates, read audit logs. All require the minor
or their guardian (household admins have an override).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cp/protection` | **Admin-only.** Protection status for every household user — `{users: [{user_id, username, is_minor, declared_age}]}`. `is_minor`/`declared_age` are `SENSITIVE_FIELDS` stripped from `/api/users`; this admin-gated endpoint is their only surface (powers the admin panel's "Protected" column). |

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

Pushed to by the companion `socialhome` Home Assistant integration. The
integration resolves the externally-reachable URL inside HA
(`external_url` or Nabu Casa Remote UI) and mirrors it here so the addon
can stamp it into new pairing QRs + fan out `URL_UPDATED` to
already-paired peers. Admin Bearer auth (the integration holds the
auto-provisioned token written to `<data_dir>/integration_token.txt`).

There is **no separate download step** under the add-on: `HaBootstrap`
pushes a Supervisor discovery entry on every boot
(`platform/haos/bootstrap.py`, `platform/haos/supervisor.py` →
`POST /discovery`) advertising the add-on's host, port and integration
token, so Home Assistant surfaces the integration for setup on its own.

The integration's value is stored separately from the admin-set one
(`instance_config['ha_federation_base']` vs `['federation_base_url']`)
because they mean different things: the integration pushes *Home
Assistant's* URL and the adapter appends `/api/socialhome/inbox` (an
HA-hosted view forwarding into the add-on), whereas an admin-entered value
is the address Social Home is reachable at directly and gets Social Home's
own `/federation/inbox`. Conflating them would yield an unreachable URL for
one source or the other. An admin-set value wins when present, so typing
one is never a silent no-op; clearing it returns control to the
integration.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ha/integration/federation-base` | Current base the addon advertises. Returns `{"base": string \| null}`. |
| PUT | `/api/ha/integration/federation-base` | Upsert `{"base": "https://..."}`. Validates scheme (http/https) and strips trailing slash. On value change, fans out `URL_UPDATED` to every confirmed peer. Returns `{ok, base, changed, peers_notified}`. |

### WebRTC ICE servers are pulled, not pushed

There is **no** `/api/ha/integration/ice-servers` endpoint. Earlier revisions of
this page documented a `GET` and a `PUT` there; both were removed and now 404.

Social Home pulls the list itself: `HaIceServerSync`
(`platform/ha/ice_servers_sync.py`) issues the `web_rtc/ice_servers` command
over the **HA Core WebSocket**, once shortly after boot and then every 24 h
(60 s retry after a failure), and applies the result via
`FederationService.set_ice_servers`. Wired for both `ha` and `haos` in each
adapter's `on_startup`.

The push was replaced because the integration's listener only fired on YAML
reloads — so a freshly-rotated Nabu Casa Cloud TURN credential could stay
invisible for hours — and because a failed push left no record on the Social
Home side.

Consequences worth knowing:

* The pulled list **replaces** the config-derived one
  (`webrtc_stun_url` / `webrtc_turn_url` / …) for the federation transport. A
  reply with nothing usable in it is ignored rather than applied, so HA without
  the WebRTC integration cannot blank out an operator's servers.
* The **first fetch attempt** releases the federation transport's
  first-handshake gate, whatever its outcome — list applied, nothing usable,
  or the fetch failed outright. The transport holds its first outbound
  handshake for up to `ICE_PRIME_TIMEOUT_S` (15 s) waiting for that signal, so
  the boot-time outbox drain doesn't build every peer STUN-only just before the
  TURN credentials land; the bound is what stops an unreachable HA Core from
  stalling federation. Platforms that never pull (standalone) release the gate
  at startup instead — `PlatformAdapter.provides_ice_servers`.
* A list arriving **after** peers were built still reaches them. A content
  change bumps the transport's ICE generation, and any peer that has not yet
  opened its DataChannel is retired and rebuilt under the new list on its next
  send. Connected peers are untouched. Every push also clears the per-peer RTC
  retry suppressions, changed or not.
* It reaches the **federation** transport only. The SPA's own
  `/api/webrtc/ice_servers` and the public highlight/moment signalling paths
  still serve the config-derived list, so under HA those can differ.

## HFS — Apps

Admin-installed embedded JS apps from the Social Home Apps catalog.
The catalog is fetched from the `socialhome-apps` GitHub releases
(`apps_catalog_url` / `SH_APPS_CATALOG_URL`). On install the bundle
tarball is downloaded, its `sha256` verified against the catalog, and
unpacked (path-traversal-guarded) under `apps_path/<app_id>/<version>/`
(`apps_path` / `SH_APPS_PATH`, default `<data_dir>/apps`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/apps` | Any member | List installed apps. Non-admins see enabled apps only; admins see all. Age-restricted apps (those with `min_age > 0`) are filtered out server-side for protected minors — the SPA never performs client-side age filtering. Each entry includes `min_age` (0/13/16/18; 0 = no restriction). |
| GET | `/api/apps/catalog` | Admin | Browse the remote app catalog (fetches `catalog.json` from the configured release URL). |
| POST | `/api/apps` | Admin | Install an app from the catalog. Body: `{app_id}`. 201 on success; 409 if already installed; 400 on bad id or failed sha256 integrity check. |
| GET | `/api/apps/{app_id}` | Any member | One installed app. 404 when missing or disabled (non-admin). |
| PATCH | `/api/apps/{app_id}` | Admin | Update app settings. Body: `{enabled}` and/or `{min_age}`. `min_age` must be one of `0/13/16/18` (0 = no restriction). |
| DELETE | `/api/apps/{app_id}` | Admin | Uninstall an app — removes the bundle from disk; cascades all per-user `app_kv` rows. |
| GET | `/api/apps/updates` | Any member | List installed apps that have a newer version in the catalog. Returns `{"updates": [{app_id, name, current_version, latest_version}]}`. Result is served from a server-side cache refreshed at most once per 24 h (a background check also runs daily). `?refresh=1` forces a live catalog re-fetch but is **admin-only** — non-admins always receive the cached result regardless of the query parameter. |
| POST | `/api/apps/{app_id}/update` | Admin | Pull and install the latest catalog version of the app. Returns the serialised `InstalledApp` (`{app_id, name, version, enabled, capabilities, icon}`) on success. 404 if the app is not installed; 400 if no newer version is available or the integrity check fails. |
| GET | `/api/apps/{app_id}/runtime` | Any member (bearer) | Launch payload for a running app. Returns `{app_id, name, entry_url, self_user_id, capabilities}` where `entry_url` is a short-lived signed bundle URL. 404 if the app is not installed; 403 if the app is disabled or the caller is a protected minor and the app has `min_age > 0`. |
| GET | `/api/apps/{app_id}/bundle/{tail}` | Signed URL / cookie (no bearer) | Serve bundle static files. The `entry_url` carries a media-signer signature over the bundle prefix as `?exp=&sig=` query parameters; on first access those are exchanged for a short-lived HttpOnly path-scoped cookie so relative sub-resources load without re-signing. Every response carries a strict CSP (`connect-src 'none'`, `worker-src 'none'`, `frame-ancestors 'self'`, etc.) and `X-Frame-Options: SAMEORIGIN`. Path traversal is guarded. Re-checks that the app is enabled on every request. |
| GET | `/api/apps/{app_id}/store` | Any member | List the caller's per-user KV entries for this app. Returns `{"items": {key: value}}`. |
| GET | `/api/apps/{app_id}/store/{key}` | Any member | Read one KV entry. Returns `{"key", "value"}`. 404 if the key does not exist. |
| PUT | `/api/apps/{app_id}/store/{key}` | Any member | Upsert a KV entry. Body: `{"value": <any JSON>}`. 413 if quota exceeded (500 keys / user, 64 KiB per value, 256-char key). 403 if the app is disabled. 404 if the app is not installed. |
| DELETE | `/api/apps/{app_id}/store/{key}` | Any member | Delete one KV entry. Returns `{"status": "ok"}`. |

**App federation (cross-household, capability v_17+)**

These endpoints let an installed app exchange state with the same app running
in a paired household.  All require a valid member bearer token and that the
app is installed and enabled; they return 404 / 403 otherwise.  The wire
transport (binary `fed-app-v1` DataChannel or `APP_MESSAGE` JSON fallback) is
selected transparently by the server.  See [protocol/apps.md](./protocol/apps.md).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/apps/{app_id}/peers` | Any member | List confirmed peer instances as `[{instance_id, display_name}]`. The SPA uses this to populate the peer picker when starting a cross-household app session. |
| GET | `/api/apps/{app_id}/contacts` | Any member | Person roster for the app's contact picker — `{contacts: [{instance_id, user_ref, display_name, is_local, online}]}`. Returns local household members (excluding the caller, `is_local: true`, `user_ref` is the user's `user_id`) and known remote users across all paired households (`is_local: false`, `user_ref` is the remote username), block-filtered. `online` is `true` for locally-connected users; always `false` for remote contacts (presence wiring deferred). **Capability v_18.** |
| POST | `/api/apps/{app_id}/sessions` | Any member | Open a person-addressed app session. Body: `{target: {instance_id, user_ref, is_local}}`. Returns `{session_id}`. Sends an `APP_SESSION {verb:"open"}` event; includes `to_user`/`from_user` when the peer is v_18+. Local targets get a direct WS loopback (no federation send) and an `AppChallengeReceived` notification for the target. 403 (`APP_CONTACT_NOT_FOUND`) when `target` is not in the caller's contact roster. Back-compat: `{peer_instance_id}` still accepted (maps to a household-addressed open, no per-user routing). |
| GET | `/api/apps/{app_id}/pending-sessions` | Any member | Drain (return + clear) the caller's replayable session invites for the app — invites that arrived while the app was closed. Read-and-clear: each invite is returned at most once, scoped to the authed user. Returns `{sessions: [{session_id, from_instance, from_user, payload}]}`. |
| POST | `/api/apps/{app_id}/messages` | Any member | Send an app-layer message to a person. Body: `{session_id, target: {instance_id, user_ref, is_local}, payload}` where `payload` is any JSON dict. The payload is AES-256-GCM-sealed inside the signed federation envelope — never sent in plaintext. 403 (`APP_CONTACT_NOT_FOUND`) when target is not a contact. Back-compat: `{session_id, peer_instance_id, payload}` still accepted. |

## HFS — Storage, backup, misc

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/storage/usage` | Own usage. |
| GET / PATCH | `/api/admin/storage/quota` | Admin quota config. |
| POST | `/api/backup/pre_backup` | HA snapshot hook. |
| POST | `/api/backup/post_backup` | HA snapshot hook. |
| GET / POST | `/api/backup/{export\|import}` | Full archive round-trip. |
| POST | `/api/recovery-kit` | Admin-only. Body `{"passphrase": str}` (≥8 chars, in body so it never hits access logs); returns the passphrase-sealed `.shrk` Recovery Kit (trust layer for same-identity disaster recovery — see `docs/crypto.md` "Recovery Kit"). 422 on a short/missing passphrase. |
| POST | `/api/setup/recovery/restore` | **Setup-gated, no bearer** (fresh-box only; 409 once setup is complete). Body `{"kit_b64": str, "passphrase": str}`. Validates the Kit, wipes the auto-minted throwaway identity, restores the Kit's trust layer (same `instance_id`), marks setup complete, and schedules a process restart. Returns `{instance_id, restart_required: true}`. 422 `BAD_KIT` (wrong passphrase / corrupt — generic, no oracle) or `RESTORE_FAILED`. |
| GET / PATCH | `/api/theme` | Household theme. |
| GET / PUT | `/api/household/preferences` | Household-wide feature toggles plus `household_name` and `tz` (IANA timezone). `PUT` accepts a partial body: `{"household_name"?: str, "toggles"?: {"feat_feed": bool, "feat_pages": bool, "feat_tasks": bool, "feat_stickies": bool, "feat_calendar": bool, "feat_bazaar": bool, "feat_presence": bool, "feat_gallery": bool}, "tz"?: "<iana>"}`. `tz` is validated via Python's `zoneinfo` — an unknown name returns 422. In ha / haos modes the value is mirrored from HA Core's `time_zone` on adapter startup; explicit operator edits via PUT are still honoured but get overwritten on the next restart. |
| GET / PATCH | `/api/me/preferences` | Per-user sidebar preferences. `GET` returns `{user_id, hide_highlights, hide_momentum, hide_bazaar}`. `PATCH` accepts a partial body; body keys MUST be a subset of `{hide_highlights, hide_momentum, hide_bazaar}` — unknown keys return 400. Changes apply only to the authenticated user; other household members are unaffected. |
| POST | `/api/media/upload` | Upload a blob. Images/audio return `{url, filename, signed_url}` once processed. **Video transcoding is async** — the upload returns immediately with `{url, thumbnail_url, filename, media_status:"processing", signed_url, signed_thumbnail_url}` before the `.webm`/`.webp` files exist; a background `MediaTranscodeService` worker produces them and pushes a `media.ready` WS frame to the uploader when done. The `.webm` output and its `.webp` poster share one UUID stem (`<stem>.webm` + `<stem>.webp`) so the poster path is derivable server-side from the media URL. List endpoints that surface the video (feed, space feed, momentum, DM messages) carry a `media_status` field (`'processing'` / `'failed'` / `'ready'`; absent ⇒ ready) **and** a signed `media_thumbnail_url` (the poster — the signed `.webp` sibling of `media_url`) on **video** items; the `media.created`/`space.post.created` WS frames carry the same poster. (Gallery items keep their own stored `thumbnail_url`.) |
| GET | `/api/media/{filename}` | Download a blob. |
| GET | `/api/map/config` | Map rendering config for the SPA: `{tile_url, attribution, max_zoom}`. `tile_url` is a **relative** (no leading slash, so it resolves against the ingress-aware `<base href>`) signed template — `api/map/tiles?z={z}&x={x}&y={y}&exp=…&sig=…` — with the literal `{z}`/`{x}`/`{y}` placeholders left for Leaflet to substitute. One HMAC (7-day TTL) authorises every tile, because the signature covers only the path. 503 `SIGNER_UNAVAILABLE` if the media signer is not wired yet (never an unsigned URL). |
| GET | `/api/map/tiles?z=&x=&y=` | Raster tile proxy. OpenStreetMap 403s browser-issued tile requests (a browser cannot set `User-Agent` / `Referer`), so the backend fetches with an identifying agent string and caches in memory. Auth via the signed `?exp=&sig=` above (Leaflet's `<img>` carries no bearer header) or a normal bearer token. Responds with the tile bytes, the upstream content type, and `Cache-Control: public, max-age=604800`. 400 on missing / non-integer / out-of-grid coordinates, 502 `TILE_UNAVAILABLE` when upstream fails and nothing cached can stand in. Upstream is `map_tile_url` / `SH_MAP_TILE_URL`. |
| GET | `/healthz` | Liveness (public). |

## HFS — WebSockets

| Path | Purpose |
|---|---|
| `GET /api/ws` | Realtime event stream — posts, comments, presence, typing, calls, notifications. Auth via `Authorization: Bearer` or `?token=`. Frames: `"ping"` → `"pong"`; client `{"type":"typing","conversation_id":"..."}`. Server fans out `presence.updated` (physical state), `conversation.user_typing` (typing dots), `dm.message` (full Message object — appended without re-fetch), `dm.message_updated` (in-place patch for an existing message — payload `{conversation_id, message_id, content, edited_at}`; today drives voice-note transcript fill-in via either the sender's STT or the recipient's fallback STT), `dm.message_reaction` (per-user reaction add / remove — payload `{conversation_id, message_id, user_id, emoji, action}`; the SPA aggregates by glyph for the per-bubble reaction strip), `dm.media_ready` (cross-household media full-bytes arrival — swaps preview URL → full URL), `media.ready` (background video transcode finished — payload `{type, output_filename, media_url, thumbnail_url}`; pushed only to the uploader so its SPA swaps the "Processing…" placeholder for the player. Other viewers pick up readiness via the `media_status` field on their next list fetch), `media.failed` (background video transcode permanently failed at the attempt cap — payload `{type, output_filename}`; pushed only to the uploader so its SPA flips the "Processing…" placeholder to the failed state at once. Other viewers pick up the `'failed'` status via the `media_status` field on their next list fetch), `dm.conversation.created` (inbox refresh trigger), `dm.read`, `pairing.confirmed` (pair handshake complete), `pairing.aborted` (pair failed), `peer.transport_changed` (a paired peer's federation transport flipped between WebRTC DataChannel and HTTPS inbox — payload: `{instance_id, transport: "rtc" \| "https"}`), `local.home_changed` (this household's own home coordinates changed — payload: `{latitude: <4dp>, longitude: <4dp>}`; fires on HA / HAOS adapter startup or when HA Core pushes a new location; the Connections Map tab re-centres the own-household pin without a page reload), `peer.home_changed` (a paired peer updated its home coordinates — payload: `{instance_id, latitude: <4dp>, longitude: <4dp>}`; fires when an inbound `LOCAL_HOME_LOCATION_CHANGED` event is processed; the Map tab moves or adds the peer's pin without a refetch), and `user.online` / `user.idle` / `user.offline` (session-presence dot — payload `{user_id, last_seen_at}`; the subject is excluded from the fan-out), and `user.preferences_changed` (per-user sidebar preference update — payload `{user_id, changed: {key: new_value}}`; delivered only to connections owned by the affected user, never household-wide), and `app.message` (real-time event from an installed app — fields: `app_id` (string), `session_id` (string, hex UUID scoping the session), `from_instance` (string, sender household), `from_user` (string | absent, sender's username — present for v_18+ person-routed events and local-loopback sessions; absent for legacy household-addressed and binary inbound), `kind` (`"session"` for `APP_SESSION` control events; `"message"` for `APP_MESSAGE` JSON and binary `fed-app-v1` data frames), `payload` (object, application-defined dict); for v_18+ person-routed events the frame is delivered only to the addressed user; for legacy/binary events it is delivered to every local user whose WebSocket is open when the matching app is enabled; produced by the `AppFederationService` inbound path — either the binary `fed-app-v1` DataChannel or the `APP_MESSAGE` JSON event fallback — after §24.11 validation and AES-256-GCM decryption; the host bridge forwards qualifying frames into the app iframe as `MessageEvent {type:"app:event", kind, sessionId, fromInstance, fromUser?, payload}` so apps can route by session, show the challenger's name, and distinguish invites from in-game moves). |
| `GET /api/stt/stream` | Streaming speech-to-text (binary audio frames → `{"type":"final","text":"..."}`). |

> Speech-to-text and AI data generation are HA-adapter-only in v1 — the
> standalone adapter raises `NotImplementedError` on
> `transcribe_audio` / `stream_transcribe_audio` / `generate_ai_data`.
> The HA adapter requires `[homeassistant].stt_entity_id` to be set
> (e.g. `stt.home_assistant_cloud`) before STT routes return data.

## HFS — federation inbox

| Method | Path | Purpose |
|---|---|---|
| POST | `/federation/inbox/{inbox_id}` | Inbound federation envelope. Runs the §24.11 validation pipeline before dispatch. Bodies carrying `event_type` of `PAIRING_PEER_ACCEPT` / `PAIRING_PEER_CONFIRM` are dispatched ahead of the pipeline (§11 bootstrap — the pair doesn't exist yet, so the pipeline's instance lookup would reject them). See [protocol/README.md](./protocol/README.md). |

## GFS — Public relay (HTTPS REST, SH → GFS)

| Method | Path | Purpose |
|---|---|---|
| GET | `/gfs/info` | Public GFS descriptor — `{gfs_instance_id, public_key, server_name, base_url, anonymous_publish, capabilities, capabilities_sig, capabilities_sig_suite}`. Unauthenticated by design (the key is public); an HFS that scanned the pairing QR (`{base_url, token}` only) fetches the Ed25519 `public_key` here to pin before registering. The GFS↔HFS leg has no `proto_version` negotiation, so capabilities ride this endpoint — **signed**: `capabilities` (today `{"anonymous_publish": true}`) travels with `capabilities_sig` (b64url Ed25519 over `b"gfs-capabilities:v1:"` + canonical JSON of `{gfs_instance_id, capabilities}`) and `capabilities_sig_suite` (`ed25519`; unknown suites are rejected, never defaulted), made with the same identity key this response publishes as `public_key`. A household trusts the capability **only** when that signature verifies against the key it pinned at pair time — the bare top-level `anonymous_publish` is informational, since an on-path stripper could otherwise force the legacy identified `/gfs/publish` body. Once verified the household's cache **ratchets**: a later response without a valid block logs a downgrade warning and does not flip it back (in-process only). A capability block is omitted entirely when the GFS has no signing key wired. The `public_key` itself is **random per deployment**: the GFS mints a 32-byte Ed25519 seed on first boot and persists it as `<data_dir>/gfs_identity.seed` (mode 0600) — it is no longer derived from `gfs_instance_id`, which this same response publishes in the clear and which would therefore have let anyone recompute the private key and forge a signed block. Back the file up with the database; losing it changes the identity and every paired household has to re-pair. Operators who inject secrets from a vault pin it instead with `[server] signing_seed_hex` / `GFS_SIGNING_SEED` (64 hex chars; a malformed value stops the boot rather than minting a different identity). |
| POST | `/gfs/register` | Register instance. |
| POST | `/gfs/instance` | Update a registered instance's `display_name`. Body `{instance_id, display_name, ts, signature}`; Ed25519-signed over canonical JSON of `{instance_id, display_name, ts}` and verified against the registered public key (replay-guarded ±300 s on `ts`). |
| POST | `/gfs/publish` | **Anonymously** relay a space event to the space's subscribers. Canonical body `{space_id, event_type, payload}` — it carries **no household identity**. The **only** authenticator is the **space-authority signature** inside the opaque `payload` (`authority_sig` + `authority_sig_suite`), verified against the space's TOFU-pinned `identity_public_key` under the wire `event_type`, which must be in `{space_post_public, space_subscriber_key_handoff}`. Any seed-holder (owner OR delegated admin) can produce one, so the space keeps working while the owner is offline; the GFS stays blind to content — it verifies a signature over opaque bytes and never decrypts. **What the anonymity does and does not buy:** the GFS does not *require, store, log or forward* the relaying household's identity — it is NOT that the GFS *cannot learn* it. The same household normally holds an authenticated `/gfs/ws` socket to that server from the same address, so network-level correlation (source IP, timing, body size) remains available to the GFS operator. **Moderation:** because the caller is anonymous, instance-level moderation (`client_instances.status = 'banned'`) cannot gate this path — a banned household simply omits the legacy fields. The **space**-level ban is the only moderation lever on the relay; per-IP rate limiting is the only other handle. **Legacy fields accepted but ignored:** an older household still sending `from_instance` + `signature` (its Ed25519 transport signature over canonical JSON of `{space_id, event_type, payload, from_instance}`) succeeds provided the authority sig is valid — the transport signature is still verified when present (a bogus legacy field is a 403), then discarded: it authorizes nothing, never enters the fan-out frame, and is never logged. There is **no owner path** any more. `space_id` and `event_type` must each be a non-empty string of ≤128 chars → 400 otherwise. The body is capped at **256 KiB** (declared `Content-Length` or actual bytes read) → 413. Response is `{status:"published", delivered_to:<count>}` — a COUNT, never the subscriber ids: the authority signature has no nonce/timestamp, so a captured relay frame stays valid forever and must not double as a roster read (that is what `/gfs/spaces/{id}/subscribers` gates behind a replay-guarded authority query). **Replay dedupe:** the GFS remembers a BLAKE2b digest of each successfully authorized payload (over the same canonical JSON the authority signature covers) for **5 minutes**, capped at 10 000 entries. A re-POST of a byte-identical payload inside that window is an idempotent **no-op** — HTTP 200 with `delivered_to: 0`, nothing fanned out, no distinguishable body. The digest is recorded only AFTER authorization succeeds, so a rejected payload cannot pre-poison the same bytes sent legitimately later. The cache is in-memory and **per GFS node**: it bounds the amplification burst one captured frame can drive, it is not a permanent content-id store (past the TTL, after a restart, or on another cluster node the same bytes relay once more). In a cluster of N nodes behind one address a replay burst is therefore suppressed only 1-in-N — each node has to see the bytes once before it starts suppressing them. So subscriber-side dedupe by the post id inside the payload remains the standing backstop and the relay stays at-least-once. **Fan-out deadline:** one relay's fan-out runs at most `FAN_OUT_CONCURRENCY = 8` deliveries at a time under an **8 s** whole-fan-out deadline (per-target HTTPS-inbox timeout 10 s; the whole-fan-out deadline deliberately sits below the household's 10 s publish client timeout, so the GFS — not the client — decides when a slow fan-out ends); on expiry the stragglers are cancelled and `delivered_to` reports only what was reached. Without the deadline a fan-out to N blackholing subscribers would pin the request handler for `ceil(N / 8) × 10 s`, which an anonymous caller could multiply by the per-IP publish rate. **Every** authorization failure returns the SAME 403 body `{"error": "not authorized to relay for this space"}` — distinct messages would let an unauthenticated caller enumerate space existence, moderation status and pin status; the precise reason is logged at DEBUG. That covers: a missing / malformed / non-verifying legacy signature, a legacy `from_instance` that is unregistered or banned, an unpublished space, a moderator-`banned` space, an `event_type` outside the allow-set, a space with no pinned authority key, a missing / invalid authority signature, or an unknown suite (no fallback). A `withdrawn` space still relays — withdrawal delists from the directory only. Rate-limited 120/min per IP → 429. |
| POST | `/gfs/spaces/{id}/publish` | Publish space metadata. Body carries `{owning_instance, name, …, identity_public_key, ts?, signature}`; the Ed25519 `signature` is **mandatory** and verified against the owning instance's registered public key — empty / malformed / invalid → 403. `identity_public_key` (hex, the space's Ed25519 authority verify key) is **TOFU-pinned on first publish** and immutable thereafter — a later publish offering a different pubkey keeps the pinned one. The pinned key is what authorizes a later space-authority-signed relay via `/gfs/publish`. `ts` is **optional for backward compatibility**: when present it is inside the signed canonical body and replay-guarded (±300 s, tz-aware — naive / stale → 403), and only such a *fresh* publish clears the `withdrawn` flag set by `/unpublish`. A publish without `ts` (an older household) still registers and refreshes metadata but **cannot restore a withdrawn listing** — its body is replayable forever, so honouring it would let a captured publish re-list a space its owner delisted. |
| POST / DELETE | `/gfs/spaces/{id}/unpublish` | **Owner withdrawal** of a published listing. Body `{owning_instance, ts, signature}`; the Ed25519 `signature` (over canonical JSON of `{action:"unpublish", owning_instance, space_id, ts}`) is **mandatory**, verified against the registered public key, and replay-guarded (±300 s on `ts`). `action` rides inside the signed bytes, so a captured subscribe signature can't be replayed as a delisting. Authentication is not enough: the caller must also **be** the space's `owning_instance`. A missing `owning_instance`/`ts`/`signature` → 400; unknown instance / bad signature / stale `ts` / non-owner → 403. Success sets the reversible `withdrawn` flag (the row, its subscribers and the pinned authority key survive, and `status` is untouched — `banned` stays the GFS moderator's sticky verdict), so the owner's next `publish` restores the listing. Withdrawal hides the space from discovery only; the relay and existing subscribers keep working. Unknown space → 200 no-op (idempotent fan-out), but only after the signature verifies. |
| POST | `/gfs/subscribe` | Subscribe (default) or unsubscribe (`action:"unsubscribe"`) to a published space's relay fan-out. Body `{instance_id, space_id, ts, signature, action?}`; **both** actions are Ed25519-signed — the `signature` (over canonical JSON of `{action, instance_id, space_id, ts}`) is **mandatory**, verified against the registered public key, replay-guarded (±300 s on `ts`), and binds the request to `instance_id` so a caller can only (un)subscribe itself. `action` rides inside the signed bytes (domain separation), so a subscribe signature can't be replayed as an unsubscribe, or vice versa. A missing `instance_id`/`space_id`/`ts`/`signature`, or an `action` outside `{subscribe, unsubscribe}` → 400; bad signature / stale `ts` / unknown instance → 403. Subscribe additionally requires the space to already be published (no auto-create) — unknown space → 403; unsubscribe is idempotent and needs no existing space. |
| POST | `/gfs/report` | File a fraud / abuse report. |
| POST | `/gfs/appeal` | Appeal a ban. |
| GET | `/gfs/spaces` | Public directory listing. |
| GET | `/gfs/spaces/{id}` | Single published space's metadata — the subscriber-side mirror source. Returns the stored listing (`{space_id, owning_instance, name, description, about_markdown, category, min_age, status, identity_public_key, …}`). Unknown, non-`active`, or owner-`withdrawn` space → 404. Unauthenticated, like the directory. |
| GET | `/gfs/spaces/{id}/subscribers` | Release the space's subscriber list to a verified **seed-holder** (owner OR delegated admin) — the Phase-5b-c reconcile. Query params `{ts, authority_sig, authority_sig_suite}`; the **space-authority signature** is over canonical JSON of `{space_id, ts}` under event type `space_subscribers_query` and verified against the space's TOFU-pinned `identity_public_key` (the same key `/gfs/publish` pins), replay-guarded (±300 s on `ts`). Returns `{subscribers: [{instance_id, identity_public_key, keywrap_public_key, keywrap_sig}…]}` — only each subscriber's already-registered public key material (no inbox URL), so the seed-holder can re-seal the per-space content key to each. Fail-closed — missing / forged / stale signature, unknown suite, unknown space, or a space with no pinned pubkey → 403. |
| GET | `/healthz` | Liveness. |

**Public-Momentum directory** (§Momentum-public)

| Method | Path | Purpose |
|---|---|---|
| POST | `/gfs/moments/users/register` | Opt a user into the public directory. Body carries `username`, `display_name`, `bio` (≤280 chars), `picture_url`, `home_instance_pk`. |
| POST | `/gfs/moments/users/{user_id}/deregister` | Pull a registration. |
| POST | `/gfs/moments/users/{user_id}/picture` | Push avatar bytes (signed; `mime` ∈ `image/{jpeg,png,webp}`, ≤256 KiB, base64-encoded). Idempotent on `digest`. |
| POST | `/gfs/moments/users/{user_id}/follow` | Record a follower. Returns the followed user's directory entry incl. `home_instance_pk`. |
| POST | `/gfs/moments/users/{user_id}/unfollow` | Drop a follower. |
| GET | `/gfs/moments/users` | JSON directory; `?q=<substr>` filters `display_name`/`username` (`LIKE '%q%'`), `?limit=` caps at 200. |
| GET | `/gfs/moments/users/{user_id}` | Single-user JSON detail incl. `follower_count`. |
| GET | `/gfs/moments/users/{user_id}/picture` | Anon avatar fetch with `Cache-Control: public, max-age=86400, immutable` and ETag = digest. |

**Public-content RTC + relay fallback** (highlights §highlights_public, moments §Momentum-public)

Public highlights and the public moments index both stream live from the
author's SH — a direct WebRTC DataChannel first, with a GFS-relay HTTP
fallback when WebRTC can't connect. The GFS stores **zero** content bytes
for either; the relay is a transient in-memory pipe of the byte-identical
framed stream. Author-offline → `503`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/gfs/highlights/ice-servers` | Anon STUN/TURN list for the browser bootstrap. |
| POST | `/gfs/highlight_rtc/offer` | Anon (rate-limited 20/min per IP). Body `{instance_id, highlight_id, token, sdp}`; pushes a `highlight_signal kind=offer` WS frame to the author. Returns `{session_id}`. |
| GET | `/gfs/highlight_rtc/session/{session_id}` | Anon poll for `answer_sdp` + author ICE. |
| POST | `/gfs/highlight_rtc/ice/viewer` | Anon. Trickle the viewer's ICE candidate. |
| POST | `/gfs/highlight_rtc/answer` | Author SH (Ed25519-signed). Authority guard: `session.initiator_id` == signer. |
| POST | `/gfs/highlight_rtc/ice/author` | Author SH (signed). Same guard. |
| GET | `/gfs/highlight_rtc/relay/{instance_id}/{highlight_id}?token=...` | Anon, token-gated (rate-limited 20/min per IP). Chunked `application/octet-stream`; pushes a `highlight_signal kind=relay_offer` and pipes the author's framed bytes. `503` author offline / relay capacity, `410` bad/expired token, `422` missing token. |
| POST | `/gfs/highlight_rtc/relay-stream/{relay_id}` | Author SH. Header-auth `X-SH-Instance` + `X-SH-Timestamp` (±300 s) + `X-SH-Signature` (Ed25519 over canonical `{"instance_id","relay_id","ts"}`); body is the raw framed stream. `403` target != signer, `404` unknown relay, `401` bad sig / stale ts, `422` missing headers. |
| POST | `/gfs/moment_rtc/offer` | Anon (rate-limited 20/min per IP). Body `{user_id, sdp}`; pushes a `moment_signal kind=offer` (carries `user_id` + `gfs_id`) to the author. `404` unregistered/suspended, `503` author offline. Returns `{session_id}`. |
| GET | `/gfs/moment_rtc/session/{session_id}` | Anon poll for `answer_sdp` + author ICE. |
| POST | `/gfs/moment_rtc/ice/viewer` | Anon. Trickle the viewer's ICE candidate. |
| POST | `/gfs/moment_rtc/answer` | Author SH (signed). Authority guard: `session.initiator_id` == signer. |
| POST | `/gfs/moment_rtc/ice/author` | Author SH (signed). Same guard. |
| GET | `/gfs/moment_rtc/relay/{user_id}` | Anon chunked relay fallback (registration-gated, rate-limited 20/min per IP). `404` unregistered, `503` author offline / relay capacity. |
| POST | `/gfs/moment_rtc/relay-stream/{relay_id}` | Author SH. Header-auth (same `X-SH-Instance`/`X-SH-Timestamp`/`X-SH-Signature` scheme as the highlight relay-stream); body is the raw framed stream. |

## GFS — Push WebSocket (GFS → SH)

| Method | Path | Purpose |
|---|---|---|
| GET (Upgrade) | `/gfs/ws` | Persistent push channel. SH opens this once paired; the GFS pushes `{type:"relay", space_id, event_type, payload}` frames as space events fan out — the frame is identity-free (the GFS never learns which household relayed the event, so it forwards no `from_instance`). It also pushes public-content RTC signalling to the author: `{type:"highlight_signal", kind}` with `kind` ∈ `offer` / `ice` / `relay_offer` (the `relay_offer` carries `{relay_id, highlight_id, token}` for the GFS-relay fallback), and `{type:"moment_signal", kind}` with `kind` ∈ `offer` / `ice` / `relay_offer` (the `offer` carries `user_id` + `gfs_id`). On an operator rename it pushes `{type:"server_info_updated", server_name}` to every connected client, which re-fetches `GET /gfs/info` to update the cached server name in real time (the reconnect-refresh path is the fallback). First client frame must be a signed hello `{type:"hello", instance_id, ts, sig}` within 5 s — see spec §24.12. WebSocket close codes: 4400 protocol violation, 4401 auth failure, 4408 hello timeout, 4409 replaced. Heartbeat is the WS-protocol-level ping (30 s). |

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
| GET | `/admin/api/spaces` | Published spaces, including owner-withdrawn ones (each row carries `withdrawn`). |
| POST | `/admin/api/spaces/{space_id}/{accept\|reject\|ban}` | Moderate spaces. |
| GET / PATCH | `/admin/api/policy` | Operator policy. |
| GET / PATCH | `/admin/api/branding` | Branding text. |
| POST / DELETE | `/admin/api/branding/header-image` | Header image. |
| GET | `/admin/api/reports` | Report queue. |
| PATCH | `/admin/api/reports/{id}/review` | Decide. |
| GET | `/admin/api/appeals` | Appeal queue. |
| PATCH | `/admin/api/appeals/{id}/decide` | Decide. |
| GET | `/admin/api/audit` | Audit log. |
| GET | `/admin/api/cluster` | Cluster status — enriched `nodes` list (self + peers) with per-node status, live connected-client count, and active sync sessions. |
| GET | `/admin/api/cluster/peers[/{node_id}]` | Peer list / detail. |
| POST | `/admin/api/cluster/peers` | Add a peer by URL. |
| DELETE | `/admin/api/cluster/peers/{node_id}` | Remove a peer. |
| POST | `/admin/api/cluster/peers/{node_id}/ping` | Healthcheck a peer. |

## GFS — Public SSR pages

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Operator landing page. |
| GET | `/spaces/{slug}` | Public space detail. |
| GET | `/join/{gfs_token}` | Landing page for an invitation link. |
| GET | `/moments` | Public-Momentum directory (SPA shell). Loads `/static/users_directory.js`, which fetches `GET /gfs/moments/users` and renders cards + search. |
| GET | `/moments/{user_id}` | Per-user landing — avatar + display_name + bio + follower count + "Follow on your Social Home" deeplink. |

These pages are server-rendered HTML and require no auth.

## Rate limits

| Endpoint | Limit |
|---|---|
| `POST /api/presence/location` | 10 / min / user |
| `POST /api/calls` | 10 / min / user |
| `POST /api/calls/{id}/decline` | 10 / min / user |
| `POST /api/calls/{id}/hangup` | 30 / min / user |
| `GET /api/map/tiles` | 1200 / min — one shared bucket: every Leaflet `<img>` authenticates as the signed-URL principal, and a desktop viewport is ~20 tiles. Still a ceiling, so a leaked signed URL can't drive unbounded upstream traffic from the household IP. |
| `POST /cluster/signaling-session{,/release}` | 60 / min / paired instance |
| `GET /` + `GET /spaces/{id}` (GFS public listing) | 30 / min / IP |
| `POST /gfs/{highlight,moment}_rtc/offer` + the anon `/relay/*` GETs | 20 / min / IP — bounds the WS-push amplification from the anonymous RTC entry points |
| `POST /gfs/publish` | 120 / min / IP — the relay is authorized by the space-authority signature alone and carries no household identity, so the per-IP window is the only shedding handle, set far above a real household's publish rate (one per public post / subscriber key handoff, per GFS). Bodies over 256 KiB are refused with 413 before being read; a byte-identical payload replayed inside 5 min is a 200 no-op that fans out nothing, and one relay's fan-out is capped at 8 s (below the household's 10 s client timeout). |
| Federation inbound (per signing instance) | Rolling window; see §24.11. |
| `POST /federation/inbox/{inbox_id}` (per remote IP) | 1000 / min — defends against unauthenticated floods that the per-user limiter would otherwise miss. |

Rate-limit responses return HTTP 429 with a `Retry-After` header.

**Which IP a GFS limiter counts.** `X-Forwarded-For` is client-supplied, so the
GFS believes it only when the TCP peer is itself a trusted proxy, and then uses
the **last** entry (the hop that proxy appended); otherwise the peer address is
used. The trusted set is `[server] trusted_proxies` in `global_server.toml`
(env: `GFS_TRUSTED_PROXIES`, comma-separated IPs/CIDRs, empty string to clear).
It defaults to loopback + the RFC1918 ranges + `fc00::/7`, so the usual
"reverse proxy on the same host or private network" deployment keeps per-client
limiting with no configuration, while a peer connecting straight from the
internet can never spoof its own client IP. IPv4-mapped IPv6 peers and header
entries (`::ffff:1.2.3.4`) are unmapped to their IPv4 form before both the
trusted-peer test and the bucket key, so a dual-stack listener still recognises
a loopback/RFC1918 proxy and one host cannot claim two buckets by switching
address family. An IPv6 zone (`2001:db8::1%eth0`) is stripped from both
the peer and the header entry before keying, so one host cannot mint a fresh
bucket per zone spelling — nor inflate the bucket key with an arbitrarily long
zone string. Each limiter tracks at most 10 000 addresses, evicting the
least recently seen.

Three limits of this scheme, stated so operators can design around them:

- **The front end must OVERWRITE `X-Forwarded-For`.** Trust here means "this
  peer's last entry is authoritative". An L4/TCP proxy — or an L7 proxy set to
  *append* rather than *set* — passes the client's own header through, so the
  last entry is attacker-chosen again and one source can mint unlimited
  buckets. Behind such a front end the only safe setting is
  `trusted_proxies = []` (every request then keys on the proxy's address, i.e.
  one shared bucket).
- **Single hop.** Only the last entry is read, so a chain of two or more
  trusted proxies resolves to the address the *innermost* proxy appended — the
  outer proxy, not the client — and every client behind the chain shares one
  bucket. Collapse the chain at the outermost proxy before the GFS sees it.
- **Not a defence against a distributed flood.** The counters are per-source
  and LRU-bounded at 10 000 addresses; an attacker with at least that many
  source addresses evicts every bucket each window and is never limited. The
  per-IP windows shed a single noisy source, they do not shed a botnet —
  volumetric defence belongs at the network edge.

## Version & compatibility

API responses include `X-Social-Home-Version` when running in
standalone mode (derived from `pyproject.toml`). Breaking changes
bump the major version. Endpoints added in minor versions are
announced in `CHANGELOG.md`.
