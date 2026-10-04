# Federated moderation

"Reviewed" (`moderated`, §4.3) across households. A plain member's new
item — and their edit / delete of somebody else's — waits in a moderation
queue until content authority (owner, admin or moderator) approves it.
Since v_43 that works in spaces whose members live on several households:
the item travels from the submitter's household to every household that
may review it, and any of them may approve or reject. The **host alone
applies** an approved item — from its own stored copy, still as the
submitter's — whichever household's moderator approved it.

What queues, the caps, expiry, resume and the REST surface are described in
[`spaces.md` — Moderation queue](./spaces.md#moderation-queue). This page is
the wire protocol.

## Scope

- **HFS**: full participant.
  - **Submitter household** — holds its own copy of the item (the author's
    `GET …/moderation/mine` pending strip) and sends it to the reviewers.
  - **Reviewer households** — the space's **host** and every household
    holding a **live `admin` or `moderator` seat** in `space_remote_members`.
    Each holds a copy (same id everywhere), shows it in the Moderation tab
    to its local content authority, and may decide it. A reviewer
    household's approval goes to the host, which applies the item; a
    rejection is decided where it is made.
  - **Plain member households** — never see a pending item. They receive the
    approved content like any other write.
- **GFS**: never sees a pending item. Submissions and decisions are pairwise,
  sealed sends (see *Confidentiality*); nothing about the queue is published
  to a connection server. Only the **outcome** reaches the followers of a
  public / global space with `allow_subscribers`, on the host relay and
  content-blind — see *Outcomes for GFS followers* below.

## Event types

- `SPACE_MODERATION_SUBMITTED` — submitter household → host + reviewer
  households. Classified as a space **write** (`SPACE_WRITE_EVENT_TYPES`):
  a Follower household may not send one and an archived space takes none.
- `SPACE_MODERATION_DECIDED` — two uses. An **approval** from a reviewer
  household goes to the **host only** (a request: publish this item); the
  host, once it applied the item, announces the approval to the reviewer
  households and the submitter's household. A **rejection** goes from the
  deciding household to the host, the reviewers and the submitter's
  household. Classified as a **reader** event
  (`SPACE_READER_EVENT_TYPES`): a decision is a verdict, not content, and a
  reject still lands in an archived space; its handler admits it only from
  content authority.

Both are in `SPACE_SESSION_ALLOWED_EVENT_TYPES` (a link-joined member
submits; a link-joined moderator decides).

There is no event for the approved content and no signed approval token:
the **host** applies the item from its own copy and emits it as the
ordinary `SPACE_*` write
(`SPACE_POST_CREATED`, `SPACE_TASK_CREATED`, `SPACE_STICKY_UPDATED`, …,
plus a post's `SPACE_SCHEDULE_CREATED` / `BAZAAR_LISTING_CREATED`) through
`broadcast_to_space_members`, authored by the submitter, with an **approval
block** inside the sealed payload:

```json
{"actor_user_id": "<submitter>", "moderation": {"item_id": "…", "approved_by": "<approver>"}}
```

Expiry is local and deterministic (the same `expires_at` everywhere) — no
event.

### Bodies (all inside the encrypted payload)

| Event | Body |
|---|---|
| `SPACE_MODERATION_SUBMITTED` | `space_id`, `item_id`, `feature`, `action` (`create` / `edit` / `delete`), `target_id`, `submitted_by`, `payload` (the feature handler's item payload), `snapshot`, `submitted_at`, `expires_at`, `public_relay?` (a post create in a public / global space with followers: the submitter's author-signed post inner, for GFS followers — see *Outcomes for GFS followers*) |
| `SPACE_MODERATION_DECIDED` | `space_id`, `item_id`, `decision` (`approved` / `rejected`), `decided_by`, `decided_at`, `reason?` |

Plaintext on the envelope: the routing `space_id` only (encryption-first,
§25.8.21). `space_id` is repeated inside the payload because a mesh-routed
envelope carries no routing field.

## Confidentiality

| Path | Guarantee |
|---|---|
| Submitter → reviewer (paired) | One `send_with_mesh_fallback` per target — never `broadcast_to_space_members` — sealed under the pairwise session key. Only the host and live admin / moderator seat households are targets. |
| Submitter → reviewer (mesh) | The same send seals it end-to-end under `SPACE_ROUTED` for the target's ephemeral X25519 key: every relay sees ciphertext only. |
| Submitter → reviewer (link-joined, §D2b) | The pairwise session, carried by the connection-server relay as an opaque sealed envelope — the GFS can't read it. |
| Media of a pending post | `SpaceMediaSyncService.enqueue_for_post(…, target_instance_ids=<the reviewers>)` — the bytes go to the reviewer households only. |
| A plain member household | Never a target. A submission that reaches a household with no local content-authority member anyway (misdirected) is dropped and not stored. |
| Approved content | The ordinary write: `broadcast_to_space_members` to member households only. In a public / global space with followers, the host also relays an approved post to the GFS on its authority, encrypted and padded (see *Outcomes for GFS followers*). |
| Decisions | Pairwise, sealed: an approval request to the host only; the host's announcement and any rejection to the host, the reviewers and the submitter's household. |
| Queued retries | The outbox re-checks a retried `SPACE_MODERATION_SUBMITTED` at send time and drops it when the target no longer reviews the space. |
| A household that stops reviewing | When it loses its last content-authority seat in the space, it expires the pending items of other households' members it holds and NULLs their content (its own members' items stay). |

A reviewer household's local content authority sees pending items through
the Moderation tab, the `space.moderation.*` frames and the title-only
`moderation_pending` bell — exactly as on the host.

## Outcomes for GFS followers

GFS followers of a public / global space with `allow_subscribers` hold no
seat and receive no `SPACE_*` event, so the host relay carries the two
outcomes they need, as `space_post_public` relays authority-signed with the
space seed (wire shape, checks and residuals:
[`discovery.md` — Moderation outcomes on the host relay](./discovery.md#moderation-outcomes-on-the-host-relay)):

- **An approved post.** In such a space the submitter's household adds the
  post's **author-signed** inner to `SPACE_MODERATION_SUBMITTED`, next to
  the payload, as `public_relay` (signed over the queued post,
  `created_at` = the submission time). A reviewer household keeps it in its
  queue row only when it verifies for this space, this post id and the
  submitter; the same key inside the payload is dropped. When the host
  applies the approved post, it relays that copy — only if it is exactly
  the post it publishes — marked `approved_post`, with the author
  household's writer cert (a plain member of a `MODERATED` space holds
  `comment` scope). Followers verify the author's own signature, so no
  seed holder can attribute a post. No copy (an older submitter) → members
  only, as before. Rejected and expired items, and anything still
  pending, never reach a connection server.
- **A removal.** Every post or comment delete a seed holder applies to an
  item it holds — a moderator's, an admin's or the author's, local or a
  federated `SPACE_POST_DELETED` / `SPACE_COMMENT_DELETED` from a moderator
  household — is relayed as a removal notice naming the item and its
  author. Followers soft-delete it, or leave a tombstone (only for an id
  owner-bound to that author in this space) if it has not reached them
  yet, so a late create never brings it back.

**Never on that path:** who approved or removed the item, a rejection
reason, the queue item, or anything that is still pending. The GFS sees the
same event type and size bucket as for any post, and none of the inner.

## Receiver rules

### `SPACE_MODERATION_SUBMITTED`

Checked in order; any failure is a WARNING and stores nothing.

1. This household is the host, or holds ≥1 **local** content-authority
   member of the space.
2. `submitted_by` holds a live writer seat on the **sending** household
   (`acts_for`) and is not banned.
3. A create's `target_id` is owner-bound to `submitted_by` in this space
   (`owner_bound_id_refused`; a legacy unbound id is refused too).
4. The feature is on, its level **here** is `moderated`, and the space is
   not archived (the archived gate refuses the envelope first).
5. The feature's handler validates the payload with the live codecs and
   caps; media references must be local (`api/media/…`) — a remote URL
   is dropped, coordinates are cut to 4 decimals. The "before" snapshot is
   rebuilt from **this** household's copy of the target; the sender's is
   ignored.
6. Caps: 20 pending per (space, submitter), 50 per (space, sending
   household), 500 per space, 256 KiB per item; `expires_at` is clamped to
   at most 14 days ahead, `submitted_at` to at most 5 minutes ahead and at
   most 14 days before `expires_at`.
7. Stored once (`INSERT OR IGNORE`): a replay changes nothing — and a
   decision that overtook the submission left a contentless tombstone
   under that id, so the late submission is never stored as pending.

Local content authority gets the `moderation_pending` bell (title only).

### `SPACE_MODERATION_DECIDED`

The sender has content authority (`has_content_authority`) and `decided_by`
is a content-authority user seated on it (`moderates_as`, live seats; from
the host also one of our own users by our roster's role).

- **An approval from a non-host** is acted on by the **host** only: it
  checks the approver's live seat on the sender and runs every gate of a
  local approve for that approver — expiry, archived space, feature off,
  the feature's level (`admin_only` → an admin), the submitter still a
  writer — then applies the item **from its own stored payload** through
  the feature's normal persist path (`approved_by` = the approver) and
  announces the approval. Any other household ignores it.
- **An approval from the host**, or **a rejection** from any reviewer, is
  recorded on the held row. The first decision wins — **except that an
  approval may overturn a rejection when the approver's role is at least
  the rejecter's** (owner > admin > moderator). The host decides this from
  the seats it holds for both (a rejecter it can't place counts as a
  moderator): a moderator cannot overturn an owner's or admin's rejection —
  it stands everywhere — while an admin or owner may overturn a
  moderator's. Every copy then converges on what the host published (an
  approval the host announces also claims a rejected row). The submitter's
  household notifies the submitter (`moderation_decided`, title only).
- **No item held yet**: a contentless **tombstone** keeps the decision (at
  most 200 live per deciding household; beyond that the decision is
  dropped with a WARNING; tombstones older than 14 days are deleted by the
  hourly expiry sweep). A submission that arrives later fills in the
  tombstone's content and keeps its decided status. Approving a tombstone
  that never received its content answers 409 `ALREADY_DECIDED`.

Decided payloads are purged after the usual retention.

### Content with an approval block

`SpaceAuthorship.may_author_approved(event, space, author)` decides; it is
used in place of `may_author` for creates and in place of the `moderated`
rule in `access_admits` for edits / deletes:

- the event is a reviewable content write;
- the sender is the space's **host** — only the host applies a queue
  item, so only the host can release one (a moderator household cannot
  make one up);
- `approved_by` is a content-authority user (live seats — a demoted
  moderator fails); under `admin_only` the approver must be an admin;
- the author holds a live writer seat and is not banned;
- when the author is one of **our** users, we hold the item (defence in
  depth — the host is the roster authority already): same id, space and
  submitter, same feature / kind of write / target, status pending,
  approved or rejected, and **exactly** the item's content
  (`federation/moderation_approval.py`): a create carries the item's value
  in every field the receiver stores, and nothing the item never set (no
  recurrence, archive stamp, other creator, mirror origin); an edit
  carries the patch and every other content field equal to the row we
  hold (layout fields — a task's or sticky's position — are free). The
  check **fails closed**: every key the event carries is either routing /
  bookkeeping its event type may carry freely (ids, timestamps the
  receiver derives, layout) or content a rule compares — a key nobody
  classified, a new wire field or an alias the receiver also reads,
  refuses the release until it is. Any household holding the item checks
  it the same way.
- Accepted from the host, a release moves the held row to approved.

#### Space pages (v_48, host-sequenced)

Pages are sequenced by the space's host, and the host alone applies a
page queue item. An `edit` item (a patch, or `op: "resolve_conflict"`)
stores the `base_seq` the submitter saw.

- **Stale / force.** On approval, if the page's `seq` moved since the
  item was submitted, the host answers 409 `STALE` (an item without
  `base_seq`, from before v_48, falls back to `base_updated_at`).
  **Force** applies the item's patch as a **fast-forward** over the current
  version, never as a merge, so the approval stays bound to exactly that
  patch. A conflict never blocks an approved edit.
- **Resolutions.** The item carries `resolution` (`side` /
  `merged_content` / `mine` / `theirs`), `side` (the kept version's
  `sha256:` hash) and `sides` (the hashes the submitter saw). It also
  carries `merged_content`, and `side_content`, which is the kept side's
  body for the reviewer's preview only and never compared. `validate()`
  keeps exactly these fields and refuses a `side` resolution without a
  well-formed hash or a malformed `sides`. A resolution with no open
  conflict is refused at submit. On apply, sides that changed since the
  item was submitted give 409 `STALE` **even when forced**. A conflict
  resolved meanwhile gives 410 `TARGET_GONE`.
- **Release binding** (`_page_resolution`). A `side` resolution's wire
  `title` + `content` + `cover_image_url` must hash to exactly the item's
  `side`. A `merged_content` resolution carries the item's text under the
  held title and cover. `mine` / `theirs` (two-way, pre-v_48) leave the
  content uncompared.
- **Free keys.** The host-sequencing bookkeeping is free on
  `SPACE_PAGE_CREATED` / `SPACE_PAGE_UPDATED`: `seq`, `version_hash`,
  `conflict`, `sequenced`, `last_editor_user_id`, `cover_image_url`,
  `updated_at`. The rules still compare `cover_image_url` whenever the
  wire carries it. `ancestors` (an earlier v_48 draft) is no longer free.
- **Member check.** Members don't re-run the access level on a host
  version. When the moderation block is present they still run the
  release check against the item they hold.

A create's owner-bound id still binds to the author. A plain member's
create (or edit of someone else's row) without a valid block is refused
under `moderated` on every v_43 receiver — posts included, whatever
version the sender advertises.

## Sequence

```mermaid
sequenceDiagram
    participant C as Member @ C (submitter household)
    participant H as Host H
    participant B as Moderator @ B
    participant M as Member households
    C->>C: POST /api/spaces/{id}/tasks/... (tasks_access = moderated)
    C->>C: store own copy (pending) → 202 queued
    C->>H: SPACE_MODERATION_SUBMITTED {item, payload} (sealed, targeted)
    C->>B: SPACE_MODERATION_SUBMITTED {item, payload} (sealed, targeted)
    Note over C,M: plain member households receive nothing
    H->>H: checks 1–7 → hold copy, moderation_pending bell
    B->>B: checks 1–7 → hold copy, moderation_pending bell
    B->>B: moderator approves → gates ✓ → row "publishing"
    B->>H: SPACE_MODERATION_DECIDED {approved, decided_by: B's moderator}
    H->>H: approver seat ✓, gates ✓ → apply FROM H's OWN COPY (release scope)
    H->>M: SPACE_TASK_CREATED {created_by: submitter, moderation: {item_id, approved_by}}
    H->>B: SPACE_TASK_CREATED {…, moderation}
    H->>C: SPACE_TASK_CREATED {…, moderation}
    C->>C: may_author_approved: sender is host, our user → our item, exact content ✓
    H->>B: SPACE_MODERATION_DECIDED {approved}
    H->>C: SPACE_MODERATION_DECIDED {approved} → moderation_decided bell
```

## Reports (`SPACE_REPORT`, `SPACE_REPORT_DECIDED`)

A **report** about content inside a space — a post, comment, page, task,
sticky, calendar event or gallery item — or about a member's conduct in it,
is triaged by the space's **content authority** (owner, admins,
moderators), never by household admins: a household admin is not a seat in
the space, and a space's reports are as private as its content. A household
admin who holds no seat sees nothing of a space's reports (`403` on
`GET /api/spaces/{id}/reports`). Household-level reports (feed content, a
user outside any space, a space itself, highlights, moments) stay with
household admins (`/api/admin/reports`). A space report never gates the
reported user's household relay (`relay_policy` counts household-level
reports only).

