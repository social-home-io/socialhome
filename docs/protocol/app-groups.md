# Social Home Apps — group games (tables)

A **table** is an app session with 2–30 seats (up to 10 when opened by
picking people). Its **host household** — the *hub*, the household of the
person who opened it — keeps the roster and is the only household the
others talk to about it. The *host person* can change (hand-over); the hub
never does. Every other household with a player keeps a copy of the table
as the hub last sent it. Households never talk to each other because of a
table: each talks only to the hub, over the pairing it already has.

This page covers **the roster** (capability v_76): opening a table from the
invite sheet, invites, join / decline / leave / remove, Start, the host
hand-over, closing and expiry. Messages at a table (`APP_MESSAGE.group`,
sealed seat keys, the per-seat mailbox) and space tables (v_77) come next;
the 1:1 sessions are in [`apps.md`](./apps.md).

## Scope

- **HFS**: the hub runs the roster (`AppGroupService`); each player's
  household keeps a copy and applies its own people's rules (blocks,
  children). Same §24.11 pipeline and sealing as every app event.
- **GFS**: uninvolved. A table opened by picking people only reaches
  households the host's household is paired with.

## Event types

No new event type: new `verb`s of `APP_SESSION`, all inside the sealed
payload, sent only to and taken only from households at v_76 or later
(`FederationCapability.MIN_FOR_APP_GROUPS`).

| Verb | From → to | Sealed fields |
|---|---|---|
| `group_invite` | hub → an invitee's household (one person) | `app_id, session_id, to_user, from_user` (the host person), `others` (how many more were invited — never who), `table` |
| `group_join` | a player's household → hub | `app_id, session_id, from_user` |
| `group_seat` | hub → the joiner's household | `app_id, session_id, to_user, table` |
| `group_roster` | hub → households with someone seated or invited | `app_id, session_id, table` — at most one per 5 s per table (coalesced) |
| `group_start` | hub → seated households | `app_id, session_id, table` |
| `group_start` | host person's household → hub (host lives elsewhere after a hand-over) | `app_id, session_id, from_user` |
| `group_leave` | a player's household → hub | `app_id, session_id, from_user, reason` (`left`, or `declined` before joining) |
| `group_remove` | host person's household → hub | `app_id, session_id, from_user, seat` |
| `group_close` | hub → every household at the table | `app_id, session_id, table` — the table ended |
| `group_close` | hub → one household | `app_id, session_id, to_user, reason` (`removed`, `withdrawn`, `full`), `table` — that one person's place is over |
| `group_close` | host person's household → hub | `app_id, session_id, from_user` — "close this game" |

**The table snapshot** (`table`), the one shape the hub sends:

```json
{ "rev": 7, "state": "lobby", "end_reason": null, "host_seat": 1,
  "min": 3, "max": 8, "late_join": false, "note": "Bring snacks",
  "created_at": "…", "expires_at": "…", "started_at": null,
  "roster": [{"seat": 1, "instance": "<hub>", "user": "anna", "name": "Anna",
              "household": null, "state": "seated"}, …] }
```

`state` is `lobby` → `playing` → `ended` (`end_reason`: `finished`,
`cancelled`, `expired`, `host_gone`, `app_removed`,
`closed_by_moderator`). Roster entries are `seated`, `left` or `removed`:
**who was invited and hasn't joined never travels.**

## Life of a table

```mermaid
sequenceDiagram
    autonumber
    participant B as Ben's household
    participant H as Hub (Anna's household)
    participant C as Cleo's household
    H->>H: Anna picks Ben + Cleo in the invite sheet → table (lobby), Anna seat 1
    H->>B: group_invite {to_user: ben, from_user: anna, others: 1, table}
    H->>C: group_invite {to_user: cleo, from_user: anna, others: 1, table}
    Note over B,C: each household checks its own rules → Needs you "Anna invited you…"
    B->>H: group_join {from_user: ben}
    H->>H: invited? from Ben's own household? open, not full? → seat 2, rev+1
    H->>B: group_seat {to_user: ben, table}
    H-->>C: group_roster {table} (≤ 1 per 5 s)
    C->>C: anyone seated Cleo can't play with? → hide the invite (nothing goes back)
    C->>H: group_join {from_user: cleo}
    H->>C: group_seat
    H-->>B: group_roster
    H->>H: Anna presses Start (≥ min seated)
    par one envelope per household
        H->>B: group_start {table}
        H->>C: group_start {table}
    end
    Note over H: Anna leaves → host passes to the player seated longest; the hub keeps relaying
    H-->>B: group_roster {host_seat: 2}
```

