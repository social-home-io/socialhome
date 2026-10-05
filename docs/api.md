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
- Errors use one shape, `{"error": {"code": "...", "detail": "...",
  "params": {…}}}` — see [Errors](#errors). HTTP status codes are
  standard: 200 / 201 / 204 for success, 400 for validation, 401 for
  missing auth, 403 for authorisation failures, 404 for missing
  resources, 409 for conflicts, 422 for refused input, 429 for rate
  limits.
- Pagination uses `?limit=N&cursor=…`. Cursors are opaque; don't
  parse them.
- Timestamps are ISO-8601 UTC, serialised via orjson.
- **Space feature access levels (§4.3).** Every write to a space's posts,
  pages, tasks / task lists, stickies or calendar events (including a
  poll / schedule-poll attach, a Bazaar listing edit and a bot's post)
  passes the space's `*_access` level for the acting user. Under
  `admin_only` only the owner and admins write; anyone else gets
  `403 {"error": {"code": "ACCESS_ADMIN_ONLY", "feature": "posts" | "pages"
  | "tasks" | "stickies" | "calendar", …}}` and nothing changes. Comments,
  reactions, votes, schedule answers, bids and RSVPs are never gated. See
  [`protocol/spaces.md`](./protocol/spaces.md#feature-access-levels-v_42).

## Errors

Every error answer is `{"error": {"code", "detail", …}}`, built centrally by
`BaseView._iter` (`socialhome/routes/base.py`):

- `code` — a stable, machine-readable code. Branch on this, never on `detail`.
- `detail` — a short English sentence for API clients and logs. It never
  carries raw ids, library error text or other internals.
- `params` — optional, flat JSON values the message needs (a minimum age, a
  length limit, a price floor). Present only when the code has some.
- A few older codes carry their hints as top-level fields instead
  (`feature`, `reason`, `section`, `count`, `current_version`…).

The SPA shows its own translated text for a known `code`
(`client/src/apiErrors.ts`, keys `error.*`); an unknown code shows `detail`,
and a generic answer (`NOT_FOUND`, `RATE_LIMITED`, `INTERNAL_ERROR`, the
blanket `422 UNPROCESSABLE`, any 5xx, or no body) shows a per-status line.

Coded refusals (`socialhome/domain/errors.py` `CodedError` subclasses):

| Code | Status | `params` | Raised by |
|---|---|---|---|
| `ALREADY_MEMBER` | 422 | — | `POST /api/spaces/{id}/join-requests` by a member |
| `USER_ALREADY_MEMBER` | 403 | — | `POST /api/spaces/{id}/members` for someone already in |
| `BANNED` | 403 | — | Joining, following or accepting an invite to a space you're banned from |
| `USER_BANNED` | 403 | — | An admin adding / inviting a banned person (no user id in the answer) |
| `INVITE_ONLY` | 403 | — | A join request to an invite-only space |
| `INVITE_EXPIRED` | 404 | — | `POST /api/remote_invites/{token}/accept` / `decline`, `POST /api/spaces/join` with an unknown, expired or used-up token |
| `SUBSCRIBE_NOT_ALLOWED` | 403 | — | `POST /api/spaces/{id}/subscribe` on a space that takes no followers |
| `SUBSCRIBER_READ_ONLY` | 403 | `action` (`post` / `comment` / `react` …) | A follower writing to a space |
| `SPACE_ARCHIVED` | 403 | — | Any write to an archived space (posts, pages, tasks, stickies, calendar) |
| `NOT_PAIRED` | 403 | — | `POST /api/public_spaces/{space_id}/join-request` when the host household isn't a confirmed connection |
| `AGE_RESTRICTED` | 403 | `min_age` | §CP.F1 age gate on joining a space |
| `DM_SELF` | 422 | — | `POST /api/conversations/dm` to yourself |
| `GROUP_TOO_SMALL` | 422 | `min` | `POST /api/conversations/group` with fewer than 3 people |
| `DM_TOO_LONG` | 422 | `max` | Sending / editing a message over the length cap |
| `DM_BLOCKED` | 403 | — | The recipient blocked you (a guardian block reads the same) |
| `DM_YOU_BLOCKED` | 403 | — | You blocked the recipient |
| `DM_NOT_ALLOWED` | 403 | — | A protected account messaging someone its guardian blocked |
| `DM_GROUP_NOT_ALLOWED` | 403 | — | A group that a guardian block rules out |
| `GROUP_MEMBER_UNSUPPORTED` | 422 | `reason` (`legacy_group` / `not_paired` / `too_old`), `name` | Adding a remote person to a group |
| `RSVP_PAST` | 422 | — | `POST /api/calendars/events/{id}/rsvp` for an occurrence that has ended |
| `POLL_CLOSED` | 409 | — | Voting on a closed poll |
| `MOMENT_RATE_LIMIT` | 429 | — | Posting more than one moment per 15 minutes |
| `BID_TOO_LOW` | 422 | `floor_amount` (the listing's stored units — cents, or whole units for JPY / KRW / ISK), `currency` | `POST /api/bazaar/{id}/bids` under the floor |
| `OWN_LISTING` | 422 (bids) / 403 (offers) | — | A seller bidding on / offering for their own listing |
| `LISTING_NOT_ACTIVE` | 422 (bids) / 409 (offers) | — | A bid or offer on a sold, expired or cancelled listing |
| `IMAGE_TOO_LARGE` | 422 | `max_mb` | `POST /api/me/picture`, `/api/spaces/{id}/members/me/picture`, `/cover`, `/icon` over the size limit |
| `IMAGE_UNREADABLE` | 422 | — | The same picture endpoints and `POST /api/media/upload`, when the file isn't a supported image or the image library can't open it (the library's error text and the file name stay in the server log) |

## HFS — Authentication & self

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/token` | Issue a bearer token (standalone mode). |
| POST | `/api/auth/redeem-password-reset` | Public: redeem a one-time admin-issued reset token to set a new password. Body: `{token, new_password}`. 204 on success; 410 when expired / already used; 422 for short password / missing fields. Same per-IP rate limit as `/api/auth/token` (5 / 15 min). |
| POST | `/api/admin/users/{username}/issue-password-reset` | Admin: mint a single-use, 1h-TTL reset token for a user. Returns `{token, expires_at, username}` once — admin hands the resulting `/reset-password?token=…` URL to the user out-of-band. Standalone mode has no SMTP, so this is the only recovery path. |
| GET | `/api/admin/auth-audit` | Admin: read the auth audit log — append-only trail of login attempts (success + failure), reset issues, and reset redeems. Query `?limit=N` (default 100, max 500). |
| GET | `/api/me` | Current user profile. Also carries the caller's **own** protection state — `protected` (bool) and `restrictions` (the capability ids the server refuses, see *Protected accounts* below; `[]` when unprotected). Never `is_minor` / `declared_age`, and no other user payload (`/api/users`, members, profiles) carries `protected`. |
| GET | `/api/me/protection` | The caller's own protection: `{protected, restrictions, guardians: [{user_id, username, display_name}]}` — what is limited and who to ask. `{protected: false, restrictions: [], guardians: []}` for an unprotected account. |
| PATCH | `/api/me` | Update profile fields. Body: any of `{"display_name", "bio", "preferences", "tz"}`, or the status keys `{"status_emoji", "status_text", "status_clear_after"}`. Status: `status_text` is one line of at most 80 characters, `status_emoji` a single emoji (at most 16 code points, no whitespace, not plain ASCII); both blank or `null` clears the status. `status_clear_after` is `null` (keep until changed), `"30m"` / `"1h"` / `"4h"`, `"today"` (midnight in the user's `tz`), or an ISO-8601 instant with an offset that is in the future and at most 7 days away; the response's `status.expires_at` is the resolved UTC deadline. A status key that is absent keeps its current value, so `{"status_clear_after": "1h"}` alone re-times the current status; setting emoji/text without `status_clear_after` means no expiry. Invalid input → 422, nothing saved. A change fires `user.status_changed` to the household and `USER_STATUS_UPDATED` to paired households; a once-a-minute sweep clears a status at its deadline (same fan-out), and reads already treat an expired status as unset. `tz` is validated against the IANA database (unknown name → 422); the SPA's cold-start probe sends it once on first login so personal calendar events default to the user's local wall clock. |
| POST | `/api/me/username` | Rename the caller's login username. Body: `{"username"}`. The cryptographic `user_id` is unaffected. Invalid / reserved / taken → 422; HA-managed (HA-source) accounts → 403. |
| POST | `/api/me/handle` | Set the caller's public `@handle`. Body: `{"handle"}`. Returns `{handle}` on success. Per-household case-insensitive uniqueness enforced. Invalid / reserved / taken → 422. Unlike `/api/me/username`, **all** users (including HA-managed) may set their own handle. |
| GET | `/api/me/picture` | Download current user's avatar. |
| POST | `/api/me/picture` | Upload avatar (multipart). |
| DELETE | `/api/me/picture` | Remove avatar. |
| POST | `/api/me/picture/refresh-from-ha` | HA-mode only: re-fetch from HA user profile. |
| GET | `/api/me/notify-targets` | Selectable push notify targets for the notification-settings dropdown. HA mode lists the household's `notify.*` entities (`[{entity_id, name}]`); other platforms return `[]`. |
| GET | `/api/me/export` | Initiate a data-export job. |
| GET | `/api/me/corner` | "My Corner" aggregated feed — one payload for the home screen (`/`) and `/corner`: `unread_notifications`, `unread_conversations`, `upcoming_events` (from now, next 7 days, ≤ 8), `presence`, `tasks_due_today`, `bazaar`, `followed_space_ids`, `followed_spaces_feed`, plus `today_timetable` — `[{timetable_id, name, color, tz, date, lessons: [...]}]`, the caller's assigned timetables in effect today, then the space timetables they pinned (`preferences.timetable_home_pins`, while still a member and the space feature is on) (today in each timetable's tz; ≤ 3, ≤ 24 lessons each; cancelled lessons included, untitled template slots left out — a timetable with no filled lesson today is skipped; `[]` when `feat_timetable` is off). A lesson is the `/api/timetables/day` effective-lesson shape plus `start_at` / `end_at` (UTC ISO 8601) — and `today_events`, the caller's events overlapping today's household-tz day (≤ 20, same shape and visibility as `upcoming_events`, including events that already ended; an all-day event only on the dates it covers in its own tz, end exclusive). Each slice fails soft to empty. |
| GET / POST / DELETE | `/api/me/tokens[/{id}]` | Manage the caller's own API tokens (browser sign-ins appear here too, labelled `web`). `GET` returns `{base_url, tokens: [{token_id, label, created_at, last_used_at, expires_at, revoked_at}]}` (revoked rows omitted); `base_url` is the origin an external client reaches this Social Home at — derived from `PlatformAdapter.get_public_base_url()` (the admin-set external URL or `[standalone].external_url`), `null` when the only public route is Home Assistant's inbox forwarder or nothing is configured. `POST {label, expires_at?}` returns `201 {token_id, token}` (403 `ACCOUNT_PROTECTED` / `api_tokens` for a protected account; listing and revoking stay open) — the raw token appears only in this response (only its SHA-256 is stored). `DELETE` returns `204` and only revokes a token the caller owns; another user's id is a silent no-op (admins use `/api/admin/tokens/{id}`). |

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
| POST | `/api/feed/posts` | Create a household post. Body: `{type, content?, media_url?, location?, pinned?, no_link_preview?}`. `type` ∈ `text\|image\|video\|file\|poll\|schedule\|location`. When `type='location'` the body **must** include `location: {lat, lon, label?}` — server truncates `lat`/`lon` to 4 decimal places (~11 m) at the boundary, `label` is optional and capped at 80 characters. `bazaar` posts are space-scoped; `event` posts are auto-created by the calendar bridge. A `text` post whose content contains a web link gets a server-built `link_preview` (see *Link previews* below) unless `no_link_preview: true`; the client can only opt out — any preview fields in the body are ignored. |
| POST | `/api/link-preview` | The composer's live link card. Body `{url}` → `{"preview": {url, title, description, site_name, thumbnail_url} \| null}`. Built by the **author's** household through the SSRF-guarded fetcher (see *Link previews*), cached ~15 min per URL — creating the post reuses the result. `thumbnail_url` is a signed local `api/media/…` URL of the page image re-encoded to WebP without metadata. Any member; 403 `FEATURE_DISABLED` when the household admin turned previews off (`allow_link_preview`); 422 on a missing `url`; `{"preview": null}` for a link that yields no card, cannot be fetched, or when the member's fetch budget is spent. |
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
| GET | `/api/spaces` | List spaces the caller belongs to. Each row carries the same `features` block as `GET /api/spaces/{id}` — the browser reads `features.allow_subscribers` per row to decide whether a discoverable space is publicly readable, so no per-space detail fetch is needed. |
| POST | `/api/spaces` | Create a new space. Body: `{name}` (required), `description?`, `emoji?`, `space_type` (`private`\|`household`\|`public`\|`global`, default `private`; the API accepts any of the four, the SPA only offers `global` in the create dialog when a global server is connected), `join_mode` (default `invite_only` — the **membership** gate only; readability is the separate `features.allow_subscribers` opt-in, not settable at create time, so a new space is never publicly readable until its owner PATCHes it on), `lat?`/`lon?` (public/global tiers only and now **optional** — a public space without coordinates is listed in discovery but not map-pinned), `category?` (one of the 10 discovery values — `general`, `hobby_crafts`, `sports_outdoors`, `gaming`, `music_arts`, `food_drink`, `tech`, `local`, `family_parenting`, `learning`; applied for public/global), `min_age?` (`0`\|`13`\|`16`\|`18`; applied only for public/global tiers, ignored otherwise). |
| GET / PATCH / DELETE | `/api/spaces/{id}` | Read / update / **dissolve**. GET response includes `category` (the §23.50 discovery taxonomy, normalized to `general` when unset). PATCH accepts `category` (plus the usual `name`/`description`/`emoji`/`join_mode`/feature toggles) and **`allow_here_mention`** (boolean, owner/admin only like every config edit; non-boolean values are ignored) — whether owners and admins may page everyone with `@here`; GET returns it. **Retention:** `retention_days` (positive integer = soft-delete feed posts older than N days; `0` or negative = keep forever, GET returns `null`) and `retention_exempt_types` — a list of post types the sweep never deletes. Valid values are the `PostType` enum: `text`, `image`, `video`, `transcript`, `poll`, `schedule`, `file`, `bazaar`, `event`, `location`, `highlight_share`; the list is de-duplicated and sorted, and an unknown value or a non-list is `422` (nothing is stored). The SPA offers `image`/`video`/`file`/`poll`/`schedule`/`event`/`bazaar`/`location`. GET returns both, including on a space hosted by another household (the host's values, mirrored over `space_meta`; only the host runs the sweep). Every PATCH field is optional and an absent field is left unchanged; the SPA sends only the fields the admin edited. The `features` block carries **`allow_subscribers`** (default `false`) — the **owner-only** opt-in that makes a public/global space publicly readable (a space admin who is not the owner gets `403 owner required` if they try to change it, exactly as for `delegated_admin_authority`; a forwarded cross-household `update_config` has it pinned to the stored value on the host): with it off nothing is relayed to a connection server, no content key is sealed to a follower, and `POST /api/spaces/{id}/subscribe` is refused `403 this space does not allow subscribers`. It is independent of `join_mode`, and flipping it on a `global` space immediately re-publishes the space's metadata to every paired connection server (which is also what purges existing subscriber seats when it goes off; the household's own local `role='subscriber'` rows are dropped at the same time, so a follower is not left reading a space the owner withdrew). The `features` block also carries **`gfs_publish_mode`** (`"trusted"` default / `"strict"`, v_50) — the **owner-only** choice of how member households publish over a connection server: `trusted` = identified (the server learns which household posted, never the content), `strict` = anonymous under the epoch's writer group key (the server learns only that some publisher of the space posted, and refuses identified publishes into it). A non-owner gets `403 owner required`; any other value is `422 UNPROCESSABLE`; a forwarded cross-household `update_config` has it pinned on the host, and a member household takes it only from the owner household's own `SPACE_CONFIG_CHANGED`. Switching to `strict` rotates the space content key (the rotation hands every v_50 publishing household the new writer key and tells each connection server the mode and the key's pin); switching back to `trusted` tells each connection server at once. GET returns it in `features`. It applies to a **private** space too once that space uses an opaque connection-server channel (v_51 — its owner turned `private_gfs` on and it has remote members); GET returns **`gfs_private_channel`** (boolean) — `true` when this private space uses such a channel, which is when the SPA shows the owner the choice. The `features` block also carries **`private_gfs`** (boolean, default `false`) — the **owner-only** option "use the connection server for this space" of a **private** space (ignored on other tiers). Off: the space never touches a connection server — no `gfs`-type invite link, no opaque channel, no subscription. On: `gfs`-type invite links are allowed, the owner registers the space's opaque channel and every member household connected to that server takes a seat (paired and mesh-only members included), so members reach each other while the host is offline. A non-owner gets `403 owner required`; a non-boolean is `422 UNPROCESSABLE`; a forwarded cross-household `update_config` has it pinned on the host, and a member household takes it only from the owner household's own `SPACE_CONFIG_CHANGED`. **Turning it off while households that joined through an invite link are still members answers `409 PRIVATE_GFS_LINK_MEMBERS`** with `error.households: [{instance_id, display_name, members: [{user_id, display_name}]}]` and changes nothing — remove them first. Turning it off otherwise deletes every `gfs`-type invite link of the space, unregisters the channel and rotates the space content key. New private spaces are off; migration 0079 turned it on only for existing private spaces that already had link-joined members. A space left off keeps its pre-0079 invite links as grandfathered `via: "gfs_legacy"` links (listed by `GET …/invite-tokens`), still redeemable through the relay; the first household that joins through one that way turns `private_gfs` on automatically. The `features` block is **merged onto the space's current features** — a partial body changes only the keys it names and leaves the rest alone. It carries the per-feature access levels **`posts_access`**, **`pages_access`**, **`tasks_access`**, **`stickies_access`**, **`calendar_access`** (`open` / `moderated` / `admin_only`). Raising a level off `open` while a member household is below protocol v_42 — or setting one to `moderated` while a member household is below v_43 (federated moderation) — answers **`409 PEERS_TOO_OLD`** with `error.households: [{instance_id, display_name, proto_version}]` and changes nothing; re-send the same PATCH with **`"force": true`** to apply it anyway. The answer carries **`forwarded`** — `true` when the space is hosted by another household and the edit was only forwarded to it (the host applies it, and re-checks `PEERS_TOO_OLD` itself; a refused access level stays as it was while the rest of the edit applies). PATCH ignores `space_type` (publication tier is proposal-gated — see below). DELETE opens a `dissolve` **approval proposal** (v_16) — a permanent hard delete that needs a majority of admins to approve (executes immediately for a solo-admin space). |
| GET / POST | `/api/spaces/{id}/proposals` | Multi-admin approval (quorum) for critical actions (v_16). GET lists open proposals + tally; each carries `proposed_by_label` (the signed proposer's display name, `null` when unknown) and its `params` never echo a proposer copy. POST `{action, space_type?}` opens a `dissolve` or `set_public_tier` proposal. Any admin may propose; executes once a majority of admins approve. `remote_admin_action` is not proposable (422) — only the host's forward gate opens one, for a forwarded admin action while delegation is off: the owner alone approves it, it runs as the signed sender's seat, a household holds at most 20 open at a time, and a newer `set_member_role` request for the same seat replaces the older one. A protected admin can't propose — or approve — a move to `public` / `global` (403 `ACCOUNT_PROTECTED` / `public_spaces`); rejecting stays open. Nobody can move a space a protected account owns to `public` / `global` (403 `This space can't be made public.`; an open one is rejected). `POST /api/spaces` with `space_type` `public` / `global` is refused the same way. |
| POST | `/api/spaces/{id}/proposals/{pid}/vote` | Admin approves / rejects an open proposal (`{approve}`). A reject cancels it; a majority of approvals executes it. |
| POST / DELETE | `/api/spaces/{id}/archive` | Archive (read-only, reversible) / unarchive. Owner or admin. |
| GET | `/api/spaces/{id}/compat` | Owner / admin only. Per-space protocol-version compatibility of member households (#319 ¶5). Returns `{"ours": <int>, "min_member_proto_version": <int\|null>, "lagging_features": [...], "lagging_feature_keys": [...], "behind_members": [{instance_id, display_name, proto_version, lacking_features, lacking_feature_keys}]}`. `lagging_features` are the shared-space features unavailable because the weakest known member household lacks them; `behind_members` lists each lagging household and the space features it's missing. Each `*_features` list holds English labels; its `*_feature_keys` sibling holds the stable snake_case slugs of the same features in the same order (e.g. `fast_media_transfer`), which the SPA translates as `capability.<slug>`. Slugs never change once shipped; the English lists stay for older clients. Member households that have never advertised capabilities (mid-handshake) are excluded — they aren't genuinely behind. `min_member_proto_version` is `null` when there are no known remote members. |
| POST | `/api/spaces/join` | Join a space via an invite token. Body: `{token}` plus, for a code minted on another household, `issuer_instance_id` (routes the redeem over federation) and `space_id`. Only `token` is required. A code minted for someone the issuer has never federated with also carries the §D2b bootstrap block — `issuer_identity_pk`, `issuer_keywrap_pk`, `issuer_keywrap_sig` (all three required for the block to count), `issuer_proto_version`, `expires_at`, and `gfs` (base URL of the connection server that served the invite). Those are used **only** when neither a direct pairing nor a mesh route reaches the issuer: the redeem is then sealed to `issuer_keywrap_pk` and relayed by instance id through that connection server. A `subscriber` (Follower) link redeems across households like any other (v_30): the redeeming user lands at `role='subscriber'` locally, the household is seated as a read-only follower on the host, the host gossips that seat to every other member household, and every space-content write it sends is refused by each of them (§24.11 `check_space_writer`; the one opt-in is comments under `allow_subscriber_comment`). An `admin`/mod link does not grant admin on redeem: the user is seated as a `member` and the response carries `pending_role: "admin"`, with a pending elevation filed for an owner to approve (see the join-requests routes below). 422 `REDEEM_DENIED` on a refusal (bad token, ban, no relay that can carry the envelope), 504 `ISSUER_TIMEOUT` when the issuer never answers. |
| POST | `/api/spaces/{id}/ownership` | Transfer ownership (`{to_user_id}`). A `public` / `global` space can't be handed to a protected account (403 — make it private first). |
| GET | `/api/admin/spaces` | Admin-only: list all spaces on this HFS. |
| GET | `/api/spaces/{id}/feed` | Space feed summary. |
| POST | `/api/spaces/{id}/sync` | Trigger a re-sync with the space hosts. |
| POST / DELETE | `/api/spaces/{id}/subscribe` | Subscribe / unsubscribe to a public or global space. Idempotent. Subscribe adds the caller as `role='subscriber'` in `space_members` (read-only member — receives content, cannot post / comment / react). For an id with **no local row**, subscribe first mirrors the listing from the first paired GFS that serves it (`GET {gfs}/gfs/spaces/{id}`) and seats a `space_type=global` stub with the GFS-served authority key (TOFU-pinned) before registering on the GFS relay — no GFS knows it, or the listing fails validation → unchanged 404; a paired GFS that errors → **502 `GFS_UNAVAILABLE`**. Private / household spaces return 403. Unsubscribe is a no-op for users who aren't subscribers (won't demote real members); when it removes the **last** local member of a provable GFS mirror it also unsubscribes from every paired GFS and purges the stub with its content. Returns `{subscribed}`. |

**Members**

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/spaces/{id}/members` | List / invite. Each listed row (local and remote seats) carries `mention` — the exact @-token (without `@`) a composer inserts to mention that member, unique in the space (`handle`, or `handle@<user_id prefix>` when two members share a handle; `null` when the member has no token-safe handle) — and remote rows add `household_name` (the paired peer's display name, `null` if unknown). Every row has `is_owner`; on a member household the owner's mirrored seat is reported with `role: "owner"` once the host's roster named it. |
| GET / PATCH / DELETE | `/api/spaces/{id}/members/me` | Self member profile. |
| POST / DELETE | `/api/spaces/{id}/members/me/picture` | Space-specific avatar. |
| GET / PATCH / DELETE | `/api/spaces/{id}/members/{user_id}` | Admin-only ops. `PATCH {"role":"admin"\|"moderator"\|"member"}` changes a role: the owner sets any of them, an admin moves a member only between `member` and `moderator` (403 otherwise; 422 for any other role). On a member household the change is forwarded to the space's host (v_47): **202** `{"user_id","role","forwarded":true}` — the host re-checks it and the new role arrives with its roster update (a refused one never does); 409 `HOST_TOO_OLD` (`feature: "role_change"`) when the host is below v_47; 503 `HOST_UNREACHABLE` (`reason: "unreachable"` / `"unknown_host"`) when the forward reached nobody and was not queued (also for every other forwarded admin action). |
| GET | `/api/spaces/{id}/members/{user_id}/picture` | Fetch a member's space avatar. |

**Invites / joins / moderation**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/spaces/{id}/invite-tokens` | Mint an invite link. Body `{role?, uses?, ttl_seconds?, publish_to_gfs?, via?}`; `via` is the link's type — `"gfs"` (a household that never met us redeems it through the connection-server relay) or `"internal"` (`"gfs_legacy"` appears only on listed pre-0079 links and is never mintable — `422`) (only households paired with us or reachable over the mesh; never touches a connection server, and its `code` carries no key-wrap key). Omitted: `"internal"` on a private space whose `features.private_gfs` is off, `"gfs"` otherwise (every public / global space, unchanged). `"gfs"` on a private space with `private_gfs` off → **`409 PRIVATE_GFS_OFF`** (nothing written); an unknown `via`, or `"internal"` with `publish_to_gfs`, → `422`; `role` is `member` (default) / `subscriber` / `moderator` / `admin` and is the **issuer's** decision — the redeemer can't ask for more. Admin or owner may mint `member` / `subscriber` / `moderator` (the same people who may promote to moderator); **owner only** may mint `admin`; `owner`, an unknown or a non-string role is `422 UNPROCESSABLE` with the detail `role must be 'member', 'subscriber', 'moderator' or 'admin'`. A `moderator` link seats a moderator straight through on every redeem path (local, paired, mesh, link-joined — no pending elevation; an `admin` link seats a member plus a pending elevation); a redeeming household below federation v_41 is seated as a `member` instead (never refused) and the join returns `role: "member"`. `publish_to_gfs` is a paired connection server's id — the link's blob is parked there. `ttl_seconds` defaults to `604800` (7 days); **both an explicit `null` and `0` mean never expires** (limited only by `uses`) — `null` is the service's spelling, `0` is what the SPA's "Never" picker sends, and the route normalises both. A negative value is `422`. Returns `201 {token, role, via, uses, uses_remaining, expires_at, created_by, created_at, code, gfs}` where `uses` is the minted total (so a client can render "3 of 10 left"), `code` is the `socialhome://invite#<blob>` string, and `gfs` is `null` or `{gfs_id, gfs_token, url, gfs_url}` (`url` is the shareable /join page, `gfs_url` the server's base URL). `403` when the role isn't permitted for the actor; on a member household's stub of a space hosted elsewhere the mint is **forwarded to the host** (v_52), which re-checks the actor's live admin seat and mints the link in its own table (publishing to its own connection to the chosen server) — the response is the host's link; `503 HOST_UNREACHABLE` when the host does not answer within 20 s, `409 HOST_TOO_OLD` (`feature: "invite_link"`) for a host below v_52, nothing is written locally; `422` for `owner` / an unknown role / an unpaired server / a server that can't host invite links (nothing is persisted — publish happens first). |
| GET | `/api/spaces/{id}/invite-tokens` | List the space's live invite links (admin or owner), newest first, same shape as above under `{tokens: [...]}`. Expired and exhausted links are excluded — they grant nothing. `role: "admin"` links are listed to the owner only. On a member household's stub of a space hosted elsewhere the list is **the host's** (forwarded, v_52 — every live link, whoever minted it); `503 HOST_UNREACHABLE` / `409 HOST_TOO_OLD` as for the mint. |
| DELETE | `/api/spaces/{id}/invite-tokens/{token}` | Revoke one invite link (admin or owner; any admin may revoke any of the space's links except an `admin` link, which only the owner revokes — `403`). `204`, idempotent. On a member household's stub the revoke is **forwarded to the host** (v_52), which deletes its row and takes the parked blob down; `503 HOST_UNREACHABLE` / `409 HOST_TOO_OLD` as for the mint. Also takes the blob down on the connection server it was published to — fail-soft, so a server that is down still leaves the link dead locally (one WARNING names it). Revoking never un-seats anyone who already redeemed; that is member removal. |
| GET | `/api/invite-links/{token}/code` | **Public** (no auth), per-IP rate limited. Returns `200 {"code": "socialhome://invite#<blob>"}` when `{token}` is a live invite link minted by *this* household, `404` otherwise — the same answer for an unknown token, an expired one, an exhausted one and a revoked one, so it is not an existence oracle beyond what redeeming the token already is. Unauthenticated on purpose: **the token is the credential**, and anyone holding it can already redeem the link. It exists so the `/join/{token}` landing page can serve its "you're on the wrong household" fallback with a *complete*, bootstrap-capable paste code — issuer keys, connection server and all — instead of the bare token, which the receiver's own household could not act on. |
| GET | `/api/spaces/{id}/join-requests` | Pending requests. Each row carries `requested_role`: `null` for an ordinary join, `"admin"` for a pending admin/mod elevation (the applicant is already a member; an admin link filed it). |
| POST | `/api/spaces/{id}/join-requests/{req_id}/{approve\|reject}` | Decide. Approving a `requested_role='admin'` row runs the owner-only promote (`set_role` / `set_remote_member_role`); the response omits `space_id`/`user_id` for a §D2 remote applicant whose seat finalises on its own household. |
| POST | `/api/spaces/{id}/remote-invites` | Invite a user on another HFS. Returns `201 {token}` when minted directly (owner, or a seed-holding delegated admin on a delegation-ON space); returns `202 {"status":"pending_owner_approval"}` when a non-owner admin invites on a delegation-OFF space — the invite is forwarded to the host for owner approval (rides `SPACE_REMOTE_ADMIN_ACTION`, action `invite`). |
| PATCH | `/api/spaces/{id}/remote-members/{instance_id}/{user_id}` | Promote/demote a remote member (`{"role":"admin"\|"moderator"\|"member"}`) — the same owner / admin matrix as the local route. Federates as `SPACE_MEMBER_ROLE_CHANGED` (`role:"member"` to households below v_41). `moderator` needs the member's home household at v_41 — 403 with code `HOUSEHOLD_UPGRADE_REQUIRED` otherwise. On a member household the change is forwarded to the host (v_47): 202 `{…, "forwarded": true}`, 409 `HOST_TOO_OLD` for an older host, 503 `HOST_UNREACHABLE` when nothing was sent, 403 on the owner's seat. |
| DELETE | `/api/spaces/{id}/remote-members/{instance_id}/{user_id}` | Admin/owner: kick a remote member from the host's side. Routes through `SpaceService.remove_remote_member` — federates `SPACE_REMOTE_MEMBER_REMOVED` and rotates the epoch. |
| GET | `/api/remote_invites` | Remote invites pending for this household. |
| POST | `/api/remote_invites/{token}/{accept\|decline}` | Respond. |
| POST | `/api/spaces/{id}/ban` | Ban a user from a space. |
| GET / DELETE | `/api/spaces/{id}/bans[/{user_id}]` | Ban list management. |
| GET | `/api/spaces/{id}/moderation?status=pending\|all` | The moderation queue of every feature (default `pending`). Owner / admin / **moderator** (content authority); 403 otherwise. Works on the host **and on a member household's stub** (v_43): every household holding a content-authority seat holds the items submitted in the space — the SPA shows the Moderation tab there for content authority. Each item: `{id, space_id, feature, action, entity, op, target_id, submitted_by, submitted_by_display, submitted_at, expires_at, status, reviewed_by, reviewed_at, rejection_reason, publishing, preview, snapshot, current, payload}` (`publishing`: approved on this member household, waiting for the host to publish it) — `preview` the proposed state (a create's full item, an edit's changed fields, a delete's row), `snapshot` an edit's OLD values of those fields (a delete's full row), `current` their LIVE values (`null` when the target is gone). Media URLs are signed. |
| GET | `/api/spaces/{id}/moderation/mine` | The caller's own submissions, any status, newest first (any member) — same item shape, never anybody else's. Drives the author's "Pending review" strip. |
| POST | `/api/spaces/{id}/moderation/{item_id}/approve` | Body `{force?}`. Persists the item through the feature's normal path (federates and notifies like a direct write), attributed to the submitter. `200 {item_id, status:"approved", target_id, post_id}` (`post_id` the legacy alias, `null` for non-posts). `409 STALE {current, proposed, base}` — a page changed since submit; `force:true` applies anyway. `410 TARGET_GONE` — an edit of a deleted item (the item expires). `410 EXPIRED` — past its review window (the item expires). Any household whose user holds content authority approves (v_43, no `NOT_HOST` any more): on the space's host the item is applied at once; on another household the same checks run and the approval goes to the host, which publishes the item from its own copy — the answer is `200 {status:"publishing"}`, nothing is applied there, and the queue item carries `publishing: true` until the host's decision arrives (approval needs the host online; while it is unreachable the request waits in the outbox). `409 IN_PROGRESS` — another approve of the same item is running (double-click, two moderators). `complete: false` in the 200 — published, but a poll / schedule / listing riding with it did not save; approving the (now approved) item again finishes it and answers `complete: true`, else `409 ALREADY_DECIDED`. `409 ALREADY_DECIDED` (also: an item rejected by a higher role than the approver's — owner > admin > moderator — or one decided elsewhere before this household received it), `409 FEATURE_UNAVAILABLE` (feature off / space archived), `403 ACCESS_ADMIN_ONLY` (the feature is now admin-only and the approver is a moderator). |
| POST | `/api/spaces/{id}/moderation/{item_id}/reject` | Body `{reason?}` (≤ 500 characters, else 422). `200 {item_id, status:"rejected"}`. Works on an archived space / disabled feature too. |

**Queued writes (§4.3 `moderated`).** Under a "Reviewed" feature a plain member's create, and their edit / delete / archive of somebody else's item, answers **`202 {queued: true, item_id, feature, action, entity, target_id}`** instead of the usual 200 / 201 / 204 — nothing was persisted. That covers `POST /api/spaces/{id}/posts` (with an optional `poll` / `schedule` object in the same request), `POST /api/bazaar`, `POST /api/highlights/{id}/share` (space scope), space pages `POST` / `PATCH` / `DELETE` / `resolve-conflict`, task and list `POST` / `PATCH` / `DELETE` / archive (a status change or column move included), stickies `POST` / `PATCH` / `DELETE`, and space calendar events `POST` / `PATCH` / `DELETE`. Never queued: own edits / deletes, a position-only move, comments, reactions, RSVPs. A space calendar `POST` with `announce_in_feed` under reviewed posts saves the event (201) and queues its feed card: the response carries `announce_queued: true, announce_item_id`. On a member household's stub the item is stored there (the author's pending strip) and sent to the host and the admin / moderator households (v_43, [`protocol/moderation.md`](./protocol/moderation.md)). Errors: `429 QUEUE_FULL` (20 pending per member per space, 500 per space), `413 PAYLOAD_TOO_LARGE` (> 256 KiB), **`409 HOST_TOO_OLD`** when this household is not the space's host and the host is below protocol v_43 (nothing is stored or sent; the SPA toasts "This space's host household needs an update before your changes can be reviewed."). `GET /api/spaces/{id}` carries `has_remote_households`. A space poll / schedule poll attach (`…/posts/{pid}/poll`, `…/schedule-poll`) is 403 under reviewed posts for a member — send it with the post. `POST /api/bot-bridge/spaces/{id}` from a member's personal bot under reviewed posts is `403 BOT_POSTS_REVIEWED` (refused, not queued). |

**Appearance**

| Method | Path | Purpose |
|---|---|---|
| GET / POST / DELETE | `/api/spaces/{id}/cover` | Space cover image (hero banner). |
| GET / POST / DELETE | `/api/spaces/{id}/icon` | Space icon (avatar), distinct from the cover; owner/admin upload. Falls back to the emoji when unset. |
| GET / PUT | `/api/spaces/{id}/theme` | Space-level theme. `GET` returns `{primary_color, accent_color, header_image_file, background_tint, mode_override, font_family, post_layout, is_default:false}`, or the household theme with `is_default:true` when the space has none. `PUT` (owner/admin) takes a partial body of those fields — an absent field keeps its value. Colours are `#RRGGBB`; `background_tint` / `mode_override` accept `null` (no override); `mode_override` ∈ `light` \| `dark` \| `auto`; `font_family` ∈ `system` \| `serif` \| `rounded` \| `mono` (an id, never a CSS font stack); `post_layout` ∈ `card` \| `compact` \| `magazine`; `null` for `font_family` / `post_layout` resets to `system` / `card`. A bad value is `422 INVALID_THEME` whose `detail` names the field and the allowed values (e.g. `font_family must be one of: mono, rounded, serif, system`); nothing is saved. |

**Customisation**

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/spaces/{id}/links` | List / create admin-configured sidebar quick-links. Members see; admin/owner writes. Body: `{label, url, position?}`. `label` must be non-empty; `url` must be an absolute `http://` or `https://` address with a host — at most 2048 characters, no spaces or control characters, no `user:password@` — otherwise `422 INVALID_LINK` (`error.detail` names the rule, never the value; nothing is stored). The SPA's links strip skips any stored link that is not http(s). |
| PATCH / DELETE | `/api/spaces/{id}/links/{link_id}` | Update or remove a link. Admin/owner. PATCH re-validates the resulting `label` / `url` with the same rules (`422 INVALID_LINK`). |
| GET / PUT | `/api/spaces/{id}/notif-prefs` | Caller's per-space notification level. Body: `{level}` where level ∈ `"all"` \| `"mentions"` \| `"muted"`. Applies to space posts **and** space comments. `all` → a `space_post_created` / `space_comment_added` bell for every post / comment by someone else; `mentions` → only posts / comments that @-mention the caller; `muted` → nothing, mentioned or not. A mentioned member (level `all` or `mentions`) gets one `space_mention` bell ("{author} mentioned you in {space}" / "… in a comment in {space}") **instead of** the generic one. Mentions resolve against the space's members only (never a non-member), the author is never notified of a self-mention, and `@here` — only when the space's `allow_here_mention` is on and the author is an owner/admin by the receiving household's own roster — gives every member at `all` / `mentions` (not `muted`, not the author) one `space_here` bell ("{author} notified everyone in {space}"), replaced by the `space_mention` bell for a member also mentioned directly; at most one `@here` per author per space per 10 min pages anyone (a later one notifies like a plain post). Push carries the title only (§25.3). |

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

`PATCH {content}` / `DELETE /api/spaces/{id}/posts/{pid}` edit / soft-delete
a post of the **path** space only (§24.11): a `{pid}` that belongs to
another space is **404** and nothing changes, whatever the caller's role in
either space. Edit / delete is the author's, or content authority's
(owner / admin / moderator) on somebody else's post; `PATCH` answers
`{id, content, edited_at}`, `DELETE` 204.

`POST /api/spaces/{id}/posts` accepts the same body as the household
endpoint, including `{type: "location", location: {lat, lon, label?}}`.
Space-scoped location posts ride on the existing
`SPACE_POST_CREATED` federation event — peers receive the location
inside the encrypted payload and render the same map card.

#### Link previews

Every post (`GET /api/feed`, `GET /api/spaces/{id}/feed`, the
`post.created` WebSocket frame) carries `link_preview`:
`{url, title, description, site_name, thumbnail_url}` or `null`. It is
built **once, by the author's household**, when a `text` post with a web
link is created (household feed or space; comments and DMs never get
one) and `no_link_preview` is not set. For a space post it travels inside
the encrypted `SPACE_POST_CREATED` payload (its image as an ordinary
`SPACE_MEDIA_BLOB`), so member households render the card without ever
contacting the linked site. Text fields are plain text (the SPA escapes
them); `url` is `http(s)` only; `thumbnail_url` is always local media.
An edit keeps the card while the post's first link stays the same and drops it when the link changes or goes (never re-fetched on edit); every household applies that rule to its own copy.

The household admin switch is `allow_link_preview` on
`/api/household/preferences` (default on). Off, this household fetches
nothing — the composer shows no card and new posts carry none; cards on
posts other households send still render (they cost no fetch here).

### Pages

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/pages` | Household-level pages. |
| GET / PATCH / DELETE | `/api/pages/{id}` | CRUD. `PATCH` takes an optional `base_updated_at` (the `updated_at` the editor loaded): a mismatch is `409 {error: "stale_update", current}` and changes nothing. A successful `PATCH` answers with the **stored** row, so its `updated_at` is exactly the `base_updated_at` for the editor's next save. |
| POST | `/api/pages/{id}/lock` | Acquire 5-minute edit lock. |
| POST | `/api/pages/{id}/lock/refresh` | Extend lock. |
| GET | `/api/pages/{id}/versions` | Version history. |
| POST | `/api/pages/{id}/revert` | Revert to earlier version. |
| POST | `/api/pages/{id}/{delete-request\|delete-approve\|delete-cancel}` | Two-admin delete. |
| GET / POST / PATCH / DELETE | `/api/spaces/{id}/pages[/{pid}]` | Space-scoped pages. `PATCH` has the same `base_updated_at` → `409 stale_update` check (between tabs of this household) and answers with the stored row, like `/api/pages/{id}`. v_48 host-sequenced pages: every page dict carries `seq` (the host's sequence number of this version), `base_seq` and `pending`. While this household's own edit waits for the space's host, `pending` is `true` and `base_seq` is the version it was made from; the SPA shows "Saved here · waiting for the host". On the host an edit is sequenced at once; on a member household it is an optimistic draft proposed to the host. List rows carry `in_conflict: bool`. `GET …/{pid}` adds `in_conflict` and `conflict: {sides: [{hash, title, content, cover_image_url, by, at, base_seq}], current_hash} \| null`. These are edits the host could not merge, one side per user. `content` media URLs are signed like the page body, and `current_hash` is the hash of the canonical body. A conflict never blocks a `PATCH` (no `409 PAGE_CONFLICT`). |
| GET | `/api/spaces/{id}/pages/{pid}/versions` | A space page's edit history (read-only, oldest first; same row shape as `/api/pages/{id}/versions`). Members and subscribers of the path space; non-member 403 → feature 403 → unknown/other-space `pid` 404. Only snapshots recorded under this space. No lock or revert routes exist for space pages. |
| POST | `/api/spaces/{id}/pages/{pid}/resolve-conflict` | Settle an open conflict. Body `{resolution, side?, sides?, content?}`. `resolution` is `"side"` (keep the version whose hash is `side`; the current body's hash keeps the current version), `"merged_content"` (store `content` under the current title), or the two-way `"mine"` / `"theirs"` (the current body / the newest other side). `sides` holds the hashes the user saw. The resolution is an ordinary edit that also retires every open side (`resolves`): the host commits it at once, a member household proposes it (`pending` until the host answers). Answers 200 `{ok, content, page}`. `422` for an unknown resolution, a `side` resolution without `side`, `merged_content` without `content`, or `sides` that isn't a list of strings. **`409 STALE`** (`{sides}` = the open set now) when `sides` or `side` no longer match the conflict. `409 NO_CONFLICT` without one. `202` when held for review (moderated pages). Any writer the space's `pages` level admits for an edit; not subscribers; archived space 403. |

**Scope and permissions.** `/api/pages[/{id}…]` is the **household**
surface only: every `{id}` route (read, edit, delete, lock, refresh,
versions, revert, delete-request / approve / cancel) answers **404** for
a space page id — even for a member of that space — exactly like an
unknown id, and nothing changes. The space surface
`/api/spaces/{id}/pages[/{pid}…]` requires membership of the path space
(**403** for a non-member, checked before any feature or id check) and
the space's `pages` feature (403 `FEATURE_DISABLED`); a `{pid}` that does
not belong to space `{id}` — another space's page or a household page —
is **404**. Writes (`POST`, `PATCH`, `DELETE`, `resolve-conflict`) are
**403** for read-only subscribers and while the space is **archived**
(reads keep working). Deleting a space page federates
`SPACE_PAGE_DELETED` to the space's member households.

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
unnecessary. A `cover_image_url` that is not a local media reference
(`api/media/<name>`) is served as `null` (page, version and conflict
side dicts) so a third-party cover never reaches an `<img>`.

### Tasks

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/tasks/lists` | List / create task lists. Each `GET` row carries `open_count` — its tasks that are not `done` and not archived (one grouped count, so a caller needs no per-list fetch to show totals). |
| GET / PATCH / DELETE | `/api/tasks/lists/{id}` | CRUD. |
| POST | `/api/tasks/lists/{id}/reorder` | Reorder tasks in a list — `{"order": [task ids], "moved_id": id}`; each id gets its index as `position`. `moved_id` (required, must be in `order`, else 422) is the card the user dragged: only it must be editable by the caller (see below), else 403 and nothing moves — neighbours whose positions shift as a side effect need no rights, **as long as they keep their current relative order**. An `order` that also rearranges the other cards needs edit rights on each of them (an admin, or their creator), else 403. Duplicate ids in `order` are 422. |
| GET / POST | `/api/tasks/lists/{id}/tasks` | List / create tasks. `POST` takes `title` plus optional `description`, `due_date`, `assignees`, `status` (so a board column's quick-add files it straight into that column; default `todo`), `priority` and `labels`; the new task is appended at the bottom of its list. |
| GET / PATCH / DELETE | `/api/tasks/{id}` | CRUD for a single task. |
| GET / POST / PATCH / DELETE | `/api/tasks/{id}/comments[/{cid}]` | Task comments. |
| GET / POST / DELETE | `/api/tasks/{id}/attachments[/{aid}]` | Task attachments. |
| GET / POST | `/api/spaces/{id}/tasks/lists` | List / create a space's task lists. Each `GET` row carries `open_count` (not done, not archived), as on the household roster. |
| PATCH / DELETE | `/api/spaces/{id}/tasks/lists/{lid}` | Rename / delete a space task list. |
| GET / POST | `/api/spaces/{id}/tasks/lists/{lid}/tasks` | List / create tasks in a space task list (same create fields as the household route). |
| POST | `/api/spaces/{id}/tasks/lists/{lid}/reorder` | Reorder a space list's tasks — same body as the household reorder (`moved_id` required; duplicates are 422; a `moved_id` of another list or space is 404; any writable member may rearrange any card). Writable members only; every moved task federates as `SPACE_TASK_UPDATED`. |
| PATCH / DELETE | `/api/spaces/{id}/tasks/{tid}` | Update / delete a space task. |
| POST / DELETE | `/api/spaces/{id}/tasks/{tid}/archive` | Archive / unarchive a space task. |

Space task routes require space membership (403 otherwise) and the
space's `todo` feature (403 `FEATURE_DISABLED`). Writes (every
`POST` / `PATCH` / `DELETE`) are refused with 403 for read-only
subscribers and while the space is **archived** (archive is
read-only; reads keep working). A `{lid}` / `{tid}` that does not
belong to the path space `{id}` is 404 — the same as an unknown id,
so ids of other spaces are neither readable, writable, nor confirmed
to exist.

A task (household and space alike) serialises `priority` (`low` /
`medium` / `high` / `urgent`, or `null`) and `labels` (an array of
strings). On create and `PATCH`, `priority` must be one of those values
or `null` and `labels` an array of at most 10 strings of at most 32
characters — anything else is 422; labels are trimmed and de-duplicated
case-insensitively (the first spelling wins). `PATCH` is partial: an
omitted key is left alone, and an explicit `null` **clears**
`description`, `due_date`, `priority` and `labels` (`null` on `title`,
`status`, `assignees` or `position` is "no change").

Task text is sanitised: control characters and bidi / spoofing marks
(U+202A–202E, U+2066–2069, U+200E/200F, U+061C, U+2028/2029) are
removed. A `title` (at most 200 characters) or task-list `name` (at most
100) that is empty or invisible (whitespace / zero-width only) after
that is 422, as is a `description` over 5000 characters (a visibly empty
description is stored as `null`). `position` must be an integer within
the signed 64-bit range, else 422.

A household task may be changed (`PATCH`, archive, delete, or dragged in a reorder) by its
creator, one of its assignees, or a household admin — anyone else gets
403. Household assignees must be active users of the household (422
otherwise; on `PATCH` only ids being *added* are checked). Space tasks
are collaborative: any writable member may change any task of the
space.

`assignees` (household and space tasks alike) must be a JSON array of
at most 10 non-empty user-id strings — anything else, including a bare
string, is 422; duplicates collapse. On a space task every assignee
must be a member of the space (local or a remote member household's
user), else 422 — on `PATCH` only ids being *added* are checked, so an
assignee who has since left does not block other edits. Assignment
and completion notifications / WS frames for a space task go only to
current local members of that space (`task.completed` fans out to the
space, with `space_id`, not to the household).

### Timetables

School timetables (*Stundenplan*) for the household, gated by the
`feat_timetable` toggle (every endpoint answers 403 `FEATURE_DISABLED`,
section `timetable`, when it is off). A timetable covers a set of
weekdays (`days`, 0=Mon … 6=Sun), each with its own entries
(`kind` `lesson` / `break`, free `HH:MM` start/end, optional `label`,
`title`, `room`, `teacher`, `note`, `color` — a theme token such as
`teal`, never hex — and `icon`, exactly one emoji such as 🔢 ✏️ 👩‍🔬 👍🏽
🇬🇧 1️⃣ for children who can't read yet, at most 16 code points; words,
markup, whitespace, bare joiners and several pictographs like ★★ are
refused).
Per-date overrides `cancel` / `replace` an entry (any of the fields above
plus start/end, including `icon`) or `add` an extra slot. `week_start`
(0 Monday / 6 Sunday) decides what a week is; `validity` holds an
inclusive `valid_from` / `valid_until` and holiday `excluded_weeks`
(week-anchor dates — any day of the week is snapped to its anchor).
Every date must fall in 1900–2199. Ids are `[A-Za-z0-9_-]{1,64}`.
Request bodies are strict: an unknown key is a 422 (the federation wire
format stays lenient). An override dated more than 14 days in the past is
a 422 — such overrides are pruned on every save.

Every mutation except create / delete carries the client's `version`
(body, or `?version=N` on DELETE — query preferred) and fails with
409 `TIMETABLE_CONFLICT` (+ `current_version`) when stale. Mutations
answer `{"timetable": …}` — the wire dict (`schema: 1`) plus computed
`active_this_week` / `valid_today` in the timetable's `tz`. Other
errors: 422 `UNPROCESSABLE` (overlap, < 5 minutes, entry on a hidden
day, unknown color / field, bad icon…), 409 `DAY_HAS_ENTRIES` /
`DAYS_ORPHAN_ENTRIES` (+ `count`; retry with `replace` / `drop_orphans`),
409 `TIMETABLE_LIMIT` (30 per household).

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/timetables` | List `{"timetables": […]}` / create `{name, template?: "school" \| "empty", week_start?, days?, tz?, assignees?, color?}` → 201. `school` seeds six untitled lessons 08:00–13:20 with two breaks per day; `tz` defaults to the household tz, `assignees` to the creator. |
| GET | `/api/timetables/day?date=YYYY-MM-DD` | The caller's day: `{date, timetables: [{timetable_id, name, color, lessons: […]}]}` for timetables the caller is assigned to and that are in effect that day. `date` defaults to today in the household tz. |
| GET / PATCH / DELETE | `/api/timetables/{id}` | Read / edit the header `{version, name?, color?, week_start?, tz?, days?, assignees?, defaults?, drop_orphans?}` / delete. `assignees` must be a list (`[]` clears it; `null` is a 422); every id must be an active local user. |
| POST | `/api/timetables/{id}/duplicate` | Deep copy with fresh ids `{name?}` (body optional; default name `"<name> (copy)"`) → 201. Assignees who are no longer active are dropped. |
| POST | `/api/timetables/{id}/days/{weekday}/generate` | Set a day's entries `{version, slots: [entry…], replace?}`. |
| POST | `/api/timetables/{id}/days/{weekday}/copy` | Copy a day `{version, to: [weekday…], with_subjects?, replace?}`. |
| POST | `/api/timetables/{id}/days/{weekday}/shift` | Move entries starting at/after `from` `{version, from: "HH:MM", minutes}`. |
| POST / PUT | `/api/timetables/{id}/entries` | Add one entry `{version, weekday, start, end, …}` / replace all `{version, entries: […]}` (an element carrying the `id` of an existing entry keeps it and its overrides; any other id is replaced by a minted one). |
| PATCH / DELETE | `/api/timetables/{id}/entries/{entry_id}` | Partial entry edit `{version, …fields}` (`null` clears) / delete (drops its overrides). |
| PUT | `/api/timetables/{id}/validity` | `{version, valid_from, valid_until, excluded_weeks: [date…]}`. |
| GET | `/api/timetables/{id}/weeks/{date}` | `{"week": {anchor, valid, days: [{date, valid, lessons: [{source_id, date, start, end, kind, label, title, room, teacher, note, color, icon, status: normal \| cancelled \| changed \| added, override_id, original}]}]}}` for the week containing `date`. |
| DELETE | `/api/timetables/{id}/weeks/{date}/overrides?version=N` | Drop every override in that week. |
| POST | `/api/timetables/{id}/overrides` | Add `{version, date, kind: cancel \| replace \| add, entry_id?, …fields}`. |
| PATCH / DELETE | `/api/timetables/{id}/overrides/{override_id}` | Partial override edit / delete. |

#### Space timetables

A space's shared timetables (a class plan) live under
`/api/spaces/{space_id}/timetables` with **the same endpoints and bodies**
as the household ones above, minus `/day`: collection (`GET` / `POST`),
`/{id}` (`GET` / `PATCH` / `DELETE`), `/{id}/duplicate`,
`/{id}/days/{weekday}/{generate,copy,shift}`, `/{id}/entries`,
`/{id}/entries/{entry_id}`, `/{id}/validity`, `/{id}/weeks/{date}`,
`/{id}/weeks/{date}/overrides`, `/{id}/overrides`,
`/{id}/overrides/{override_id}`. Differences:

- Gated by the space's `timetable` feature (`SpaceFeatures.timetable`,
  default off) instead of `feat_timetable` — 403 `FEATURE_DISABLED`,
  section `space:timetable`.
- Any member (followers included) reads; **writes are owner / admin
  only** — a non-member gets 403 `FORBIDDEN` on everything, a member or
  follower 403 on every write. The SPA derives the caller's role from
  `GET /api/spaces/{id}/members` (its own row's `role`: `owner` / `admin`
  edit, `member` / `subscriber` read).
- No assignees: `assignees` must be absent, `null` or `[]` (else 422);
  the wire dict always has `assignees: []`. `tz` defaults to the space's
  tz. Ids are owner-bound (32 hex).
- A timetable id of another space is 404. Limit 409 `TIMETABLE_LIMIT`
  (10 per space).
- `DELETE /{id}` tombstones the timetable (it federates, and can't come
  back); answers `{"ok": true}`.
- Every edit fans out to the space's member households
  (`SPACE_TIMETABLE_UPSERTED` / `_DELETED`, see
  [`protocol/timetables.md`](protocol/timetables.md)) and to local members
  as the `timetable.changed` / `timetable.deleted` WS frames with
  `space_id` set.
- **Home pins:** `PATCH /api/me {"preferences": {"timetable_home_pins":
  [id…]}}` adds those space timetables to the caller's
  `GET /api/me/corner` → `today_timetable` (pins into a space the caller
  left, or whose feature is off, are ignored; ≤ 20 pins read).

### Calendar

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/calendars/invitees` | List cross-household invitees for the calendar event dialog (§23.60). Returns members of confirmed paired peer instances grouped by instance: `{"instances": [{"instance_id", "instance_name", "members": [{user_id, instance_id, remote_username, display_name, picture_hash, picture_url}]}]}`. **Local household members are never returned** — coordinating with a household member is done via the calendar selector, not the invite picker. Empty list when no instances are paired. |
| GET / POST | `/api/calendars` | List / create calendars. |
| GET / PATCH / DELETE | `/api/calendars/{id}` | CRUD. |
| GET / POST | `/api/calendars/{id}/events` | List / create events. Body fields: `summary`, `start`, `end`, `all_day`, `description`, `attendees`, `rrule`, `rsvp_enabled`, `cover_url`, `tz`. `start` / `end` are UTC ISO 8601 — the SPA converts the local-time form input via `Intl` before submitting. `tz` is the optional IANA name the event anchors to (e.g. `"Europe/Berlin"`); when absent the server resolves to the creator's `users.tz`, then the household `preferences.tz` row, then `"UTC"`. `attendees` accepts only confirmed-paired-instance user_ids — local household member user_ids are rejected with 422 (coordinate via the calendar selector instead). Authorization: any active household member can create / edit events on any household member's personal calendar. Response carries the resolved `tz` so the SPA can render the event in the host's wall clock with an "≈ HH:MM your time" hint when the viewer's browser zone differs. **All-day events keep their day bounds in the event's `tz`, not in UTC** — the composer submits `00:00` / `23:59` of the authored day converted through `tz`, so an all-day "1 May" authored in `Europe/Zurich` is on the wire as `2026-04-30T22:00:00Z → 2026-05-01T21:59:00Z`. A client MUST read an all-day event's day back in `tz` (falling back to `"UTC"` when absent); reading its UTC components instead shifts the day for every household east or west of UTC and makes a single-day event look like a two-day span. Every event in the response carries `copies` — the **server-authoritative** sibling set of the household fan-out, resolved from `client_event_uuid`: `[{"event_id", "calendar_id", "owner_username"}, …]`, one entry per member calendar the event was shared to. It is **independent of which calendars the caller currently has visible** — that independence is the point: a client that infers the sibling set from its loaded agenda will duplicate rows when editing a shared event. `copies` is `[]` for an event with no `client_event_uuid` (legacy rows, and ICS-imported events whose VEVENT had no `UID`) and for space events (those live in `space_calendar_events` and have no household fan-out); it is `[{self}]` for a uuid'd event that was only written to one calendar. It never contains `remote_invite` mirrors — those carry the *peer's* `client_event_uuid` and are not ours to edit. For a **recurring** event the ids in `copies` are the **stored** row ids, never the synthetic `{id}@{iso}` occurrence ids the range query expands to. `POST` is idempotent per `(calendar_id, client_event_uuid)`: when a local row already exists on that calendar with the same client-minted uuid, the request **updates that row in place** (preserving its `id`, `created_by` and provenance, emitting an *updated* rather than a *created* domain / federation event) instead of minting a second copy of the same event on the same calendar. The status code stays `201`, and the response body is the updated row. This makes a retried fan-out POST — and an edit the client sent as a create — safe; the partial unique index `ux_calendar_events_fanout` is the on-disk backstop. |
| GET / PATCH / DELETE | `/api/calendars/events/{id}` | CRUD. PATCH treats `cover_url` as tri-state: omitted = leave unchanged, explicit `null` = clear, string = set. `tz` is validated against the IANA database; an unknown name returns 422. PATCH also accepts `client_event_uuid` to attach the event to a shared household group; absent leaves any existing group id untouched. Promoting an event into a group that another local row on the **same calendar** already occupies returns **422** (the partial unique index `ux_calendar_events_fanout` allows one local copy per `(calendar_id, client_event_uuid)`, and two indistinguishable copies of one shared event on one calendar is a client error). GET and PATCH responses carry `copies` with the same shape and guarantees as on `/api/calendars/{id}/events` — the authoritative, visibility-independent fan-out sibling set (`[]` for a uuid-less row; `[]` for a space event, which `GET` also serves to members of its space (404 for anyone else); never `remote_invite` mirrors). `PATCH` / `DELETE` act on personal-calendar events only — a space event id is 404. A space event's `GET` response also carries `can_rsvp` (boolean UI hint: `false` for a read-only subscriber or an archived space; the RSVP route enforces it) and `can_edit` (boolean UI hint: whether the space's `calendar_access` level lets the caller change it; the PATCH enforces it). |
| GET | `/api/calendars/events/{id}/rsvps` | List RSVPs. Members of the event's space only (404 otherwise, same as an unknown event). `?occurrence_at=<iso>` (URL-encoded) scopes to one occurrence of a recurring event. |
| POST | `/api/calendars/events/{id}/rsvp` | Set own RSVP. Body: `{"status": "going\|maybe\|declined", "occurrence_at": "<iso>"}`. `occurrence_at` required for recurring events; defaults to `event.start` for non-recurring. |
| DELETE | `/api/calendars/events/{id}/rsvp` | Clear own RSVP. `?occurrence_at=<iso>` (URL-encoded) required for recurring. |
| POST | `/api/calendars/events/{id}/approve` | Approve / deny pending request-to-join (capped events, Phase C). Approver = event creator OR space admin. Body: `{"user_id": "<uid>", "action": "approve\|deny", "occurrence_at"?: "<iso>"}`. |
| GET | `/api/calendars/events/{id}/pending` | List pending requests (capped events). Approver-only. `?occurrence_at=<iso>` to scope. |
| GET | `/api/calendars/events/{id}/reminders` | List own reminders (Phase D). Optional `?occurrence_at=<iso>` filter. |
| POST | `/api/calendars/events/{id}/reminders` | Add a reminder for the calling user. Body: `{"minutes_before": <int>, "occurrence_at"?: "<iso>"}`. |
| DELETE | `/api/calendars/events/{id}/reminders` | Remove a reminder. Required `?minutes_before=<int>` and optional `?occurrence_at=<iso>`. |
| GET | `/api/calendars/events/{id}/export.ics` | iCal export of one event (Phase F). Member-only. Includes the caller's reminders as VALARM blocks. |
| GET | `/api/spaces/{id}/calendar/export.ics` | Subscribable iCal feed for the next 90 days. Auth via `?token=<feed-token>` (no Bearer required — public path). Honours `If-None-Match` for conditional GET. |
| POST | `/api/spaces/{id}/calendar/feed-token` | Mint / regenerate a per-(user, space) feed token, replacing any earlier one. 403 `ACCOUNT_PROTECTED` / `calendar_feeds` for a protected account, whose earlier feed tokens also stop serving (401) until protection is lifted. Returns `201 {token, url, external_url}`: `url` is the relative feed path, `external_url` the absolute link on the deployment's public origin (`PlatformAdapter.get_public_base_url()`), or `null` when there is none (e.g. an add-on reachable only through ingress) — calendar apps poll from outside the SPA, so the ingress-prefixed `document.baseURI` is never a usable base. The raw token is shown only in this response (only its hash is stored). |
| DELETE | `/api/spaces/{id}/calendar/feed-token` | Revoke the current feed token. Future fetches return 401. |
| POST | `/api/calendars/{id}/import_ics` | Upload iCal. Body is raw `text/calendar` bytes or JSON `{ics}`; capped at 1 MiB by the request-size limit. All-or-nothing: `422 ICS_PARSE_ERROR` (with the parser's reason) when any VEVENT lacks `SUMMARY`/`DTSTART` or the file holds none, else `201 {events: [...], created, updated}`. Re-importing updates instead of duplicating: each VEVENT is keyed on its `UID` (+ `RECURRENCE-ID`, so a series and its overrides stay separate) into a stable `client_event_uuid`, and an event whose key already exists on that calendar is overwritten from the file (summary, times, description, location, rrule) through the idempotent `POST …/events` path (same authorization as editing it); fields a file cannot carry — attendees, `rsvp_enabled`, `cover_url` — keep their stored values. `created` / `updated` count the new vs. updated-in-place rows. A VEVENT without `UID` is always added; events removed from the file are left on the calendar (import is not a sync). |
| POST | `/api/calendars/{id}/{import_image\|import_prompt}` | AI-assisted import. |
| GET | `/api/calendar/{id}/export.ics` | iCal export. |
| …same under `/api/spaces/{id}/calendar/...` | | Space-scoped variants. Space event create/list also accepts/returns `announce_in_feed` (§23.15, default **false**): when true the event also mirrors to the space feed as a `PostType.EVENT` post; otherwise it lives only in the Calendar tab. Each row of the space list (`GET /api/spaces/{id}/calendar/events`) carries `can_edit` — the same boolean UI hint as on `GET /api/calendars/events/{id}`: whether the caller may edit / delete that event (a writable seat in a non-archived space that the `calendar_access` level lets in; `true` under `moderated` for a member's change to somebody else's event, which queues). The `PATCH` / `DELETE` routes enforce it. |

**Announcement dropped (§4.3).** A `POST /api/spaces/{id}/calendar/events`
with `announce_in_feed: true` whose creator the space's `posts_access`
level keeps from posting straight away (`admin_only`, or `moderated` for a
member) still saves the event, with `announce_in_feed: false`, and the 201
body adds `announce_suppressed: true` and `announce_suppressed_reason`
(`"admin_only"` / `"moderated"`) so the SPA can say why. (`PATCH` never
changes `announce_in_feed`.)

**Space calendar scope and permissions.** `GET` / `POST
/api/spaces/{id}/calendar/events` and `PATCH` / `DELETE
/api/spaces/{id}/calendar/events/{eid}` require membership of the path
space (**403** for a non-member, checked before the `start`/`end`,
feature and id checks) and the space's `calendar` feature (403
`FEATURE_DISABLED`); an `{eid}` that belongs to another space is
**404** and nothing changes. The id-only routes —
`GET /api/calendars/events/{id}` (for a space event) and its `rsvp`,
`rsvps`, `pending`, `reminders`, `approve` and `export.ics` — take the
space from the event row itself and answer **404** both for an unknown
id and for an event in a space the caller is not a member of (no
existence oracle); membership is the caller's `space_members` row (an
accepted invitation alone is not membership). Then the `calendar`
feature (403). Writes — create, edit, delete, `rsvp` (`POST` /
`DELETE`) and `approve` — are **403** for read-only subscribers and while
the space is **archived**; reads (list, event read, `rsvps`, `pending`,
`export.ics`) keep working for members and subscribers. Reminders are
the caller's own and never federate, so any member — subscribers
included — may set them. The subscription feed
(`/api/spaces/{id}/calendar/export.ics?token=`) and minting a feed token
also require the `calendar` feature; revoking a token does not (so a
leaked token can always be killed). The household `PATCH` / `DELETE
/api/calendars/events/{id}` only reach personal-calendar rows: a space
event id there is **404** (edit it under
`/api/spaces/{id}/calendar/events/{eid}`). Personal calendars keep the
household trust rule (any active household member may edit any member's
personal events, §23.60).

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