The report's space is **derived from the target** on every household — the
reporter's household and each receiver look the item up themselves. A
client names a space only for a member report (`target_type: "user"`), and
the reported user must then hold a live seat in it. A report on an id this
household does not hold is refused exactly like one in a space the reporter
is not in (the same 404 body) and nothing is stored — no existence oracle.

### Event types

- `SPACE_REPORT` — reporter's household → host + reviewer households
  (v_45+). Reader event (`SPACE_READER_EVENT_TYPES`): a report is about
  content, not content.
- `SPACE_REPORT_DECIDED` — the deciding household → the other reviewer
  households (v_45+): `{space_id, target_type, target_id,
  reporter_user_id, decision: resolved|dismissed, decided_by,
  decided_at}`, all inside the sealed payload. Reader event; in
  `SPACE_SESSION_ALLOWED_EVENT_TYPES` (a link-joined moderator decides).
  The rows are matched on `(space, target, reporter)` — every household
  holds its own row id.

### Rules

- **Delivery.** `SPACE_REPORT` goes to the space's **host** and every
  household holding a **live `admin` or `moderator` seat** at **v_45 or
  above** — one targeted send each (`send_with_mesh_fallback`, sealed under
  `SPACE_ROUTED` across a relay). Plain member households, households below
  v_45 and the GFS never receive it. Nor does a household whose **only** live
  admin / moderator seats belong to the report's subject — a report about
  X never lands where only X could read it (the host always gets it). Plaintext on the envelope is the
  routing `space_id`; target, category, notes and reporter ride inside the
  encrypted payload (which repeats `space_id`).
