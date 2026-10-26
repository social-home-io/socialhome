# Public Momentum (§Momentum-public)

Opt-in extension to the [Momentum](momentum.md) pillar that lets a
registered user fan their moments out beyond the household-paired
mesh, brokered by a GFS. Followers across the public internet
discover authors via the GFS directory, follow them explicitly, and
receive every subsequent moment over the same persistent SH↔GFS
WebSocket the rest of the public-pillar surface (Highlights) already
uses.

## Scope

* Per-user opt-in. The author of each moment chooses whether to fan
  it out via their registered GFSes; an opt-out checkbox at compose
  time overrides the per-user default.
* Multi-GFS publishing. One user can register on several GFSes at
  once. The fan-out posts to each in turn.
* Follower-side gate. The GFS only forwards a moment to households
  that have at least one follower of the author. Strangers see
  nothing.
* **No re-distribute rule.** A moment that arrived via a GFS
  fan-out is *not* relayed back into the recipient's paired
  household mesh. The federation outbound (`MomentFederationOutbound.relay_inbound`)
  reads `received_via='gfs'` on the inbound payload and skips relay.
* A reply goes public only under a public moment. A reply to a moment
  that isn't public is stored as not public whatever the composer sent
  (`MomentService.create_moment` — the thread's root decides), and
  `MomentPublicOutbound` checks the parent again before publishing; the
  composer offers the toggle on a reply only when the parent is public
  and never pre-ticks it ("share by default" is for new moments). A
  [share link](#share-links) for a reply checks the parent the same way
  (`MomentPublicShareService.link`, fail closed), and migration `0123`
  cleared `is_public` on this household's replies the old composer stored
  public under a moment that isn't. "Public" is what this household
  stores. A public moment that reached it through a paired household
  first is held as that household copy (`is_public = 0`); when the
  verified GFS copy arrives it is not stored or announced again, but the
  held row is marked public (`SqliteMomentRepo.mark_public` — only the
  flag; a row authored here or a tombstone never changes), so a reply to
  it can be public. A household that holds the moment only through
  pairing (it follows nobody on a GFS) keeps it not public, and its
  replies stay with paired households.
* Public replies fan out **both** through the GFS path and through the
  replier's paired households (normal moment federation). A follower
  that does not hold the parent drops a GFS-delivered reply (INFO) — it
  would be out of context, and its parent reference cannot be stored.
* Author-side moments still fan out to paired households via the
  existing federation alongside the GFS path. Recipients dedupe by
  `moment.id` (the `moments` PRIMARY KEY makes the second save a
  no-op).

## Encryption posture (v_62)

Per §25.8.21 (encryption-first) **the GFS never reads a public moment.**
It stays **content-blind — not blind**: it still holds the follow graph,
author and moment ids, key ids, timing and sizes, and the plaintext author
directory. Nothing more is claimed.

* **Keys.** Each author's household keeps one random 32-byte root per
  *generation* (`key_gen` = Unix seconds at mint, never repeats). Step keys
  ratchet forward once a day — `K(g, s+1) = HKDF(K(g, s),
  "sh-moment-ratchet:v1")` — and every household keeps only its newest step
  key (forward secrecy at rest, at zero delivery cost). Each moment is sealed
  under its own **item key** `HKDF(K(g, s), "sh-moment-item:v1|" +
  moment_id)`, so one moment's key (a [share link](#share-links)) opens
  that moment and nothing else. See [Moment keys](#moment-keys-v_62).
* **Envelope.** Exactly `{author_user_id, instance_id, moment_id, key_gen,
  key_step, seal_suite, ciphertext, signature}`
  (`socialhome/moment_public_seal.py`). `seal_suite: "aesgcm-256"` —
  AES-256-GCM, fresh 96-bit nonce, associated data binding suite, author,
  generation, step and moment id. Inside: `op` (create / delete), text,
  media reference, duration, parent, origin, timestamps, `profile_rev`, and
  the author's profile only when it changed. The author's household signs
  the whole envelope (ciphertext + key id + moment id). The same signature
  authenticates the publish to the GFS and is forwarded intact.
* **One upload, one ciphertext.** A moment is sealed and signed once; the
  same bytes go to every registered GFS, and each GFS fans those bytes out
  to every follower household. Nothing is sealed per follower when a moment
  is published.
* **The GFS refuses anything else.** A publish or delete with any field
  beyond those eight is `422 encryption_required` and is never fanned out;
  only the household the author registered from may publish as them
  (`403 instance_mismatch`); ≤ 16 KB of ciphertext.
* **Only households that can open it.** A follow request declares (signed)
  what the follower household can open: `moment_suites: {kem, seal}`. The
  GFS fans a moment out only to households that declared the seal suite,
  and the author seals keys only to households that declared both the KEM
  suite of their key-wrap key and the seal suite. A household re-declares
  its suites on each (re)connect (`POST /gfs/moments/suites`), so follows
  made before it upgraded start receiving. An older household
  declared nothing: it is sent nothing, and the author's sharing settings
  count it ("can't read your public moments yet").
* **Keys reach each follower household sealed to it alone**, through the
  GFS, which keeps the latest one per (author, household) in a mailbox it
  can't open — see [Moment keys](#moment-keys-v_62).
* **Receivers** verify the signature against the author's
  `home_instance_pk` cached in `moment_public_follows` at follow time, then
  open the envelope with the step key for `(key_gen, key_step)`. A bad
  signature, an unknown suite, a wrong key, or a sealed `op` / author that
  doesn't match the frame drops it. A **plaintext envelope** (an older
  author household's) is refused, never stored. A create whose
  `expires_at` has passed or whose `created_at` is older than a moment can
  live (30 days) is refused **before** anything is read from it — a replayed
  old envelope can neither re-store an expired moment nor roll a profile
  back (review finding M7).
* **Lifetime (v_65).** A public moment lives as long as its author chose
  for followers — `moments.follower_retention_days`, 1–30 days, 7 by
  default — sealed as `expires_at`. A follower keeps it at most
  `created_at + 30 days` (an in-range value byte for byte) and holds a
  delete for a moment it never received 30 days. Following through a GFS
  is a follow: the moment stays in the follower's inbox until `expires_at`,
  not just 24 h (`moment_public_follows` counts like `user_follows`). An
  older (v_64) follower keeps whatever the author sealed and holds a
  never-seen delete 7 days. The GFS's own windows (the 25 h `sent_at`
  check, the 24 h offline queue, the seen cache) are delivery windows, not
  the moment's life, and are unchanged. A delete a follower was away for
  reaches it later through the GFS's 30-day certificate log
  ([v_66](#late-deletes-delete-certificates-v_66)).
* **Signed send time (v_64).** Every sealed body — create and delete —
  carries `sent_at`, the Unix second the author's household signed the
  envelope. It is inside the ciphertext the envelope signature covers (and
  bound by the AEAD), so nobody else can set, shift or strip it, and the
  GFS sees no new field. A follower refuses an envelope sent more than
  25 h ago (`SENT_AT_MAX_AGE_S`: the GFS's 24 h offline queue + the 10 min
  key wait + skew — a captured envelope re-posted later), one more than
  5 min ahead of its clock, and a create whose `created_at` is after its
  `sent_at`. Once an author's envelope carried `sent_at`, one of theirs
  without it is refused (monotonic per author, in memory). A moment
  already held is never stored or announced twice — a replay inside the
  window, a second GFS, or the household relay having delivered it first.
  **Older senders:** a v_63 household's envelopes have no `sent_at` and are
  judged by the rules above; a v_63 follower ignores the field. **Cost:**
  ~25 bytes inside each ciphertext; no extra message, no new plaintext
  field, nothing stored.

**What stays readable on purpose: the author directory.** An author opts into
a GFS's public directory — `display_name`, `username`, `bio`, avatar and
`home_instance_pk` (`gfs_user_registrations`, `gfs_user_pictures`). That is
the discovery surface strangers find someone to follow by, so the GFS serves
it to anyone. No moment content, caption or media reference is in it.

**No plaintext fallback.** A household publishes public moments only to a GFS
whose signed `/gfs/info` capability block proves `encrypted_moments`. Any
other GFS gets **no public moments at all** — one WARNING per server, and the
author's sharing settings show it (`GET /api/moments/public/registrations` →
`delivery: "unsupported_server"`).

**Owner decision 2026-10-20 — who learns what.** The author's household
learns which households follow it (it already got `follow_changed` with the
follower's instance id) and seals to each household's key-wrap key directly.
An opaque-handle alternative (the author never learning follower ids) was
weighed: without an identity the author can verify, a GFS could substitute
its own key-wrap key for every follower and read silently — strictly weaker.

**Residuals.**

* *Public means anyone may follow* — the GFS operator too, from a registered
  household; that follow is visible to the author. What the GFS cannot do is
  read moments *without* following.
* *Trust on first use of the author key* at follow time (unchanged).
* *Metadata* — the follow graph, which author posts when, moment ids, key
  ids, sizes, timing, when keys are delivered.
* *Media* (v_63) — the GFS stores each moment file as padded ciphertext it
  can't open: it learns a size bucket, the blob's address and who fetches
  it when. See [Encrypted media](#encrypted-media-v_63).
* *Share links* — a moment opened through its share link is fetched from
  the GFS by the viewer: the GFS learns that blob was fetched, from which
  address, when. It never sees the key (URL fragment). Whoever holds a link
  can open that one moment until it expires; a link can't be revoked short of
  deleting the moment. The pinned viewer's host is trusted to serve the page
  whose hash the link names (SRI-pinned bundle, see
  [highlights_public.md](highlights_public.md)). See [Share links](#share-links).

## Replays at the GFS

`sent_at` is sealed, so the GFS cannot read it. Without more, anyone holding
an envelope (every follower household gets the signed bytes) could re-post
it to `POST /gfs/moments/publish` after the GFS's old ten-minute duplicate
memory and have it fanned out to every follower again, who all then drop it.
Nothing is stored twice, but the bandwidth is amplified. A popular author's
moment re-posted once is 10 000 followers × ≤ 16 KB ≈ 160 MB, and it could
be repeated every ten minutes, forever. Two checks close this. Neither adds
a wire field, and neither tells the GFS anything new:

* **The key step must be today's.** `key_gen` is the generation's mint time
  and `key_step` its day (`RATCHET_STEP_S`). Both are plaintext routing
  fields the GFS already reads, so it already knew the day an envelope was
  sealed in. An author's household always seals under the current step, so
  the GFS refuses an envelope whose step day (`key_gen + key_step × 86 400`
  to one day later) is more than an hour (`MOMENT_STEP_SLACK_S`) away from
  its own clock: `409 stale_envelope`, nothing fanned out. That hour is the
  clock disagreement a household may have. It also means an envelope is
  accepted at most 25 h after it was sealed, the same window a follower
  accepts it in.
* **Inside that window the same bytes count once.** Each node remembers the
  digest of every moment it fanned out, forwarded or was forwarded for
  25 h (`MOMENT_SEEN_TTL_S`, one step plus the slack). That is the same
  cache the cluster relay uses for `followers` items, so a re-post to any
  node is a no-op (`delivered: 0`).

Together: a captured envelope is fanned out once, never again.

**Cost.** No message, no wire field, no database row. Memory is ~140 B per
remembered moment per node, capped at 200 000 (`MOMENT_SEEN_MAX`, ~28 MiB
when full):

| GFS size | Public moments / day | Remembered (25 h) | Memory per node |
|---|---|---|---|
| Typical: 2 000 authors × 5 | 10 000 | ~10 400 | ~1.5 MiB |
| Large: 50 000 authors × 3 | 150 000 | ~156 000 | ~22 MiB |
| Past the cap | > ~190 000 | 200 000 | ~28 MiB |

Before, it was 20 000 entries × 10 min in each of two caches.

**Older households:** a v_62/v_63 author seals under the current step like
v_64, so nothing changes for it. A household whose clock is more than an
hour off has its publish refused with a WARNING in its log (followers
already refuse an envelope dated more than 5 min ahead).

**Residuals.**
* A restarted node has forgotten what it saw, and a node in a cluster that
  missed a forward has nothing to remember. So a captured envelope can still
  be fanned out once more per node restart, or once per node that never saw
  it, until its step day ends.
* Past the cap the oldest digests go first. One household can't get there
  alone: publishes and deletes share a budget of **30 a minute per author
  household, cluster-wide** (`MOMENT_PUBLISHES_PER_MINUTE`; each node allows
  its `node_share` over the live nodes; `429 rate_limited`,
  `Retry-After: 60`). That is at most ~45 000 envelopes in 25 h, under a
  quarter of the 200 000-entry cache. A real household is far below it:
  each user can post a top-level moment at most once per 15 min, so even ten
  busy users replying fast stay at a few a minute. Several registered
  households acting together that publish more than 200 000 envelopes
  within 25 h could still make a node forget other authors' moments early.
  Each of those could then be re-posted once, until its step day ends.

The alternative was a send time in plaintext for the GFS to check. It was
rejected: it would be one more field the GFS reads, it would need a protocol
bump, and older GFSs refuse unknown envelope fields.

## Late deletes: delete certificates (v_66)

A public moment may live 30 days at a follower (v_65), but its delete crossed
a GFS only live, or from the 24 h offline queue. A follower household offline
longer kept showing a moment its author had deleted. Owner decision 6c and
sign-off 4 (2026-10-25): **the GFS keeps each public delete's signed
certificate for 30 days** and hands it to a follower household when it
reconnects.

**The certificate is the mesh delete's own signature.** A `MOMENT_DELETED`
already carries the origin household's Ed25519 identity signature over
`b"moment-origin:v1:"` + canonical JSON of `{event_type, sig_suite,
moment_id, author_user_id, origin_instance_id, occurred_at}`
(`socialhome/moment_origin.py`). Those four fields plus `origin_identity_pk`,
`origin_sig_suite` and `origin_sig` are, on their own, a self-verifying
delete certificate: who deleted which moment and when — no content. No new
key, no new signature format, no new suite (the suite tag is the existing
`origin_sig_suite`; an unknown one is refused).

* **Author household.** On a public moment's delete it posts, to every GFS
  whose signed `/gfs/info` proves `moment_delete_log`, `POST
  /gfs/moments/delete_cert` signed by the household: `{instance_id, cert}`.
  One signature, the same body to every such GFS. The sealed delete
  (`/gfs/moments/delete`) still goes as before. A post that fails for a
  reason a later try can fix — transport error, 408, 429 (its `Retry-After`
  honoured), 5xx, and 404 / 422 (an older node of a cluster mid-upgrade
  behind a server that advertised the log) — is retried with backoff (1 min,
  5 min, 15 min, then hourly) for up to 24 h after the delete, the same
  signed body each time (`services/moment_delete_cert_retry.py`, in memory,
  ≤ 1 000 queued, ≤ 500 per GFS). Any other answer (409, 403) is final. A
  moment that was never
  public reaches no GFS at all when it is deleted — neither the sealed
  delete nor a certificate (`MomentDeleted.is_public`).
* **GFS.** Keeps it only when it checks out against the key that household
  registered with (`delete_cert_refusal`): exactly the seven fields, each a
  string ≤ 256 chars, `origin_identity_pk` = the sender's registered key and
  deriving to `origin_instance_id` = the sender, a known suite, a signature
  over a *delete* (a create's signature never passes). The author must be
  registered here by that household (else 403), the delete time within the
  last 25 h — a day of the author's retries plus slack (else `409
  stale_cert`, so a captured old certificate can't restart its 30 days) —
  and someone must follow the author here (else it is
  accepted but not kept). One row per (author, moment), `INSERT OR IGNORE` —
  a replay changes nothing. Kept in `gfs_moment_delete_log` (GFS migration
  0024) and pruned 30 days after it was written (maintenance sweep).
* **Reconnect.** When a household's `/gfs/ws` hello is accepted, the node
  that took it drains what the household is owed from the shared log:
  certificates of authors a user of that household follows, written after
  that follow began, within the last 30 days. What is owed is tracked per
  (household, author) by a delivery cursor (`gfs_moment_delete_cursor`,
  GFS migration 0024): a push first opens a cursor for every followed
  author with certificates since the household was last seen
  (`relay_seen_at`, refreshed hourly while its socket is open, minus 2 h;
  a household never seen: the whole 30 days), moves it past each
  `moment_delete_certs` frame (≤ 50) that went out, and closes it once that
  author is drained. The seen mark only decides where a *new* cursor
  starts — an open one ignores it — so a capped push, a socket that drops
  mid-push, or a flood never makes the GFS skip a certificate. One push
  carries ≤ 200, ≤ 100 of one author, dealt round-robin (each author's
  oldest owed first, the newest of each round first — a certificate
  written weeks ago is about a moment that has nearly run out anyway);
  while more is owed and the socket stays open, another push follows a
  minute later. A followed author posting certificates for made-up ids
  (up to 1 000) delays only its own.
* **Follower.** Checks each certificate again — against the key it pinned
  when it followed (`moment_public_follows.followed_instance_pk`), never one
  riding with the certificate — refuses one older than 31 days or dated ahead, and
  applies it exactly like a delete through the GFS: owner-bound ids, the
  stored row's author and origin win, a moment held is wiped to a tombstone
  (and `MomentDeleted` published), an id never held gets a 30-day tombstone
  so a later create is refused, and a tombstone already held changes
  nothing.

The GFS can neither forge nor alter a certificate; it can only withhold one,
as it can withhold any delete. What it learns beyond the sealed delete is
the delete time, which it already saw as the time of the POST.

**Cluster.** The log lives in the shared database and the push runs on the
node holding the household's socket, so no forward is needed.

### Sequence: a follower that was away catches up

```mermaid
sequenceDiagram
  participant A as Author SH
  participant G as GFS
  participant B as Follower SH (offline 4 days)

  A->>A: author deletes a public moment
  A->>G: POST /gfs/moments/delete (sealed, as before)
  G-->>G: B has no socket, not seen in 24 h — nothing queued
  A->>A: sign MOMENT_DELETED origin fields (moment-origin:v1)
  A->>G: POST /gfs/moments/delete_cert {instance_id, cert} (signed)
  opt the post failed (down, 5xx, 429, 404 / 422 from an old node)
    A->>G: same body again — backoff, for up to 24 h after the delete
  end
  G->>G: cert checks out against A's registered key? A registers the author? fresh? someone follows?
  G->>G: INSERT OR IGNORE gfs_moment_delete_log (30 days)
  Note over B: four days later
  B->>G: /gfs/ws hello
  G->>G: open a cursor per followed author with certs since B was last seen − 2 h
  G->>G: owed past each cursor: ≤ 100 per author, ≤ 200 in all, round-robin
  loop each frame of ≤ 50
    G->>B: WS moment_delete_certs {certs: [≤ 50]}
    G->>G: move each author's cursor past it (close it when drained)
  end
  Note over G: more owed and the socket still open? push again in a minute
  B->>B: verify against pinned followed_instance_pk · ≤ 31 days old
  B->>B: tombstone the moment · MomentDeleted on the bus
```

### Bounds and cost

* **Rate:** 30 certificate posts a minute per author household, across the
  cluster (`DELETE_CERT_POSTS_PER_MINUTE`, each node its `node_share`;
  `429 rate_limited`, `Retry-After: 60`) — the same budget as publishes and
  deletes, counted apart so a delete's two posts don't halve it.
* **Size:** body ≤ 4 KB (413 past it), each field ≤ 256 chars. A real
  certificate is ~0.35 KB.
* **Storage:** at most 1 000 live certificates per author
  (`MAX_DELETE_CERTS_PER_AUTHOR`, `429 author_delete_quota` past it; the
  live delete still goes), so ≤ ~350 KB per author. A GFS with 50 000
  authors each deleting 3 moments a month holds ~150 000 rows ≈ 52 MB.
* **Per delete:** one extra POST of ~0.6 KB per GFS the author publishes
  through — not per follower. A failing GFS gets at most ~27 tries of it
  over 24 h (1 min, 5 min, 15 min, then hourly), then nothing.
* **Per push:** ≤ 200 certificates ≈ 70 KB, ≤ 100 of one author, only
  what that household is owed; one push a minute while more is owed and
  its socket stays open. The cursor table holds a row only per (household,
  author) with something still owed — written when a push finds some,
  moved per frame (≤ 4 writes per author per push), deleted when drained,
  pruned 30 days after last moved. Worst realistic case: a household
  following 200 authors who each deleted one moment while it was away —
  200 certificates in one push, 200 cursor rows opened and closed. Worst
  hostile case: a followed author posting its whole 1 000-certificate
  allowance of made-up ids (34 minutes at 30 a minute) — ≈ 350 KB to each
  follower household that comes back, in ten pushes over ~10 minutes,
  while every other author's deletes ride in the first push. Typical:
  none.

**Older households and servers.** Gated on the GFS's signed capability,
never on a peer version. A GFS that doesn't prove `moment_delete_log` gets
no certificate (its followers get the live delete or the 24 h queue, as
before). A v_65 follower ignores the unknown frame. A v_65 author sends no
certificate.

**Residuals (best effort, signed off 2026-10-25, see
[`principles.md`](../principles.md)).** A certificate lives 30 days. One
whose post kept failing for 24 h — or whose author household restarted
while it was still failing (the retry queue is in memory) — is not kept.
The 30-day GFS log, the per-push caps and the cursor bound the work, never
what is eventually handed over: a household is owed everything it missed
until each certificate's 30 days run out. An author who leaves the
directory takes their certificates with them (and their follows, so nobody
would be pushed them).

### Media lives as long as its moment (v_66)

A share link's viewer fetches a moment's file from the GFS for as long as
the moment lives, but a blob was kept at most 7 days (a GFS refused more
than 8), so a link to a 30-day moment lost its picture after a week. A GFS
that proves the signed `moment_media_30d` capability now keeps a blob (and a
share copy) up to 31 days. The author's household asks for the moment's
own expiry, at most 30 days there and 7 days anywhere else
(`MomentPublicMediaOutbound.keep_until`). The per-author cap of 256 MiB of
live ciphertext (`MAX_AUTHOR_LIVE_BYTES`) still bounds what one author
holds, so a GFS's worst case does not change; only an author who picks a
longer lifetime holds more of it. An author posting one 1 MB picture a day
with a 30-day lifetime holds ~30 MB. Past the cap an upload is refused and
that GFS's followers get the moment without its file.

**A cluster mid-upgrade.** `/gfs/info` may prove `moment_media_30d` while
the node that takes a `PUT` is still an older one, which answers `422` to an
expiry past 8 days. The author's household then asks again for a week (the
same blob, the rest of that moment's blobs too) instead of failing the
publish; only a share link outliving that week loses its picture.

## Reactions through the GFS (v_68)

A reaction to a public moment that came through a GFS stayed in the
follower's household: the author and the follower are not paired, and
households met through a GFS never talk directly. Owner decision 9,
sign-off 1 (2026-10-25): **the reaction goes back through the GFS the moment
came on, sealed end to end to the author's household.** It is the same
sealed reaction as the mesh's routed reactions
([`momentum.md` § Routed reactions](momentum.md#routed-reactions-v_67)) —
the same `moment-reaction:v1:` household signature, padding and key-wrap
seal, the same checks at the author — only the transport differs.

* **The author's key-wrap key.** Every sealed create body names the author
  household's existing static key-wrap key: `author_keywrap_pk` (64 hex) +
  `author_keywrap_suite` (`"x25519"`, the `kem_suite` vocabulary). Inside the
  ciphertext the envelope signature covers, so the GFS sees nothing new and
  can't swap it (~75 bytes per moment). A follower keeps it from a create
  that verified against the pinned `followed_instance_pk` and opened, on the
  follow rows of that author through that GFS
  (`moment_public_follows.followed_keywrap_pk`, migration `0126`); an
  unknown suite or a malformed key is ignored — never read under a default —
  and that author's moments keep their reactions at home.
* **Follower household.** A reaction (or its removal) to a public moment
  that came through a GFS (`received_via = 'gfs'`) is coalesced per
  (moment, person) for 3 s, signed, padded to 1 KB and sealed to that key,
  then posted — only to the GFS the moment came on
  (`received_via_gfs_id`), only when that GFS's signed `/gfs/info` proves
  `moment_reactions`, and only when the author's household named its key —
  as `POST /gfs/moments/users/{author}/react` with exactly `{instance_id,
  sealed}`, signed by the household. A paired origin still takes the direct
  `MOMENT_REACTED` path under the per-pair hide list. A post that fails for
  a reason a later try can fix (transport error, 404 from an older node
  mid-upgrade, 408, 429 with its `Retry-After`, 5xx) is retried with backoff
  (1 min, 5 min, 15 min, then hourly) for up to 24 h — in memory (no outbox:
  it is keyed by a paired household), ≤ 1 000 queued, ≤ 500 per GFS, the
  latest state per (moment, person) replacing the pending one, the same
  signed bytes each time. 403 / 422 are final. Otherwise the reaction stays
  in the household and the API says `reactions_reach: "household"`.
* **GFS.** Takes it only from a household that follows the author there
  (any of its users; never the author's own household — else `403
  not_following`), only in that exact shape (anything next to the seal —
  a moment id, an emoji, a user — is `422 invalid_reaction`: the GFS must
  have nothing to read), body ≤ 4 KB, `sealed.ciphertext` ≤ 3 000 chars.
  It can't open the seal. Limits, cluster-wide (each node its
  `node_share`): ≤ 30 a minute per (follower household, author), ≤ 120 a
  minute per follower household (`429 rate_limited`, `Retry-After: 60`); a
  retried post of the same seal within 10 minutes is answered `202`, not
  pushed again and not charged. It pushes `moment_reaction {from_instance,
  sealed}` to the **author's household only** — `from_instance` is the
  household it authenticated, which it already knows — on this node, or
  forwarded once to the node holding that socket (cluster `instance`
  audience), or queued 24 h in that household's relay queue when no node
  holds it. Queued reactions are a capped kind of relay row: at most 60
  per author household at a time (`RELAY_QUEUE_MAX_REACTIONS_PER_RECIPIENT`,
  ~240 KiB, over the queue's 24 h life), the oldest queued reaction making
  room for a newer one — and a reaction **never evicts** that household's
  queued public moments or space items: when those fill its 250-row / 4 MiB
  queue, or the server-wide relay cap is reached, the reaction is dropped
  instead (#1179 F1 — anyone who follows a public author can send
  reactions the GFS can't open). Nothing is charged for a reaction that
  reached the author's socket on any node (#1179 F2). `202` either way:
  whether the author is online is not told back.
* **Author household.** Hands the seal to the routed-reaction origin
  checks: it opens with this household's key-wrap key, the reactor's
  household key derives to its instance id (and equals the pinned key when
  paired), the user id derives from that key and its anchor, the signature
  verifies under a known suite, the moment is live and authored here, the
  reactor is neither local, blocked by the author, behind a guardian block
  nor on a banned household, the reaction is under 25 h old and not a
  replay, and it is newer than the stored reaction and removal watermark
  (`moment_reaction_removals`). Plus, for a GFS frame: `from_instance` is
  the reactor's own household (a follower can't pass off another
  household's reaction), and the moment is public (only a public moment
  ever crossed a GFS). ≤ 600 frames a minute per GFS connection before any
  crypto; the reactor household's ≤ 60 a minute is checked after every
  other check passed and charged only for a reaction that changed something
  (#1178 F2, #1179 F3): a replay — refused while the in-memory replay cache
  holds it, and after a restart a no-op against the stored reaction and
  removal watermark — costs that household nothing. Nothing goes back.

### Sequence: a follower reacts to a public moment

```mermaid
sequenceDiagram
  participant F as Follower SH
  participant G as GFS (the one the moment came on)
  participant N as Other GFS node
  participant A as Author SH

  A->>G: POST /gfs/moments/publish (sealed body names author_keywrap_pk)
  G->>F: WS incoming_public_moment
  F->>F: verify (pinned key) · open · keep followed_keywrap_pk
  Note over F: a person reacts (or removes it) — 3 s debounce
  F->>F: sign moment-reaction:v1 · pad to 1 KB · seal to author_keywrap_pk
  F->>G: POST /gfs/moments/users/{author}/react {instance_id, sealed} (signed)
  G->>G: F follows the author here? not A itself? shape? not a retry? limits?
  G-->>F: 202 (online or not is not said)
  alt A's socket is on this node
    G->>A: WS moment_reaction {from_instance: F, sealed}
  else on another node
    G->>N: NODE_DRAIN_HINT (instance audience, A)
    N->>A: WS moment_reaction {from_instance: F, sealed}
  else no node holds it
    G->>G: queue 24 h (≤ 60 reactions per author household, never evicting its moments / space items)
    Note over A: reconnects
    G->>A: WS moment_reaction (drained)
  end
  A->>A: open · verify · F is the reactor's household · public · live · not blocked / banned · not a replay · newer
  A->>A: apply_reaction_at · charge the reactor's budget only if it changed · MomentReactionChanged
```

### Bounds and cost

* **Per reaction:** one post of ~1.7 KB (the ~1.4 K-char padded seal, the
  household id and its signature) and one push of ~1.6 KB, to one
  household. No fan-out.
* **Worst realistic case:** a popular author's moment gets 200 reactions
  from 200 follower households — 200 × (~1.7 KB up + ~1.6 KB push) ≈
  0.65 MB through the GFS, 0.32 MB into the author's household; a burst of
  changes by one person within 3 s is one post.
* **Per create:** +~75 bytes inside each public moment's ciphertext.
* **Hostile:** one follower household is held to 120 posts a minute
  (30 per author); an offline author household's queue holds at most 60
  reactions (~240 KiB) beside — never instead of — its moments and space
  items, so any number of followers flooding it only churns reactions; the
  author's household opens 600 a minute per GFS.

**Older households and servers.** Gated on the GFS's signed capability and
on the key's presence, never on a peer version (author and follower are not
paired). An older author names no key — reactions to its moments stay home.
An older GFS proves no `moment_reactions` — nothing is posted to it. An
older follower ignores the new body fields. An older author never gets the
frame (it named no key). An older node of a cluster that gets the forwarded
item refuses the unknown frame type (reports nothing held), so the origin
node queues it.

**Residual (signed off 2026-10-25, see
[`principles.md`](../principles.md#sign-off-a-gfs-learns-that-a-follower-household-reacted-to-an-author-v_68)).**
The GFS learns that follower household F sent author A's household a
reaction or a removal, and when and how often. It is not told which person,
which emoji, which moment, or whether it was an add or a removal (the seal
is padded to one size) — but it knows which of A's moments it pushed to F
and when, so a post soon after one, or while A has a single live moment,
points at that moment, and quick successive posts hint at an add and its
removal. It could withhold or delay one, as it can any moment; it can't
forge, alter or replay one past the author's checks.

## Reaction totals through the GFS (v_69)

Owner decision 10 (2026-10-25), GFS part — the mesh part is in
[`momentum.md`](momentum.md#reaction-totals-v_69). Only the author's
household holds every reaction to its public moment, so it sends the
**totals** — per-emoji counts (8 most used) and the total, never who — to
the GFSs its followers read the moment on:

* **When:** at **+10 min, +3 h and +24 h** after the moment was made (the
  last only while it is still in its window) and **only when they changed**
  since the last GFS send; after a restart, one checkpoint for each own live
  public moment of the last 24 h that has reactions. At most 8 posts a tick
  (≥ 60 s apart — under the GFS's 10 a minute); the rest wait for the next
  ticks. A checkpoint counts as sent to a GFS only on a `2xx`: one that
  answered `429` or failed is posted again after 5 min — only that GFS, at
  most 3 posts a checkpoint — then left to the next checkpoint, which goes to
  every GFS even if the totals didn't change since.
* **Where:** every GFS the author is registered on that proves the signed
  `encrypted_moments` **and** `moment_counts` capabilities; an older GFS gets
  nothing (its followers keep their own counts).
* **Shape:** `POST /gfs/moments/counts` with exactly the eight envelope
  fields of a publish — the totals are sealed: `{op: "counts",
  author_user_id, rev, counts, total, sent_at}` under the item key of
  **today's** step for that moment id (followers keep only the newest step
  key, as for a delete). The body is **padded to exactly 640 bytes**
  (`COUNTS_BODY_BYTES`, a `_pad` key inside the AEAD; the largest possible
  body — eight 32-byte emoji at 1 000 000 each — is 499), so every totals
  ciphertext is the same length: the GFS reads no count, and can't tell one
  reaction from a million, or one emoji from eight, by size either.
* **GFS:** checks the shape, that the signer is the household the author
  registered from, that the step is today's, and its seen cache (a retried
  post is a no-op that spends nothing); then its budgets, cluster-wide:
  **10 a minute per author household** (each node its share) and **3 a day
  per moment** (every node also counts the updates it is forwarded, so the
  cap holds whichever node a post lands on). It pushes
  `{type: "incoming_public_moment_counts", payload: <envelope>}` only to the
  follower households that declared `ops: ["counts"]` and are **connected**
  — on this node, and on the others through the cluster audience
  `count_followers` — and **never queues it**: a follower offline now gets
  the next checkpoint, or keeps its own count. Bounded residuals of the
  cluster-wide day cap: two posts of one moment landing on two nodes at the
  same moment both pass before either is forwarded (≤ one extra per node,
  and the author sends at most 3 + a retried one anyway); a node whose
  forward buffer is full forwards nothing, so the others don't count that
  one; and during a rolling upgrade an older node ignores the
  `count_followers` audience — it pushes nothing to its followers and counts
  nothing, so the cap is per node (≤ 3 × nodes a day) until it is upgraded.
  None of these reaches a follower that didn't ask, or queues anything.
* **Follower:** declares `ops: ["counts"]` (in its follow and its suites
  declaration) only to a GFS that proved `moment_counts` — an older one
  refuses the unknown key — and declares again once a server it follows
  through starts proving it. It keeps totals only when the envelope verifies
  against the author's pinned key, opens under a step key **it already
  holds** — without one it drops the update and asks for no key (no request
  storm; the next moment brings it) — names `op: "counts"` and that author,
  was sent within 25 h, and the moment is held here by that author and
  household (live, older totals only) — the same
  `moments.reaction_summary` as the mesh, compared by `rev`.

### Sequence: totals reach connected followers

```mermaid
sequenceDiagram
    participant A as Author household
    participant G as GFS node 1
    participant G2 as GFS node 2
    participant F as Follower (ops: counts, online)
    participant F2 as Follower (offline)
    Note over A: +10 min, totals changed
    A->>G: POST /gfs/moments/counts {8 envelope fields}
    G->>G: author's household? today's step? seen? budgets
    G-->>A: 202
    G->>F: incoming_public_moment_counts {payload}
    G->>G2: NODE_DRAIN_HINT relays [("count_followers", author)]
    G2->>G2: push to its connected followers — never queued
    Note over F2: gets nothing — the next checkpoint, or its own count
    F->>F: verify, open with the step key it holds, keep if newer
```

### Bounds and cost

Measured with the real encoders: a counts post is **1.22 KB** and a pushed
frame **1.27 KB**, whatever the totals (padded; unpadded they were
0.55–0.70 KB and their length told the GFS roughly how many emoji and how
big). An author with **10 000 follower households** and 200 reactions in the
first hour: the moment is ~12 MB through the GFS; each totals update ≤
10 000 × 1.27 KB ≈ **12.7 MB** (≈ 7.6 MB with 60 % connected), at most 3 a
moment and only on change — typically 2 ≈ 25 MB. A refused post is retried
to that GFS only (≤ 2 more 1.22 KB posts a checkpoint, no push). Pushing
every reaction instead: 200 × 10 000 × 1.27 KB ≈ 2.5 GB. The GFS keeps no totals at rest; the seen
cache and the per-moment counters are in memory, bounded like the publish
cache.

**What the GFS learns** ([`principles.md`](../principles.md)): that moment
M has at least one reaction and its totals changed before checkpoint *k* (an
update came) — not the counts, and not their size.

## Event types

| Event type (federation) | Direction | Purpose |
|---|---|---|
| `MOMENT_PUBLIC_FOLLOW` | GFS → author | Follower count tick. |
| `MOMENT_PUBLIC_UNFOLLOW` | GFS → author | Follower count tick. |

The moment payload itself rides as a **WS frame**, not a federation
event — the GFS is the validated middlebox and the recipient
verifies the author's signature directly.

| WS frame type | Direction | Carries |
|---|---|---|
| `moment_public` | author SH → GFS (HTTP `POST /gfs/moments/publish`) | Signed, encrypted moment envelope. |
| `moment_public_delete` | author SH → GFS (HTTP `POST /gfs/moments/delete`) | Signed, encrypted tombstone. |
| `incoming_public_moment` | GFS → follower SH | Forwarded envelope, signature intact — only to households that declared the seal suite. |
| `incoming_public_moment_delete` | GFS → follower SH | Forwarded tombstone. |
| `moment_delete_certs` | GFS → follower SH (v_66) | `{certs: [≤ 50 delete certificates]}` on reconnect — only of authors that household follows, what it is owed past its per-author delivery cursor, ≤ 200 a push (≤ 100 of one author), a push a minute until drained. See [Late deletes](#late-deletes-delete-certificates-v_66). An older household ignores the frame. |
| `follow_changed` | GFS → author SH | A follower came or went. `action: "add"` (v_62) carries `follower: {instance_id, identity_public_key, keywrap_public_key, kem_suite, keywrap_sig, moment_suites}`; `action: "remove"` carries `follower_instance_id` and `household_still_follows`. |
| `moment_key_request` | GFS → author SH (v_62) | A follower household lacks the key for `want_gen` and the mailbox had none: `{followed_user_id, want_gen, follower_user_ids, follower}`. Only for a household that follows; ≤ 2 a minute per (household, author). |
| `moment_reaction` | GFS → author SH (v_68) | `{from_instance, sealed}` — a follower household's sealed reaction (or removal) to one of the author's public moments; `from_instance` is the household the GFS authenticated. To the author's household only (this node, the node holding its socket, or its 24 h queue). See [Reactions through the GFS](#reactions-through-the-gfs-v_68). An older household ignores the frame. |
| `incoming_public_moment_counts` | GFS → follower SH (v_69) | `{payload: <envelope>}` — an author household's encrypted reaction totals (`op: "counts"`), only to connected follower households that declared `ops: ["counts"]`; never queued. See [Reaction totals](#reaction-totals-through-the-gfs-v_69). An older household ignores the frame. |
| `moment_key` | GFS → follower SH (v_62) | One sealed key `{author_user_id, instance_id, to, key_gen, key_step, key_suite, sealed: {kem_suite, eph_pk, ciphertext}, signature}`, pushed to the `to` household only — at once, or from the mailbox when it reconnects. |

## Wire endpoints

### GFS-side

| Method | Path | Purpose |
|---|---|---|
| POST   | `/gfs/moments/users/register` | Signed body — register a user. `user_id` ≤ 128 printable chars (the cluster `followers` audience key bound). |
| POST   | `/gfs/moments/users/{user_id}/deregister` | Signed body — drop a registration. |
| POST   | `/gfs/moments/users/{user_id}/follow` | Signed body — record a follower; v_62 optional `moment_suites: {kem: [...], seal: [...]}` (≤ 8 names of ≤ 64 chars each; 422 otherwise); v_69 optional `ops` (only `"counts"`; any other name, or `ops` to an older server, is 422). |
| POST   | `/gfs/moments/users/{user_id}/unfollow` | Signed body — drop a follower. |
| POST   | `/gfs/moments/users/{user_id}/picture` | Signed body — push avatar bytes. |
| POST   | `/gfs/moments/publish` | Signed, encrypted envelope — fan out. Anything but the eight envelope fields: `422 encryption_required`. |
| POST   | `/gfs/moments/delete` | Signed, encrypted tombstone — fan out the delete. Same shape rule. |
| POST   | `/gfs/moments/delete_cert` | v_66. Signed by the author's household: `{instance_id, cert}` — the delete's origin certificate (`moment_id, author_user_id, origin_instance_id, occurred_at, origin_identity_pk, origin_sig_suite, origin_sig`). Kept 30 days for followers that were away. `422 invalid_cert` (shape, key, suite, signature), `403 instance_mismatch`, `409 stale_cert` (delete time not within the last 25 h), `413` past 4 KB, `429 rate_limited` / `author_delete_quota`. Returns `{kept}`. |
| PUT / GET | `/gfs/moments/media/{blob_id}` | v_63. `PUT` header-signed by the author's household: one encrypted, padded blob addressed by its SHA-256 (≤ 8 MB + overhead; expiry ≤ 31 days ahead since v_66 — `moment_media_30d`, 8 before; 422 for a wrong address / size / expiry, 429 over the author's quota). `GET` anonymous, immutable, CORS `*`: the ciphertext. |
| POST   | `/gfs/moments/media/{blob_id}/delete` | v_63. Signed by the author's household: withdraw a blob (its moment was deleted). |
| POST   | `/gfs/moments/suites` | v_62. Signed by a follower household: `{instance_id, moment_suites: {kem, seal[, ops]}}` (v_69 `ops: ["counts"]` only to a server proving `moment_counts`, declared again once it does) — what it can open, recorded on every follow it holds here (a follow made before it upgraded declared nothing). Sent once per process per server on (re)connect, only when a local user follows someone there. Returns `{updated}`. |
| POST   | `/gfs/moments/keys` | v_62. Signed by the author's household: `{instance_id, deliveries: [≤ 100 sealed keys]}`. Each is kept (latest per author + household) and pushed to its `to` household only while it follows the author. |
| POST   | `/gfs/moments/users/{user_id}/key_request` | v_62. Signed by a follower household: `{instance_id, want_gen}`. Always `202`; answered from the mailbox, or forwarded to the author, only for a follower, throttled. |
| POST   | `/gfs/moments/users/{user_id}/react` | v_68. Signed by a follower household: exactly `{instance_id, sealed}` — a sealed reaction to one of the author's public moments, pushed (`moment_reaction`) to the author's household only. `202` (whether the author was online is not said), `403 not_following` (not a follower household here, or the author's own), `422 invalid_reaction` (any other field, a malformed seal), `413` past 4 KB, `429 rate_limited` (30 / min per household + author, 120 / min per household, cluster-wide). Advertised as the signed `moment_reactions` capability. |
| POST   | `/gfs/moments/counts` | v_69. Signed by the author's household: exactly the eight envelope fields (`op: "counts"` sealed under today's step). Pushed as `incoming_public_moment_counts` to connected followers that declared `ops: ["counts"]` — never queued. `202` (also for a retried post), `403 instance_mismatch`, `409 stale_envelope` (an earlier day's step), `413` past 4 KB, `422 encryption_required`, `429 rate_limited` (10 / min per author household, 3 / day per moment, cluster-wide). Advertised as the signed `moment_counts` capability. |
| POST   | `/gfs/moments/users/{user_id}/follower_stats` | v_62. Signed by the author's household: `{followers, households, households_unable}` — counts only. |
| GET    | `/gfs/moments/users` | Public JSON directory. |
| GET    | `/gfs/moments/users/{user_id}` | Public JSON per-user detail. |
| GET    | `/gfs/moments/users/{user_id}/picture` | Public avatar bytes. |
| GET    | `/moments` | Public HTML directory. |
| GET    | `/moments/{user_id}` | Per-user public HTML landing: the person's profile and a Follow button — **no moments** (owner decision 2026-10-20; a moment opens only through its [share link](#share-links)). |

### SH-side (auth-gated)

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/moments/public/registrations` | Caller's registrations, each with `delivery` (`"encrypted"` / `"unsupported_server"`) and `followers_unable` (follower households on an older Social Home, sent none; `null` when unknown). |
| POST   | `/api/moments/public/registrations` | Register on a GFS. |
| DELETE | `/api/moments/public/registrations/{gfs_id}` | Deregister. |
| PATCH  | `/api/moments/public/registrations/{gfs_id}` | Toggle `default_share`. |
| GET    | `/api/moments/public/follows` | Caller's GFS follows. |
| POST   | `/api/moments/public/follows` | Follow another author. |
| DELETE | `/api/moments/public/follows/{gfs_id}/{user_id}` | Unfollow. |
| GET    | `/api/gfs/{gfs_id}/moments/users` | Proxy the GFS directory. |
| POST   | `/api/moments/{moment_id}/share-link` | The author's own public moment only. Optional body `{gfs_id}`. Uploads the 4 KB share copy on first use (per GFS) and returns `{url}` — the [share link](#share-links). `404` for someone else's / unknown / non-public moment or a reply under a moment that isn't public, `409 SHARE_LINK_UNAVAILABLE` without a viewer address or a GFS that keeps encrypted media. |

## Sequence: discover + follow (+ the key, v_62)

```mermaid
sequenceDiagram
  participant B as Follower SH
  participant G as GFS
  participant A as Author SH

  B->>G: GET /gfs/moments/users (anonymous)
  G-->>B: directory listing (incl. home_instance_pk per user)
  B->>G: POST /gfs/moments/users/{user_id}/follow {…, moment_suites} (signed by B)
  G->>G: persist gfs_moment_follows row (+ moment_suites)
  G->>B: response carries the author's directory entry
  B->>B: cache row in moment_public_follows
  G->>A: WS follow_changed (add, follower = B's identity + key-wrap key + suites)
  A->>A: suites match? verify_keywrap_binding? (else seal nothing)
  A->>A: seal current step key to B's key-wrap key, sign the delivery
  A->>G: POST /gfs/moments/keys {deliveries:[…]} (batched, signed)
  G->>G: B follows the author? keep it in the mailbox (latest per author + B)
  alt B online
    G->>B: WS moment_key (to B only)
  else B offline
    Note over G: pushed when B's socket comes back (undelivered only)
  end
  B->>B: verify author signature (pinned pk), open_keywrap, store KEK-sealed
```

## Sequence: publish + fan-out

```mermaid
sequenceDiagram
  participant A as Author SH
  participant G as GFS
  participant B as Follower SH
  participant P as A's paired peer

  A->>A: User posts moment (is_public=1)
  par Household path (existing federation)
    A->>P: MOMENT_CREATED via paired-instance relay
  and GFS path (encrypted, v_62)
    A->>A: step key for today → item key(moment_id) → seal {op, moment, profile_rev[, profile]} ONCE
    A->>G: POST /gfs/moments/publish {author_user_id, instance_id, moment_id, key_gen, key_step, seal_suite, ciphertext, signature}
    G->>G: shape = the eight fields? signer = author's household? (else 422 / 403)
    loop per follower household that declared the seal suite
      G->>B: WS incoming_public_moment (the same ciphertext)
    end
    B->>B: verify signature · step key (ratchet forward) · open (op + author must match)
    B->>B: expired / older than 30 days? → refuse before reading anything
    B->>B: profile included? → refresh name / bio / picture digest
    B->>B: persist moment (received_via='gfs') · MomentCreated on the bus
    Note right of B: relay_inbound() short-circuits on received_via=='gfs'
  end
```

If A's paired-household mesh and B's mesh overlap (same household
pair), B receives the same ``moment_id`` twice — once via federation,
once via GFS. The recipient dedupes on the row's PRIMARY KEY; the
second save is a no-op.

## Cluster delivery

A GFS may run as several nodes on one shared database behind a round-robin
balancer, and each node holds only its own `/gfs/ws` sockets. Every push
above therefore reaches households on **every** node
(`MomentPublicRegistry`, `global_server/cluster_relay.py`):

* **Moments and tombstones.** The node that took `POST /gfs/moments/publish`
  (or `/delete`) pushes to the able followers connected to it, then forwards
  the envelope **once per peer node** inside the next coalesced
  `NODE_DRAIN_HINT` (`relays`, audience `followers` + the author's user id).
  Each node resolves the author's able follower households from the shared
  database and pushes to the sockets it holds; it remembers the frame (its
  own digest, 25 h — the cache the node's own publishes use), so no node
  pushes it twice and a retried or re-posted publish to any node is a no-op. Each socket lives on one node: each follower
  household gets the moment once.
* **Offline followers.** A follower household that held a socket in the last
  24 h (`relay_seen_at`) but has none on any node gets the moment queued in
  the shared relay queue — the same ciphertext, as its own
  `incoming_public_moment[_delete]` frame — and gets it when it reconnects,
  to any node (24 h = the delivery window; deleted on delivery; the per-household
  relay caps of 250 items / 4 MiB apply). The peers answer the hint with
  `held` (the followers whose socket they hold), so the accepting node
  queues only for the ones nobody holds. A household not seen in 24 h is
  sent nothing at rest. Before this, an offline follower never got the
  moment at all.
* **Sealed keys.** A key whose household has no socket on the node that took
  `POST /gfs/moments/keys` stays undelivered in the shared mailbox, and the
  household id rides the next drain hint; the node holding its socket pushes
  its undelivered keys and marks them delivered. A key request answered from
  the mailbox for a household elsewhere marks the kept key undelivered
  first, so the same path delivers it.
* **Author notices.** `follow_changed` and `moment_key_request` for an
  author household whose socket is on another node ride the drain hint as
  `instance` items; only the node holding that socket pushes them, as the
  same frame. They are not deduped — a repeat request a minute later
  arrives.

```mermaid
sequenceDiagram
  participant A as Author SH
  participant N1 as GFS node 1
  participant N2 as GFS node 2
  participant B as Follower SH (on node 2)
  participant C as Follower SH (offline)

  A->>N1: POST /gfs/moments/publish (encrypted envelope)
  N1->>N1: push to followers on node 1
  N1->>N2: NODE_DRAIN_HINT relays:[{aud:[followers, author], q, frame}] (once per peer)
  N2->>N2: resolve followers from the shared DB
  N2->>B: incoming_public_moment (same ciphertext)
  N2-->>N1: held:{followers:author: [B, …]}
  N1->>N1: queue for recently-seen followers nobody holds (C)
  Note over C: reconnects to any node → drained as incoming_public_moment
```

**Rolling upgrade.** An older node ignores the `followers` / `instance`
items and answers without `held` for them: the accepting node queues the
moment for every recently-seen follower it did not hear about, and the older
node drains those rows on the hint (an older drain sends a queued row as
`{type: "relay", **row}`, in which the row's own `type` wins — the same
frame). An older accepting node pushes only to its own sockets, as before.
No household change: the frames are the ones households already get.

**What the GFS learns.** Nothing new — the forwarded frame is the moment
ciphertext the node already holds, the audience is an author id the shared
database already holds, and `held` lists the cluster's own sockets. The only
new copy at rest is the offline one: ciphertext the GFS can't open, for a
household that already follows the author, for at most a day.

**Traffic** (N nodes, F able follower households, envelope ≈ 1.2 KB):

| Trigger | Cost |
|---|---|
| A moment | (N−1) forwards of ≈ 1.2 KB in coalesced hint frames (≤ 40 / min per peer); each peer: 1 follower read per author per frame + its ≈ F/N pushes; each peer's `held` reply ≈ F/N × 35 B. Queue rows: only the seen followers no node holds |
| Worst case: 10,000 follower households, 4 nodes, 200 moments / min | per peer ≈ 200 × 1.2 KB ≈ 240 KB of forwards and ≈ 40 frames, each answered with ≤ 87 KB of `held` per author in it; 0 queue rows for connected followers. Before: 7,500 followers missed each moment |
| An offline follower | ≤ 1 queue row (≈ 1.2 KB) per moment, only if seen in 24 h; bounded by 250 rows / 4 MiB per household and 256 MiB per server |
| A sealed key for a household on another node | 1 instance id (≈ 35 B) in the hint to each peer; 1 indexed mailbox read on the node holding it |
| A notice for an author on another node | (N−1) × ≈ 0.6 KB in the hint; 1 push |

## Moment keys (v_62)

* **Generation + ratchet.** `MomentPublicKeyOutbound.current_key` mints a
  generation root on first use (32 random bytes, KEK-sealed with `own |
  author | key_gen` as associated data) and ratchets the step key to today
  in place — the household keeps no older step. A generation older than
  400 steps starts a new one. Two generations are kept.
* **Delivery is targeted and kept.** A sealed key is `{author_user_id,
  instance_id, to, key_gen, key_step, key_suite, sealed, signature}`:
  `sealed` is `seal_to_keywrap` to the `to` household's key-wrap key (fresh
  ephemeral X25519 per seal, `kem_suite` tagged), its plaintext `{author_user_id,
  key_gen, key_step, to, key_suite, key}` repeats the routing fields so a
  sealed blob can't be lifted into another delivery, `key_suite` is the
  space content-key tag `aesgcm-256`, and the author's household signs the
  whole delivery. The GFS keeps the latest delivery per (author, follower
  household) in `gfs_moment_key_mailbox` — ciphertext it can't open — and
  pushes it at once or when that household reconnects (undelivered rows
  only, ≤ 500 per connect). It never replaces a kept key with an older one
  (`key_gen`, then `key_step`): an author household only ever seals its
  current key, so an older delivery is a re-post of a captured signed
  `/gfs/moments/keys` body — e.g. by a CDN or proxy terminating TLS in front
  of the GFS, a different party from the GFS — and is neither kept nor
  pushed. The check is one conditional upsert; no wire change.
* **Anti-substitution.** The follower's key-wrap key comes from the GFS.
  The author seals only after `verify_keywrap_binding`: the instance id
  derives from the identity key, and the key-wrap key is self-signed by it.
* **The follower takes a key only** when a local user follows that author
  through that GFS, the delivery is signed by the author key that follow
  pinned and names the household that key derives from, `key_suite` is
  known, it opens with its own key-wrap key, the sealed plaintext matches
  the delivery, the author is not a local user, and it moves forward (a new
  generation, or a later step — never back, never a swap). Keys go when
  nobody here follows the author any more.
* **Three ways to get a key.** Pushed when the GFS names a new follower;
  kept in the GFS mailbox for a household that was offline; and pulled
  when a moment arrives under a generation the follower has no key for —
  the envelope is held (≤ 16 per author-generation, ≤ 256 in all, ≤ 10 min)
  and the follower asks (≤ 1 a minute per author); the GFS answers from the
  mailbox when it holds that generation, else forwards to the author (≤ 2
  a minute per household and author); the author seals at most once per
  household per key id per 30 s. Held envelopes open in order once the key
  lands.
* **Lazy rotation (a follower leaves, is blocked, a registration ends).**
  The key is per *household*, so a user unfollowing while another user of
  that household still follows changes nothing (the GFS's `follow_changed`
  `remove` says `household_still_follows`; an older GFS doesn't, and then
  none is assumed). Otherwise — and on a block by the author, or a
  deregistration — the author's current generation is only **marked**
  (`moment_public_keys.rotate_due`). The next **publish** starts a new
  generation, at most once per 6 h (`ROTATE_AT_MOST_EVERY_S`); sealing to a
  newcomer never rotates, and a mark with no new moment costs nothing
  (moments live a day). Many leaves between two publishes are one rotation.
  The remaining follower households then fetch the new key once each by the
  pull path (mailbox misses the new generation, so the author seals, ≤ 100
  per batch). What a household already holds stays readable to it; the
  next generation is not. The author also refuses to seal to a household
  whose following users (as the GFS names them) are all blocked. Routine
  forward secrecy needs no rotation: the daily ratchet is free.
* **Batched.** Sealed keys queue per GFS and go out in batches of ≤ 100
  half a second after the first — never one POST per follower, never
  blocking the GFS socket.

### Sequence: a follower without the key asks for it

```mermaid
sequenceDiagram
  participant B as Follower SH
  participant G as GFS
  participant A as Author SH

  G->>B: incoming_public_moment (key_gen = g)
  B->>B: no key for (author, g) → hold the envelope (bounded)
  B->>G: POST /gfs/moments/users/{author}/key_request {want_gen: g} (signed)
  G->>G: B follows? throttle (2/min per household + author)
  alt mailbox holds g (or newer) for B
    G->>B: moment_key (from the mailbox — the author isn't asked)
  else
    G->>A: moment_key_request {follower: B's keys + suites}
    A->>A: verify binding · throttle · seal
    A->>G: POST /gfs/moments/keys (batched)
    G->>B: moment_key
  end
  B->>B: import → open the held envelope(s) → store
```

### Traffic (worst realistic case: an author with 10,000 follower households)

Sizes: encrypted envelope ≈ 1.2 KB without the profile (≈ 1.5 KB with it),
sealed key delivery ≈ 0.7 KB, mailbox row ≈ 0.7 KB, key request ≈ 0.3 KB,
forwarded notice ≈ 0.6 KB.

| Trigger | Before (plaintext) | v_62 |
|---|---|---|
| A moment | 1 upload per GFS + 10,000 × ~1.2 KB ≈ 12 MB from the GFS | 1 upload per GFS + 10,000 × ~1.2 KB ≈ 12 MB — the same bytes to everyone; profile (+0.3 KB) only on change or once a day |
| A new follower | 1 notice | 1 notice (+0.6 KB keys) + 1 sealed key to that household ≈ 2 KB; 0.7 KB kept on the GFS |
| A follower reconnects | — | only its undelivered sealed keys (0 when nothing changed), plus the moments queued for it while it was away (see [Cluster delivery](#cluster-delivery)) |
| Every pre-v_62 follow | — | one `POST /gfs/moments/suites` per upgraded household per server (≈ 0.3 KB, once per process); then one key per household on its first encrypted moment (pull, ≈ 1.6 KB, ≤ 100 per batch from the author) — 10,000 × 1.9 KB ≈ 19 MB once, in 100 author POSTs. A household not yet upgraded is sent nothing |
| A follower without the key (lost mailbox, restore) | — | ≈ 1.6 KB, answered from the mailbox without the author when it can |
| GFS storage | — | ≤ 1 sealed key per (author, follower household): 10,000 × 0.7 KB ≈ 7 MB |
| Daily ratchet step | — | 0 bytes — every holder derives it |
| A household leaves / is blocked (lazy rotation) | — | 0 bytes at the leave; at the next publish ≥ 6 h later one new generation, then each remaining household pulls one key: 10,000 × ~1.6 KB ≈ 16 MB in 100 author POSTs — at most 4 rounds a day (≤ 64 MB/day worst case), however many households left. Re-sealing to every follower on every unfollow would be 10,000 × 1.6 KB per unfollow |

No trigger makes N households send to M: one follower is one sealed key, a
reconnect sends only what changed, and publishing seals nothing per
follower. Bounds, enforced by sender and receiver: envelope ≤ 16 KB
ciphertext; ≤ 100 deliveries / 128 KB per keys request; ≤ 1 KB per sealed
key; held envelopes ≤ 16 per author-generation, ≤ 256 total; ≤ 10,000
queued keys per GFS; ≤ 500 mailbox pushes per connect.

## Encrypted media (v_63)

Before v_63 a follower never got a public moment's photo or video: only the
author's `api/media/<name>` reference travelled, which names a file on the
author's household. Now the file itself travels — encrypted, once:

* **Seal once** (`socialhome/moment_public_media.py`,
  `MomentPublicMediaOutbound`). Key `HKDF(item_key, "sh-moment-media:v1|" +
  role)` — derived from the moment's item key, so whoever can open the
  moment can open its file and nobody else; `role` is `media` or `poster`
  (a video's still). Plaintext `u32 length ‖ file ‖ zeros` padded to the
  next bucket of 64 KB, 256 KB, 1 MB, 2 MB, 4 MB, 8 MB — the GFS learns a
  size bucket, not the size; files over **8 MB** are not sent (the moment
  goes without). AES-256-GCM, fresh nonce, AAD binding suite, author,
  moment id and role. `media_suite: "aesgcm-256-pad"` (unknown suites
  refused).
* **Address = SHA-256 of the blob.** The sealed moment names it:
  `media: {blob_id, media_suite, content_type, poster_blob_id?}`
  (`content_type` from an allow-list: WebP, JPEG, PNG, GIF, WebM, MP4). The
  author's own `media_url` no longer travels.
* **Upload once per GFS, before the moment.** `PUT
  /gfs/moments/media/{blob_id}`, header-signed by the author's household
  (`X-SH-Instance` / `-Author` / `-Expires` / `-Timestamp` / `-Signature` over
  `{instance_id, blob_id, author_user_id, expires_at, ts}`, ±300 s). Only to
  a GFS whose signed `/gfs/info` proves `encrypted_moment_media`. The GFS
  checks the signer is the author's registered household, the body's
  SHA-256 is the address, the size is a padded bucket, and the expiry is no
  later than 31 days out (8 before v_66). Followers fetch the file when the
  moment reaches them and keep their own copy, but a share link's viewer
  fetches it here as long as the moment lives, so the author asks for the
  moment's expiry — at most 30 days on a GFS that proves `moment_media_30d`,
  a week on an older one ([v_66](#media-lives-as-long-as-its-moment-v_66)).
  It caps each author at 256 MB live and 60 blobs an
  hour, and drops blobs when they expire, when the author deletes the moment
  (`POST /gfs/moments/media/{id}/delete`, signed) or deregisters.
* **A video still transcoding** is held by the author's household until
  `MediaTranscodeReady` (or sent without its file on `MediaTranscodeFailed`).
* **Fetch off the socket** (`MomentPublicMediaInbound`). A follower that
  opens a moment naming a blob queues a fetch (≤ 64 waiting) from the GFS
  the moment came through (`GET /gfs/moments/media/{id}` — anonymous,
  immutable, CORS `*` for a future share-link viewer), checks the SHA-256,
  opens it (AEAD bound to author, moment and role), keeps it as
  `gm-<id>.<ext>` (a poster as `gm-<id>.webp`, the name a video's poster is
  derived by) and only then stores the moment. Anything that doesn't check
  out stores the moment **without** a file — never a broken or foreign one.
  A delete removes the kept file; retention sweeps it with the moment (at
  its `expires_at` — up to 30 days, v_65).

```mermaid
sequenceDiagram
  participant A as Author SH
  participant G as GFS
  participant B as Follower SH

  A->>A: item key(moment) → media key → pad + seal the file ONCE → blob, sha256
  loop per GFS that proved encrypted_moment_media
    A->>G: PUT /gfs/moments/media/{sha256} (header-signed, ≤ 8 MB + overhead)
    G->>G: signer = author's household? sha256 matches? bucket size? quota? → keep (expires with the moment)
  end
  A->>G: POST /gfs/moments/publish (sealed body names media.blob_id)
  G->>B: incoming_public_moment
  B->>B: open the moment → queue the fetch (off the socket)
  B->>G: GET /gfs/moments/media/{sha256}
  G-->>B: ciphertext
  B->>B: sha256 ok · open (author, moment, role) · keep gm-<id>.webp → store the moment
```

**Traffic (N = 10,000 follower households, a 2 MB photo → 2 MB bucket):**
the author's uplink carries **2 MB once per GFS** (before: nothing reached
followers at all; a "stream it from home" design would be N × 2 MB = 20 GB
from the household). The GFS serves N × 2 MB = 20 GB to followers that open
it — fetched once per household, not per user, and only by households that
can open it; it stores 2 MB until the moment expires (≤ 30 days, v_66). Each
follower household keeps its own 2 MB for the author's chosen lifetime
(1–30 days, v_65). The moment frame
itself grows by ~0.2 KB. Worst case per author: 256 MB live on a GFS,
60 blobs/hour.

## Author profile: name, picture and bio

Followers met the author only through the GFS, so they hold no roster
row for them. The author's profile reaches them two ways — both carry
only what the author already published in the GFS public directory, so
the GFS learns nothing new:

* **At follow time** the `POST /gfs/moments/users/{user_id}/follow`
  response carries the directory entry: `display_name`, `bio` and
  `picture_digest` (plus `home_instance_pk`).
* **Inside moments, on change** (v_62): every sealed body names the
  author's `profile_rev` (a 16-hex digest of name, username, bio and
  picture digest); the `profile` itself — `display_name`, `username`, `bio`
  (only when it fits the directory's 280-character cap) and
  `picture_digest` — rides only the first moment after it changed, and at
  least once a day. `picture_digest` is the author's current picture hash
  only for a GFS whose `last_picture_digest` is that hash (that server
  mirrors exactly those bytes; otherwise `null`); a moment is sealed once
  per distinct digest — normally once for every GFS. All of it is inside
  the ciphertext and under the author's signature, so a GFS can neither
  read nor rename anyone.

The follower stores them on `moment_public_follows`
(`followed_display_name`, `followed_bio`, `followed_picture_digest`) —
refreshed only by a verified, live moment it has not seen, so a replayed
older envelope cannot roll a newer profile back. A moment without a
profile keeps the held values. Names are checked on the way in: blank,
overlong, or equal to the author's user id (the directory's fallback)
counts as no name, and the SPA shows "Someone" — never an id. A picture
digest must be 16 hex characters (the household's form) or the GFS's
64-hex SHA-256, which is shortened to the same 16.

`GET /api/moments`, `/archive` and `/{id}` name such an author from the
follow (`PersonSummaryService`, `gfs_via`): the name, a bio, and the
picture through the signed proxy `api/gfs/{gfs_id}/moments/users/{user_id}/picture`,
which fetches the bytes from the GFS the household is already connected
to — the browser never contacts the GFS. A guardian block (§CP.F2) still
withholds the picture and the bio. A protected (child) account never publishes
public moments, so it never reaches this path (§CP.R).

## Protected accounts never see them

A received public moment is one household-wide row. A protected (child)
account (§CP.R) never sees it — the per-viewer visibility rule
(`moment_repo._PROTECTED_CIRCLE_SQL`, part of the inbox rule the detail
read shares) leaves it out of the inbox, the archive, trending tags, a
moment's replies and the detail read (the unknown-id 404), and the
`moment.created` WebSocket frame (`MomentCreated.received_via = "gfs"`)
skips protected accounts. Notifications: a GFS-delivered moment carries no
parent author, so it never notifies anyone.

## Creator-bound moment ids (v_36)

A moment's id commits to its author (`federation/owner_bound_id.py`,
unscoped `moment` kind). The public inbound refuses an
`incoming_public_moment` or `incoming_public_moment_delete` naming a
bound id for any other author — validly signed or not — before it is
stored or tombstoned, so nobody can pre-empt another author's moment
id. See [`momentum.md`](./momentum.md).

**Stored row wins** — as on the household path: a moment id this
household already holds (live, or a delete's tombstone) keeps its author
and origin, whatever the id's shape. A public create or delete naming
another author or origin for it is refused at WARNING — never upserted
over the held row, never tombstoned. A delete's origin is its
`origin_instance_id`, else the sending household's `instance_id`; a
delete for a moment not held yet leaves a tombstone under that origin,
so a later create stays refused (the "deletes stick" rule).

## Signature canonicalisation

Every signed wire body — register, follow, unfollow, publish, delete
— uses the same canonical-JSON shape:

```python
canonical = json.dumps(
    body, separators=(",", ":"), sort_keys=True
).encode("utf-8")
```

The Ed25519 signature is computed over those bytes, base64url-encoded
(no padding), and appended to the body under the ``signature`` key.
The ``signature`` field is **excluded** from the canonical bytes —
the verifier strips it back out before re-running ``json.dumps`` to
compare.

* **Algorithm**: Ed25519 (per §25.8 instance-key suite).
* **Key**: the sending instance's federation identity key (32-byte
  Ed25519 seed). For follower-side calls (``follow`` / ``unfollow``)
  the follower's *own* instance signs; for publish / delete the
  author's instance signs.
* **Public key encoding**: 64-character lowercase hex on every
  GFS-stored or wire-carried form (``client_instances.public_key``,
  ``gfs_user_registrations.home_instance_pk``,
  ``moment_public_follows.followed_instance_pk``). The choice of hex
  for keys + base64url for signatures matches the existing
  Highlights flow.
* **Freshness**: the sealed, signed ``sent_at`` (v_64, above) bounds a
  replay to 25 h at the follower, which keeps one copy per ``moment_id``
  (unique per author). The GFS refuses a re-post before it reaches any
  follower — see [Replays at the GFS](#replays-at-the-gfs).

The recipient's verification is in
``socialhome/services/moment_public_inbound.py:_verify`` — single
``json.dumps(..., separators=(",",":"), sort_keys=True)`` call,
identical bytes-for-bytes to the sender's canonical encoding.

## DB schema (this PR ships into `0001_initial.sql`)

* `gfs_user_registrations` (GFS) — directory of opted-in users.
  Carries `bio` (≤280 chars) and `picture_digest` so the public
  directory landing renders cards without joining
  `gfs_user_pictures`.
* `gfs_user_pictures` (GFS) — avatar bytes mirrored from the home
  instance so the anon `/moments` SPA + `/moments/{id}` detail page
  can serve `<img src="…/picture?v=<digest>">` without round-
  tripping to the (often NAT-shielded) household.
* `gfs_moment_follows` (GFS) — follower graph keyed by
  `(follower_user_id, followed_user_id)`.
* `moment_public_registrations` (SH) — author-side opt-in per
  `(user_id, gfs_id)`, plus the `default_share` flag and
  `last_picture_digest` so the profile-sync flow skips redundant
  avatar uploads.
* `moment_public_follows` (SH) — follower-side cache, including the
  followed user's `home_instance_pk` for signature verification and
  (0112) their public `followed_bio` / `followed_picture_digest`.
* `moment_public_shares` (SH, 0119) — one row per public moment this
  household authored: the moment's item key (KEK-sealed, associated data
  the moment id), its media descriptor per GFS holding the file, and — once a link was asked for —
  the share copy's blob address and GFS. Cascades with the moment, so it is
  gone when the moment expires or is deleted. Never backed up or exported.
* `moment_public_keys` (SH, 0118, v_62) — moment keys, KEK-sealed, one
  row per (author, role, generation) with the step key advanced in place:
  `is_own = 1` minted here for local authors, `is_own = 0` delivered by
  followed authors. Never backed up or exported.
* `gfs_moment_follows.moment_suites` (GFS 0021, v_62) — what the follower
  household declared it can open; `gfs_moment_key_mailbox` (GFS 0021) —
  the latest sealed key per (author, follower household), which the GFS
  can't open.
* `moments.is_public` / `moments.received_via` /
  `moments.received_via_gfs_id` — provenance on every row so the
  inbox UI can render a "via {gfs}" chip and so the federation
  outbound can enforce the no-redistribute rule.

## Public directory + profile sync

* **Anon landing** at `/moments` (SPA shell) and `/moments/{user_id}`
  (per-user detail) — the directory rendered by the GFS itself; the JS at
  `/static/users_directory.js` fetches `GET /gfs/moments/users` and
  renders cards with avatar + name + bio + handle. The per-user page
  (profile + Follow) is a hand-off page to the pinned viewer when the GFS
  has `public_viewer_base_url` set — its link-preview tags carry the
  display name, bio and picture of an ACTIVE registration only
  (`global_server/link_preview.py`) — and the viewer renders it from
  `GET /gfs/pages/people/{user_id}` (picture inline as a `data:` URI) —
  see [the public pages](highlights_public.md#the-public-pages-one-viewer-for-the-outside-world).
* **Per-user detail JSON** at `GET /gfs/moments/users/{user_id}` —
  adds `follower_count` to the registration shape so the detail
  page can show social proof.
* **Server-side filter** `GET /gfs/moments/users?q=<substr>` matches
  `display_name` and `username` (`LIKE '%q%'`). Capped at 200
  rows; the SH-side proxy passes `q` through.
* **In-app Discover** at `/momentum/public/discover` — fetches
  `GET /api/gfs/{gfs_id}/moments/users` (SH proxy), rendering avatar
  via `GET /api/gfs/{gfs_id}/moments/users/{user_id}/picture` so the UI
  renders even when the home instance is NAT-shielded.
* **Profile sync** — `UserProfileUpdated` (published from
  `UserService.patch_profile` and `set_picture`) is consumed by
  `ProfileSyncService`, which calls
  `MomentPublicService.push_profile_to_gfs` once per active
  registration. Failures log + drop; reconcile happens on the
  next save.
* **Single identity** — there is no per-Momentum profile override
  in v1. What's in `users.{display_name, bio, picture_hash}` is
  what every paired GFS sees.

## Share links

Owner decision 2026-10-20: the GFS's `/moments/{user_id}` page shows the
person's profile (name, picture, bio) and a Follow button — **no moments**.
A stranger sees a public moment only through a link its author shares. The
old live moments index (WebRTC stream + GFS relay of plaintext moments,
`/gfs/moment_rtc/*`) is gone; an old `/moments/{user_id}` link now shows the
profile, and the person can ask the author for a link to a moment.

A share link opens the **pinned viewer** — the same static, SRI-pinned page
public highlights use (see [highlights_public.md](highlights_public.md)), not
served by any GFS — with everything in the URL fragment, which the browser
never sends to a server:

```
{viewer}/v/<hash>/#g=<GFS>&a=<author>&m=<moment>&b=<share copy>&k=<item key>
```

* **`k` is the moment's item key**, never the generation or step key — it
  opens exactly that moment (its share copy and its file) and nothing else
  the author published.
* **`b` is a share copy** — the moment's text, timestamps, the author's
  display name and the media descriptor, sealed under the item key
  (`moment_public_media.seal_share`, role `share`, `media_suite:
  "aesgcm-256-pad"`, padded to the 4 KB bucket) and stored on the GFS like any
  moment file (`PUT /gfs/moments/media/{blob_id}`, header-signed, expires with
  the moment). It is uploaded **only when the author asks for a link**, once
  per GFS — a moment nobody shares never has one.
* **The viewer** fetches the share copy and the moment's file from the GFS
  (`GET /gfs/moments/media/{blob_id}`, anonymous, CORS), checks each
  SHA-256 address, and opens them in the browser with WebCrypto
  (`client/gfs/moment_seal.ts`; a golden vector,
  `tests/fixtures/moment_share_vector.json`, pins the TypeScript and Python
  shapes together). A missing blob reads "no longer available"; anything that
  doesn't open reads "incomplete or damaged". Nothing is shown in the clear.
* **The GFS a link names always holds the moment's file.** A GFS that
  didn't take it at publish (registered later, or refused it then) gets it
  when the link is made: sealed again under the same item key — a new blob,
  ciphertext only — and uploaded once; the share copy names that blob. A
  file still processing mints no link yet (`409`); a file this build
  doesn't send (missing, over 8 MB) leaves the share copy without it, as for
  followers.
* **The author's household is never contacted** — it can be offline, and
  its uplink never carries a viewer's copy.
* **Keeping the key.** The item key comes from the day's ratchet step, which
  the household overwrites daily, so the household keeps it per public
  moment at publish (`moment_public_shares`, KEK-sealed), gone with the
  moment.

No protocol version bump: share links touch only the author's own household,
the GFS media store (v_63) and the pinned viewer.

### Sequence: a stranger opens a shared moment

```mermaid
sequenceDiagram
  autonumber
  participant U as Author (SPA)
  participant A as Author SH
  participant G as GFS
  participant P as Pinned viewer host
  participant V as Stranger (browser)

  U->>A: POST /api/moments/{id}/share-link
  A->>A: own public moment? unseal item key (KEK)
  opt no share copy on this GFS yet
    A->>A: seal_share(item key) → 4 KB blob
    A->>G: PUT /gfs/moments/media/{blob_id} (header-signed)
  end
  A-->>U: { url: viewer/v/<hash>/#g&a&m&b&k }
  U-->>V: shares the link (any channel)
  V->>P: GET /v/<hash>/ (fragment stays in the browser)
  P-->>V: pinned page (SRI-checked bundle)
  V->>G: GET /gfs/moments/media/{share blob}
  G-->>V: ciphertext
  V->>V: sha256 = b? open with k
  opt the moment has a photo or video
    V->>G: GET /gfs/moments/media/{media blob}
    G-->>V: ciphertext
    V->>V: sha256 check, open with k
  end
```

### Traffic

Per link: one 4 KB upload per (moment, GFS), only when the author asks —
plus, once, the moment's file (≤ 8 MB bucket) to a GFS that didn't take it
at publish.
Per view: the stranger's browser fetches 4 KB + the moment's file (at most
the 8 MB bucket) from the GFS; the author's household sends nothing. A
moment shared widely and viewed 10,000 times costs the author 4 KB once;
the GFS serves immutable, cacheable ciphertext.

## Implementation pointers

* SH author orchestration:
  `socialhome/services/moment_public_service.py`,
  `socialhome/services/moment_public_outbound.py`.
* SH recipient:
  `socialhome/services/moment_public_inbound.py`.
* Wire crypto (envelope + sealed-key shapes, suites, signatures):
  `socialhome/moment_public_seal.py`.
* Encrypted media (v_63): `socialhome/moment_public_media.py` (seal, pad,
  address), `socialhome/services/moment_public_media_outbound.py` (author:
  seal once, upload per GFS, withdraw), `socialhome/services/moment_public_media_inbound.py`
  (follower: fetch, check, open, keep), `socialhome/global_server/moment_media.py`
  (GFS store, quotas, expiry).
* Moment content keys — author side (mint, seal to followers, batched
  delivery): `socialhome/services/moment_public_key_outbound.py`;
  follower side (import, ask): `socialhome/services/moment_public_key_inbound.py`;
  storage: `socialhome/repositories/moment_public_repo.py`
  (`SqliteMomentPublicKeyRepo`, migration `0118_moment_public_keys.sql`).
* GFS broker:
  `socialhome/global_server/moment_public_registry.py`.
* GFS routes:
  `socialhome/global_server/routes/moments_public.py`.
* SH routes:
  `socialhome/routes/moments_public.py`.
* Relay guard:
  `socialhome/services/moment_federation_outbound.py:relay_inbound`.
* Share links: `socialhome/services/moment_public_share_service.py`
  (route `MomentShareLinkView`), storage `SqliteMomentPublicShareRepo`
  (migration `0119_moment_public_shares.sql`); viewer
  `client/gfs/public_moment.tsx`, `client/gfs/moment_link.ts`,
  `client/gfs/moment_seal.ts` in the pinned viewer bundle.
* Reactions through the GFS (v_68): follower side
  `socialhome/services/moment_reaction_gfs.py` (target, post, retry),
  sealing + author-side checks `socialhome/services/moment_reaction_router.py`
  (`on_gfs_reaction`), key kept by `moment_public_inbound.py`
  (`moment_public_follows.followed_keywrap_pk`, migration
  `0126_moment_public_follow_keywrap.sql`), GFS
  `MomentPublicRegistry.relay_reaction` + `GfsMomentReactView`.
* Privacy filter:
  `socialhome/repositories/moment_repo.py:list_public_for`.

## Spec refs

* §Momentum (the underlying pillar; this page extends it).
* §24.7 GFS pairing handshake.
* §24.12 SH↔GFS WebSocket transport.
* §25.8.21 Encryption-first invariant.