| Step | Rule |
|---|---|
| **Open** | Only through Social Home's invite sheet (D3): people of `GET /invitable` with `groups: true` (households at v_76). Host + picked people within the app's `players` range, ≤ 10. Caps: ≤ 5 tables waiting per person per app, ≤ 10 new a day; each person picked counts once against the 1:1 invite caps (10 open, 30 a day). Waits 1, 3 (default), 7 or 14 days. |
| **Invite** | A household below v_76 is refused (409 `APP_GROUP_HOUSEHOLD_TOO_OLD`). A person on the hub's own household gets no envelope. |
| **Join** | The joiner's household checks first (app here and on, age, blocks, children); the hub seats only an invited person, from their own household, while the table is open and not full. A refusal is `group_close {to_user, reason: full / withdrawn}`; a stranger's join is ignored. |
| **Start** | Host person only, once `min` are seated. Unanswered invites are withdrawn (`group_close {to_user, reason: withdrawn}`) unless the app takes late joiners. A table of two (`max: 2`) starts by itself when the second seat fills. |
| **Leave** | Before Start the seat is free again; the host person leaving a lobby closes it (`cancelled`); after Start the host passes to the player seated longest. A decline is a leave before joining (the hub marks it; the players aren't told). |
| **Remove** | Host person only; the removed person's household gets `group_close {to_user, reason: removed}`, they can't come back. |
| **Close / expire** | Host person closes (`cancelled`); a lobby past its time ends `expired` (the hub's hourly sweep; a copy ends on its own an hour later if the hub is gone). The app uninstalled at the hub ends its tables (`host_gone`); at a player's household its people leave. |

## Each household's own rules (design 2.11, D8, D12)

The hub is never told why someone isn't at a table:

- An invite from a table whose host or a seated player the invitee blocked
  — or who blocked them here, or a guardian block separates them from — is
  **hidden**: it never reaches Needs you, and an invite already waiting
  stops waiting when such a person sits down. **Nothing goes back** (not
  even a decline); the hub's invite just runs out.
- Someone like that sitting down at a table one is seated at takes one
  out: an **ordinary leave** to the hub; only that person is told why
  (`app.group` frame, `reason: "cant_play"`).
- A **protected account** sits only where every player is someone it may
  already message — its own household, or a household it paired with
  directly (§CP.F3). Checked at the invite, at Join and on every roster.
- The app's age rating applies as for 1:1 invites.

**Residuals.** A leave a household makes for its person right after
someone sits down shows up at the same time as that join — the players
see "Ben left" next to "Dev joined", as the design accepts (a block means
"keep us apart"). A player who knows a table's id could send another
household an invite for that id first, which then drops the real one
(nuisance only: nothing is learned or seated).

## Receiver rules

- A copy takes a snapshot only from the household that invited it (the
  hub, the signature-proven sender) and only a newer `rev`; a table's app
  and hub never change. A snapshot out of shape — over 30 seats, a seat or
  person twice, a host without a seat, an expiry past 14 days, a note over
  80 characters or across lines, a name over 80 — is dropped.
- An invite is kept only for a table still open, of at most 10 seats, whose
  host is the sending household's `from_user`, for an app installed here,
  on, allowed to reach other households and marked `tables`; for a session
  id the person has no row for; and at most 50 open tables from one hub.
- A table with a `space_id`, or a verb arriving space-routed, is dropped
  (space tables are v_77).
- The hub acts on `group_start` / `group_remove` / `group_close` only from
  the host person's own household, on `group_join` / `group_leave` only for
  the person of the sending household.

## Older households

| Case | Behaviour |
|---|---|
| A picked person's household is below v_76 | The sheet marks them (`groups: false`); an open naming them answers 409 `APP_GROUP_HOUSEHOLD_TOO_OLD`. Nothing is sent. Their 1:1 games are untouched. |
| An app without `tables: true` in its manifest | 409 `APP_GROUPS_NOT_SUPPORTED`; 1:1 invites only. |
| An invite for an app not here or turned off | Dropped (the 1:1 "waits for its app" row isn't offered for tables yet). |

## Cost

Invites are one envelope per person invited (`group_invite` names one
person; ≤ 9), the hub's own people none. Everything after is one
envelope per household, never per person. A join: one envelope
to the hub, one seat answer, and at most one coalesced roster per 5 s to
the other households at the table (≤ 0.6 KB each sealed). Filling 10 seats
over 10 households one by one: ≈ 10 × 2 + ~10 × 9 rosters × 0.6 KB ≈ 60 KB,
once. Start / close: *h − 1* envelopes.

## Storage

`app_group_sessions` (one row per table a household knows, migration
0136) and the people's own `app_pending_sessions` rows (marked `group`),
which keep Needs you, the TTLs and the uninstall cascade. See
[`../database.md`](../database.md).

## Implementation

- `socialhome/domain/app_group.py` — the table, the snapshot and its
  bound check, the bounds and the coded errors.
- `socialhome/services/app_group_service.py` — the hub, the copies, each
  household's rules; `AppFederationService` routes the `group_*` verbs to
  it and keeps group rows out of every 1:1 path.
- `socialhome/repositories/app_group_repo.py`, `socialhome/routes/app_groups.py`.
- Tests: `tests/services/test_app_group_service.py`,
  `tests/protocol/test_app_group_federation.py` (security).

## Spec references

Group-play design §2 (roster, topology, life of a table, wire, storage,
older households, blocks and children, bounds), decisions D1–D3, D8, D11,
D12; [`capabilities.md`](./capabilities.md) v_76.
