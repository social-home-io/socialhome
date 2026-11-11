# Feeds — Posts, Comments, Reactions

Once a space is live and synced, every new post, comment, and reaction
is broadcast to every member instance. These are the highest-volume
federation events.

## Scope

- **HFS**: origin broadcasts; every peer member instance applies.
- **GFS**: only involved when a peer is offline and opted into push
  fan-out — see [push-relay](./push-relay.md). Never for spaces marked
  private.

## Event types

**Household feed (same HFS only; not federated but included here for
symmetry with UI)** — not a federation event type.

**Space feed**

`SPACE_POST_CREATED`, `SPACE_POST_UPDATED`, `SPACE_POST_DELETED`,
`SPACE_COMMENT_CREATED`, `SPACE_COMMENT_UPDATED`,
`SPACE_COMMENT_DELETED`, `SPACE_POST_REACTION` (v_93 — see
[Reactions on space posts](#reactions-on-space-posts-v_93)).

**Space-wide poll + schedule-poll (carried inside posts)**

`SPACE_POLL_CREATED`, `SPACE_POLL_VOTE_CAST`, `SPACE_POLL_CLOSED`.

## Reactions on space posts (v_93)

A reaction on a space post is shared by every member household of the
space, whichever way that household is reached (paired, mesh, or a GFS
mailbox seat). Before v_93 it stayed in the household that made it.

`SPACE_POST_REACTION` is a delta, sent by the reactor's household on the
add and on the remove. Everything but the routing fields is inside the
encrypted payload:

| Field | Meaning |
|---|---|
| `space_id` | the space (also the envelope's routing field) |
| `post_id` | the post |
| `user_id` | the reactor — a person of the sending household |
| `emoji` | NFC, trimmed, 1–32 characters. On an `add` also the status-emoji sanity check (`looks_like_emoji`: no whitespace or control character, at least one non-ASCII code point — an emoji, not free text; not a full emoji parser). A `remove` is held to the length rule only, so a free-text reaction from before v_93 can still be taken back |
| `action` | `"add"` or `"remove"` |
| `at` | the stamp the reactor's household stored with the change, tz-aware ISO 8601: its clock, or one microsecond after the stamp it replaces when the clock stepped back — a change is never older than the one before it |

```mermaid
sequenceDiagram
    autonumber
    participant U as Anna<br/>(HFS A browser)
    participant A as HFS A
    participant B as HFS B<br/>(paired)
    participant G as GFS
    participant E as HFS E<br/>(mailbox seat)
    U->>A: POST /api/spaces/{id}/posts/{pid}/reactions
    A->>A: store reaction + stamp,<br/>publish PostReactionChanged
    par broadcast_to_space_members (v_93 households only)
        A->>B: SPACE_POST_REACTION {add, at}
        A->>G: POST /gfs/envelope/multi<br/>(one sealed body, a wrap per mailbox)
        G->>E: envelope
    end
    B->>B: seat check, then apply if `at`<br/>is newer than the held stamp
    E->>E: same
    U->>A: DELETE …/reactions/{emoji}
    par
        A->>B: SPACE_POST_REACTION {remove, at'}
        A->>G: POST /gfs/envelope/multi
        G->>E: envelope
    end
    Note over B,E: a copy of the add that arrives now<br/>(queue, second path) is older than at' — dropped
```

**Receiver rules** (`FederationInboundService._on_space_post_reaction`),
after the §24.11 pipeline:

- **Who.** `user_id` must hold a live seat **on the sending household**:
  a writer seat (`member` / `moderator` / `admin`), or a `subscriber` seat
  while the space has `allow_subscriber_react` on — the same toggle a local
  follower is held to (`SpaceAuthorship.item_seat_admits`). Never a local
  user of the receiver, never a banned user, and the host relays nobody's
  reaction. A follower-only household passes the §24.11 writer gate only
  under the same opt-in, with the reactor bound to its live follower seat
  ([spaces.md](./spaces.md#two-kinds-of-follower-v_30)). A user whose seat
  has not reached the receiver yet is held and replayed with the seat.
- **Order-independent, idempotent.** The receiver applies the event only
  when `at` is newer than the stamp it holds for that `(post, user,
  emoji)` — `space_posts.reaction_stamps_json`, written in the same
  transaction as the reaction (migration `0075`, the v_49 member-relay
  stamps; no new storage). The newest stamp wins in any arrival order, a
  re-delivery is a no-op, and a late add never brings back a removed
  reaction (a removal's stamp is kept 30 days). `at` more than 300 s ahead
  of the receiver's clock is refused.
- **Five per person per post** (`MAX_REACTIONS_PER_USER_PER_POST`). The
  reactor's household refuses a sixth (`422`). A receiver that is sent
  more shows that person's five newest by stamp — a pure function of the
  stamps, so every household shows the same five in any arrival order —
  and one past the cap shows again when a newer one is taken back. There
  is no post-wide cap on distinct emoji any more: it let one member fill
  every slot and left each household with whichever arrived first.
- **Bounded.** A sender keeps only the latest queued toggle per `(space,
  post, user, emoji)` for an unreachable household (the outbox's
  latest-only key, like presence), so toggling never pushes posts out of a
  peer's queue. A receiver always STORES an applied reaction; it announces
  (the bus event behind notifications) at most 600 a minute per sending
  household and space (`REACTION_ARRIVALS_PER_MIN`), counting only
  reactions it applied — a stale copy, a refusal and one held for its
  post cost nothing. Nothing is dropped: a handler cannot ask the sender
  to retry (the registry swallows handler errors and the replay cache
  refuses a re-send), and the host would drop the same one, so a drop
  would be lost for good.
- **Where.** The post must live in the event's space; a reaction that
  overtakes its post is held and applied when the post lands; an add on a
  deleted post is dropped. An archived space takes none (it is a write, not
  a removal) — and the local API refuses both the add and the remove
  there, so the copies stay equal.
- **One apply across paths.** In a space that is also read through a GFS,
  the reactor's household still publishes the v_49 `space_item`
  (`reaction_add` / `reaction_remove`,
  [discovery.md](./discovery.md#comments-reactions-and-own-edits--deletes-v_49))
  with the **same** stamp, so a member household that gets both applies the
  first and drops the second as not newer.
- **Missed while offline.** The outbox queues the event for an unreachable
  household as for any other; one lost for good heals from the host's
  §25.6 stream, which carries each post's reaction sets and stamps
  ([sync.md](./sync.md), v_92 `reaction_stamps`). **A removal travels
  only as a stamp:** the host's sets lacking a reaction is no removal (it
  may never have received the add — it was below v_93, or the event was
  lost), so a reaction whose add a household applied stays until a removal
  stamp arrives. Removal stamps live 30 days — the reach of a catch-up —
  and a `posts` record carries removals before adds when it must cut
  its stamp list (1024). **Known limit:** a household that misses a
  removal for more than 30 days (offline that long, the queued event
  gone), or on a post holding more than 1024 removal stamps, keeps that
  reaction. Dropping held adds the host lacks on a full stream would
  fix it, but it is the same rule that deleted reactions the host
  never received (made while it was below v_93), so it is not done.

**Followers** get what they got before: reactions through the GFS member
relay in a public / global space, nothing new. **Older households** (below
v_93) are skipped by the fan-out and keep showing their own people's
reactions.

**No backfill.** A reaction made before every household involved was at
v_93 reaches the others only if the host holds it: the catch-up stream
runs host → member, and a member's own reactions never travel up to the
host in it. Such a reaction shows everywhere once it is toggled again.

**Not covered:** comments have no reactions (there is no such row), and
space **chat** reactions already federate (`SPACE_CHAT_REACTION`,
[space-chat.md](./space-chat.md)). An open space view does not update
live for a reaction (as for a local one); it shows on the next load.

**Cost** — one reaction (or removal) in a space of 200 member households;
the payload is 214 B of JSON (measured, a `remove` with a one-glyph emoji):

| Path | Per trigger |
|---|---|
| Paired / mesh member households | 1 envelope each, ~1 KB sealed (estimate: 214 B payload + envelope fields and signature, base64) → 200 × ~1 KB ≈ 200 KB uplink |
| Mailbox seats behind one GFS (v_91 multi) | ONE upload: one body in the smallest size bucket (≤ 5.5 KB, the figure measured for a ~1 KiB event) + 229 B per mailbox → ≤ 51.4 KB for 200; the GFS pushes body + wrap to each |
| Receiver work | one seat read, one row update; no reply, no re-broadcast |
| Worst case: one person toggling as fast as the API allows | online peers: every toggle is sent (200 × ~1 KB each), and each receiver stores every one but announces at most 600 a minute per (household, space); an offline peer's queue holds ONE entry per (post, person, emoji) — at most 5 live reactions per person per post — however many toggles were made |
| Followers | 0 new |

One actor triggers 1 × N sends, never N × M: a receiver answers nothing
and forwards nothing. Nothing is sent on a timer, and only the delta
travels — never the post's whole reaction set.

## Flow — new post fan-out

```mermaid
sequenceDiagram
    autonumber
    participant U as User<br/>(HFS A browser)
    participant A as HFS A
    participant B as HFS B
    participant C as HFS C
    U->>A: POST /api/spaces/{id}/posts
    opt text post with a web link (no_link_preview unset)
        A->>A: build link_preview<br/>(cached; guarded fetch<br/>of the linked page)
    end
    A->>A: persist locally,<br/>publish SpacePostCreated
    par broadcast to every member HFS
        A->>B: SPACE_POST_CREATED<br/>(link_preview inside the<br/>encrypted payload)
        A->>C: SPACE_POST_CREATED
    end
    B->>B: persist + publish<br/>RemoteSpacePostCreated
    C->>C: persist + publish<br/>RemoteSpacePostCreated
    Note over B,C: each HFS pushes the post<br/>to its connected clients<br/>via WebSocket.<br/>Neither fetches the linked URL.
```

A `text` post's optional `link_preview` card is built only by the
author's household and never re-fetched by a receiver — see
[spaces.md § Link previews](./spaces.md#link-previews-author-built-receiver-never-fetches).

A post's pictures reach paired and mesh members as `SPACE_MEDIA_BLOB`
chunks. A member seated from an invite link (v_80) and a follower of a
global space instead get a `media_refs` descriptor inside the encrypted
post (the follower: in the relayed inner) and fetch the files from their
GFS media cache — see [`media.md`](./media.md#space-media-via-the-gfs-cache-v_80).

## Flow — reaction

```mermaid
sequenceDiagram
    autonumber
    participant U as User (HFS A)
    participant A as HFS A
    participant B as HFS B (host<br/>of original post)
    U->>A: POST /api/spaces/{id}/posts/{pid}/reactions/{emoji}
    A->>A: persist local Reaction row
    A->>B: SPACE_REACTION_ADDED
    Note over B: host aggregates counts,<br/>may re-broadcast a rolled-up<br/>count event to the rest of the<br/>space on a timer
```

## Edit & delete

`_UPDATED` carries the new content and a fresh `updated_at`. `_DELETED`
carries the `post_id` or `comment_id` only — content is already gone
on the sender side. The receiver idempotently updates or removes by
ID; if the receiver has never seen the ID (e.g. missed the create
event during a network partition), the `_UPDATED` / `_DELETED` is
dropped silently.

A delete keeps the row (`deleted = 1`, content cleared) as the post's or
comment's **tombstone**. A household that missed the `_DELETED` (offline
past the outbox, a mesh drop) learns of it from the §25.6
`posts_deleted` / `comments_deleted` sync resources — `{id, author}`
(plus `post_id` for a comment), never content — judged by the same rule
as the live delete: the author's household or content authority (host,
admin, moderator), then the space's `posts` level. From the host, an id
never held gets the soft-deleted row, so a stale copy streamed later
cannot create it. See [`sync.md`](./sync.md#post-and-comment-tombstones).

## Polls

A poll hangs off a post. The post travels as `SPACE_POST_CREATED`; the
poll follows as `SPACE_POLL_CREATED`, which carries the whole definition
inside the sealed payload:

```json
{"post_id": "…", "space_id": "…", "question": "…", "allow_multiple": false,
 "closes_at": null, "options": [{"id": "<32 hex>", "text": "…"}, …]}
```

The option ids are the ids every household stores and every vote names —
so tallies agree everywhere. They are **bound to the post**:
`poll_option_id(post_id, index)` = `"po"` + the first 30 hex characters of
`SHA-256("poll-option\0" + post_id + "\0" + index)`. A receiver recomputes
them — a list with any id of that shape must be the post's own ids, all
of them, in order — so a poll can only carry its own post's option ids
and no other poll (on any post, in any space) can take them first and
block it. A poll made before ids were post-bound has random uuid ids
(never starting with `po`): they are taken as stored — the definition is
bound to the post's author's household (or the host) by the create rule
below, so such a poll still reaches every household live and on resume.
Only for those older polls can another poll still claim an id first.
The poll has its own event (rather than a field of `SPACE_POST_CREATED`)
because it is created *after* its post on every path: in the same request,
attached later (`POST …/posts/{pid}/poll`), or when a moderation approval
that published the post finishes its poll.

`SPACE_POLL_VOTE_CAST` carries one voter's **whole selection** —
`{post_id, voter_user_id, option_ids: [...]}`; an empty list is a
retraction — and replaces that voter's earlier votes on the poll.
`SPACE_POLL_CLOSED` ends the voting window.

A member household applies them like this (`SpaceContentInboundHandlers`):

- **Create** — never on a system (bot) post, which has no author's
  household (any household may relay one); otherwise only from the
  household of the wrapper post's author (or
  the host relaying it, or the host's release of a reviewed post, whose
  approval block is compared with the queued poll), and then the space's
  `posts` access level for the payload's actor (§4.3, `access_admits`): a
  plain member's household cannot attach a poll by itself where posts are
  reviewed or admin-only — as the local route refuses it. 2–20 options
  carrying the post's option ids in order, question and option text ≤ 500
  characters, `allow_multiple` a boolean, `closes_at` absent or a
  parseable ISO 8601 stamp of at most 40 characters; anything else is
  dropped. It is **created once**: a post that already holds a poll
  keeps it, so a re-send, a resume replay or the §25.6 stream never
  rewrites a poll people voted in. One transaction proves the post is a
  live post of the routed space (`create_poll_in_space`).
- **Vote** — only from the household seating the voter, and only while
  the poll is open: a closed poll, or one past its `closes_at`, takes no
  vote and no retraction (the local `PollService.cast_vote` rule — a
  closed poll's result stands). Every option must belong to that poll and
  a single-choice poll takes at most one (`set_votes_in_space`, one
  transaction); a refused vote leaves the earlier one. A vote that raced
  the close stays on the voter's own household only.
- **Arrival order.** With several delivery paths per space a vote from one
  household routinely reaches a third before the author's definition, and
  the definition can overtake its post. Nothing resends an event that was
  answered `ok`, so they are **held**, not dropped: the definition until
  its post lands, a vote or a close until the poll does
  (`PendingParentBuffer`, kinds `post` / `poll` — in memory, bounded,
  10 min), then replayed through the §24.11 gates and the handler again.
  A poll the host's stream creates releases them the same way. What a
  household misses anyway (offline past the outbox) heals from the host's
  stream, below.
- **Vote order.** A vote is the voter's whole selection — state, not
  history — and the receiver keeps no stamp, so the sender guarantees the
  order: `SPACE_POLL_VOTE_CAST` has a latest-only outbox key
  (`poll-vote:{space}:{post}:{voter}`, `supersede_key`). Per peer at most
  one vote of that voter on that poll waits in the outbox; a newer one
  replaces it, and a newer one delivered live drops it — so a vote queued
  while a household was unreachable can never land after a later one (on
  the host that would roll the voter back for everyone through its
  stream). The grouped mailbox rows of v_91 follow the same key. What is
  left: two votes of one voter seconds apart that travel different
  transports to the same household (direct and mesh-routed) are applied in
  arrival order; a per-vote stamp that survives a retraction would need
  new persistent state and is left for a follow-up.
- **Edit / delete.** A poll cannot be edited — locally either: only the
  post's author attaches one, and a post that has a poll takes no second
  (`409 POLL_EXISTS`), so no household ever rewrites option text under
  live votes. A schedule poll follows the same local rule (author only,
  once, slot ids minted by the server). Editing the post leaves the poll alone; deleting the post drops the poll, its options and votes on
  every household (migration 0085 trigger), and a deleted post takes no
  new poll.

**Who gets it.** `broadcast_to_space_members`, so every member household on
every path (paired, mesh, GFS mailbox — one upload for all mailbox
households since v_91) gets the same three events. A **follower household**
(a `subscriber` invite link) is in that fan-out too: followers see the
poll and its live tallies but cannot vote — the SPA's vote is refused
locally (`403 SUBSCRIBER_READ_ONLY`; `GET …/poll` says `can_vote: false`
and the SPA shows the options as results only) and every household drops a follower
household's `SPACE_POLL_VOTE_CAST` at the §24.11 writer gate. **GFS
subscribers** of a public / global space (no seat) do not get polls: the
poll events are not relayed to a GFS, so they see the post without its
poll.

**Cost.** One poll = one `SPACE_POLL_CREATED` (≤ ~11 KB at the caps,
typically < 1 KB) per member household; one vote = one
`SPACE_POLL_VOTE_CAST` (~200 B) per member household — unchanged from
before, when both were already sent but could not be applied. No new
event type, no version bump: an older household ignores the definition
(it had no handler) and heals from the host's v_92 stream as before.

Only the author's household closes a poll — by hand, or when its
`closes_at` passes: `PollDeadlineScheduler`
(`infrastructure/poll_deadline_scheduler.py`, every 60 s) closes that
household's due polls with an atomic `closed=0 → 1` update, so each poll
sends exactly one `SPACE_POLL_CLOSED` to its member households and a
restart re-finds only polls still open. Member households never close a
copy themselves (a peer's close is accepted only from the author's
household), but read a poll past its deadline as closed and refuse votes
on it. Nothing new on the wire: a member household tells a deadline close
from a hand close by the poll's own `closes_at`. A redelivered close or
finalise tells nobody twice.

`SPACE_SCHEDULE_FINALIZED` (a date poll's time picked) tells this
household's people who answered (`poll_scheduled`) through a local-only
`RemoteSchedulePollFinalized` event — not `SchedulePollFinalized`, whose
calendar bridge would add a second entry next to the one the organiser's
household already created and federated.

**Catch-up (v_92).** The host's §25.6 stream converges a member household
on the host's polls: the `polls` record creates a poll and its options it
lacks, carries the votes (`votes`, ≤ 1000, `votes_partial` when cut) and
the closure; the `schedules` record carries the answers (`responses`,
≤ 1000, last writer wins on `responded_at`) and the pick
(`finalized_slot_id`). Taken from the host only, never over this
household's own people's votes and answers; a cut list deletes nothing. See [sync.md](./sync.md#coverage--every-space-item-type).

Votes are encrypted: the vote's `option_ids` and the voter's `user_id`
stay inside the space's encrypted payload. GFS cannot tally votes even on public spaces.

## Implementation

- `socialhome/services/post_service.py` — space and household post CRUD.
- `socialhome/services/comment_service.py` — comment CRUD.
- `socialhome/services/reaction_service.py` — reaction toggling.
- `socialhome/services/poll_service.py`,
  `schedule_poll_service.py` — polls;
  `socialhome/infrastructure/poll_deadline_scheduler.py` — closing at
  `closes_at`.
- `socialhome/services/federation_inbound/space_content.py` —
  inbound handlers for all of the above.
- `socialhome/routes/post_routes.py`, `comment_routes.py`,
  `reaction_routes.py` — REST endpoints.

## Spec references

§13.4 (space feed semantics),
§25.8.21 (poll-vote encryption),
§27.5 (integration tests for fan-out).
