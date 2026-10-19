# Local communities

A **public** space can be bound to an area — a centre and a radius — and
become a *local community*: a neighbourhood, a village, a street. A
household holds a seat only while the home it **already shares with the
hosting household** lies inside the circle. The host decides, on every path
that grants a seat; nobody is ever asked to send a location to join — and
the centre never leaves the host (owner decision 2026-10-19: people's
locations are not shared by design).

## Scope

- **HFS (host)**: stores the area (`spaces.lat` / `lon` / `radius_km`,
  host-local) and shows the centre on a map to its own people only. Each
  paired peer's directory snapshot gets "local community, within X km" and
  the host's verdict for **that peer's** shared home — no centre. Checks
  every seat against the joiner's shared home.
- **HFS (paired peer)**: keeps the radius and its verdict for each directory
  entry and shows them before joining ("Local community · within 5 km of
  the host's chosen centre", *In your area* / *Outside your area* /
  *Near the edge — ask to join and the host will check* /
  *Share your home with …*) — no map, no centre. Discover sorts in-area
  communities first (near-the-edge ones with the ordinary spaces), can
  narrow to in-area and near-the-edge ones ("Near you"), and offers no Join
  on an outside one. The verdict is a convenience; the host's check is the
  rule.
- **GFS**: uninvolved. Only the `public` tier can be a local community, and a
  public space is never on a GFS. A household met through a GFS never shares
  a home with the host, so a local community's invite link never seats it.

## The rules

- **Public tier only.** Create / settings take `lat` / `lon` / `radius_km`
  (all three or none) only for a public space (`422 LOCAL_AREA_PUBLIC_ONLY`
  otherwise). Leaving the public tier drops the area. A pin stored before
  this feature (a centre without a radius) is no area.
- **Precision.** The centre is truncated to 4 dp (about 11 m) on write and
  again on receipt; the radius is 0.5–50 km, kept to one decimal. Peers
  re-validate a received area and drop a malformed one rather than guess.
- **Whose home.** A local user of the host → the host household's own home
  (`instance_identity.home_lat` / `home_lon`). A remote household → the home
  it shares with the host through pairing (`remote_instances.home_lat` /
  `home_lon`, from `PAIRING_PEER_ACCEPT` and `LOCAL_HOME_LOCATION_CHANGED`,
  cleared when that household turns `share_home` off toward the host). A
  household with no pairing row (met over the mesh, or through an invite
  link — `space_session`, which never receives a home) has none.
- **Unknown is refused.** No home → `403 HOME_NOT_SHARED`; outside →
  `403 OUTSIDE_AREA`. The rim counts as inside (haversine on a 6 371 km
  sphere, the same as the zone match). The seat check always uses the
  **exact** distance — the "unsure" band below is only for what the
  directory shows.