- **Receiver rules for a report** (each refusal stores nothing; WARNING
  except the first): this household reviews the space (host, or ≥1 local
  content-authority member) — a misdirected report is dropped at INFO; a
  routing `space_id` that disagrees with the payload copy is dropped; the
  target resolves into that same space here (a cross-space or unknown id
  is dropped); the reporter holds a live seat on the **sending** household
  and is not banned; for a member report, the reported user holds a live
  seat in the space; caps — 20 pending per (space, sending household,
  reporter), 50 per (space, sending household), 500 per space; notes are
  cut at 1000 characters. One report per (reporter, target, space) — a
  replay is a no-op. A report **without** `space_id` (household-level)
  must name a reporter who is a user of the sending household.
- **Receiver rules for a decision:** the sender has content authority in
  the space (`has_content_authority`) and the named decider holds a
  content-authority seat on it (`moderates_as`); the decider is not the
  report's subject; the first decision wins — a replay or a later contrary
  verdict changes nothing.
- **No self-triage.** A report's *subject* — the reported member, or the
  reported item's author / creator — never sees it in the queue, cannot
  resolve it (404) and is not notified. Exception: the space **owner when
  the space has no other content authority anywhere** (no other local
  owner / admin / moderator, no remote admin / moderator seat). Otherwise
  that report could never be cleared, so the owner sees it with the
  reporter hidden (`anonymous`: no reporter id, household, name or
  **notes** — their own words could name them) and may only **dismiss** it
  (`dismiss_only`; resolving → 403) — the reporter cannot be retaliated
  against. Eligibility is **pinned when the report is filed**
  (`content_reports.sole_reviewer_user_id`): if anyone else held content
  authority then, the owner never gets the fallback — demoting everyone
  afterwards does not unlock it — and it must still hold at review time
  (a moderator promoted later takes the report over).