`/api/stickies[/{id}]` is the **household** board only: `PATCH` /
`DELETE` on the id of a space sticky is 404, exactly like an unknown
id. Every household route (`GET` included) requires the household
`stickies` feature (403 `FEATURE_DISABLED`). The space board lives at
`/api/spaces/{id}/stickies` (`GET` / `POST`) and
`/api/spaces/{id}/stickies/{sid}` (`PATCH` / `DELETE`): every handler
requires space membership (403) and the space's `stickies` feature (403
`FEATURE_DISABLED`) — only that one, the household toggle does not gate
space boards; writes are refused with 403 for read-only subscribers and
while the space is **archived** (reads keep working); an `{sid}` that
does not belong to the path space `{id}` — another space's note or a
household note — is 404.

Sticky fields (both boards; `socialhome/domain/sticky.py`), else 422 and
nothing is written: the body must be a JSON object; `color` must be a
hex colour `#RGB` / `#RRGGBB` (case-insensitive) and is stored and
returned as upper-case `#RRGGBB` — named colours, `url(...)` and any
other CSS are refused, since the SPA renders it as a `background`;
`position_x` / `position_y` must be finite numbers and are clamped into
the board's `0–1000 × 0–700` coordinate space; `content` has control
and bidi-override characters stripped, must not be empty after that and
is at most 2000 characters. Federated / synced stickies follow the same
rules leniently: a non-hex colour becomes the default `#FFF9B1`, a bad
coordinate `0`, over-long content is truncated (WARNING) — the raw value
is never stored.

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
| POST   | `/api/highlights/{id}/publish` | Author mints a public share token via a paired GFS (403 `ACCOUNT_PROTECTED` / `public_links` for a protected account). Body: `{gfs_id, label?}`. Returns 201 `{token, url, label}` — the URL is `https://{gfs}/highlight/{instance}/{highlight}/{token}`. Mint additional tokens with repeat calls. |
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
| POST | `/api/bazaar/{id}/offers` | Make an offer on a fixed / negotiable listing. Body: `{amount, message?}`. Returns the new offer row. 403 `ACCOUNT_PROTECTED` / `bazaar` for a protected account (as are `POST /api/bazaar` and `POST /api/bazaar/{id}/bids`). |
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
| GET | `/api/conversations` | List the caller's conversations. Each row carries `members[]` (other participants only — caller is filtered out) and `member_count` so the inbox renders avatar stacks + peer-name fallbacks (`Anna · Bob`) without N+1 follow-up fetches. Also returns the caller's own `last_read_at` (ISO 8601 or `null`) — the SPA uses it to find the first-unread message in the loaded thread window and anchor the entry scroll on a "New messages" divider. `managed_here` says whether this household keeps a group's member list (it created the group) — the SPA offers add / remove / rename only then. `muted_until` is the caller's own mute (UTC ISO 8601, `9999-12-31T23:59:59+00:00` = until they turn it back on), `null` when not muted or once the time has passed. `notif_level` is the caller's own group level (`"all"` \| `"mentions"`; always `"all"` for a 1:1). |
| POST | `/api/conversations/dm` | Get-or-create 1:1 DM. Body `{username}`. |
| POST | `/api/conversations/group` | Create group conversation (≥3 participants total — creator + ≥2 others). Body `{members: [username, ...], member_user_ids?: [user_id, ...], name?: string}`. `member_user_ids` may name people from directly paired households at v_37+ — this household becomes the group's authority (see [cross-household groups](./protocol/dm.md#group-conversations-across-households)). A person who can't join is refused with 422 `GROUP_MEMBER_UNSUPPORTED` and a message naming them and why (not paired / their household needs an update). |
| PATCH | `/api/conversations/{id}` | Rename a group — body `{name: string \| null}`. Only a member on the group's authority household; 403 otherwise. |
| POST | `/api/conversations/{id}/members` | Add people to a group — body `{usernames?: [...], user_ids?: [...]}`; 201. Only a member on the group's authority household (403 otherwise); 422 `GROUP_MEMBER_UNSUPPORTED` as above. |
| DELETE | `/api/conversations/{id}/members/{user_id}` | Remove someone from a group. Only a member on the group's authority household (403 otherwise); 404 when they are not in it. The removed person's household gets the new roster and drops the group. |
| PUT / DELETE | `/api/conversations/{id}/mute` | Mute / unmute a conversation **for the caller only**. `PUT` body `{duration: "1h" \| "8h" \| "1w" \| "forever"}` → `{muted_until}`; `DELETE` → `{muted_until: null}`. Members only (403); 422 for any other `duration`. While muted, new messages still arrive and count toward `unread`, but create no `dm_message` bell row and no push for the caller. A time in the past reads as unmuted (no scheduler). Local only — a mute is never federated. |
| GET / PUT | `/api/conversations/{id}/notif-prefs` | The caller's own notification level for a **group** conversation. Body / response `{level}` where level ∈ `"all"` \| `"mentions"`. `all` (default) → a `dm_message` bell for every message by someone else; `mentions` → only messages that @-mention the caller. A mentioned member (either level) gets one `dm_mention` bell ("{sender} mentioned you in {chat}") **instead of** the `dm_message` one; an edit rings only the members it newly mentions. Mentions resolve against the conversation's seats on this household only. An active mute wins over both. Members only (403); `PUT` → 422 for another level or a 1:1. Push carries the title only (§25.3). Local only, never federated. |
| POST | `/api/conversations/{id}/leave` | The caller leaves. A 1:1 is hidden for them; a group is left for good (on another household's group, the authority is told with `DM_GROUP_LEAVE`). |
| GET / POST | `/api/conversations/{id}/messages` | List / send. |
| PATCH / DELETE | `/api/conversations/{id}/messages/{mid}` | Edit / delete **your own** message. `PATCH` body `{content}` → `{id, content, edited_at}`; `DELETE` → `{ok: true}` (soft delete: the row stays as "message deleted"). Sender only (403); a message that isn't in this conversation is 404; empty content, a deleted message, or a voice note / call entry (not the sender's text) is 422. Text, media captions and location pins are editable; a location is re-validated and rounded like on send. Both federate (`DM_MESSAGE` carrying `edited_at` / `DM_MESSAGE_DELETED`), open threads patch the bubble live (`dm.message_updated`), and an edit notifies only the people it newly @-mentions. |
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
| GET / POST / DELETE | `/api/presence` | Own presence. `GET` rows now also carry session-presence fields `is_online`, `is_idle`, `last_seen_at` so the UI can render the green / amber dot without a separate fetch, and `status` (`{emoji, text, expires_at}`, or `null` when unset or expired). |
| POST | `/api/presence/location` | Location update (rate-limited 10/min). |
| GET | `/api/spaces/{id}/presence` | Presence visible in this space. Carries GPS only — `zone_name` is stripped at the household boundary (§23.8.6). |
| GET | `/api/spaces/{id}/members` | Roster for this space. Each row carries `is_online`, `is_idle`, `last_seen_at` alongside the member metadata so the SPA renders the same dot on space pages. |
| GET | `/api/conversations/{id}/members` | Roster for one DM / group DM — members only (403 otherwise); a group lists only people still in it. Each row carries `user_id`, `username`, `display_name`, `picture_url` (signed avatar URL or `null`), `is_self`, `is_online`, `is_idle`, `last_seen_at`, and `instance_id` / `household_name` (`null` for this household's people; `household_name` is `null` too for a group member on a household this one isn't paired with) so the thread header can render a WhatsApp-style "Online" / "Last seen 2 h ago" line AND surface the peer's avatar next to the TopBar title without a follow-up fetch. `mention` is the exact @-token (without `@`) the composer inserts to mention that member (`null` when they can't be mentioned by token). |
| POST | `/api/conversations/{id}/messages` | Send a message into a DM / group DM. Body: `{ content, type?, media_url?, file_name?, mime_type?, file_size_bytes?, reply_to_id? }`. `type` ∈ {`text`, `image`, `video`, `file`, `transcript`, `location`} — defaults to `text`. A `location` message carries its pin as a JSON object in `content`: `{lat, lon, label?, accuracy_m?}` — the server rounds `lat`/`lon` to 4 dp, buckets `accuracy_m` up to a coarse radius, trims and caps `label` at 80 characters, and rejects anything malformed or out of range with 422 (see [DM location messages](./protocol/dm.md#location-messages)). Media types (`image` / `video` / `file`) require `media_url` (typically obtained from `POST /api/media/upload` immediately before) and may carry the file metadata triple. `media_url` must be a local upload reference (`api/media/<name>`; a leading `/` and a signature query are dropped) — anything else (`javascript:`, a remote URL, another path) is **rejected with 422 `INVALID_MEDIA_URL`**. `GET …/messages` serves `media_url: null` for any older stored row whose `media_url` is not in that shape. **Rejected with 422 `MEDIA_REQUIRES_DIRECT_PAIRING`** when the conversation requires the multi-hop `DM_RELAY` route — media only flows over directly-paired federation, see [DM media](./protocol/dm-media.md). |
| GET | `/api/conversations/{id}/messages` | List messages in a conversation. Each row carries the same shape as the POST body above, plus `id`, `sender_user_id`, `created_at`, `edited_at`, `deleted`, and the cross-household `media_sync_status` (`null` / `'pending'` / `'failed'`) so the SPA knows when to render the preview-spinner overlay on a media bubble. |
| GET / POST | `/api/spaces/{id}/zones` | List or create a per-space display zone (§23.8.7). `GET` open to space members; `POST` admin/owner only. Body: `{name, latitude, longitude, radius_m, color?}`. `name`: 1–64 characters after trimming, no control characters; `color`: `#RRGGBB` or `null` — otherwise 422. |
| PATCH / DELETE | `/api/spaces/{id}/zones/{zone_id}` | Update or delete a per-space zone. Admin/owner only. Partial update; `color: null` clears, omitting fields leaves them. |
| PATCH | `/api/spaces/{id}/members/me/location-sharing` | Member-self-service opt in or out of GPS sharing for this space (§23.8.8). Body: `{enabled: bool}`. Returns `{location_share_enabled: bool}`. |
| GET | `/api/notifications` | Paginated list. Each row carries an optional `link_url` deep-link target — the bell renders unread items as anchors. `dm_message` rows are **collapsed per conversation** — a burst of N inbound DMs from the same peer bumps one bell row rather than stacking N entries; dedupe is scoped to currently-unread rows, so once the recipient opens the thread (`POST /api/conversations/{id}/read` clears the row) the next DM starts a fresh one. Space post / comment rows (`space_post_created`, `space_comment_added`, `space_mention`) go to the space's members only, per their `/api/spaces/{id}/notif-prefs` level (an edit of a post / comment adds a `space_mention` row only for members it newly mentions); `dm_mention` rows ("{sender} mentioned you in {chat}") go to a group member a message — or an edit — newly mentions, per `/api/conversations/{id}/notif-prefs`, and clear with the thread's `dm_message` rows on read; household-feed comments (`comment_added`) still notify the household. `calendar_event_created` rows are **scoped to the event's audience**: personal-calendar events notify the calendar's owner only (and not when the owner created the event themselves); space-calendar events notify space members except the creator. The household-wide fanout from earlier builds is gone. |
| GET | `/api/notifications/unread-count` | Count. |
| POST | `/api/notifications/{id}/read` | Mark read. |
| POST | `/api/notifications/read-all` | Mark all read. |
| GET | `/api/search` | Full-text search (posts, comments, spaces, users). |

## HFS — Pairing

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/pairing/initiate` | Admin-only. Generate a QR payload. Empty body; base URL comes from the platform adapter (`[standalone].external_url` or the HA integration's pushed base). Returns 422 `NOT_CONFIGURED` if unset. |
| POST | `/api/pairing/accept` | Admin-only (signed-in; `401` without credentials, `403` for a non-admin). Scanner posts its side of the DH. `422 INVALID_PEER_URL` when the code's inbox URL is not a usable household address. |
| POST | `/api/pairing/confirm` | Admin-only. Confirm SAS-verified pair. |
| POST | `/api/pairing/introduce` | Admin-only. Introduce self to an intermediary. |
| POST | `/api/pairing/auto-pair-via` | Admin-only. Ask a mutual peer to relay. |
| GET / POST | `/api/pairing/auto-pair-requests[/{id}/{approve\|decline}]` | Admin-only. Auto-pair queue. |
| GET | `/api/pairing/connections` | Paired peers. Readable by any signed-in member (the dashboard map renders it read-only); every write under `/api/pairing/*` is admin-only (`401` without credentials, `403` for a non-admin). Now also carries `home_lat` / `home_lon` per row (4dp-truncated, `null` when unset) so the SPA can render a household map without a follow-up fetch. Each row carries `instance_id`, `display_name`, `status`, `reachable`, **`transport`** (`"rtc"` when the WebRTC DataChannel is open, `"https"` when running on the HTTPS-inbox fallback, `null` when the peer is unreachable or pending), **`share_home`** (`true` / `false` — whether this household's home coordinates are shared with the peer; defaults to `true`), **`queued_envelopes`** (integer — federation envelopes still `pending` in the outbox for that peer, i.e. waiting to be sent and retried automatically once the peer is reachable; `0` when there is no backlog, which is what tells an admin a long-dark household apart from a momentary drop), and **`dropped_envelopes`** (integer — envelopes in the terminal `failed` state: a PERMANENT rejection or an exhausted retry budget. These are **not** retried and are purged 24 h after going terminal, so a non-zero value is delivery loss, not a queue). The two counts are deliberately separate: reporting only the pending one renders dropped messages as "queued for delivery". Also **`last_relay_accepted_at`** (naive UTC `"YYYY-MM-DD HH:MM:SS"` or `null` — when the connection-server relay last *accepted* an envelope for this peer; held in memory, so `null` again after a restart until the next relay send) and **`relay_only`** (bool — `true` when that acceptance is newer than `last_reachable_at`, or there has never been a proven delivery: the peer's recent traffic has only been handed to the relay, which answers a uniform 202 whether or not the household is online, so it is "accepted", not "delivered"). Local operator info only — nothing new is sent to the connection server. Whitelisted fields only. |
| GET | `/api/connections` | Alias of the above. Returns the same shape including `share_home`, `queued_envelopes`, `dropped_envelopes`, `last_relay_accepted_at`, `relay_only` and `local_alias` per row. |
| DELETE | `/api/pairing/connections/{instance_id}` | Admin-only. Unpair — first sends the peer a signed `UNPAIR` (first attempt bounded to 5 s), then drops its queued outbox envelopes, the mesh hints it announced and its row, and pushes a `connection.removed` WS frame to every household member. Space membership is kept. Returns `200 {ok: true, peer_notified}` — `peer_notified: false` when the peer was unreachable: the connection is gone locally at once, and the `UNPAIR` stays queued for up to 30 days behind an `unpairing` tombstone that grants no trust (see `docs/protocol/pairing.md` § Unpairing); `404` for an unknown peer. |
| GET | `/api/pairing/connections/{instance_id}/transport-detail` | Admin-only. Returns `{"last_relay": {"via": <iid>, "ts": <iso>} \| null, "inbox_url": <str> \| null}` — the most recent DM that relayed via a third household within the last 24h, and the peer's inbox address. `inbox_url` is set only for a confirmed, directly paired (`manual`) peer that isn't relay-only — never for a `space_session` household (the connection server shields addresses between link-introduced households). The member-readable connections listing never carries it. Powers the SPA's Manage detail panel. |
| PATCH | `/api/pairing/connections/{instance_id}` | Admin-only. Accepts `{"share_home": bool}` — flip whether this household's home coordinates are shared with the peer. Setting `false` immediately fires a one-shot `LOCAL_HOME_LOCATION_CHANGED` with null coords to revoke the peer's pin; setting `true` fires the current coords to restore it. Idempotent. |
| PATCH | `/api/pairing/connections/{instance_id}/alias` | Admin-only. Body `{alias: string\|null}` — set or clear the local-only rename of the peer (cap 80 chars; whitespace-only clears). Returns `{instance_id, display_name, local_alias, effective_display_name}`. Never federated. |
| GET / POST | `/api/pairing/relay-requests[/{id}/{approve\|decline}]` | Admin-only. Relay-request queue. |
| GET | `/api/friends` | Connected-people dashboard payload (non-admin). Returns `{instance, households[], totals}` — the local household block + every confirmed remote household with its member list (joining `remote_instances` × `remote_users`) plus household coordinates, and per household `supports_group_dm` (v_37+: its people can be put in a group chat). Whitelisted fields only — `routing_secret` / `key_self_to_remote` / `remote_inbox_url` / identity public keys never appear. |
| GET | `/api/admin/federation/compat` | Admin-only. Federation-compatibility panel. Returns `{"ours": <int>, "peers": [...]}` where `ours` is this build's advertised `proto_version` and each peer carries `instance_id`, `display_name`, `proto_version`, `status`, `last_reachable_at`, `capabilities_known` (bool — `false` ⇒ peer is paired but has never advertised capabilities, so its `proto_version` is the conservative default rather than a confirmed value), `lacking_features` (plain-language English labels of the features the peer's version is below, e.g. "Member lists shared between households"), and `lacking_feature_keys` (the stable snake_case slugs of the same features in the same order, e.g. `roster_gossip`; the SPA translates them as `capability.<slug>` and falls back to the English label for a slug it doesn't know). Slugs never change once shipped. Confirmed peers only, ordered by display name. |
| POST | `/api/admin/federation/resync` | Admin-only. Ask a peer to re-broadcast state for a named scope (§319.6). Body `{instance_id, scope}` where `scope` is `"capabilities"`, `"space:<id>"`, or `"calendar:<id>"` (the latter two replay membership-gated content via the §4.4 resume path). Returns `{"status": "ok", "instance_id", "scope"}`. 400 `UNPROCESSABLE` on a missing `instance_id` / unrecognised scope; 409 `PEER_TOO_OLD` when the peer's advertised `proto_version` is below v_19 (it has no resync handler). |
| GET | `/api/admin/federation/external-url` | Admin-only. The admin-set federation inbox base URL. Returns `{base, effective, source}` — `base` is the stored value (`null` when unset), `effective` is what `PlatformAdapter.get_federation_base()` actually resolves (the peer-facing URL, i.e. `base` + `/federation/inbox`), and `source` is `"manual"` / `"auto"` / `null`. The two differ whenever an automatic source is also present, so the UI can say which one is in effect rather than leave an admin guessing. |
| PUT | `/api/admin/federation/external-url` | Admin-only. Upsert `{"base": "https://..."}`. Validates the scheme (http/https), strips a trailing slash, and strips a trailing `/federation/inbox` so pasting a full inbox URL doesn't double it. `{"base": null}` (or an empty string) deletes the row, handing control back to the deployment's automatic source. On a value change, fans out `URL_UPDATED` to every confirmed peer so their cached `remote_inbox_url` tracks the move. Returns `{ok, base, changed, peers_notified}`. 422 `UNPROCESSABLE` on a non-http(s) value. |
| GET | `/api/admin/federation/ice-servers` | Admin-only, read-only. The ICE servers the **federation transport** is currently using, **with secrets removed**: each entry is `{urls, kinds, has_credentials}` — `credential` is an HMAC of `webrtc_turn_secret` under the recommended coturn setup, so it is never returned, and `username` (an `expiry:user_id` pair) is reduced to the `has_credentials` flag. Also returns `has_turn`, `turn_usable` (the same conclusions the boot diagnostics warn about) and `pulls_from_home_assistant`. Purely diagnostic — whether RTC can traverse a network is otherwise visible only in a log warning. |
| GET | `/api/admin/diagnostics` | Admin-only. Support bundle an operator can attach to a bug report: build/version, deployment mode, whether a federation base resolves (host only), the peer table with reachability timestamps (`last_reachable_at` / `unreachable_since` / `capabilities_seen_at`) plus `last_relay_accepted_at` / `relay_only` (accepted by the connection-server relay vs. delivered — same meaning as on `/api/connections`), outbox backlog grouped by peer and status with `max_attempts`, redacted ICE servers, and the schema migration version. Built from an **allow-list** of fields — never by subtracting `SENSITIVE_FIELDS` from rows — so a column added later is absent by default rather than leaked. Excludes peer display names, peer inbox paths (they embed a per-pair secret; only scheme+host is kept), home coordinates, TURN credentials and all user content. `?download=1` adds a `Content-Disposition` filename. |
| PATCH | `/api/admin/instance` | Admin-only. Rename the household — set the federated instance display name. Body `{display_name}` (1–80 chars after trimming). Persists `instance_identity.display_name` and re-broadcasts it to every confirmed peer via `INSTANCE_CAPABILITIES_UPDATED`, so paired households see the new name without re-pairing. Returns `{"display_name"}`. 422 `UNPROCESSABLE` on a missing/blank/over-length name. |

## HFS — Calls & WebRTC

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/webrtc/ice_servers` | STUN/TURN config (alias: `/api/calls/ice-servers`). |
| GET / POST | `/api/calls` | List / initiate. Body `{conversation_id, call_type, sdp_offer}` for 1:1, or `sdp_offers: {user_id: sdp}` (one offer per callee) for a group mesh; a callee with no offer is not rung. Response carries `participants` (everyone invited, caller included). 422 `too_many_participants` above 6 people (`MAX_CALL_PARTICIPANTS`). |
| GET | `/api/calls/active` | Current active call. |
| POST | `/api/calls/{id}/{answer\|join\|decline\|hangup}` | Lifecycle. `answer {sdp_answer}` answers the caller; a second answer from the same callee (another device) is 409 `already_answered`. `answer {sdp_answer, to_user}` answers another participant's mesh-leg offer and is relayed to them only. `join {sdp_offers}` offers mesh legs to named participants (`call.peer_join`) — used by a late joiner and by a callee opening legs to higher-id callees. In a group call `decline` / `hangup` take only that participant out (frames carry `by` + `over`); the call closes once fewer than two are left. `to_user` must be another participant (403). |
| POST | `/api/calls/{id}/ice` | Trickle ICE candidate. Optional `to_user` targets one mesh leg; without it the candidate fans out to every other participant. |
| POST | `/api/calls/{id}/quality` | Report RTT / jitter / loss. |

## HFS — Push

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/push/vapid_public_key` | Public VAPID key for `pushManager.subscribe()` (any signed-in user). |
| POST | `/api/push/subscribe` | Register (or re-register) this browser's subscription. Body is `PushSubscription.toJSON()` plus an optional `id`; the SPA sends a stable id derived from the endpoint (SHA-256) so it can delete the row later without the server ever echoing the endpoint. An upsert never rewrites another user's row. Returns `{id}`. |
| DELETE | `/api/push/subscribe/{sub_id}` | Remove one of your own subscriptions (204; 404 if unknown or not yours). |
| GET | `/api/push/subscriptions` | List own subscriptions. |

## HFS — GFS connections & public spaces

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/gfs/connections` | List / connect. |
| GET / POST | `/api/gfs/connections/default` | Admin. The onboarding "Connect to the GFS" step. **GET** answers from local facts only and never contacts the GFS: `{url, available, reason, connection}` — `url` is `[gfs] default_url` / `SH_GFS_DEFAULT_URL` (default `https://gfs.social-home.io`), `reason` is `null` when `available`, else `disabled` (empty `url`), `no_external_url` (pairing needs the External URL — the inbox the GFS relays to) or `already_connected` (`connection` then holds that row, `status` `active` / `pending`). **POST** (no body) pairs through the GFS's open sign-up: `GET /gfs/info` once, verify the signed `open_signup` capability against the key in that response (the key it then pins, TOFU — same as a QR scan; https unless loopback / LAN), `POST /gfs/signup-token`, then `POST /gfs/register` with exactly the fields QR pairing sends. `201` + the connection (`status` `active`, or `pending` when the GFS approves households by hand). Errors: `404 GFS_DEFAULT_DISABLED`, `422 NOT_CONFIGURED` (no External URL), `409 ALREADY_CONNECTED`, `409 GFS_SIGNUP_CLOSED` (no verified capability, or the token endpoint said no), `502 GFS_UNREACHABLE`, `503 GFS_BUSY` (the GFS rate-limited the token), `422 GFS_IDENTITY_MISMATCH` (`/gfs/info` presents another public key than the pinned one — the shipped default pins the project GFS key `33cf798c…12ab0e` for the shipped URL only; an operator override pins only their own `[gfs] default_public_key` / optional `default_instance_id`), `422 GFS_PAIRING_FAILED` (unusable URL, or the GFS refused the registration). Details are the household's own plain sentences, never the GFS's words. |
| GET / DELETE | `/api/gfs/connections/{id}` | Inspect / disconnect. |
| POST | `/api/gfs/connections/{id}/appeal` | Appeal a ban. |
| GET | `/api/gfs/publications` | Spaces published to GFS. |
| GET | `/api/spaces/{id}/publications` | This space's GFS publications, each with `status` (`active`/`pending`/`banned`). Admin. |
| POST / DELETE | `/api/spaces/{id}/publish/{gfs_id}` | Publish (returns the publication object: `{space_id, gfs_connection_id, published_at, status}`; `422` if the GFS is unreachable or rejects) / unpublish. |
| GET | `/api/public_spaces` | Aggregated directory. Each row carries the host's real `join_mode` (how to become a posting member) **and** `allow_subscribers` (whether the content is publicly readable at all) — both normalised fail-closed, so an older connection server that reports neither reads as `invite_only` + not-readable and the SPA offers no Subscribe button. |
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

### Protected accounts (§CP.R)

A household admin places a user under child protection
(`POST /api/cp/users/{username}/protection`). On top of the space age gate
(§CP.F1), guardian blocks (§CP.F2) and direct-pair-only DMs (§CP.F3), the
**server** refuses a fixed set of surfaces for that account — every refusal
is `403 {"error": {"code": "ACCOUNT_PROTECTED", "capability": "<id>", "detail": …}}`,
enforced in the owning service (so every route and API client gets the same
answer), and lifted the moment protection is disabled:

| Capability | Refused | Why |
|---|---|---|
| `bazaar` | `POST /api/bazaar`, `POST /api/bazaar/{id}/bids`, `POST /api/bazaar/{id}/offers`; accepting an offer or bid on the account's own listing | Trading means money and contact with other households. Browsing and saving listings stay open. |
| `public_spaces` | `POST /api/spaces` with `space_type` `public` / `global`; proposing or approving `set_public_tier` → `public` / `global` | Public / global spaces are advertised to peers and connection servers with the owner's name. Private / household spaces stay open; joining public spaces keeps its age gate. |
| `public_moments` | `POST /api/moments/public/registrations`, `POST /api/moments/public/follows` | Lists the account in a connection server's public directory / announces it to a stranger as a follower. |
| `public_links` | `POST /api/highlights/{id}/publish` | Puts a highlight on the open web. |
| `api_tokens` | `POST /api/me/tokens` | A personal token is a bearer credential an external tool holds. Signing in is unaffected (browser sessions mint through the platform adapter). |
| `calendar_feeds` | `POST /api/spaces/{id}/calendar/feed-token`; serving an existing feed token | The feed URL is its own credential and exposes the account's schedule to whoever holds it. |

**What was set up before.** Turning protection on also closes what the
account had already opened to the outside. Everything is revoked or closed,
not suspended — lifting protection restores the capabilities, not the old
links:

- personal API tokens are revoked (a password sign-in — a token the platform
  login minted — keeps working);
- public highlight links are unpublished (the local flag clears even when the
  connection server is unreachable, so the link stops streaming at once);
- public-moment directory registrations and public follows are removed,
  and the account's earlier public moments are never streamed to a viewer
  again (even if a connection server still lists them);
- active bazaar listings end as `cancelled` (an existing, federated state
  every viewer already shows as closed); no offer or bid on the account's
  listings can be accepted, and nothing is sold *to* the account — its
  earlier offers can't be accepted (422 `This offer can no longer be
  accepted.`) and its bid never wins an auction;
- `public` / `global` spaces the account owns go `private` (through the
  normal federated config change, which also unpublishes a global space);
- earlier calendar feed tokens stop serving (401).

A step that fails is logged and the admin's request answers 422 (`Protection
is on, but some earlier sharing could not be turned off yet. Try again.`),
so it can be retried (every step is idempotent); protection itself is on
either way.

**Spaces a protected account owns** never go `public` / `global`, whoever
asks — an adult admin's proposal (403, detail `This space can't be made
public.`), a quorum, an open proposal from before protection (it is
rejected on the next vote), or a direct config change. A `public` / `global`
space is not handed to a protected account either (`POST
/api/spaces/{id}/ownership` → 403; make the space private first). The wording
never says why.

**Guardian blocks (§CP.F2)** — `POST /api/cp/minors/{minor_id}/blocks/{blocked_id}`
— apply like a personal block, in both directions, while the account is
protected: the two can't open or continue a 1:1 DM, share a group DM
(creating or adding refuses; the block steps the account out of every group
they share), react to each other, or call; an existing 1:1 drops out of both
lists (and a withheld group message never counts as unread for the
account); typing indicators never cross; their moments and highlights are
hidden from each other — and the account's own moments and highlights are
not sent to a household where someone it has blocked lives (the moment
3-hop relay mesh can still carry a moment there through a third household,
which knows nothing of the block); and a blocked person's space posts,
household posts and comments, @mentions, @here, DMs, contact requests (not
stored either), follows, moment replies and reactions never notify the
account. Refusals are 403: the blocked person sees exactly a
personal block's `Recipient has you blocked.`, the protected account `You
can't message this person.` / `You can't call this person.`, and a group
that can't hold both `One of these people can't be added to this group.` —
none says why. The block never
leaves the household; inbound DMs, history sync, media blobs and call offers
from a blocked remote person are refused on arrival (see
`docs/protocol/dm.md` → *Guardian blocks*).

The account learns *that* it is protected and *what* is limited from
`GET /api/me` (`protected`, `restrictions`) and `GET /api/me/protection`
(plus its guardians) — never its recorded age. Nobody else learns who is
protected: the `cp.*` WebSocket frames (protection on/off, guardian and
block changes) reach only household admins, the account itself and its
guardians, and carry no age. The account itself also gets
`me.protection_changed` (no data) on each of those changes, and its SPA
reloads `/api/me` + `/api/me/protection` straight away.
Remote households never learn protection state, `space_session` peers can't
send DMs at all (§24.11 peer class), relayed DMs from a protected sender are
already refused (§CP.F3), and remote invites above a space's age gate are
refused on accept.

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
| POST | `/api/reports` | File a report (`{target_type, target_id, category, notes?, forward_gfs?, space_id?}`). `target_type` ∈ `post, comment, page, task, sticky, calendar_event, gallery_item, user, space, highlight, moment`. Content inside a space is **space-scoped** automatically (the space is derived from the target); `space_id` names the space for a member report (`user`) and, for content, must match it. A target this household does not hold is 404 and nothing is stored; a space-scoped report needs the reporter to be a member — otherwise the SAME 404 (no existence oracle); a `space` report needs a known space the reporter is in (or a public / global one). `notes` ≤ 1000 characters (422 over). 429 when the reporter's daily cap (20), their pending reports in that space (20) or the space's queue (500) is full; `space_id` on `space` / `highlight` / `moment` → 422. Response `{id, status, federated, forwarded_to_gfs, space_id}` — `forwarded_to_gfs` is `true` only when the report actually goes to an active GFS (never for private-space or feed content; the GFS never gets the reporter or the notes) — `space_id` set means the space's moderators (not household admins) triage it. |
| GET | `/api/admin/reports` | Household admin queue — pending **household-level** reports only (never a space's): a plain list of `id, target_type, target_id, reporter_user_id, reporter_instance_id, category, notes, status, created_at, resolved_by, resolved_at, space_id` (`space_id` always `null` here). |
| POST | `/api/admin/reports/{id}/resolve` | Resolve a household-level report; body `{"dismissed": true}` dismisses it instead. A space report id → 404. |
| GET | `/api/spaces/{id}/reports` | The space's pending reports, for its owner / admins / moderators (403 for anyone else — a plain member, or a household admin without a seat; 404 unknown space). A report about the viewer themself — a member report on them, or a report on an item they authored — is left out, except for the space owner when nobody else holds content authority: then the row is `anonymous: true` (reporter fields `null`) and `dismiss_only: true`. Rows as above plus `reporter_name`, `target_preview` (a ≤160-char plain-text glimpse of the item — text / title / caption — or `null`), `target_gone` (the item was deleted, or the member left) and, for a member report, `target_name`. |
| POST | `/api/spaces/{id}/reports/{report_id}/resolve` | Content authority marks one of the space's reports resolved (`{"dismissed": true}` dismisses). 403 as above; a report of another space, or about the caller themself → 404 (the sole-authority owner may dismiss one about themself; resolving it → 403); already decided → 409. The decision is synced to the other reviewer households (`SPACE_REPORT_DECIDED`). |

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
| GET / PUT | `/api/theme` | Household theme. `GET` returns `{primary_color, accent_color, surface_color, surface_dark, mode, font_family, density, corner_radius, updated_at}`. `PUT` (admin) takes a partial body of those fields: colours `#RRGGBB` (`surface_*` nullable), `mode` ∈ `light` \| `dark` \| `auto`, `font_family` ∈ `system` \| `serif` \| `rounded` \| `mono`, `density` ∈ `compact` \| `comfortable` \| `spacious`, `corner_radius` 0–24. A bad value is `422 INVALID_THEME` naming the field and the allowed values; nothing is saved. |
| GET / PUT | `/api/household/preferences` | Household-wide feature toggles plus `household_name` and `tz` (IANA timezone). `PUT` accepts a partial body: `{"household_name"?: str, "toggles"?: {"feat_feed": bool, "feat_pages": bool, "feat_tasks": bool, "feat_stickies": bool, "feat_calendar": bool, "feat_bazaar": bool, "feat_presence": bool, "feat_gallery": bool, "feat_timetable": bool, "allow_link_preview": bool, …}, "tz"?: "<iana>"}` (every `allow_*` post-type toggle is accepted too). `tz` is validated via Python's `zoneinfo` — an unknown name returns 422. In ha / haos modes the value is mirrored from HA Core's `time_zone` on adapter startup; explicit operator edits via PUT are still honoured but get overwritten on the next restart. |
| GET / PATCH | `/api/me/preferences` | Per-user sidebar preferences. `GET` returns `{user_id, hide_highlights, hide_momentum, hide_bazaar}`. `PATCH` accepts a partial body; body keys MUST be a subset of `{hide_highlights, hide_momentum, hide_bazaar}` — unknown keys return 400. Changes apply only to the authenticated user; other household members are unaffected. |
| POST | `/api/media/upload` | Upload a blob. Images/audio return `{url, filename, signed_url}` once processed. **Video transcoding is async** — the upload returns immediately with `{url, thumbnail_url, filename, media_status:"processing", signed_url, signed_thumbnail_url}` before the `.webm`/`.webp` files exist; a background `MediaTranscodeService` worker produces them and pushes a `media.ready` WS frame to the uploader when done. The `.webm` output and its `.webp` poster share one UUID stem (`<stem>.webm` + `<stem>.webp`) so the poster path is derivable server-side from the media URL. List endpoints that surface the video (feed, space feed, momentum, DM messages) carry a `media_status` field (`'processing'` / `'failed'` / `'ready'`; absent ⇒ ready) **and** a signed `media_thumbnail_url` (the poster — the signed `.webp` sibling of `media_url`) on **video** items; the `media.created`/`space.post.created` WS frames carry the same poster. (Gallery items keep their own stored `thumbnail_url`.) |
| GET | `/api/media/{filename}` | Download a blob. |
| GET | `/api/map/config` | Map rendering config for the SPA: `{tile_url, attribution, max_zoom}`. `tile_url` is a **relative** (no leading slash, so it resolves against the ingress-aware `<base href>`) signed template — `api/map/tiles?z={z}&x={x}&y={y}&exp=…&sig=…` — with the literal `{z}`/`{x}`/`{y}` placeholders left for Leaflet to substitute. One HMAC (7-day TTL) authorises every tile, because the signature covers only the path. 503 `SIGNER_UNAVAILABLE` if the media signer is not wired yet (never an unsigned URL). |
| GET | `/api/map/tiles?z=&x=&y=` | Raster tile proxy. OpenStreetMap 403s browser-issued tile requests (a browser cannot set `User-Agent` / `Referer`), so the backend fetches with an identifying agent string and caches in memory. Auth via the signed `?exp=&sig=` above (Leaflet's `<img>` carries no bearer header) or a normal bearer token. Responds with the tile bytes, the upstream content type, and `Cache-Control: public, max-age=604800`. 400 on missing / non-integer / out-of-grid coordinates, 502 `TILE_UNAVAILABLE` when upstream fails and nothing cached can stand in. Upstream is `map_tile_url` / `SH_MAP_TILE_URL`. |
| GET | `/healthz` | Liveness (public). |

## HFS — WebSockets

| Path | Purpose |
|---|---|
| `GET /api/ws` | Realtime event stream — posts, comments, presence, typing, calls, notifications. Auth via `Authorization: Bearer` or `?token=`. Frames: `"ping"` → `"pong"`; client `{"type":"typing","conversation_id":"..."}`. Server fans out `presence.updated` (physical state), `conversation.user_typing` (typing dots), `dm.message` (full Message object — appended without re-fetch), `dm.message_updated` (in-place patch for an existing message — payload `{conversation_id, message_id, content, edited_at}`; drives voice-note transcript fill-in via either the sender's STT or the recipient's fallback STT, and a sender's edit), `dm.message_reaction` (per-user reaction add / remove — payload `{conversation_id, message_id, user_id, emoji, action}`; the SPA aggregates by glyph for the per-bubble reaction strip), `dm.media_ready` (cross-household media full-bytes arrival — swaps preview URL → full URL), `media.ready` (background video transcode finished — payload `{type, output_filename, media_url, thumbnail_url}`; pushed only to the uploader so its SPA swaps the "Processing…" placeholder for the player. Other viewers pick up readiness via the `media_status` field on their next list fetch), `media.failed` (background video transcode permanently failed at the attempt cap — payload `{type, output_filename}`; pushed only to the uploader so its SPA flips the "Processing…" placeholder to the failed state at once. Other viewers pick up the `'failed'` status via the `media_status` field on their next list fetch), `dm.conversation.created` (inbox refresh trigger), `dm.group.updated` (a group's member list or name changed — payload `{conversation_id, name}`; sent to its local members before and after the change, the thread refetches its roster), `pairing.confirmed` (pair handshake complete — payload `{instance_id}`; the SPA refetches `GET /api/connections` so a new household appears in the connections list and map without a reload), `pairing.aborted` (pair failed), `connection.reachable` / `connection.unreachable` (a paired peer's reachability flipped — payload `{instance_id}`), `connection.removed` (a pairing was torn down, by this household's admin via `DELETE /api/pairing/connections/{instance_id}` or by the peer's inbound `UNPAIR` — payload `{instance_id}`; fanned to every household member, like the other `connection.*` frames, since every member can read the connections list; the SPA drops the row in place), `page.conflict` (a page save collided — payload `{page_id, space_id, theirs, theirs_by, federated}`; `federated: true` when a space page's host recorded a conflict side (v_48) — an open viewer refetches the page), `page.sequenced` (v_48: the space's host answered this household's page edit — payload `{page_id, space_id, outcome, reason}`, `outcome` `applied` / `refused`, `reason` `access` / `gone` / `rate_limited` / `bad_base` on a refusal; the viewer drops its "waiting for the host" pill or shows the refusal, `gone` offering "Save as new page"), `peer.transport_changed` (a paired peer's federation transport flipped between WebRTC DataChannel and HTTPS inbox — payload: `{instance_id, transport: "rtc" \| "https"}`), `local.home_changed` (this household's own home coordinates changed — payload: `{latitude: <4dp>, longitude: <4dp>}`; fires on HA / HAOS adapter startup or when HA Core pushes a new location; the Connections Map tab re-centres the own-household pin without a page reload), `peer.home_changed` (a paired peer updated its home coordinates — payload: `{instance_id, latitude: <4dp>, longitude: <4dp>}`; fires when an inbound `LOCAL_HOME_LOCATION_CHANGED` event is processed; the Map tab moves or adds the peer's pin without a refetch), and `user.online` / `user.idle` / `user.offline` (session-presence dot — payload `{user_id, last_seen_at}`; the subject is excluded from the fan-out), `timetable.changed` (a timetable was created or edited — payload `{type, space_id, timetable}` with the full wire dict; household-wide when `space_id` is `null`, else to that space's local members) and `timetable.deleted` (payload `{type, timetable_id, space_id}`, same scoping), and `user.preferences_changed` (per-user sidebar preference update — payload `{user_id, changed: {key: new_value}}`; delivered only to connections owned by the affected user, never household-wide), and `me.protection_changed` (no payload — this account's child protection, guardians or guardian blocks changed; delivered only to that account, whose SPA reloads `GET /api/me` + `GET /api/me/protection`), and `app.message` (real-time event from an installed app — fields: `app_id` (string), `session_id` (string, hex UUID scoping the session), `from_instance` (string, sender household), `from_user` (string | absent, sender's username — present for v_18+ person-routed events and local-loopback sessions; absent for legacy household-addressed and binary inbound), `kind` (`"session"` for `APP_SESSION` control events; `"message"` for `APP_MESSAGE` JSON and binary `fed-app-v1` data frames), `payload` (object, application-defined dict); for v_18+ person-routed events the frame is delivered only to the addressed user; for legacy/binary events it is delivered to every local user whose WebSocket is open when the matching app is enabled; produced by the `AppFederationService` inbound path — either the binary `fed-app-v1` DataChannel or the `APP_MESSAGE` JSON event fallback — after §24.11 validation and AES-256-GCM decryption; the host bridge forwards qualifying frames into the app iframe as `MessageEvent {type:"app:event", kind, sessionId, fromInstance, fromUser?, payload}` so apps can route by session, show the challenger's name, and distinguish invites from in-game moves). |
| `GET /api/ws` — space, gallery and other frames | Thin frames: the SPA refetches the canonical `GET` shape on receipt instead of rendering the frame body. **Space config** — `space.config.changed` (payload `{space_id, sequence, event_type}`; `event_type` is a `SpaceConfigEventType` value such as `rename` / `feature_changed` / `admin_granted` / `admin_revoked` / `role_changed` (member ↔ moderator) / `cover_updated`, or `space_config_changed` for a change applied from a remote host; `{space_id, event_type:"dissolved"}` when the space is dissolved here or by its remote host via `SPACE_DISSOLVED`) to the space's local members. The open space feed and settings page refetch the space detail + the viewer's role; the side-nav spaces list refetches, or drops the space on `dissolved`. On `dissolved` an open space screen leaves for `/spaces` with a toast, unless this tab is the one dissolving it. **Bots** — `space.bot.created` / `space.bot.updated` (`{space_id, bot}`, token-free public shape) and `space.bot.deleted` / `space.bot.token_rotated` (`{space_id, bot_id}`) to the space's members; the "Bots & automations" tab refetches `GET /api/spaces/{id}/bots`. **Moderation** — `space.moderation.queued` / `.approved` / `.rejected` / `.expired` (`{space_id, item}`) to the space's owner, admins and moderators only — the same people who can read the queue — plus, for the three outcomes, the submitter of that item while they are still a member; the moderation queue refetches `GET /api/spaces/{id}/moderation` (owner / admin / moderator). `space.moderation.mine` (`{space_id, item_id, feature, action, status}` — a receipt, no content) to the submitter alone on queue and every outcome; their "Pending review" strip refetches `GET /api/spaces/{id}/moderation/mine`. `space.join.requested` goes to the owner and admins; `space.join.denied` to them plus the requester. **Pairing** — `pairing.auto_pair_requested` and `pairing.intro_received` go to household admins only (pairing management is admin-only). **Location** — `space_location_updated` (`{type, data: {mode, space_id, user_id, lat, lon, accuracy_m, updated_at}}`, or `{mode:"zone_only", …, zone_id, zone_name}`) to the space's local members when a member's pin moves — a local member's, or a remote member's once their household's `SPACE_LOCATION_UPDATED` is stored (coordinates truncated to 4 decimals; a pin whose mode differs from this space's `location_mode` is not sent, so a `zone_only` space never gets coordinates); the space Map tab refetches `GET /api/spaces/{id}/presence` (bursts coalesced). **Gallery** — `gallery.album_created` / `gallery.album_updated` (rename, description or cover change) / `gallery.album_deleted` (`{album_id, space_id}`) and `gallery.item_uploaded` / `gallery.item_deleted` (`{album_id, item_id, space_id}`), for local edits and for changes applied from another household alike, to the space's members for a space album or to the household for a household album (`space_id: null`). The gallery refetches its albums (and the open album's items); an open album follows a rename, and returns to the list with a toast when it is deleted. **Emitted but not consumed by the SPA:** `call.peer_join` (group-call late join; only sent in reply to `POST /api/calls/{call_id}/join`, which the 1:1 call UI never calls), `pairing.intro_received` (`{from_instance, via_instance_id, message}`, household admins only; the §11.9 `POST /api/pairing/introduce` flow it ends has no SPA surface and the intro is not stored, so there is nothing to list; the live friend-of-friend flow is auto-pair, `pairing.auto_pair_requested`). **Status** — `user.status_changed` (`{user_id, status}`, `status` `null` when cleared or expired) to the household, for a local member's `PATCH /api/me` or expiry and for a paired household's `USER_STATUS_UPDATED`; the Presence page refetches `GET /api/presence` and updates the viewer's own "Your status" line. |
| `GET /api/stt/stream` | Streaming speech-to-text (binary audio frames → `{"type":"final","text":"..."}`). |

> Speech-to-text and AI data generation are HA-adapter-only in v1 — the
> standalone adapter raises `NotImplementedError` on
> `transcribe_audio` / `stream_transcribe_audio` / `generate_ai_data`.
> The HA adapter requires `[homeassistant].stt_entity_id` to be set
> (e.g. `stt.home_assistant_cloud`) before STT routes return data.

## HFS — federation inbox

| Method | Path | Purpose |
|---|---|---|
| POST | `/federation/inbox/{inbox_id}` | Inbound federation envelope. Runs the §24.11 validation pipeline before dispatch. A valid envelope is answered `200 {"status": "ok"}` whether it was dispatched or dropped by a post-decrypt gate (idempotent duplicate, banned household, deprovisioned author, archived space, reader / non-member write, write held for a seat) — one body for all of them, so the answer never reveals whether a space exists, is hosted here, is archived or has banned the sender; the reason is logged server-side only. Transport-level failures keep their generic error codes (400 malformed / undecryptable, 403 bad signature, 404 unknown inbox, 410 replay / skew, 413, 429). Bodies carrying `event_type` of `PAIRING_PEER_ACCEPT` / `PAIRING_PEER_CONFIRM` are dispatched ahead of the pipeline (§11 bootstrap — the pair doesn't exist yet, so the pipeline's instance lookup would reject them). See [protocol/README.md](./protocol/README.md). |

## GFS — Public relay (HTTPS REST, SH → GFS)

| Method | Path | Purpose |
|---|---|---|
| GET | `/gfs/info` | Public GFS descriptor — `{gfs_instance_id, public_key, server_name, base_url, anonymous_publish, capabilities, capabilities_sig, capabilities_sig_suite}`. Unauthenticated by design (the key is public); an HFS that scanned the pairing QR (`{base_url, token}` only) fetches the Ed25519 `public_key` here to pin before registering. The GFS↔HFS leg has no `proto_version` negotiation, so capabilities ride this endpoint — **signed**: `capabilities` (today `{"anonymous_publish": true, "envelope_relay": true, "invite_links": true, "authority_rotation": true, "member_publish_trusted": true, "member_publish_strict": true, "private_channels": true, "open_signup": false}` — `open_signup` mirrors the operator's `[policy] open_signup` switch and tells a household it may call `POST /gfs/signup-token` (the onboarding one-click connect offers it only when this signed flag is `true`) — `private_channels` (v_51) says this server carries the opaque `/gfs/channels/*` routes for private spaces (anonymous channel-key-signed registration / epoch notice / unregister, pass-gated identified seats, trusted and strict channel publish), so a household creates a private space's channel — and its members subscribe — only against a server whose signed block proves it (a private space's items otherwise take the host path) — `member_publish_strict` (v_50) says this server carries the anonymous, writer-group-key-signed `POST /gfs/member-publish-anon`, takes `publish_mode` / `writer_key_cert` on the epoch notice and refuses identified publishes into a strict space, so a household sends an anonymous item or those notice fields only to a server whose signed block proves it (a strict space's items otherwise take the host path, never the identified one) — `member_publish_trusted` (v_49) says this server carries the identified, writer-cert-authorized `POST /gfs/member-publish` and the `POST /gfs/spaces/{id}/epoch` notice, so a member household sends an identified item body only to a server whose signed block proves it (otherwise its posts take today's host path) — `authority_rotation` (v_44) says this server re-pins a space's authority key from an owner-signed `authority_cert` on publish, so a household only sends it the cert there and otherwise warns that the old key keeps authorizing relays — `invite_links` says this server carries `POST /gfs/spaces/{id}/invite` and the public `/join/{token}` page, so a household only offers to mint a link against a server that can actually serve it — the latter says this server carries `POST /gfs/envelope`, so a household only attempts a §D2b bootstrap redeem against a connection server that proved it can relay one) travels with `capabilities_sig` (b64url Ed25519 over `b"gfs-capabilities:v1:"` + canonical JSON of `{gfs_instance_id, capabilities}`) and `capabilities_sig_suite` (`ed25519`; unknown suites are rejected, never defaulted), made with the same identity key this response publishes as `public_key`. A household trusts the capability **only** when that signature verifies against the key it pinned at pair time — the bare top-level `anonymous_publish` is informational, and a household sends `/gfs/publish` only to a server whose signed block proves `anonymous_publish` (never an identified body — an older GFS gets no publishes until it upgrades). Once verified the household's cache **ratchets**: a later response without a valid block logs a downgrade warning and does not flip it back (in-process only). A capability block is omitted entirely when the GFS has no signing key wired. The `public_key` itself is **random per deployment**: the GFS mints a 32-byte Ed25519 seed on first boot and persists it as `<data_dir>/gfs_identity.seed` (mode 0600) — it is no longer derived from `gfs_instance_id`, which this same response publishes in the clear and which would therefore have let anyone recompute the private key and forge a signed block. Back the file up with the database; losing it changes the identity and every paired household has to re-pair. Operators who inject secrets from a vault pin it instead with `[server] signing_seed_hex` / `GFS_SIGNING_SEED` (64 hex chars; a malformed value stops the boot rather than minting a different identity). |
| POST | `/gfs/signup-token` | **Open sign-up** (operator opt-in, `[policy] open_signup` / `GFS_OPEN_SIGNUP`, off by default). No body. When on: `200 {token, expires_in}` — a fresh single-use pairing token from the same token service as the landing-page QR (10-minute TTL, consumed by `/gfs/register`), so a household can connect from its onboarding without scanning. When off: one uniform `404 {"error": "not_found"}`. A second token for the same client address inside 30 s → `429` with `Retry-After: 30`. Rate-limited 5 / min / IP and 30 / min server-wide (`429`, `Retry-After: 60`). `auto_accept_clients` still decides whether the registration lands `registered` or `pending`. The capability is advertised as `open_signup` inside the signed `/gfs/info` block. |
| POST | `/gfs/register` | Register instance. The token is consumed with one atomic conditional update, so a token registers exactly one household however many requests race it. Rate-limited 10 / min / IP (`429`, `Retry-After: 60`). `inbox_url` must be a usable household address (same rules as pairing, `socialhome/peer_url.py`); otherwise `422 invalid_inbox_url` and the single-use token is not consumed. |
| POST | `/gfs/instance` | Update a registered instance's `display_name`. Body `{instance_id, display_name, ts, signature}`; Ed25519-signed over canonical JSON of `{instance_id, display_name, ts}` and verified against the registered public key (replay-guarded ±300 s on `ts`). |
| POST | `/gfs/publish` | **Anonymously** relay a space event to the space's subscribers. Canonical body `{space_id, event_type, payload}` — it carries **no household identity**. The **only** authenticator is the **space-authority signature** inside the opaque `payload` (`authority_sig` + `authority_sig_suite`), verified against the space's TOFU-pinned `identity_public_key` under the wire `event_type`, which must be in `{space_post_public, space_subscriber_key_handoff}`. Any seed-holder (owner OR delegated admin) can produce one, so the space keeps working while the owner is offline; the GFS stays blind to content — it verifies a signature over opaque bytes and never decrypts. **What the anonymity does and does not buy:** the GFS does not *require, store, log or forward* the relaying household's identity — it is NOT that the GFS *cannot learn* it. The same household normally holds an authenticated `/gfs/ws` socket to that server from the same address, so network-level correlation (source IP, timing, body size) remains available to the GFS operator. **Moderation:** because the caller is anonymous, instance-level moderation (`client_instances.status = 'banned'`) cannot gate this path — a banned household simply omits the legacy fields. The **space**-level ban is the only moderation lever on the relay; per-IP rate limiting is the only other handle. **Legacy fields accepted but ignored:** an older household still sending `from_instance` + `signature` (its Ed25519 transport signature over canonical JSON of `{space_id, event_type, payload, from_instance}`) succeeds provided the authority sig is valid — the transport signature is still verified when present (a bogus legacy field is a 403), then discarded: it authorizes nothing, never enters the fan-out frame, and is never logged. There is **no owner path** any more. `space_id` and `event_type` must each be a non-empty string of ≤128 chars → 400 otherwise. The body is capped at **256 KiB** (declared `Content-Length` or actual bytes read) → 413. Response is `{status:"published", delivered_to:<count>}` — a COUNT, never the subscriber ids: the authority signature has no nonce/timestamp, so a captured relay frame stays valid forever and must not double as a roster read (that is what `/gfs/spaces/{id}/subscribers` gates behind a replay-guarded authority query). **Replay dedupe:** the GFS remembers a BLAKE2b digest of each successfully authorized payload (over the same canonical JSON the authority signature covers) for **5 minutes**, capped at 10 000 entries. A re-POST of a byte-identical payload inside that window is an idempotent **no-op** — HTTP 200 with `delivered_to: 0`, nothing fanned out, no distinguishable body. The digest is recorded only AFTER authorization succeeds, so a rejected payload cannot pre-poison the same bytes sent legitimately later. The cache is in-memory and **per GFS node**: it bounds the amplification burst one captured frame can drive, it is not a permanent content-id store (past the TTL, after a restart, or on another cluster node the same bytes relay once more). In a cluster of N nodes behind one address a replay burst is therefore suppressed only 1-in-N — each node has to see the bytes once before it starts suppressing them. So subscriber-side dedupe by the post id inside the payload remains the standing backstop and the relay stays at-least-once. **Fan-out deadline:** one relay's fan-out runs at most `FAN_OUT_CONCURRENCY = 8` deliveries at a time under an **8 s** whole-fan-out deadline (per-target HTTPS-inbox timeout 10 s; the whole-fan-out deadline deliberately sits below the household's 10 s publish client timeout, so the GFS — not the client — decides when a slow fan-out ends); on expiry the stragglers are cancelled and `delivered_to` reports only what was reached. Without the deadline a fan-out to N blackholing subscribers would pin the request handler for `ceil(N / 8) × 10 s`, which an anonymous caller could multiply by the per-IP publish rate. **Every** authorization failure returns the SAME 403 body `{"error": "not authorized to relay for this space"}` — distinct messages would let an unauthenticated caller enumerate space existence, moderation status and pin status; the precise reason is logged at DEBUG. That covers: a missing / malformed / non-verifying legacy signature, a legacy `from_instance` that is unregistered or banned, an unpublished space, a moderator-`banned` space, an `event_type` outside the allow-set, a space with no pinned authority key, a missing / invalid authority signature, or an unknown suite (no fallback). A `withdrawn` space still relays — withdrawal delists from the directory only. Rate-limited 120/min per IP → 429 with `Retry-After: 60`. **Household side:** a transient failure (transport error, timeout, 408, 429, 5xx) is retried with backoff, honouring `Retry-After`, re-POSTing the byte-identical identity-free body; any other 4xx is not retried. A current household never sends the legacy fields: a server that does not prove `anonymous_publish` in its signed `/gfs/info` block gets no publish at all (one WARNING per connection), and one whose `/gfs/info` is unreachable gets it from the retry queue once it does. The replay dedupe above makes a retry whose first attempt already landed a no-op. The publish and its retries ride a separate cookie-less HTTP session, so no cookie from the household's authenticated GFS calls accompanies them. |
| POST | `/gfs/envelope` | **Opaque household-to-household relay** (§D2b invite bootstrap). Body `{to_instance, sealed:{kem_suite, eph_pk, ciphertext}}` — the exact outer envelope `federation/invite_bootstrap.py` produces. **Unauthenticated on purpose:** the sender is deliberately anonymous, so two households introduced by an invite link never learn each other's address and this server never learns their relationship. The `sealed` dict is **opaque** — the GFS validates only that it carries exactly those three non-empty string keys (an extra key is a 400, so no routing hint or sender id can be smuggled through) and never looks at the values; the suite tag is the recipient's business, so a household can move to a Phase-2 hybrid suite without the connection server being redeployed. `to_instance` must be a **well-formed instance id** — exactly 32 lowercase base32 characters (`[a-z2-7]{32}`, what `derive_instance_id` produces), anchored. It was previously any string up to 128 chars, which let an anonymous caller smuggle a newline into the one field this server writes into its own log lines and author a fabricated second record. The body is capped at **320 KiB** (declared `Content-Length` or actual bytes read) → 413 — sized from the largest legitimate envelope, the bootstrap ACK carrying `space_meta` (base64 cover + icon WebP + roster) under the household's own 256 KiB sealed-blob cap, plus framing and headroom. **The response is a uniform `202 {"status":"accepted"}` for every well-formed request** — recipient online, recipient offline, recipient not registered here at all, byte-identical in each case. Anything else would be a presence/existence oracle for anyone willing to walk instance ids. An envelope for an unknown, pending or banned recipient is dropped server-side, logged at DEBUG, and stored nowhere. **Delivery:** a live `/gfs/ws` socket gets `{type:"envelope", sealed}` and nothing else; otherwise the blob is queued in `gfs_envelope_queue` for **24 h** (max **2000** rows and **64 MiB** per recipient; at either ceiling the NEW envelope is **tail-dropped** — never evict-oldest, which on an anonymous endpoint would hand a stranger a delete primitive over a sleeping household's queued mail — logged at WARNING with the recipient and depth, and still answered with the same uniform `202`) and drained in order on that household's next authenticated hello, each row deleted only after its frame went out. The GFS never parses or logs the sealed content and stores no sender attribute — there is none on the wire. **What the relay therefore learns** is exactly what the wire carries: `to_instance`, the time and the byte size — no sender, no space, no event type, no token. That is a statement about the request *body*, not about the socket: the sending household's IP is still in the server's HTTP access log, a residual stated in full in [principles.md](./principles.md) ("Sign-off: the connection server learns the recipients of a link-joined pair"). Malformed → 400; rate-limited 600/min per IP → 429 — a 429 here is a cooldown the requesting household backs off and retries against, not a terminal failure. |
| POST | `/gfs/member-publish` | **Trusted-mode member publish** (v_49): a space member household publishes its OWN item over the connection server with no host signature. Body (exactly these keys — any other is a 400) `{instance_id, gfs_instance_id, ts, signature, target, event_type, epoch, writer_cert, payload}`: `gfs_instance_id` is this server's id as the household pinned it from `/gfs/info` (any other value → 403, so a signed request cannot be replayed to another server), `target` is the space id, `event_type` is always the generic `space_item` (anything else → 400), `payload` is a non-empty ciphertext **string** (≤ 200 KiB chars) under the space's epoch content key that carries the **real** item type and the author-signed inner, and `writer_cert` is the household's plaintext space-authority-signed writer cert. `signature` is the household's Ed25519 signature over canonical JSON of every other field plus `"action": "gfs-member-publish:v1"` (the same scheme as `/gfs/subscribe`; the `action` value is the domain separator). **The request is identified on purpose** — the owner-decided default (see [principles.md](./principles.md)): the server learns which household published into which space, at which epoch, and when; never the content, never the real type. **Authorization**, every refusal the same `403 {"error": "not authorized to publish to this space"}` (reason at DEBUG): the signature verifies against the registered `client_instances.public_key` of `instance_id`, `ts` within ±300 s, instance `active`, `gfs_instance_id` is this server's; the space is published, not moderator-`banned`, publicly readable (`allow_subscribers`) and has a pinned authority key; `verify_writer_cert` against that pinned key for `target` and `epoch`, with the author key = the publishing household's **registered** key (so a household can only publish with its own cert) and scope `comment` — the weakest, because the type is hidden; **receivers** decrypt and enforce the scope the real type needs. **Epoch freshness:** once the owner has confirmed an epoch (see `/gfs/spaces/{id}/epoch`), the cert's epoch must lie between the confirmed epoch and the current one + 1, or back to the previously confirmed epoch for 600 s after the owner confirmed the newer one; before any confirmation the receivers' own check is the only one. Writer certs never raise the stored epoch. The state is stored per space and cleared on an authority re-pin. Past the per-(household, space) limit of 30/min, or the per-space limit of 120/min across all households → 429 with `Retry-After: 60` (checked only after the household signature verified). A byte-identical request (signature included) replayed within 5 min is a 200 no-op. **Fan-out** runs in the background after the 200 (2 workers with each space pinned to one, so a space's items go out in publish order; 128 items per worker and at most 16 per space; a full share or a stopping server → `503` with `Retry-After: 5`; shutdown drains accepted items for up to 5 s), to every active subscriber of the space except the publisher, at most 8 live pushes in flight: a live `/gfs/ws` socket gets `{type:"relay", space_id, event_type:"space_item", epoch, writer_cert, payload}` — no `from_instance`, no publisher id — otherwise the frame is queued in `gfs_envelope_queue` (`frame_type='relay'`) for 24 h, but only for a subscriber that held a `/gfs/ws` session for at least 60 s within those 24 h (`client_instances.relay_seen_at`; a bare hello earns nothing), under a per-recipient cap (250 rows / 4 MiB — the recipient's own oldest item makes room) and a server-wide cap of 256 MiB of relay rows (the largest holder's oldest item makes room — fair share, in the same transaction as the insert), and drained on the next hello. One registered household may hold at most 500 subscriptions (`POST /gfs/subscribe` → 403 past it). Body cap 256 KiB → 413. Response `200 {"status": "published"}`. |
| POST | `/gfs/member-publish-anon` | **Strict-mode (anonymous) member publish** (v_50): a member household of a space whose owner set `gfs_publish_mode = "strict"` publishes its OWN item **without naming itself**. Body (exactly these keys — any other, in particular `instance_id`, `signature` or `writer_cert`, is a 400) `{gfs_instance_id, ts, nonce, target, event_type, epoch, payload, writer_sig, writer_sig_suite}`: `ts` tz-aware ISO 8601 within ±300 s (households send whole seconds plus up to ±60 s of random jitter, so no clock offset fingerprints them), `nonce` 16–64 chars of per-request randomness, `event_type` always `space_item`, `payload` the content-key ciphertext (the real type, the author-signed inner AND the writer cert ride inside it), `writer_sig` the Ed25519 signature under the space's **writer group key** for `epoch` over `b"gfs-member-publish-anon:v1:"` + canonical JSON of every other field, `writer_sig_suite` `ed25519` (unknown → 403, never defaulted). **Authorization**, every refusal the same `403 {"error": "not authorized to publish to this space"}`: addressed to this server, `ts` fresh, the space published / not banned / publicly readable / pinned, a writer key pinned for `epoch` (by the owner's notice, or a delegated admin's within the +1 rule — see `/gfs/spaces/{id}/epoch`), `writer_sig` verifies under it, the epoch is open (the same tiers and 600 s grace as `/gfs/member-publish`), and the body was not seen in the last 600 s (a replay is refused). Past 30/min per (space, client address), 120/min per space or 120/min per writer key → 429 (after the signature). The per-address limit is the only thing that keeps one key holder — any publisher, a comment-scope follower included — from starving the rest: abuse inside a strict space is unattributable, and rotating the key does not stop it (the abuser gets the new one); the owner's remedy is switching back to trusted mode or removing households. Fan-out as for `/gfs/member-publish`, to every subscriber (the publisher is unknown, so it gets its own echo and drops it), frame `{type:"relay", space_id, event_type:"space_item", epoch, payload}` — no cert. Accepted in a `trusted` space too wherever a writer key is pinned (it reveals less than the identified path). The household sends it over its cookie-less publish session, never the identified one. Response `200 {"status": "published"}`. |
| POST | `/gfs/spaces/{id}/epoch` | **Content-epoch notice** (v_49), in two forms. **Owner:** `{owning_instance, gfs_instance_id, epoch, ts, signature}` — the space owner's household signature over canonical JSON of `{action: "gfs-owner-epoch-notice:v1", owning_instance, gfs_instance_id, space_id, epoch, ts}`, verified against the owner's registered key (±300 s on `ts`; `owning_instance` must be the space's owner and `gfs_instance_id` this server); it **confirms** the epoch, any raise up to `max(confirmed + 1000, now + 1 day)` (the v_44 post-restore jump to unix seconds lands), and the previously confirmed epoch stays open for 600 s. **Seed-only:** `{epoch, authority_sig, authority_sig_suite}` — a space-authority signature over `{space_id, epoch}` under event type `space_epoch_notice` (not a relay type — never fanned out, and a relay payload's signature is never accepted as a notice), verified against the pinned `identity_public_key`; anonymous like `/gfs/publish`. It raises only the *current* epoch, by exactly +1, at most once a minute, never the owner-confirmed floor, and only after the owner confirmed one — anything else is a 200 that changes nothing (the same rule applies to the `epoch` of an authorized `space_post_public` relay). That split is what keeps any seed holder — a demoted one too, until the re-pin — from locking writers out. **v_50 fields** (both optional; a household adds them only for a server proving `member_publish_strict`): the owner form may carry **`publish_mode`** (`trusted` / `strict`, signed inside the owner's household signature — it moves only forward in `ts`; in a `strict` space `/gfs/member-publish` is refused) and **`writer_key_cert`** (`{writer_key_suite, space_id, epoch, writer_pk, cert_sig}`, authority-signed, also inside the owner signature) which pins the writer group key of the confirmed epoch, replacing any delegated pin of it; the seed-only form may carry `writer_key_cert` (authority-signed on its own), pinned only for an epoch with `confirmed <= epoch <= current` after the +1 step, above the newest pin and never over an existing one. The current and the previous pin are kept; both are cleared on an authority re-pin (the mode is not). A `writer_key_cert` that does not verify against the pinned space key for the notice's epoch → 403, nothing written. Unknown / banned / unpinned space, a bad / stale / non-owner / wrong-server signature, an unknown suite, an over-ceiling owner epoch, or a non-integer / negative `epoch` → `403 {"error": "not authorized for this space"}`; a missing field → 400. |
| POST | `/gfs/spaces/{id}/publish` | Publish space metadata. Body carries `{owning_instance, name, …, category, join_mode?, identity_public_key, ts?, authority_cert?, signature}`; the Ed25519 `signature` is **mandatory** and verified against the owning instance's registered public key — empty / malformed / invalid → 403. `identity_public_key` (hex, the space's Ed25519 authority verify key) is **TOFU-pinned on first publish**; a later publish offering a different pubkey keeps the pinned one **unless** it carries `authority_cert` (v_44): the owner's cert for a rotated key, inside the signed canonical body, which then **requires** a fresh signed `ts` (missing → 403). The pin moves only when the cert verifies against the owning instance's registered key (which must derive to `owning_instance`), names exactly the offered `identity_public_key`, and has a higher `key_epoch` than the stored cert; otherwise the pin is kept and a warning logged. A non-object `authority_cert` → 400. The pinned key is what authorizes a later space-authority-signed relay via `/gfs/publish`. `ts` is **optional for backward compatibility**: when present it is inside the signed canonical body and replay-guarded (±300 s, tz-aware — naive / stale → 403), and only such a *fresh* publish clears the `withdrawn` flag set by `/unpublish`. A publish without `ts` (an older household) still registers and refreshes metadata but **cannot restore a withdrawn listing** — its body is replayable forever, so honouring it would let a captured publish re-list a space its owner delisted. `join_mode` (`invite_only` / `open` / `request` — the **membership** gate shown on the listing) and `allow_subscribers` (`true`/`false` — the **readability** opt-in) are likewise **optional for backward compatibility**, each folded into the signed canonical body only when present (an older household signs a body without the key). A missing or unknown `join_mode` stores the fail-closed `invite_only`; a missing `allow_subscribers` stores the fail-closed `false`, meaning the space is listed for discovery but is **not publicly readable** — `POST /gfs/subscribe` refuses it. Existing subscriber seats are purged **only on an explicit `allow_subscribers: false`** (logged at INFO with the count): an absent key is an older household that does not know the field, not an owner withdrawing readability, so it gates without evicting — otherwise one un-upgraded household re-publishing would mass-evict every reader on a mixed-version server. The truth arrives by itself on the owner's next GFS-WS connect, which re-publishes every space's metadata; readers whose seat was purged re-register on their own next GFS-WS connect. **Size:** `cover_url` / `icon_url` carry the space's cover and icon as self-contained `data:image/webp;base64,…` URIs, each bounded by the shared `SPACE_IMAGE_EMBED_MAX_BYTES` (1.5 MiB raw, ≈2 MiB base64): the household leaves an image over it out of the body (WARNING in its log), and the GFS stores an over-bound URI as `""` (WARNING; the signature is verified over the full value first) — the space still lists, just without that art. The body is read under a per-route cap, `SPACE_PUBLISH_MAX_BODY_BYTES` (two maximal images + 1 MiB ≈ 5 MiB, declared `Content-Length` or actual bytes read) → 413; every other GFS route keeps aiohttp's 1 MiB default. A non-object JSON body → 400. |
| POST / DELETE | `/gfs/spaces/{id}/unpublish` | **Owner withdrawal** of a published listing. Body `{owning_instance, ts, signature}`; the Ed25519 `signature` (over canonical JSON of `{action:"unpublish", owning_instance, space_id, ts}`) is **mandatory**, verified against the registered public key, and replay-guarded (±300 s on `ts`). `action` rides inside the signed bytes, so a captured subscribe signature can't be replayed as a delisting. Authentication is not enough: the caller must also **be** the space's `owning_instance`. A missing `owning_instance`/`ts`/`signature` → 400; unknown instance / bad signature / stale `ts` / non-owner → 403. Success sets the reversible `withdrawn` flag (the row, its subscribers and the pinned authority key survive, and `status` is untouched — `banned` stays the GFS moderator's sticky verdict), so the owner's next `publish` restores the listing. Withdrawal hides the space from discovery only; the relay and existing subscribers keep working. Unknown space → 200 no-op (idempotent fan-out), but only after the signature verifies. |
| POST | `/gfs/spaces/{id}/invite` | **Mint an invite link** (§24.8.5). Body `{owning_instance, blob, expires_at, ts, signature}`. The Ed25519 `signature` is over canonical JSON of `{action:"mint_invite", owning_instance, space_id, ts}` — `action` inside the signed bytes, so no other signed request this household made (publish, unpublish, subscribe, revoke) can be replayed as a mint — verified against the registered public key and replay-guarded (±300 s on `ts`). Authentication is not authorisation: the caller must also **be** the space's `owning_instance`, and the space must be `status='active'` and not `withdrawn`. `blob` is **opaque** — the connection server validates its SIZE (≤ 4 KiB) and base64url ALPHABET and never parses it; it is the fragment of the `socialhome://invite#<blob>` code the receiving SPA decodes, and it MUST NOT contain a household address (the page is served to anyone with the link). `expires_at` is a **required** absolute unix-seconds expiry, in the future and at most **30 days** out. Returns `201 {gfs_token, url}` where `url` is `{base_url}/join/{gfs_token}`. Missing field / bad blob / bad expiry → 400; unknown instance, bad or stale signature, non-owner, space not listed → 403 (verification happens first, so a 400-vs-403 never tells an unsigned caller whether a space exists here); over the mint rate → 429. |
| DELETE / POST | `/gfs/spaces/{id}/invite/{gfs_token}` | **Revoke one invite link.** Body `{owning_instance, ts, signature}`; the signature covers canonical JSON of `{action:"revoke_invite", gfs_token, owning_instance, space_id, ts}` — both the action AND the token are inside the signed bytes, so a mint signature can't be replayed as a revoke and a revoke can't be redirected at another token. Owner check as above. `204` on success and **idempotent**: an unknown or already-revoked token still answers `204`, but only after the signature verifies and the caller is confirmed as the owner, so the endpoint is never an existence oracle for invite tokens. `POST` is accepted alongside `DELETE` because some proxies strip a DELETE body. Withdrawing the space (`/unpublish`) revokes **every** invite for it — a delisted space must not keep a working public side door. |
| POST | `/gfs/subscribe` | Subscribe (default) or unsubscribe (`action:"unsubscribe"`) to a published space's relay fan-out. Body `{instance_id, space_id, ts, signature, action?}`; **both** actions are Ed25519-signed — the `signature` (over canonical JSON of `{action, instance_id, space_id, ts}`) is **mandatory**, verified against the registered public key, replay-guarded (±300 s on `ts`), and binds the request to `instance_id` so a caller can only (un)subscribe itself. `action` rides inside the signed bytes (domain separation), so a subscribe signature can't be replayed as an unsubscribe, or vice versa. A missing `instance_id`/`space_id`/`ts`/`signature`, or an `action` outside `{subscribe, unsubscribe}` → 400; bad signature / stale `ts` / unknown instance → 403. Subscribe additionally requires the space to already be published (no auto-create) — unknown space → 403 — **and to be publicly readable**: a space whose stored `allow_subscribers` is `false` → 403 `space is not publicly readable` (it is listed for discovery but relays no content and hands out no content key, so the seat would linger forever receiving nothing). This is **not** keyed on `join_mode` — an `invite_only` space that allows subscribers seats them normally. Unsubscribe is idempotent and needs no existing space. |
| POST | `/gfs/report` | File a fraud / abuse report. |
| POST | `/gfs/appeal` | Appeal a ban. |
| GET | `/gfs/spaces` | Public directory listing — `{spaces: [...]}`, each entry the full stored row including `join_mode` (the membership gate), `allow_subscribers` (`false` means listed but **not publicly readable**: no subscription, no content relay, no content key) `authority_rotation_seq` (v_44 — this GFS's own count of accepted, owner-cert-verified re-pins of `identity_public_key`, `0` before any) and `member_publish_mode` (v_50 — `trusted` / `strict`, as the owner's epoch notice set it; a household reads it here, over its cookie-less session, and never sends an identified member publish into a space listed as `strict`; absent on an older server, read as `trusted`). The owner's `authority_cert` and its wall-clock epoch are **never** served publicly — they name the owner household's identity key and date the revocation. |
| GET | `/gfs/spaces/{id}` | Single published space's metadata — the subscriber-side mirror source. Returns the stored listing (`{space_id, owning_instance, name, description, about_markdown, category, join_mode, allow_subscribers, min_age, status, identity_public_key, authority_rotation_seq, …}`). A follower household heals its pin from `identity_public_key` + a strictly higher `authority_rotation_seq`, from the GFS that seated its mirror only (v_44); the GFS re-pinned only after verifying the owner's cert, which it keeps private. Unknown, non-`active`, or owner-`withdrawn` space → 404. Unauthenticated, like the directory. |
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

### GFS — Opaque channels for private spaces (v_51)

A private space with members seated through an invite link publishes through
an **opaque channel**: a random 128-bit `channel_id` and a channel key the
household derives from the space seed (unlinkable to the space key). The
server never sees a space id, a space name, a space key or an owner — every
identifier rides in the **body** (never the URL path, so no access log holds
a channel id). Every refusal is the same `403 {"error": "not authorized for
this channel"}`; a malformed body (exact key sets — e.g. a `space_id` or, on
`publish-anon`, an `instance_id` / `signature` / `channel_cert`) is a 400.
Wire shapes: `socialhome/domain/gfs_channel.py`; rationale:
[`protocol/discovery.md`](./protocol/discovery.md#private-spaces-opaque-channels-v_51).

| Method | Path | Description |
|---|---|---|
| POST | `/gfs/channels/register` | **Anonymous, channel-key-signed.** `{channel_suite, channel_id, channel_pk, gfs_instance_id, ts, nonce, channel_sig}`; `channel_sig` under the key being registered (`b"gfs-channel-register:v1:"` + canonical JSON) — proof of possession. A new id pins the key (`201 {"status":"registered"}`), the same key refreshes (`200 "refreshed"`), another key is `409 {"error":"channel_pinned"}` — there is no re-pin (a `repin_cert` field is a 400; a household starts a fresh channel instead). `ts` ±300 s, addressed to this server; 10 / min / client address (429); past the server-wide cap of live channel rows a NEW registration is `503`. A channel that never got a notice or a seat is swept after 24 h, a used one after 30 idle days. `nonce` only makes each signature unique (not cached: replays inside the `ts` window are harmless). |
| POST | `/gfs/channels/epoch` | **Anonymous, channel-key-signed** epoch notice `{channel_suite, channel_id, gfs_instance_id, ts, nonce, epoch, publish_mode, writer_key_cert?, channel_sig}` (`gfs-channel-epoch:v1:`). One tier: epochs are channel epochs (content epoch + a secret per-channel offset). The first notice after registration sets any epoch up to 2^62; later the epoch rises by at most one per 60 s since the last raise — ahead of that allowance is `429` + `Retry-After` (the household's retry queue lands it); an epoch at or below the current one changes nothing (200). The answer is `200 {"status":"ok", "epoch", "writer_pk"}` — what the server holds after the notice (only a channel-key holder gets that far), so the owner sees another key holder moved the channel past it and starts a fresh one. `publish_mode` (`trusted` / `strict`) moves only with a notice that raises the epoch (a mode switch rotates), then forward in `ts`. `writer_key_cert` (`{writer_key_suite, channel_suite, channel_id, epoch, writer_pk, cert_sig}`, channel-key-signed under `gfs-channel-writer-key-cert:v1:`) pins the channel writer key of `epoch` when it is the current epoch after the notice; the first pin of an epoch wins. |
| POST | `/gfs/channels/unregister` | **Anonymous, channel-key-signed** `{channel_suite, channel_id, gfs_instance_id, ts, nonce, channel_sig}` (`gfs-channel-unregister:v1:`) — drops the channel and every seat. Idempotent (an unknown channel is a 200). |
| POST | `/gfs/channels/subscribe` | **Household-signed** (identified, like a follower's subscribe) `{instance_id, gfs_instance_id, channel_id, ts, signature, channel_pass}` — signature over canonical JSON with `action: "gfs-channel-subscribe:v1"`, verified against the registered key (±300 s). `channel_pass` (`{channel_suite, channel_id, epoch, instance_pk, issued_at, pass_sig}`, channel-key-signed under `gfs-channel-pass:v1:`, scope-free) must name THIS household's registered key at an open epoch (current, current + 1, or the previous one for 600 s). Only link-joined (`space_session`) member households are issued a pass, so only they take seats; a paired member's grant is publish-only. The seat remembers the pass epoch; a seat whose epoch closed receives nothing. At most 500 channel seats per household. |
| POST | `/gfs/channels/unsubscribe` | Household-signed `{instance_id, gfs_instance_id, channel_id, ts, signature}` (`action: "gfs-channel-unsubscribe:v1"`) — drops our own seat. |
| POST | `/gfs/channels/publish` | **Trusted** member publish: household-signed `{instance_id, gfs_instance_id, channel_id, ts, signature, event_type:"space_item", epoch, channel_cert, payload}` (`action: "gfs-channel-publish:v1"`). Refused in a `strict` channel. `channel_cert` (`{channel_suite, channel_id, epoch, instance_pk, scope, issued_at, cert_sig}`, `gfs-channel-cert:v1:`) must verify under the pinned channel key for this channel, `epoch` and the publisher's registered key, scope `comment` at least; the epoch open. 30 / min per (household, channel), 120 / min per channel. Fan-out to every open seat except the publisher: `{type:"relay", channel_id, event_type:"space_item", epoch, payload}` — no cert, no space; offline seats queued 24 h as for member publish. A byte-identical replay is a 200 no-op. `503` while the fan-out is full. |
| POST | `/gfs/channels/publish-anon` | **Strict** member publish, no identity: `{gfs_instance_id, channel_id, ts, nonce, event_type, epoch, payload, writer_sig, writer_sig_suite}`, `writer_sig` by the channel writer key pinned for `epoch` over `b"gfs-channel-publish-anon:v1:"` + canonical JSON of the rest. 30 / min per (channel, client address), 120 / min per channel and per writer key; an exact replay is a 403. Fan-out to every open seat (the publisher drops its own echo). |

## GFS — Push WebSocket (GFS → SH)

| Method | Path | Purpose |
|---|---|---|
| GET (Upgrade) | `/gfs/ws` | Persistent push channel. SH opens this once paired; the GFS pushes `{type:"relay", space_id, event_type, payload}` frames as space events fan out — the frame is identity-free (the GFS never learns which household relayed the event, so it forwards no `from_instance`). It also pushes `{type:"envelope", sealed:{kem_suite, eph_pk, ciphertext}}` — one sealed household-to-household blob relayed via `POST /gfs/envelope` (§D2b invite bootstrap). That frame carries those two fields and nothing else: the GFS cannot open the box and does not know who sealed it. Envelopes that arrived while the household was offline are drained, oldest first, immediately after the hello verifies — together with any queued member-published items, which drain as the same `{type:"relay", space_id, event_type:"space_item", epoch, writer_cert, payload}` frame a live subscriber gets from `POST /gfs/member-publish` (v_49; identity-free — the publisher is authenticated by the cert and the author signature inside, never by an outer field). It also pushes public-content RTC signalling to the author: `{type:"highlight_signal", kind}` with `kind` ∈ `offer` / `ice` / `relay_offer` (the `relay_offer` carries `{relay_id, highlight_id, token}` for the GFS-relay fallback), and `{type:"moment_signal", kind}` with `kind` ∈ `offer` / `ice` / `relay_offer` (the `offer` carries `user_id` + `gfs_id`). On an operator rename it pushes `{type:"server_info_updated", server_name}` to every connected client, which re-fetches `GET /gfs/info` to update the cached server name in real time (the reconnect-refresh path is the fallback). First client frame must be a signed hello `{type:"hello", instance_id, ts, sig}` within 5 s — see spec §24.12. WebSocket close codes: 4400 protocol violation, 4401 auth failure, 4408 hello timeout, 4409 replaced. Heartbeat is the WS-protocol-level ping (30 s). |

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
| POST | `/cluster/signaling-session` | **Legacy.** Pick a least-loaded signaling node for a sync session (spec §24.10.7). Kept for older households; current households never call it (a sync tells the GFS nothing — see `protocol/sync.md`). |
| POST | `/cluster/signaling-session/release` | **Legacy.** Release a signaling session on `SPACE_SYNC_DIRECT_READY` / `DIRECT_FAILED`. Older households only. |

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
| GET | `/join/{gfs_token}` | **Invite-link landing.** Renders the space's already-public directory metadata (name, icon, accent) next to the `socialhome://invite#<blob>` code — as copyable text **and** as a QR of the same string — which the visitor pastes into their OWN Social Home (Spaces → Join with invite code). There is deliberately no clickable deep link: the redeem has to happen on the visitor's household, and a link can only ever open the issuer's. **The page writes nothing to the database** — no use counter, no fetch row, and the handler adds no log line of its own; the connection server must never become a durable record of who redeemed what (the `uses` / `max_uses` columns on `gfs_invite_tokens` are dead by design). The residual is the HTTP access log, which records `GET /join/<token>` with the visitor's IP — signed off in [`principles.md`](principles.md); operators who care should redact the `/join/` path at the front end and keep retention short. 404 — with the same styled "expired or revoked" page — when the token is unknown, expired, revoked, or its space is no longer publicly listed; one answer for all of them, so the page is not an existence oracle. |
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
| `POST /api/calls/{id}/ice` | 300 / min / user — every leg of a group-call mesh trickles several candidates within seconds |
| `POST /api/calls/{id}/answer` | 60 / min / user — a group callee answers the ring plus one leg per other callee |
| `POST /api/calls/{id}/join` | 30 / min / user |
| `POST /api/calls/{id}/quality` | 30 / min / user — the call page samples every 10 s; on the broad `/api/calls` bucket it starved `join` |
| `GET /api/calls/ice-servers` | 30 / min / user |
| `POST /api/link-preview` | 30 / min / user — on top, the service caps **fresh page fetches** (cache misses, including the ones a post create causes) at 20 / 5 min per member and 60 / 5 min per household; over budget the answer is simply "no card". |
| `GET /api/map/tiles` | 1200 / min — one shared bucket: every Leaflet `<img>` authenticates as the signed-URL principal, and a desktop viewport is ~20 tiles. Still a ceiling, so a leaked signed URL can't drive unbounded upstream traffic from the household IP. |
| `POST /cluster/signaling-session{,/release}` (legacy) | 60 / min / paired instance |
| `GET /` + `GET /spaces/{id}` + `GET /join/{token}` (GFS public pages) | 30 / min / IP — `/join/` rides the same window: it is the page an attacker would hammer to walk the token space, and since it writes nothing there is no household identity to key a limiter on. |
| `GET /api/invite-links/{token}/code` | 30 / min / IP — the endpoint is unauthenticated (the token IS the credential), so the client address is the only handle. Not brute-force protection: a token is a uuid4 hex and is not guessable. It is ordinary anonymous-endpoint shedding, and one visitor legitimately hits it once per link they open. Live and dead tokens answer identically apart from the status code. |
| `POST /gfs/spaces/{id}/invite` | 20 / min / **instance** — the mint is signature-authenticated, so the accountable identity is known before anything is written and the limiter keys on the household rather than on an address it could rotate. Far above a human minting invites, while bounding how many rows one household can park on a server per minute. |
| `POST /gfs/{highlight,moment}_rtc/offer` + the anon `/relay/*` GETs | 20 / min / IP — bounds the WS-push amplification from the anonymous RTC entry points |
| `POST /gfs/register` | 10 / min / IP — a household registers once per pairing; sheds token-guessing floods before the token lookup. |
| `POST /gfs/signup-token` | 5 / min / IP **and** 30 / min across all addresses (the global window is the sybil brake that does not depend on believing a client address), shed before any database work; on top, the token service hands one token per address per 30 s. |
| `POST /gfs/publish` | 120 / min / IP — the relay is authorized by the space-authority signature alone and carries no household identity, so the per-IP window is the only shedding handle, set far above a real household's publish rate (one per public post / subscriber key handoff, per GFS). Bodies over 256 KiB are refused with 413 before being read; a byte-identical payload replayed inside 5 min is a 200 no-op that fans out nothing, and one relay's fan-out is capped at 8 s (below the household's 10 s client timeout). |
| `POST /gfs/channels/*` (v_51) | 120 / min / IP before any signature work (shared window); registration 10 / min per client address; trusted publish 30 / min per (household, channel) and 120 / min per channel; anonymous publish 30 / min per (channel, client address), 120 / min per channel and per writer key; an epoch notice ahead of its time allowance is a 429 with `Retry-After`. Control bodies over 16 KiB, publishes over 256 KiB → 413. |
| `POST /gfs/member-publish`, `POST /gfs/member-publish-anon`, `POST /gfs/spaces/{id}/epoch` | 120 / min / IP before any signature work (shared window); on `/gfs/member-publish-anon` 30 / min per (space, client address), 120 / min per space and 120 / min per writer key once `writer_sig` verified, and an exact replay inside 10 min is a 403; plus 30 / min per (household, space) and 120 / min per space on `/gfs/member-publish` once the household signature has verified (429 with `Retry-After: 60`). Bodies over 256 KiB are refused with 413 before being read; a byte-identical member publish replayed inside 5 min is a 200 no-op. |
| `POST /gfs/envelope` | 600 / min / IP — the relay is anonymous by design, so as with `/gfs/publish` the client address is the only shedding handle. It was 30/min, which was wrong by an order of magnitude: this relay is not a sideband for the redeem handshake, it is the **only** transport a link-joined household has, so *every* federation envelope to that peer rides it — each space post, reaction, calendar event and sync chunk — and a single space catch-up backfill is hundreds of chunks. 600/min still caps what one household can push through a relay it does not own, and the durable cost stays bounded per household by the 2000-row / 64 MiB queue cap (tail-drop at either ceiling). Bodies over 320 KiB are refused with 413 before being read. A 429 is a **cooldown**, not a verdict: the sending household backs off and retries rather than failing the envelope. |
| Federation inbound (per signing instance) | Rolling window; see §24.11. |
| `POST /federation/inbox/{inbox_id}` (per remote IP) | 1000 / min — defends against unauthenticated floods that the per-user limiter would otherwise miss. The 429 carries `Retry-After` (whole seconds until the sliding window frees a slot); a sending household's outbox waits at least that long and retries — a 429 is never a drop. |

Rate-limit responses return HTTP 429 with a `Retry-After` header.

**Which IP a GFS limiter counts** — and the same answer for the admin-login
throttle, the `admin_ip` written to the admin audit log, and every route view's
`client_ip()`, which all resolve through the one shared resolver.
`X-Forwarded-For` is client-supplied, so the
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