- **Checks are budgeted per person.** A seat attempt is an exact yes / no,
  and a refused invite-link use is handed back, so somebody moving their
  shared home and asking again would trace the rim — and join → leave →
  move → rejoin would too, so **a successful check counts like a refusal**
  (owner decision 2026-10-19). Each **person** gets **3** checks per
  community per UTC day (`MAX_LOCAL_AREA_CHECKS_PER_PERSON`), counted on
  every path the person starts: open join, join request, follow, invite
  accept, invite-link redeem (local, §D2 and §D2b). An admin's own steps —
  sending an invite, a direct add, approving a request — are not counted.
  "Home unknown" spends nothing (it says nothing about the area). Past the
  budget the host answers `429 LOCAL_AREA_RATE_LIMITED` with no verdict
  until 00:00 UTC (`whose: own` for a local user, `whose: host` on a
  remote redeemer).
  - **Per person, never per community or per household:** one person
    probing does not hold off the rest of their household, and a busy
    community never locks out newcomers.
  - **Who is the person.** A local user → their user id (counted too,
    though the host's own people can read the area anyway). A remote person →
    the user id the request carries, keyed together with the sending
    household that the §24.11 envelope authenticates. That user id is the
    household's to choose, so a **remote household** also has a backstop of
    **10** checks per community per UTC day
    (`MAX_LOCAL_AREA_CHECKS_PER_HOUSEHOLD`) — minting fresh user ids buys
    no more than that. 10 is above a few people's budgets, and a person
    already past their own budget spends none of it, so one prober cannot
    use up the household's day. The host's own household has no backstop
    (its people are real accounts here).
  - **Retries are free.** Asking again while a join request is pending is
    answered by that request — no second row and no second check.
  - **A remote request costs two.** Filing a request from another
    household is one check and redeeming the approval's token is another
    (the redeem can be retried, so it must count); the approval itself
    is the admin's. A request-mode join over federation is therefore two
    of the person's three checks that day.
  - **Kept across restarts** in `local_area_attempts` (migration 0109):
    one counter per space, kind, subject and UTC day; a person is stored
    as `sha256(household \n user id)`, never the raw id; days before
    yesterday are pruned on every write.
- **Every seat path, no override.** Open join, a join request (when filed
  and again when approved), follow, an invite token / link redeem (local and
  §D2 / §D2b over the wire), a direct invite (when sent and again when the
  accept arrives), and an admin's direct add. An admin's explicit invite does
  **not** override the area (owner decision 2026-10-19).
- **Members stay.** Moving, shrinking or removing the area evicts nobody;
  the check runs when a seat is granted.
- **The host only.** A member household's stub never holds the area. For
  the same reason a local community never has *delegated admin authority*
  (`409 LOCAL_AREA_DELEGATED` either way round): a delegated admin's household
  would seat people itself, without the host's check. With delegation off a
  member household's admin invite is forwarded to the host, and invite links
  are always minted on the host (v_52), so every seat passes the host.
- **Host-local config.** Setting the area bumps no `config_sequence` and sends
  no `SPACE_CONFIG_CHANGED`; it re-sends the directory snapshot.

## What a paired household learns

- `local_community: true` and `radius_km` — "within X km". The radius is not
  a location.
- `in_area` — the host's answer for the **receiving** household's home, as
  that household chose to share it with the host: `true`, `false`, or
  `null` when it shares none **or the host is unsure** — then
  `area_edge: true` says so. Each peer's snapshot is built for it alone, so
  no household ever receives another household's verdict.
- Nothing else: no centre, no distance. The host re-sends a peer's snapshot
  when the home that peer shares with it changes (including sharing turned
  off) and when the peer first advertises v_59.

### The "unsure" band — the verdict can't be used to find the centre

Answered exactly, the verdict would let a household move its shared home
step by step and find the rim, and so the centre. The host coarsens it
(`directory_verdict`); the seat check does not:

- **Band.** `b = max(10 % of the radius, 250 m)`. Near the rim the answer is
  neither yes nor no: `in_area: null`, `area_edge: true`. A receiver that
  does not know `area_edge` reads "unknown" — never a wrong yes or no.
- **Grid.** The answer is computed for the centre of the home's grid cell
  (an absolute grid, `b` wide, independent of the community's centre), so
  every home in a cell gets the same answer and moving within it says
  nothing.
- **Secret shift.** The unsure zone reaches `1.25 b` either side of the rim,
  slid by up to `± b/2` per cell by an HMAC under a key HKDF-derived from the
  host's identity seed (never stored, never sent). Where one cell turns
  unsure says little about where the rim is. "Inside" is still always
  inside and "outside" always outside, for every home in the cell.
- **Budget.** A peer's verdicts follow at most **4** different shared homes
  per UTC day (`MAX_VERDICT_HOMES_PER_DAY`); further moves are answered for
  the last home taken (nothing new) until 00:00 UTC. Withdrawing the home
  always takes effect at once but keeps the day's count — a home shared
  again past the budget gets no answer (unknown) — so toggling sharing is
  no way around it. The count is kept in `local_area_attempts` (kind
  `verdict_home`), so a restart does not refill it; the last home taken is
  not stored, so after a restart a peer already past its budget gets
  "unknown" until the next UTC day.

What's left: a household moving its home in 20 m steps straight through the
area, with no budget, narrows the centre only to about one band on average
and never below 0.4 of it (`tests/services/test_local_community.py`, 24 host
secrets) — the same walk against the exact answer finds it within a few
steps. With the budget such a walk takes months, and every move is visible
to the host as that household's home changing
([`../principles.md`](../principles.md#peoples-locations-are-never-shared-by-design)).

## Event types

No new event type. The feature rides on:

- `SPACE_DIRECTORY_SYNC` — an entry carries `local_community: true`,
  `radius_km`, `in_area` and `area_edge` when the space is a local community
  (v_59, sender-gated: a peer below v_59 gets the entry without them).
  Redefined in place before release — the unreleased first draft carried
  the centre, the second had no `area_edge` and answered exactly.
- `SPACE_JOIN_REQUEST` — the host drops a request from a household that is
  outside or whose home it does not know, or from a person past their daily
  check budget (logged, like a banned applicant), so it never reaches the
  admins. A repeat from a person whose request is already pending is
  dropped without a check.
- `SPACE_INVITE_TOKEN_REDEEM_DENY` — three reasons besides the uniform
  `REDEEM_DENY_REASON`: `"home location not shared"`,
  `"home outside the local area"` and `"local area refusals rate-limited"`
  (the redeeming person's daily check budget is used up — the wire string
  is kept from the first revision).
  They are sent only after the token was consumed and the ban check passed
  (a dead or banned link still gets the uniform reason), the use is handed
  back, and the redeemer maps them to its own `HOME_NOT_SHARED` /
  `OUTSIDE_AREA` / `LOCAL_AREA_RATE_LIMITED` errors — the issuer's text is
  never rendered.
- `SPACE_PRIVATE_INVITE_ACCEPT` — re-checked on the host; refused → the
  invitation is declined and the household gets `SPACE_REMOTE_MEMBER_REMOVED`
  so its accepted stub goes away.

## Flow — a paired household asks to join

```mermaid
sequenceDiagram
    autonumber
    participant B as HFS B (joiner)
    participant A as HFS A (host)

    Note over A,B: paired · B shares its home with A (share_home on)
    Note over A: in_area for B = B's shared home's grid cell<br/>inside / outside, or unsure near the rim<br/>(secret per-cell shift; snapshot built for B alone)
    A->>B: SPACE_DIRECTORY_SYNC {spaces: [{…, local_community, radius_km, in_area, area_edge}]}  (v_59+)
    Note over B: Discover: "Within 5 km" + In your area /<br/>Near the edge — no map, no centre
    B-->>A: LOCAL_HOME_LOCATION_CHANGED (B moves / stops sharing)
    A->>B: SPACE_DIRECTORY_SYNC (B's fresh verdict — at most 4 new homes a UTC day)
    B->>A: SPACE_JOIN_REQUEST {space_id, user_id}
    Note over A: already pending for (B, user_id)? → drop, no check
    Note over A: home of B = remote_instances.home_lat/lon (unknown → drop)<br/>spend one check of (B, user_id) — 3 a UTC day — and of B — 10<br/>(none left → drop, no verdict) · inside? else drop (logged)
    Note over A: admin approves → re-check (area or sharing may have changed)
    A->>B: SPACE_JOIN_REQUEST_APPROVED {invite_token}
    B->>A: SPACE_INVITE_TOKEN_REDEEM
    Note over A: consume → ban → budget (one more check) → area check
    alt inside
        A->>B: SPACE_INVITE_TOKEN_REDEEM_ACK {space_meta}
    else outside / home unknown / person's checks used up today
        A->>B: SPACE_INVITE_TOKEN_REDEEM_DENY {reason}  (use handed back)
        Note over B: 403 OUTSIDE_AREA / HOME_NOT_SHARED /<br/>429 LOCAL_AREA_RATE_LIMITED (own wording)
    end
```

## Implementation

- `socialhome/domain/local_community.py` — the pure rules: `LocalArea`,
  `validate_local_area` (4 dp, bounds), `local_area_of` (public tier, whole
  area), `eligibility` (haversine, exact), `directory_verdict` /
  `edge_band_km` / `edge_cell` (the coarsened directory answer), the
  host-API shape (`local_area_wire`, with the centre) and the directory
  shape (`local_community_wire` / `parse_local_community_wire`, without).
- `socialhome/services/local_community.py` — `require_local_eligibility`,
  the one check every seat path calls (whose home, fail closed, the
  per-person check budget), `LocalAreaBudget` (the daily budgets, all three
  limits in one place) and `derive_edge_key` / `edge_shift_km` (the host's
  secret per-cell shift).
- `socialhome/repositories/local_area_attempt_repo.py` — the
  `local_area_attempts` counters: an all-or-nothing `try_spend` under
  `BEGIN IMMEDIATE`, pruning old days on write.
- `socialhome/services/peer_directory_service.py` — builds each peer's
  snapshot with its coarsened verdict and the per-peer home budget.
- `socialhome/services/space_service.py` — `create_space` /
  `set_local_area` / `require_local_seat` and the local seat paths
  (`request_join`, `approve_join_request`, `subscribe_to_space`,
  `add_member`, `invite_local_user`, `accept_local_invite`,
  `accept_invite_token`, `invite_remote_user`); `update_config` drops the
  area when the tier leaves public and refuses delegated authority with one.
- `socialhome/services/federation_inbound/space_invites.py` — inbound
  `SPACE_JOIN_REQUEST`.
- `socialhome/federation/invite_token_redeem.py` — the issuer-side check in
  `_consume_seat_and_build_ack`, the two DENY reasons and their redeemer-side
  mapping (§D2 and §D2b).
- `socialhome/federation/private_invite_handler.py` — the accept re-check.
- `socialhome/services/peer_directory_service.py` — the per-peer snapshot
  with that peer's verdict (v_59-gated), re-sent on `PeerHomeChanged` /
  `PeerProtoVersionRaised`.
- `socialhome/federation/peer_directory_handler.py` — received and
  re-validated into `peer_space_directory.radius_km` / `in_area`
  (migrations 0105, 0106).
- SPA: `client/src/features/spaces/discover.ts` (`entryAreaVerdict`,
  `sortNearFirst`), `SpaceDiscover.tsx` ("Near you"),
  `SpacePublicDetailPage.tsx` (About: map for the host's own people only).

## Spec references

§D1a (peer directory sync), §D2 / §D2b (join requests, invite redeem),
§11 (pairing — the home location it shares), §25.8.21 (the directory
snapshot travels encrypted to paired peers only), §2 GPS rule
([`../principles.md`](../principles.md#gps-truncation-4-dimension),
[people's locations](../principles.md#peoples-locations-are-never-shared-by-design)).