- **Resolution** uses the existing powers — a moderator deletes the post /
  comment / page through the ordinary delete, which federates as usual —
  then marks the report resolved or dismissed, which is synced with
  `SPACE_REPORT_DECIDED`.
- **Notification.** Each reviewing household tells its local content
  authority (except the reporter and the subject, per the rule above) with
  a title-only bell entry and push, `New report in {space_name}`, linking
  to the space's Moderation tab (`/spaces/{id}?tab=moderation`) — never the
  category, notes or reporter (§25.3).
- **Purge.** When this household stops reviewing a space (its last
  content-authority member demoted or gone) it deletes the reports other
  households filed there; a dissolved space's reports all go.
- **Re-delivery.** When the host promotes a remote seat to admin /
  moderator (that household reviews again — a temporary demotion purged
  its copies), the host re-sends the space's pending reports to it over
  the same `SPACE_REPORT` path (v_45 only; the receiver's dedupe makes a
  repeat a no-op). The payload names the reporter's own household
  (`reporter_instance_id`, sealed); the receiver trusts it **only from the
  host** — the reporter must hold a live seat on that household, and the
  per-household caps and the stored `reporter_instance_id` key on it, so a
  relayed report never counts against the host. From any other sender the
  field is ignored and the sender is the origin (it may only send reports
  by its own members). A re-delivery skips a household whose only reviewer
  is the report's subject, too.
- **GFS.** The automatic fraud forward fires for space content only when
  the space is `public` / `global`, and for a reported user (as their
  household) or a public / global space itself. What the GFS gets: the
  target, the category and this household's signed identity
  (`reporter_instance_id` + signature, which it needs to verify the report)
  — **never the reporter user or the notes**. Feed content and anything in
  a private / household space never reaches a connection server.

```mermaid
sequenceDiagram
    participant A as Reporter household (member)
    participant H as Host
    participant C as Moderator household
    participant D as Plain member household
    A->>A: POST /api/reports — derive space from target, store (space_id)
    A->>H: SPACE_REPORT {space_id, target, category, notes, reporter} (sealed)
    A->>C: SPACE_REPORT (sealed)
    Note over D: never sent
    H->>H: reviews here? reporter seated on A? not banned? target in space? caps?
    C->>C: same checks, store
    H-->>H: notify owner / admins / moderators except the subject (title only)
    C-->>C: notify local moderator (title only)
    C->>C: POST /api/spaces/{id}/reports/{rid}/resolve
    C->>H: SPACE_REPORT_DECIDED {space_id, target, reporter, decision, decided_by} (sealed)
    H->>H: C has content authority? decider seated? not the subject? still pending?
    Note over D: never sent
```

**Compatibility (v_45, `MIN_FOR_SPACE_REPORT_SCOPE`).** Gated, no
fallback: a reviewer household below v_45 is sent neither event — it would
file the report for its household admins. A pre-v_45 sender omits
`space_id` and fans a report out to every member household; a v_45
receiver derives the space from the target and drops it unless it reviews
the space.

### Known residuals (reports)

- **N4 — a reviewer household's decision is trusted.** Any household with
  a live admin / moderator seat can resolve or dismiss a report for every
  reviewer (first decision wins). A rogue moderator household can clear
  reports about its own members or allies; the remedy is the owner demoting it (its
  copies are purged, later reports no longer reach it). Decisions are not
  re-checked against the content.
- **N5 — every reviewer household learns who reported.** The reporter's
  user id and notes ride (sealed) to the host and every admin / moderator
  household, and are stored there. A moderator on another household can
  therefore see who reported a member of theirs. Only the anonymous
  sole-owner fallback hides the reporter, and only from the subject.
## Compatibility

- **v_43** (`MIN_FOR_FEDERATED_MODERATION`). Submissions skip a reviewer
  household below v_43. A host below v_43 makes a stub's submit fail
  **409 `HOST_TOO_OLD`** (nothing stored, nothing sent). Setting any feature
  to `moderated` while a member household is below v_43 answers 409
  `PEERS_TOO_OLD` until applied anyway (`force`). A v_42 receiver refuses a
  release from a non-host household (it never learnt the block) — content
  approved off the host reaches v_43 households only. A household below
  v_43 that takes a seat in a space keeping a non-post feature Reviewed
  gets the local admins the `moderation_unavailable` notice.
- A v_42 host's release (actor = approver, no block) is still admitted from
  the host (`release_ok`).

## Residuals

- **The host is trusted to publish what it holds.** Only the host releases
  items, and a plain member household — which holds no item — accepts the
  host's release on the host's word, as it accepts the host's relay of any
  member's row today. The submitter's own household and every reviewer
  household refuse a release that differs from their copy in any field.
  Because the host applies from its own copy, a submitter that sent
  different payloads to different reviewers gains nothing: only the host's
  copy is ever published.
- **Approval needs the host online.** A reviewer household's approval is a
  request to the host (queued in the outbox while the host is
  unreachable); the item reads "publishing" there until the host's
  announcement arrives. If the host refuses it (the item expired, the
  level changed), the row stays pending and the moderator may decide again.
- **Concurrent edits.** A household holding an edit item whose target was
  changed meanwhile by another write refuses the release (its copy no
  longer matches); the host's §25.6 sync converges it.
- **Submission flooding.** A household can keep up to 50 pending items per
  space at each reviewer household (and each submitter 20), each kept at
  most 14 days; the caps bound storage, not the review burden.
- **An approved event does not RSVP its creator** when approved on a
  household that is not the creator's (the approver can't speak for them),
  and its feed card is not queued in their name there.
- **Report decisions reach v_45 households only.** A reviewer household
  below v_45 receives neither the report nor its decision.
- **A sole-authority owner may dismiss a report about themself** (the
  reporter hidden). The rule trades a self-dismiss for a report that would
  otherwise sit forever; members who want an outside view can leave or
  report the space itself to their household admin.
- **Clock skew around `expires_at`**: the approver checks its own copy's
  expiry; an author's household that already expired its copy refuses the
  release.

## Implementation

- `socialhome/services/space_moderation_federation.py` —
  `SpaceModerationFederation`: targets, sends, the two inbound handlers and
  their receiver checks.
- `socialhome/services/space_moderation_service.py` — the queue: submit,
  approve (the host inside `release_scope`; elsewhere a release request),
  `release_remote`, reject, `store_received`, `apply_decision`,
  `note_release`, `record_early_decision`, `drop_held_for_others`.
- `socialhome/services/moderation_release.py` — the release scope and
  `with_release` used by every content outbound bridge.
- `socialhome/federation/moderation_approval.py` — which events carry a
  release, and the item ↔ wire content digest.
- `socialhome/federation/space_authorship.py` —
  `SpaceAuthorship.may_author_approved`, the approval path of
  `access_admits`.
- `socialhome/services/report_service.py` / `report_scope.py` — space-scoped
  reports: scope derivation, `SPACE_REPORT` targets and receiver checks,
  the content-authority triage.
- Tests: `tests/protocol/test_space_moderation_federated.py` (four real
  households), `tests/protocol/test_space_report_scope.py` (reports, four
  real households), `tests/services/test_space_moderation_federation.py`,
  `tests/federation/test_moderation_approval.py`.

## Spec refs

§4.3 (feature access levels, `moderated`), §24.11 (inbound pipeline and
authorship), §25.8.21 (encryption-first), §D2b (link-joined households).
