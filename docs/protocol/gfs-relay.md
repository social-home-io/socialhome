# GFS relay for paired households

The last-resort transport between two **paired** households: when neither
the WebRTC DataChannel nor the HTTPS inbox reaches the peer, an ordinary
§24.11 envelope is sealed to the peer's static key-wrap key and handed to a
connection server (GFS) that **both** households use, which pushes it down
the peer's WebSocket. This page covers how the two households find those
shared servers (v_53) without either one ever naming a server to the
other. The relay wire shape itself (`{to_instance, sealed}`, size buckets)
is documented in [`invites.md`](./invites.md); the transport tiers are in
[`architecture.md`](../architecture.md#progressive-sync-42).

## Scope

- **HFS**: full participant. Per peer, the admin opts into the relay
  (`remote_instances.gfs_relay`, local-only, never federated). Each
  household probes the peer through each of its own servers, answers
  probes, and keeps its own list of confirmed routes
  (`peer_gfs_routes` — each route is THIS household's own
  `gfs_connections.id`).
- **GFS**: content-blind relay. It sees two more identity-free relay
  bodies per probe round and answers `202` to all of them, exactly as for
  any other relayed blob. It takes no part in discovery and cannot tell a
  probe from any other envelope.
- **Paired peers only** (`source = manual`). A household seated from an
  invite link (`space_session`) already rides the one server that
  introduced it; the probe and ack types are not in its allow-list, so the
  §24.11 peer-class gate refuses them.

## Event types

`GFS_RELAY_PROBE`, `GFS_RELAY_PROBE_ACK`; `SPACE_MAILBOX_UPDATED` (v_88, see
[Per-space mailboxes](#per-space-mailboxes-v_88)).

Both carry exactly `{nonce}` — 128 random bits, `secrets.token_urlsafe(16)`
— inside the AES-256-GCM federation payload. The plaintext envelope fields
are the usual routing set (`from_instance`, `to_instance`, `event_type`,
`msg_id`, `timestamp`, `space_id = null`); no server URL, server id,
connection id or inbox id appears anywhere, in the clear or encrypted
(pinned by `tests/protocol/test_gfs_relay_route_discovery.py`).

Gated on `FederationCapability.MIN_FOR_GFS_RELAY_ROUTES` (v_53): a peer
below it is never probed, holds no routes, and so never gets the relay
fallback.

## Flow — probe and ack

Household A uses servers X and Y; household B uses Y and Z. Only Y is
shared.

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(servers X, Y)
    participant X as GFS X
    participant Y as GFS Y
    participant Z as GFS Z
    participant B as HFS B<br/>(servers Y, Z)

    Note over A: eligible peer: confirmed, gfs_relay on,<br/>key-wrap key known, peer at v_53+
    A->>A: nonce1 → (B, conn A·X), nonce2 → (B, conn A·Y)<br/>(in memory, 15 min TTL)
    A->>X: POST /gfs/envelope {to_instance: B, sealed}<br/>(GFS_RELAY_PROBE {nonce1})
    X-->>A: 202 (uniform)
    Note over X: B never opens a socket here —<br/>the blob waits out the queue TTL
    A->>Y: POST /gfs/envelope {to_instance: B, sealed}<br/>(GFS_RELAY_PROBE {nonce2})
    Y-->>A: 202 (uniform)
    Y->>B: WS frame {sealed}
    B->>B: unseal, §24.11 pipeline (relay opt-in gate,<br/>sig, replay, decrypt)<br/>RELAY_DELIVERED_VIA = conn B·Y
    Note over B: no route recorded on receipt
    B->>Y: POST /gfs/envelope {to_instance: A, sealed}<br/>(GFS_RELAY_PROBE_ACK {nonce2}) — same server
    Y->>A: WS frame {sealed}
    A->>A: §24.11 pipeline, RELAY_DELIVERED_VIA = conn A·Y<br/>nonce2 pending, sent to B, via A·Y ✓
    A->>A: upsert route (B, conn A·Y), drop nonce2
    Note over B: probe back (throttled): nonce3 → (A, B·Y), nonce4 → (A, B·Z)
    B->>Y: GFS_RELAY_PROBE {nonce3}
    Y->>A: WS frame {sealed}
    A->>Y: GFS_RELAY_PROBE_ACK {nonce3} — same server
    Y->>B: WS frame {sealed}
    B->>B: nonce3 pending, sent to A, via B·Y ✓<br/>upsert route (A, conn B·Y)
    B->>Z: GFS_RELAY_PROBE {nonce4}
    Note over Z: A never opens a socket here —<br/>no ack, no route
    Note over A,B: later: relayed traffic round-robins<br/>over each side's routes (here: Y only)
```

### Rules

- **Probe only through our own servers.** A probes through each of its own
  active `gfs_connections` whose server proved the `envelope_relay`
  capability on its signed `/gfs/info` block — never through a server it is
  not registered with. The relay sender enforces the same (it resolves
  the URL against our own connections).
- **A route comes only from an ack to one of OUR OWN probes**, through the
  server that probe was sent on — or, once, from a pairing code that
  named the server ([Bootstrap route](#bootstrap-route-from-a-pairing-code)). Receiving a probe records nothing: the
  relay body is identity-free, so a malicious server the prober uses can
  re-post the sealed probe onto another server the receiver uses but the
  prober never reads. Had the receiver recorded that server, its relayed
  traffic would go where the relay answers `202` and nobody collects it —
  a silent loss the attacker could keep alive every round. Instead the
  receiver acks and **probes back** (throttled per peer), so both sides
  confirm their own direction.
- **A probe is answered only if it arrived over the relay.** The relay
  inbound leg (`services/gfs_relay_inbound.py`) binds
  `RELAY_DELIVERED_VIA` to B's own connection id for the duration of the
  dispatch; the handler reads it synchronously. A probe that arrived over
  RTC or the HTTPS inbox (or through a server that is not one of B's
  active connections) is ignored.
- **The ack goes back through the same server.** B answers through the
  connection that carried the probe, explicitly — not round-robin — so
  the ack tests the same server in the other direction.
- **The prober accepts an ack only if** the nonce is pending (not
  expired), was sent to the sender of the ack, and the ack arrived over
  the very connection the probe was sent through. Anything else is ignored
  at DEBUG and the nonce stays pending (a wrong-peer or wrong-server ack
  cannot burn the genuine one).
- **Both payloads are `{nonce}` only.** Each side stores only its own
  connection ids; the intersection is learnt by what arrived where, never
  by what was said.
- **A peer we did not opt in with cannot probe us.** The §24.11 relay
  opt-in gate (`make_check_relay_opt_in`) drops a relayed envelope from a
  paired peer whose `gfs_relay` is off before any handler runs — so no
  ack is sent.

### Round-robin

Once routes exist, `FederationTransport` reaches the paired peer
RTC → HTTPS inbox → relay, and the relay tier round-robins over the
peer's routes (resolved to the base URLs of our currently active
connections), trying each at most once per send. Probes and acks
themselves never round-robin: they use
`FederationService.send_event_via_gfs`, which seals exactly like
`send_event` but delivers through one named server, best-effort (no
outbox, no reachability change; a `202` is acceptance, not delivery). Nor
is a probe's `202` recorded as a relay acceptance: every server answers
`202` to every probe, so it would mark a healthy direct peer "relay only"
after each round.

### Refresh, triggers and expiry

- **Every 24 h ± 1 h** (`GfsRouteDiscoveryScheduler`) every eligible peer
  is re-probed. Only an accepted ack refreshes a route (`last_ack_at`);
  a received probe makes the receiver probe back, which refreshes its
  side.
- **On our own GFS (re)connect** an early round is requested. Triggers
  within 30 s coalesce into one round, a triggered round never starts
  within 10 min of the previous round, and each peer is probed at most
  once a minute — so a reconnect storm or a flapping socket costs one
  round per 10 min. A scheduler stop lands between peers, never mid-POST.
- **On demand:** `probe_peer(instance_id)` probes one peer at once (for
  a fresh pairing or a newly enabled opt-in; same per-peer cap). The
  admin switching the fallback from off to on (v_54) probes past that
  window — even right after another probe — and opens none: its probe
  usually goes unanswered (the other side has not switched on yet), and
  our probe-back when the other side does must not be swallowed by it. A
  repeated "on" is an ordinary, throttled probe. Either side switching
  off clears the probe and answer windows for that peer, so the next
  switch-on starts fresh.
- **Expiry:** a route whose `last_ack_at` is older than 72 h (three
  intervals) is deleted, so one missed round never costs a working route
  but a peer that left a server stops being relayed there. Until then —
  up to 72 h, on either side, since both sides keep routes — a relayed
  send through that server is answered `202` and lost (the relay cannot
  say the recipient is gone without becoming a presence oracle). Removing
  one of OUR connection servers deletes its routes at once (FK cascade).
  A peer switching its fallback **off** does not wait for expiry: it tells
  us (`gfs_relay: false`, below) and we drop our routes to it at once.
- **Caps:** at most 1024 outstanding probes (oldest forgotten first, with
  a WARNING — its ack will be ignored), a
  15 min ack window, and at most one ack per peer per server every 30 s —
  a paired peer cannot make us spend unbounded relay posts.

### Bootstrap route from a pairing code

A pair made from a pairing code with a GFS reach
([`pairing.md`](./pairing.md#reach--pairing-through-a-gfs)) needs a
route before discovery can run: when one side has no URL, the pairing
confirm and the first §24.11 envelopes (capabilities, profiles) can only
ride the relay. So whoever writes the peer's row during the handshake —
the scanner on scan, the code owner on the accept — sets `gfs_relay`,
stores the peer's verified key-wrap key and **seeds one route**: its own
connection to the server the code named (`last_ack_at` = now) — only
when the peer provably reads that server. The scanner always does (the
code owner is on it). The code owner does when the accept arrived
through that server or the scanner has no URL (such a scanner refuses a
code whose server it is not on); a scanner that answered at the inbox
with its own URL gets no seeded route, since a route through a server
it never reads turns every fallback into a silent `202` — discovery
finds the servers the two really share.

This does not reopen the hole the ack rule closes. The rule exists
because a received relay frame proves nothing about which server the
SENDER reads; a seeded route never comes from where a frame arrived.
It comes from the code — exchanged out of band, verified against the
code owner's identity key — and from our own registration with that
same server (matched by its pinned `gfs_instance_id`). After the pair
confirms, each side calls `probe_peer`, so discovery confirms or adds
routes; a seeded route that no ack ever refreshes expires after 72 h
like any other. The code and the signed peer-accept carry each side's
`proto_version`, so the v_53 gate on probing already holds at confirm
time.

## Turning the fallback on for an existing pair

A pair made with a plain `url` code — or before the relay existed — has
`gfs_relay = 0` on both sides and neither holds the other's key-wrap
key, so neither can seal anything to the other. Each admin can switch
the fallback on later, per connection
(`PATCH /api/pairing/connections/{id}` `{gfs_relay: true}`, see
[`api.md`](../api.md); the Connections → Manage panel's "Use the GFS as
a fallback" switch). Since v_54
(`FederationCapability.MIN_FOR_GFS_RELAY_KEY_EXCHANGE`).

- **The key rides the capabilities announcement.** No new event: to a
  directly paired peer we opted in with, `INSTANCE_CAPABILITIES_UPDATED`
  also carries `keywrap_pk` / `keywrap_sig` / `keywrap_suite` — our
  static key-wrap key (the one `/gfs/info` and a GFS-reach code carry),
  its identity binding signature and the `"x25519"` suite tag — inside
  the AES-256-GCM payload, next to `gfs_relay: true`. Switching on sends
  it to that one peer at once; the startup fan-out repeats it while the
  switch stays on. A peer we did not opt in with never gets the key — its
  announcement says `gfs_relay: false` instead.
- **The receiver checks it against the pinned identity.** The suite must
  be known (unknown or missing → refused, never defaulted) and the key
  self-signed by the identity key pinned for that household at pairing
  (`verified_peer_keywrap`); anything else is dropped at WARNING and the
  stored key stays. Only `source = manual` rows take a key this way. A
  stored key does **not** opt the receiver in — that stays its own
  admin's switch.
- **Both sides must switch on.** Probing needs the peer's key, and the
  §24.11 relay opt-in gate refuses relayed envelopes from a household we
  did not opt in with — so with only one side on, no relay body is ever
  posted. When the second side switches on, its key reaches the first
  side, which probes at once (a newly learned key triggers `probe_peer`);
  the second side probes too. Discovery then runs exactly as above. A
  probe that overtakes the announcement carrying its sender's key (the
  probe rides the GFS, the key the direct link) cannot be acked — we have
  nothing to seal to — so it is ignored without opening the 30 s answer
  window; the sender's next probe, triggered by ours, is answered.
- **Off** clears our opt-in, deletes that peer's routes and our probes in
  flight to it (a late ack re-creates nothing — the ack handler re-reads
  the opt-in), and re-sends our capabilities with `gfs_relay: false`. The
  receiver (directly paired peers only; an explicit `false`, never a
  missing field) drops **its** routes to us and its probe state for us,
  without touching its own opt-in. Without that it would keep relaying to
  us for up to 72 h: the relay answers `202`, so those envelopes count as
  delivered and are never queued, while our gate refuses every one. Now
  its next send takes RTC / the HTTPS inbox and, failing those, its
  outbox. The keys stay (public, bound to the identity key, needed if the
  switch comes back on); switching on again runs the key / probe flow and
  restores the routes on both sides.
- **Older peers.** A v_53 peer never sends its key, so the switch cannot
  complete with it unless we already hold its key from a GFS-reach
  pairing; the connections API says so (`gfs_relay_available: false`)
  and the SPA tells the admin the other household needs a newer Social
  Home. The status line otherwise reads "reachable through N GFS"
  (`gfs_routes`), "waiting for the other household" (on, peer key
  unknown) or "no GFS you both use found yet" (on, keys known, no route).
  Which GFS the two share is never shown — only counted.

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(servers X, Y)
    participant Y as GFS Y
    participant B as HFS B<br/>(servers Y, Z)

    Note over A,B: paired with a url code: gfs_relay = 0, no key-wrap keys
    A->>A: admin switches on: gfs_relay = 1
    A->>B: INSTANCE_CAPABILITIES_UPDATED (RTC / HTTPS inbox)<br/>{proto_version, keywrap_pk, keywrap_sig, keywrap_suite} — encrypted
    B->>B: verify binding vs pinned identity → store A's key<br/>(B's gfs_relay stays 0)
    A->>A: probe_peer(B) → not eligible (no key for B): nothing sent
    Note over A,B: later — B's admin switches on
    B->>B: gfs_relay = 1
    B->>A: INSTANCE_CAPABILITIES_UPDATED {…, keywrap_*}
    A->>A: verify, store B's key → keywrap_learned → probe_peer(B)
    A->>Y: GFS_RELAY_PROBE {nonce} (and through X)
    Y->>B: WS frame {sealed}
    B->>Y: GFS_RELAY_PROBE_ACK {nonce} — same server
    Y->>A: WS frame {sealed}
    A->>A: route (B, conn A·Y)
    Note over A,B: B probes too (its own switch, and the probe-back) → route (A, conn B·Y)
    Note over A,B: later — A's admin switches off
    A->>A: gfs_relay = 0, delete routes to B, forget probes to B
    A->>B: INSTANCE_CAPABILITIES_UPDATED {…, gfs_relay: false} — encrypted
    B->>B: drop routes to A (B's own gfs_relay stays 1)<br/>next send to A: RTC / HTTPS / outbox, never the relay
```

## Binary media frames (v_87)

The JSON relay body tops out at ~191 KiB of sealed plaintext, so a 512 KiB
media chunk (base64'ed twice inside an envelope, ~1 MiB) never fit. Since
v_87 the same `POST /gfs/envelope` also takes a **binary** body that carries
one chunk at mesh size:

```
outer  = "\x00SHR" | u8 version 1 | to_instance (32 ASCII) | sealed
sealed = u8 kem-suite id (0x01 = x25519) | eph_pk (32) | nonce (12) | AES-256-GCM(ct || tag)
plain  = u32 BE length | media body | zero padding to a size bucket
```

The **media body** is the chunk's binary inbox body from v_81
(`\x00SHM`, the frame-suite byte, the signed §24.11 header, the AES-GCM
sealed chunk — `federation/media_framing.py`). The seal hides the header's
`from_instance`, `event_type` and `space_id` from the GFS; the AEAD's
associated data binds the outer prefix (recipient id included), the
kem-suite byte and `eph_pk`, so a re-addressed or re-suited frame does not
open. Padding buckets are 64, 128, 256, 516 KiB and the cap; a full 512 KiB
chunk lands in 516 KiB (~517 KiB on the wire, cap 528 KiB). The GFS and the
household both refuse a `sealed` part whose length is not exactly one
bucket plus the 61-byte seal overhead (`VALID_SEALED_LENGTHS`).

- **Scope:** a household seated from an invite link (`space_session`) at
  v_87, whose `relay_via` GFS proved the signed `envelope_media` capability.
  Today this carries `SPACE_MEDIA_BLOB` to a link-joined member whose GFS
  keeps **no** space media cache (its signed block carries no
  `space_media`); live pictures otherwise keep using the cache. The
  generic relay for files no longer in the cache comes with the wanted /
  catch-up work after the GFS redesign.
- **Fallback:** a peer below v_87, or a GFS without `envelope_media`, gets
  JSON chunks of at most 96 KiB (`RELAY_JSON_CHUNK_BYTES`), which fit the
  JSON relay. A binary frame that the relay throttles (429) is waited out;
  it never falls back to JSON (the chunk is too large for it). When the
  path changes under a transfer (the GFS stops proving `envelope_media`), a
  512 KiB chunk is not sent at all: the send answers
  `relay_chunk_resize` and the media outbox's next attempt re-cuts the
  file at the size the path takes then (a new `chunk_count`, so a fresh
  partial set on the receiver) — a transfer never sticks on a size.
- **Loss after a 202:** the relay answers before it delivers, so a frame it
  drops later (saturation, a full media queue, the 1 h TTL) or the
  receiver refuses (its media budget) is gone while the sender's outbox
  counts it sent. The receiver heals it on the next VIEW: a missed
  content-addressed file (`sc-…`) shown by a live row created within the
  last 3 days makes `RelayMediaRefill` ask the space's relay-seated host for
  `SPACE_SYNC_RESUME` with `refill: "posts" | "calendar" | "gallery"` — the
  one resource whose row shows it (`MediaReferenceRepo.space_rows_of`) —
  since a minute before that row. A v_87 host replays at most 20 rows of
  that resource (deduped by id on arrival) and re-ships at most 16 files /
  16 MiB of theirs, under its own (household, space, resource) window, so a
  refill never uses up the reconnect resume's window. One request per
  (space, resource) per 30 min on each side. Comment and listing pictures
  are not refilled (their replay ships no files). **Cost per trigger:** 1
  recipient × ≤ 20 rows (~2–10 KiB each through the relay) + ≤ 16 files ≤
  16 MiB; worst case a reader viewing missing files in S spaces ≤ 3 S
  refills per 30 min, ≤ 48 S MiB. A bridge: the generic one-file
  `SPACE_MEDIA_WANTED` of the GFS redesign replaces it. Since timestamps
  are compared as times on the host (`datetime()` on both sides in every
  `list_since` the replay uses), a same-day row stamped naive is found.
- **GFS:** identity-free and content-blind as for every relayed body — it
  reads the magic, the version and the recipient id, never a sender, and
  pushes the body unchanged as a BINARY WebSocket message to a socket whose
  hello listed `envelope_media`. Its budgets are separate from the JSON
  envelopes' (per-IP bytes, in-flight bytes, its own queue table with
  per-recipient and server-wide byte caps and a 1 h TTL, expired rows swept
  on every insert — see
  [`docs/operations/gfs-cluster.md`](../operations/gfs-cluster.md#envelope-relay-media-frames-v_87)),
  so media can never crowd a household's control envelopes out. On a
  drain the control envelopes go first, under their lock; the media frames
  follow in one background task per recipient, under their own, so a
  household's control link never waits behind up to 16 MiB of media. A
  media frame is pushed only while the socket's transport holds at most
  1 MiB unsent (`WS_MEDIA_MAX_BUFFERED_BYTES`), so control frames never
  queue behind media bytes. A media push that stalls
  (`WS_MEDIA_SEND_TIMEOUT_S`, 30 s) or finds the socket backed up is
  dropped — never the socket — and the drain backs off (2 s doubling to
  30 s, five tries; the rows stay queued for the next hello / hint) instead
  of re-pushing at once. On a very slow link (< ~1 Mbit/s) one frame can
  still delay a control frame by seconds and the 30 s WebSocket heartbeat
  may close the socket for a missed pong; the household reconnects and the
  queue drains again. Any push (control or media) that fails outright — a
  reset, a closing transport — and a control push that stalls
  (`WS_SEND_TIMEOUT_S`, 10 s) evict the socket AND close it with `1013`
  (try again later), on `/gfs/ws` and `/gfs/mailbox/ws` alike: the frame is
  queued (or, for a member item, handled as for an offline recipient), and
  the household reconnects and drains it. The GFS never just forgets a
  socket that is still open — that left the household connected but
  receiving nothing, with no reason to reconnect. Across a cluster the frame
  rides the shared queue and the `NODE_DRAIN_HINT` like any queued
  envelope.
- **Receiver:** the WebSocket client hands the bytes to
  `GfsRelayInbound`. Media has its own budget (`RELAY_MEDIA_INBOUND_LIMIT`,
  1200 / min — about ten senders at their GFS cap), checked before and never
  charged to the process-wide bucket the relayed control, pairing and
  invite envelopes share, and charged only for a frame that went through.
  Frames that fail to open or fail their checks are charged to a junk bucket
  (`RELAY_MEDIA_JUNK_LIMIT`, 120 / min); while it is full, media frames are
  dropped before any AES-GCM open. Refusals are logged as one WARNING a
  minute with a count, never a traceback.
  It checks the recipient id and the kem-suite byte
  (unknown → refused, no default), opens the seal and runs
  `FederationService.handle_inbound_media_relay`: the §24.11 chain on the
  signed header (lookup by its `from_instance`, a media-only event-type
  gate, peer-class and relay-opt-in gates, the relay's timestamp window,
  signature, replay, decrypt, idempotency, ban), then the same tail as the
  inbox and the DataChannel (`_finish_media_frame`: frame and AEAD suites,
  chunk decrypt, `chunk_sha256` binding).

```mermaid
sequenceDiagram
    participant A as Host household
    participant G as GFS
    participant E as Link-joined member (v_87)
    A->>A: chunk 512 KiB → inbox media body (\x00SHM, signed header + sealed chunk)
    A->>A: seal to E's key-wrap key, pad → \x00SHR | v1 | E | sealed
    A->>G: POST /gfs/envelope (raw, ≤ 528 KiB)
    G-->>A: 202 accepted (uniform)
    alt E's socket listed envelope_media
        G->>E: binary WS message (the same bytes)
    else offline / other node
        G->>G: gfs_media_queue (16 MiB / recipient, 1 h) + NODE_DRAIN_HINT
        G->>E: binary WS message on its next labelled hello
    end
    E->>E: open seal → §24.11 media pipeline → chunk stored
```

**Cost:** one frame per chunk per relay-seated member household — a
512 KiB chunk is ~517 KiB on the wire to the GFS and the same again from the
GFS to the member (JSON chunks of 96 KiB cost ~1.8× their size each way, plus
an envelope per 96 KiB). A 200 MiB file to 50 relay-seated members is
400 chunks × 50 = 20 000 frames, ~10.1 GiB up and ~10.1 GiB down through the
GFS; at the default 64 MiB / min per sending address that is ~2.7 h from one
household (the sender waits out each 429). That is why live pictures stay on
the cache (one upload per GFS, not per member) and the relay carries only
what the cache cannot.

## Anonymous per-space mailboxes (GFS redesign PR 1)

Every relay above addresses a household by its **instance id**, so the GFS
sees one household id across all the spaces it reaches that household for.
The redesign addresses a member reached through a GFS by a **mailbox** per
(household, space, generation) instead. PR 1 ships the GFS side and the
household library, with no `OURS` bump; redesign PR 2 (v_88) wires it into
the space seat flow — see [Per-space mailboxes (v_88)](#per-space-mailboxes-v_88). The GFS advertises it in its
signed `/gfs/info` as `envelope_mailbox: {"sig_suites": ["ed25519"],
"idle_days": 30}`.

**Keys and id.** The household derives the mailbox's X25519 sealing key and
Ed25519 claim key with HKDF-SHA256 from its **mailbox root** — a dedicated
32-byte secret generated once and stored under the household KEK
(`instance_config` key `gfs_mailbox_root`, v_88), not the key-wrap seed (salt `sh-mbx-v1`, info
`sh-mbx-kem:v1|<gfs id>|<space>|<gen>` / `sh-mbx-sign:v1|<gfs id>|<space>|<gen>`),
so beside the root only `gen` (and the GFS id pinned at mint time) needs
storing. With the GFS id in the derivation, two GFSs serving the same
global space get unrelated mailbox ids for the same household. `mailbox_id = base32(SHA-256("sh-mbx-v1" ‖
sign_pk))[:32]` — the shape of an instance id, so a JSON envelope's
`to_instance` and a binary `\x00SHR` frame's recipient field carry it
unchanged, and the prefix keeps it apart from the instance id of the same
key.

**Register** — `POST /gfs/mailbox/register`, anonymous, `{sig_suite:
"ed25519", sign_pk, mailbox_id, gfs_instance_id, ts, nonce, sig}`. `sig` is the proof of
possession over `"sh-mbx-register:v1" \n gfs_instance_id \n mailbox_id \n ts
\n nonce` — bound to the GFS id the household pinned, so a registration made
for one GFS proves nothing at another. A server accepts its own id, its
`instance_id_aliases` and the ids its shared-key cluster siblings serve (a
cluster whose allocs still serve per-node ids: register on one, claim on
another). The GFS rejects an unknown `sig_suite` (no
default), recomputes the id from the key, checks `ts` (±300 s) and the
signature, spends the nonce (a replay is `409`), refuses an id an instance
holds (`409 id_in_use`, atomically with the insert — and `/gfs/register`
in turn only takes an instance id derived from its key, never a mailbox's,
never moving a row to another key) and pins the key on first use;
registering again is a no-op and needs no ticket. A NEW mailbox spends one
anonymous registration ticket (`ticket_suite` + `ticket`, below). Limits:
30 / min per address, then a backstop of `mailbox_max_per_ip` (200) new
mailboxes per UTC day per /64 (×16 per /56, ×256 per /48) in salted
count-min sketches (no address kept, nothing to evict). There is no
server-wide daily cap any more: ~100 addresses could burn the old 20 000
and block every new space join for the rest of the day.

**Unregister** — `POST /gfs/mailbox/unregister`, anonymous, `{sig_suite,
mailbox_id, gfs_instance_id, ts, nonce, sig}` (redesign PR 2): when the
household leaves the space it signs `"sh-mbx-unregister:v1" \n
gfs_instance_id \n mailbox_id \n ts \n nonce` with the mailbox key. The GFS
checks the GFS id, `ts` and nonce shape as for a registration, verifies
against the pinned key (an unknown mailbox against a dummy key — the same
work) and only then spends the nonce; a verified request drops the mailbox
with its queued envelopes and media in one transaction and closes its claim
socket on that node. An unknown mailbox, a wrong signature and a replay all
get the same `200 {status: "unregistered"}` — no oracle for which mailboxes
exist. An older GFS answers `404`; the mailbox then ages out in the idle
sweep. 30 / min per address.

**Claim** — `GET /gfs/mailbox/ws`, a socket of its own, never the identified
`/gfs/ws`, and **one mailbox per socket** (owner decision: a shared socket
would link a household's spaces live). The GFS sends a random challenge; the
client signs `"sh-mbx-claim:v1" \n gfs_instance_id \n mailbox_id \n challenge`
with the mailbox key. Unknown mailbox and wrong key close alike (`4401
auth-failed`, after the same dummy-key verify). Then the GFS delivers the
mailbox's queued frames in order and its live ones: JSON envelopes as text,
binary `\x00SHR` media frames unchanged when the claim's `accepts` lists
`envelope_media`. aiohttp pings every 30 s (inside Cloudflare's 100 s idle
timeout); a claim updates `last_claim_at`.

**Routing.** `POST /gfs/envelope` (JSON and binary) to a mailbox id is
pushed or queued exactly as to an instance id — the same
`gfs_envelope_queue` / `gfs_media_queue` rows keyed by the id, the same
per-recipient caps, TTLs and in-flight bounds — and answers the same uniform
`202` before the recipient is even looked up, so a known mailbox, an unknown
one and a household are indistinguishable. In a cluster the enqueue hints
the sibling nodes (`NODE_DRAIN_HINT` carries the mailbox id like an instance
id) and the node holding the claim socket drains it.

**Idle sweep.** Both stored timestamps are rounded down to the UTC day — a
household reclaiming all its mailboxes at once would otherwise stamp them
with the same second. A mailbox whose last claim (or, never claimed, its
registration) is `mailbox_idle_days` (30) days old is dropped with its
queued frames by the maintenance loop; a claim socket held open re-stamps
its day once a day, so it is never swept while connected.

**Queues: a pool of their own.** Mailbox ids are free to mint, so their
frames never share room with households' (each queued row carries its
`pool`). Per mailbox: 200 envelopes / 4 MiB and 8 MiB of media
(households: 2 000 / 64 MiB and 16 MiB). Both envelope pools follow ONE
model — **tail-drop plus a per-recipient reserve, nothing is ever
evicted**:

- past the pool's total (households 1 GiB, mailboxes 256 MiB) a new
  envelope is dropped — anyone can post to any id, so evicting someone's
  envelopes to make room would be an anonymous delete primitive;
- except for a recipient still under its **reserve** (fewer than 20 queued
  envelopes and less than 64 KiB): it is admitted up to the pool's
  **ceiling** (households 2 GiB, mailboxes 1 GiB), so a flood of other ids
  can fill the pool but never block anybody's first envelopes. Headroom
  math: a reserve holds < 64 KiB plus one envelope (≤ 320 KiB), so the
  1 GiB / 768 MiB headroom fits ≥ 2 700 / 2 000 recipients at a worst-case
  reserve, ~16 000 / 12 000 at a typical few-KiB envelope;
- media: households 1 GiB, mailboxes 256 MiB, tail-drop.

Worst-case disk for queued relay frames is therefore 2 + 1 GiB of
envelopes + 1 + 0.25 GiB of media + 256 MiB of member relay items ≈ 4.5 GiB,
whatever the number of ids. The pool totals are running counters
(`gfs_queue_totals`, kept by triggers on every insert and delete, recomputed
hourly by maintenance as drift repair), so a cap check reads one row;
expired rows of a pool are swept (an index range) before an insert is
refused, so they never hold room. (Round 3's ranked mailbox eviction was
removed in favour of this: one model, no scans, nothing to game.)

```mermaid
sequenceDiagram
    participant H as Household (member over the GFS)
    participant A as GFS node A
    participant B as GFS node B
    participant S as Sender household
    H->>H: keys = HKDF(mailbox root, gfs id, space, gen); id = b32(SHA-256("sh-mbx-v1"‖sign_pk))[:32]
    H->>A: POST /gfs/mailbox/register {sign_pk, id, gfs_instance_id, ts, nonce, sig, ticket_suite, ticket}
    A->>A: verify PoP, nonce, id ≠ instance, ticket → spend it, pin key (gfs_mailboxes)
    A-->>H: 200 registered
    S->>A: POST /gfs/envelope {to_instance: id, sealed}
    A-->>S: 202 accepted (uniform)
    A->>A: no claim socket here → gfs_envelope_queue + NODE_DRAIN_HINT [id]
    H->>B: GET /gfs/mailbox/ws
    B->>H: mailbox_challenge {challenge, gfs_instance_id, sig_suites}
    H->>B: mailbox_claim {id, gfs_instance_id, sig_suite, sig, accepts}
    B->>B: verify under the pinned key, last_claim_at = now
    B->>H: mailbox_claimed, then queued frames in order
    S->>A: POST /gfs/envelope (binary \x00SHR to id)
    A->>A: queue + NODE_DRAIN_HINT [id]
    A->>B: NODE_DRAIN_HINT
    B->>H: binary WS message (the same bytes)
```

**What the GFS stores and learns.** `gfs_mailboxes` holds the id, the
pinned key and two day-granular timestamps — no instance id, no address,
no space id. No log line names a mailbox id on any path (the claim-socket
registry prints `<mailbox>`, the relay and the cluster drain print a
recipient id only for a household socket on the same node), so no application log sits next to an access-log
address for it. The GFS still sees, per envelope, the mailbox, the time,
the size and the sender's address, and per claim socket its address and
online time; within one space it can cluster mailboxes that the same
fan-out bursts hit. Across spaces nothing is stored or cryptographically
linked — only IP / timing correlation with the household's identified
socket, the residual named above.

Implementation: `socialhome/domain/gfs_mailbox.py` (id, signed bytes, suite
tag), `socialhome/federation/gfs_mailbox.py` (key derivation, register,
`MailboxClaim`), `socialhome/global_server/mailboxes.py`
(`GfsMailboxService`), `socialhome/global_server/routes/mailbox.py`,
`GfsEnvelopeRelay._recipient` / `_sockets` / `holds`,
`ClusterService.attach_drain(holds=…)`, GFS migration 0029.

## Mailbox registration tickets

Registration is anonymous, so per-address limits alone can't stop a botnet
from minting mailboxes — and sybil mailboxes could fill the mailbox pool's
per-recipient reserve up to its ceiling. Every NEW mailbox therefore spends
one **ticket**: a standard Privacy Pass token (RFC 9576 architecture, RFC
9577 token format, RFC 9578 §6 publicly verifiable issuance — token type
0x0002, blind RSA RFC 9474 `RSABSSA-SHA384-PSS-Deterministic`, 2048-bit).
Tickets are issued per **registered household**, so `mailbox_tickets_per_day`
(50) × the registered households is the real bound on mailboxes; who may
register a household is the operator's gate. No `OURS` bump — nothing
changes between households.

**Issuer key.** The signed `/gfs/info` capability `mailbox_tickets` is
`{suite: "privacypass-blindrsa2048-sha384-pss", issuer_key_id, public_key,
per_day, epoch_days}` — `issuer_key_id` the b64url SHA-256 of the RSASSA-PSS SPKI,
`public_key` that SPKI. A household rejects an unknown suite (no fallback)
and a key whose id does not match. One RSA-2048 key per epoch
(`mailbox_ticket_epoch_days`, 30), made by the first node that needs it and
stored in the shared DB, accepted for redemption until two epochs after it
was made. Because the key sits in the signed capability every household
reads, a GFS serving different keys to different households (to partition
their tickets) is detectable by comparing.

**Issuance** — `POST /gfs/mailbox/tickets`, signed by the household's
identity key over `{action: "mailbox_tickets", instance_id, suite,
issuer_key_id, requests, ts}` (±300 s; the `action` keeps any other signed
GFS request from being replayed here), only for a household `active` on
this GFS. `requests` are up to 64 blinded RFC 9578 `TokenRequest`s for the
fixed `TokenChallenge` (issuer `gfs.social-home.io`, origin_info
`mailbox-register`, empty redemption context — tickets are fetched ahead
of use). The GFS grants up to `per_day` per household per UTC day (one row:
instance id, day, count) and blind-signs; it sees only blinded messages and
stores none. Blinded under an older key → `409 stale_issuer_key` (nothing
counted, re-read `/gfs/info`); over the quota → `429 ticket_quota`. The
household finalizes each response (checking the unblinded signature) into
an RFC 9577 `Token` and keeps them in memory.

**Redemption** — the register body carries `ticket_suite` + `ticket` (one
`Token`, b64url). The GFS checks the challenge digest, finds the key by
`token_key_id` among the accepted keys (`403 stale_ticket_key` otherwise),
verifies RSASSA-PSS (`403 invalid_ticket`), lets the per-address backstop
count it, then records the token nonce in `gfs_spent_tickets` IN the
mailbox insert's transaction — a primary key in the shared DB, so a second
spend on any node is `409 ticket_spent`, and a register that creates
nothing (the mailbox exists, a concurrent register won, a collision, a
crash) spends nothing.
No ticket on a new mailbox is `403 ticket_required`. Spent nonces live until
their key retires; the maintenance loop prunes them, past days' counts and
retired keys.

```mermaid
sequenceDiagram
    participant H as Household (registered, active)
    participant A as GFS node A
    participant B as GFS node B
    H->>A: GET /gfs/info
    A-->>H: capabilities (signed) incl. mailbox_tickets {suite, issuer_key_id, public_key, per_day, epoch_days}
    H->>B: GET /gfs/info (anonymous, cookie-less session)
    B-->>H: capabilities (signed)
    H->>H: same key id in both views? ≤ 1 new key this epoch? epoch_days ≥ 7?
    H->>H: blind N TokenRequests (RFC 9474)
    H->>A: POST /gfs/mailbox/tickets {action, instance_id, suite, issuer_key_id, requests, ts, signature}
    A->>A: verify identity signature, active, quota (gfs_ticket_issuance)
    A-->>H: 200 {suite, issuer_key_id, responses} (blind signatures)
    H->>H: finalize → RFC 9577 Tokens (cached, unlinkable)
    Note over H,B: later (jittered, never right after a fetch), anonymously
    H->>B: POST /gfs/mailbox/register {…} (no ticket)
    B-->>H: 403 ticket_required (a NEW mailbox; an existing one is 200)
    H->>B: GET /gfs/info (anonymous) — key still an admitted one?
    H->>B: POST /gfs/mailbox/register {…, ticket_suite, ticket}
    B->>B: challenge digest, key by token_key_id, RSASSA-PSS verify, backstop
    B->>B: one transaction: INSERT nonce → gfs_spent_tickets + the mailbox
    B-->>H: 200 registered
    H->>A: POST /gfs/mailbox/register {…, same ticket}
    A-->>H: 409 ticket_spent
```

**Key consistency.** A GFS that showed each household (or each address)
its own issuer key could read the household off every redeemed ticket's
`token_key_id` — the Privacy Pass consistency problem. The household's
`TicketKeyGuard` checks the key before blinding AND before redeeming:

- the key must equal the one an **anonymous** read of `/gfs/info` shows
  (a second, cookie-less session — the one mailbox registration uses),
  so the GFS can't single the household out by its identified session.
  That read runs on its own jittered schedule (about every 6 h, ±50 %) and
  is cached; the identified ticket fetch compares against the cache, so no
  anonymous read sits milliseconds before it from the same address. Before
  redeeming (itself anonymous) the household reads afresh;
- **one new key per epoch** at most: the household pins `(key_id,
  first_seen)` and takes a different key only once its clock is in a later
  epoch (epochs aligned to the Unix epoch, as the GFS aligns them; one hour
  of skew allowed); the replaced key stays usable for its one overlap
  epoch;
- `epoch_days` below **7** is refused (`ticket_epoch_too_short`): a short
  epoch lets the GFS rotate keys to tag households and shrinks the set
  they hide in.

A failed check raises `ticket_key_inconsistent` (or
`ticket_key_unconfirmed` before the first anonymous read), logs a WARNING
with no key and no id, and leaves the tickets unused. A ticket under a key
the household no longer admits (`ticket_key_not_admitted`) is dropped with
every ticket under that key. What remains: a GFS that recognises the
household's *address* can show it its own key in BOTH views from first
contact and keep it — the same IP / timing limit as below.

**No cross-check outside the GFS (decision, v_88).** Comparing key ids among
paired households was considered and not built. It would add a federation
field and a version bump for a residual only a GFS that recognises the
household's address can use. Key consistency stays the anonymous cross-read
above plus the one-key-per-epoch pin, which v_88 persists per GFS
(`instance_config` key `gfs_mailbox_ticket_pin:<gfs id>`), so a restart
cannot reset it. The stated limit stands: a GFS that recognises the
household's IP can show it its own key from first contact.

**Anonymity set.** A redeemed ticket hides its household among the
households that fetched tickets under the **same key in that epoch** —
nothing more. On a small GFS that set is small (a GFS with five active
households can narrow a ticket to one of five). That is a property of set
size, not a leak in the design; what grows it is a longer epoch (the
default 30 days; never below 7) and more households on the GFS.

**Timing.** The issuer learns "household H took N tickets today", never
which mailbox spent one. A fetch right before a register would let the GFS
line the two up, so the cache refills on its own schedule — about once a
day, each wait jittered ±50 % (`MailboxTicketCache.start`), and a slot
within an hour after a register is skipped — and a register spends from the
cache. A failed refill or anonymous read (a GFS outage included) logs a
WARNING and the loop tries again at the next slot. On a GFS connect, a cache
that is empty gets the anonymous key read at once and one identified refill
30–300 s later (jittered, never next to the anonymous read, and not within
the quiet window after a register — `MailboxTicketCache.refill_if_quiet`),
so a fresh household is not without tickets until its first daily slot. A register never fetches: an empty cache fails a register
with `no_ticket`, and a `stale_ticket_key` refusal drops that key's tickets
and fails — so a GFS that refuses a register on purpose cannot make the
household fetch right next to it. Only `ticket_spent` is retried, once,
from the cache.

**No wasted tickets, no shown-twice tickets.** `register_mailbox` tries
without a ticket first and shows one only on `403 ticket_required`, so
re-registering an existing mailbox never shows (or spends) one. A ticket
the guard refused, or whose request never reached the GFS (no connection),
goes back to the cache. A ticket the GFS saw is never put back: showing it
again for another mailbox would link the two.

IP / timing correlation with the household's identified `/gfs/ws` stays the
accepted, stated limit (as for claims above). Household side:
`TicketIssuer.from_capability`, `read_ticket_issuer`, `TicketKeyGuard`,
`build_ticket_request`, `fetch_tickets`, `MailboxTicketCache`,
`register_mailbox(tickets=…)` in `socialhome/federation/gfs_mailbox.py`,
wired by `SpaceMailboxService` (v_88: one guard and cache per GFS
connection, the `TicketKeyPin` persisted). GFS side: `socialhome/privacy_pass.py`,
`socialhome/global_server/mailbox_tickets.py`, GFS migration 0030.

## Per-space mailboxes (v_88)

GFS redesign PR 2 puts the mailboxes above to use. A household that reaches
a space **only** through a GFS is addressed there by a mailbox for that
space, not by its instance id. So the recipient field no longer lets the
GFS tie the household's spaces to each other or to its identity.

**Who gets one.** Only a `space_session` seat: a household seated from an
invite link, or from an approved GFS "Ask to join" (which redeems over the
same sealed bootstrap leg, see
[discovery.md](./discovery.md#ask-to-join-through-the-gfs-v_84)). Paired
and mesh members never get a mailbox; they redeem over the direct or routed
path. Gated on `FederationCapability.MIN_FOR_SPACE_MAILBOX` (v_88), slug
`space_mailboxes`. Not space-scoped, no sync-stream change.

**The mailbox root.** One 32-byte secret, generated once and stored
KEK-wrapped in `instance_config` (`gfs_mailbox_root`). Every mailbox key is
derived from it (see above), so per mailbox only the generation and the GFS
it lives on are stored. No migration for the root.

**The directory entry.** What a household hands its counterpart inside the
sealed redeem or ACK, as `mailbox`
(`domain/space_mailbox.SpaceMailboxEntry`). Exactly these fields; any other
set is refused:

| Field | Meaning |
|---|---|
| `space` | The space this mailbox is for. |
| `instance_id` | The household that minted it. |
| `mailbox_id` | `base32(SHA-256("sh-mbx-v1" ‖ sign_pk))[:32]`; never equal to `instance_id`. |
| `kem_pk` | The mailbox's X25519 key (b64url, 32 bytes). Frames for the mailbox are sealed to it. |
| `kem_suite` | `"x25519"`. Unknown → refused, no fallback. |
| `sign_pk` | The mailbox's Ed25519 claim key (b64url, 32 bytes). |
| `gfs` | Base URL of the GFS the mailbox lives on. |
| `gen` | The mailbox generation (random, below 2³¹). |
| `proto_version` | The minting household's `OURS`. A sender uses the entry only at ≥ 88. |
| `keywrap_pk` / `keywrap_sig` | The household's key-wrap key and its self-signature under the identity key. |
| `sig_suite` | `"ed25519"`. Unknown → refused, no fallback. |
| `sig` | The household identity key over `"sh-mbx-entry:v1\n"` + canonical JSON of every other field. |

The signature lets the entry be passed on to other members later (v_89)
and still prove who minted it. The receiver checks it under the identity
key it already trusts for that household (the one in the sealed redeem, or
the invite / approval hint): that key must derive to `instance_id`,
`keywrap_sig` must bind the key-wrap key to it, `space` must be the space
being joined (`federation/space_mailbox_entry.verify_entry`), and `gfs` must
be the GFS the sealed body came through — a peer cannot steer our frames to
another GFS. A bad entry is ignored; the pair keeps today's relay toward
that household.

**The seat flow.**

1. **Joiner.** When the host's version (from the invite blob or the
   approval) is ≥ 88, the joiner mints its mailbox for (space, random
   `gen`) at the invite's GFS before it sends the redeem: derive the keys,
   register (spending a ticket only when the mailbox is new), open its own
   claim socket `/gfs/mailbox/ws`. Its signed entry rides in the sealed
   redeem.
2. **Host.** It verifies the entry, then mints its own mailbox toward THIS
   member at the same GFS — **one mailbox per member pair** (owner decision,
   #1333): keys from (space, the member's instance id, gen), so a host's
   mailboxes for two members of one space are as unrelated as two spaces',
   and a flood on one hurts only that pair. It registers and claims it
   **before** the ACK leaves, so the joiner's first envelopes land, and puts
   its entry in the sealed ACK. The host's mint is bounded to 3 s
   (`HOST_MINT_TIMEOUT_S`), well inside the joiner's redeem timeout: a slow
   GFS never costs the join. A mint that finishes later is not lost: the
   host stores it on the seat and hands the joiner its entry with
   `SPACE_MAILBOX_UPDATED`.
3. **Both** store `{peer: <the other's entry>, own: {gen, gfs_id, gfs}}` in
   the pair's `space_instances.mailbox` (migration 0144). A mailbox minted
   for a redeem that fails stays pending for 30 min (`PENDING_TTL_S`): a
   retry — or a second household redeeming into the same space at the same
   time — reuses it and its ticket; then the sweep drops it.

If the GFS takes no mailboxes, no ticket can be had, the register is
refused or the GFS is unreachable, that side mints none at seat time. The
instance-addressed relay toward it is only a **transient** state: the seat
is still marked as a mailbox pair (`{"peer": null, "own": null}` when
neither side has one), and each side's sweep (`retry_missing`, every
~10 min, jittered) mints the missing mailbox later — from cached tickets
only, ONE pair per run (`RETRIES_PER_SWEEP`, so two registers never sit
back to back) in random order and `REMINTS_PER_DAY` (1) per pair, only
toward a counterpart at v_88 or later. The retried claim waits its own
`CLAIM_JITTER_S` before connecting, and the new entry is announced the one
way every post-seat update is (below). The joiner uses the same path to
hand the host its entry. A late `remember(own=None)` never nulls an own a
late mint already set.

**Pairs with no state at all** — a seat made before v_88, a seat from an
invite link minted before the host upgraded, or a crash between the seat
and its row write — are marked too: each sweep marks every seated
`space_session` pair whose counterpart is at v_88 or later and whose row
holds no mailbox state as `{"peer": null, "own": null}` (`mark_unmarked`,
re-checked under the lock so a concurrent update is never wiped), and the
retry above takes it from there. The joiner gates on the host's version
carried in the identity-signed redeem ACK, not the link's, and records the
host at it. A `SPACE_MAILBOX_UPDATED` from a seated v_88 `space_session`
counterpart with no row yet creates the row instead of being dropped.

**Sending.** For each relay send to a `space_session` seat,
`FederationTransport` asks `SpaceMailboxService.target_for` for the
target's mailbox for the envelope's `space_id`. A space-less envelope of
non-space traffic (the capabilities exchange) uses any shared space's
mailbox; a space-less `SPACE_*` envelope (a redeem reply, a teardown,
mesh routing — the list is pinned by a test over every `send_event`
site) never rides a mailbox and goes instance-addressed. The target is used only when the entry is at
v_88 or above and our connection to the entry's GFS proved the signed
`envelope_mailbox` capability (and `envelope_media` for a binary media
frame). Then:

- the unchanged §24.11 envelope (signed by the household identity key,
  encrypted under the pair session key) is padded to the relay size
  buckets and sealed whole to the mailbox `kem_pk` (ephemeral X25519 ECIES,
  `kem_suite: "x25519"`, the same seal as the relay);
- it goes out as `POST /gfs/envelope {to_instance: <mailbox id>, sealed}`
  over the mailbox session — cookie-less and without keep-alive
  (`force_close`), so no pooled connection carries two mailboxes' traffic —
  never the identified one (register, claim and unregister ride it too);
- a v_87 binary media frame goes the same way: its recipient field is the
  mailbox id and it is sealed to `kem_pk`.

Otherwise the send is today's instance-addressed relay.

**Receiving.** Frames on a mailbox's claim socket are opened with that
mailbox's own X25519 key (never the key-wrap key). A mailbox carries only
relayed §24.11 envelopes and media frames; invite, pairing and join bodies
are refused. The envelope enters the unchanged §24.11 pipeline with
transport `TRANSPORT_GFS_MAILBOX` (`"gfs_mailbox"`), which gets the relay's
timestamp window and relay opt-in gate (`RELAY_TRANSPORTS`). One extra step
runs right after the parse, `check_mailbox_space`: the envelope's
`space_id` must be the mailbox's space; a space-less envelope is taken
only for non-space traffic (never a `SPACE_*` event). Anything else is
refused. Budget: ONE bucket per mailbox (`MAILBOX_INBOUND_LIMIT`, and one
for its media frames), checked before any open and charged for every
frame — junk, a frame that opens but fails the pipeline, a good one. A
mailbox serves exactly one member pair, so its only legitimate sender is
that one counterpart: whoever floods it (the member, or someone who learned
the id) shuts only that pair's mailbox, never another's, and never the
instance relay's budget.

**Rotation and release.** A mailbox changes only when the household leaves
or is removed. A mailbox is in use while a `space_instances` row refers to
it AND a person of this household still sits in the space. Every other one
(not pending) is closed and unregistered (`POST /gfs/mailbox/unregister`,
see above) — at once when a `space_session` seat is revoked, and by a
jittered sweep every ~10 min for every other path that ends a seat (leave,
dissolve, a stub purge). Each released mailbox is closed and
unregistered after its OWN random wait of 2–15 min (`RELEASE_DELAY_S`;
frames on it are dropped meanwhile), so one kick that ends several seats
never unregisters their mailboxes back to back from one address. The rows
of a left space forget their mailbox state, and a left space's mailbox is
not reopened on start. A GFS without the unregister route lets its idle
sweep drop the mailbox. A re-join picks a fresh random `gen`, so the GFS
cannot tie the old and the new mailbox. A seat write and a release never
interleave (both under the service lock).

**A lost mailbox.** The GFS forgot a mailbox (its 30-day idle sweep while
the household was away, or data loss): the claim is refused (`4401`). The
household registers the SAME mailbox again — same derived id and keys,
from a cached ticket (never fetched for it: an identified fetch right
before an anonymous register would tie them), within
`RECOVERY_REGISTERS_PER_DAY`, counted only when a ticket was used — and
claims it; the counterpart's frames
resume and nothing is announced. Only when that cannot happen now (no
cached ticket, refused, unreachable, today's re-registrations used up) is
the counterpart told `{mailbox: null}` — and told the entry again once the
mailbox is back. A mailbox the GFS keeps refusing (`LOST_ROTATE_AFTER`
refusals, not just no ticket) is replaced by a fresh generation: the seat
rows move to it, its claim waits its own `CLAIM_JITTER_S`, and the new
entry is announced.

**Frames to a forgotten mailbox are lost.** The GFS answers a frame for a
mailbox it does not know with the same uniform `202` as any other (so a
sender cannot probe which mailboxes exist) and drops it; nothing goes to
the sender's outbox. Frames sent between the GFS forgetting a mailbox and
its re-registration — or the counterpart's `null` — are lost, not
delayed. The window: with a ticket cached, until the next claim attempt
(the first refused claim re-registers at once). Without one, until the
`null` lands — at once when the counterpart's mailbox is ready; otherwise
`announce_at` (1–5 min), then the next sweep (~5–15 min), and only outside
the 5 min quiet window after an op the GFS accepted (a refused claim or a
register with no ticket never extends it; ticket-using re-registers are
capped at 4 a day per pair). Typically under ~25 min. An outage costs no
sent-budget: only an update that went out uses one of the pair's 6 daily
slots (reserved before the send, given back if it failed), and the last
slot is kept for a `null` (an entry needs two left), so entries alone can
never block it. Failed attempts have their own cap (12 a day per pair,
again the last kept for a `null`), so retries during an outage — which
may name both households — stay bounded. Worst case: the pair already SENT 6
updates today (three lost-and-back cycles) — a further `null` then waits
for the next day, and frames to that mailbox are lost until it is
re-registered.

**How every post-seat update goes out (`announce_change`).** Outside the
seat-time redeem / ACK, every `SPACE_MAILBOX_UPDATED` — a retried mint,
the late host mint, a lost notice, a re-claim after one, a rotation —
takes one path. When we hold the counterpart's entry it is sent to THAT
mailbox only, at once: the GFS sees a sealed frame to the counterpart's
mailbox from our IP, never this household naming that one (the accepted
IP-timing residual) — and if that mailbox is not ready the send does not
fall back to the instance-addressed relay. Otherwise, or when that send
did not go out, it would name both households, so it waits: the row gets `announce_at` = now + 1–5 min
(`ANNOUNCE_JITTER_S`, never sooner than one already pending), and a later
sweep (`send_due_announces`) sends the pair's state *at that moment* —
sent only through the counterpart's mailbox while within `ANON_QUIET_S`
(5 min) of an anonymous register or claim the GFS accepted (checked right
before the send). One that did not go out (daily budget, delivery) moves
forward by a fresh jitter; one that went out clears the marker unless the
row's mailbox changed meanwhile. Updates are sent once and never queued
in the outbox (its quick replays would ignore the quiet window). Each
carries a per-pair `seq` inside the encrypted payload — the sender's wall
clock in ms, at least one past its last (kept on the row as `seq`,
persisted before the send), so it keeps rising across a re-seat, a reset
row or a restored backup; the receiver keeps the last one it took as
`peer_seq` and drops any update not newer — a stalled entry can never land
after a later `null`. The receiver of an update verifies the entry like the one in the
redeem (identity signature, key-wrap binding, the space) and that it names
the GFS the seat reaches it through, and stores it on that pair's row
only. `SPACE_MAILBOX_UPDATED` ships in v_88 (unreleased when it was
added), so no version bump; it is allowed from a `space_session` seat and
is follower-safe (it touches only the pair's own row).

**What the GFS sees.** Per mailbox frame: the mailbox id, the time, the size
bucket and the sender's IP. Per claim socket: its IP and online time. Never
an instance id, space id, event type or sender id for mailbox traffic.
Residuals:

- The `SPACE_SESSION_CLEANUP` sent at teardown is space-less and goes after
  the rows are gone, so it rides the instance-addressed relay — one frame
  per teardown.
- The joiner→host direction stays instance-addressed when the host sent no
  entry (an older host, or no ticket).
- IP / timing correlation with the household's identified `/gfs/ws`, as for
  every relayed frame. **Open for the owner:** a mailbox is minted at seat
  time, right next to the identified redeem / ACK, so a GFS that watches
  timing can tie each NEW mailbox to the household that registered it the
  moment it appears. A pre-registered pool of unassigned mailboxes (taken
  at seat time with no network call) would close that, but it changes the
  derivation (a pool slot instead of the space) — left for a follow-up.
- A household that already holds a `space_session` seat with the host
  and joins a **second** space of it redeems over the existing seat (the
  direct §D2 path), so no mailbox is minted for that second space: its
  traffic stays instance-addressed until PR 3 lets a seat add a mailbox
  for a further space.
- A household that later pairs with a `space_session` peer keeps the
  pair's mailboxes until the space seat ends (the transport stops using
  the entry once the row is no longer `space_session`).

**Mixed versions.** No entry, an entry below v_88, or a GFS without
`envelope_mailbox` → today's path. An older host ignores the `mailbox`
field in the redeem and sends no entry back.

**Cost.** Per envelope: the same bytes as today (the same buckets). Per
join: two registers (≤ ~1 KiB each, one ticket each — the joiner's and the
host's), one ~1 KiB entry in the sealed redeem and one in the sealed ACK.
Sockets: +1 anonymous WS on each side per GFS-only member pair. Worst
realistic case, a global space with 200 GFS-only members: 400 extra sockets
at the GFS (200 the members', 200 the host's), and the host spends 200
tickets over time. A household caches up to `TICKET_BATCH` (32) tickets and
refills on connect and about daily — inside the GFS's default
`mailbox_tickets_per_day` (50) and well under its per-address register cap
`mailbox_max_per_ip` (200 a day). A host seating 200 members in one day
mints the first 32 at seat time; the rest start instance-addressed toward
the host (the host still uses THEIR mailboxes) and catch up through the
sweep's retry as tickets arrive — 32 a day, so about a week for 200.
Transient, never permanent. Mints run one at a time (the service lock); a
host mint is bounded to 3 s per seat. The host's fan-out is still 200
sealed envelopes per item, as before.

**Claim timing.** Each claim waits its own uniform 0–60 s
(`CLAIM_JITTER_S`) before its first connect after a restart and after a
lost socket (a GFS restart drops them all), and pings at its own period
(20–40 s, `CLAIM_HEARTBEAT_S`) — sockets opened together neither come back
nor ping in step. A mailbox minted at seat time connects at once (its
register already sits next to the seat).

**Daily caps (a GFS that answers every claim with 4401).** Per member pair
and day: at most `ANNOUNCES_PER_DAY` (6) `SPACE_MAILBOX_UPDATED` sent and
`FAILED_ANNOUNCES_PER_DAY` (12) failed attempts (the last of each kept for
a `null`),
`RECOVERY_REGISTERS_PER_DAY` (4) re-registrations of a lost mailbox (≤ 4
tickets) and `ROTATIONS_PER_DAY` (1) fresh generation (1 ticket). Such a GFS
costs a pair at most 5 tickets and 6 small envelopes a day — never a storm.

**Catch-up at 200 pairs.** One retry per sweep (~10 min, jittered): a host
with 200 GFS-only pairs that minted nothing at seat time needs about 200
sweeps ≈ 33 h to mint them all (tickets permitting). Pairs whose member
mailbox the host holds (the usual case: members mint at seat time) are told
at once through it. Pairs with no mailbox either way are announced
instance-addressed, one per quiet sweep — and a sweep that just registered
is not quiet — so about another 33 h after the retries finish: roughly 2–3
days on the instance-addressed relay for those. Accepted: the fallback is
transient (the instance-addressed relay delivers meanwhile).

**Decision and open question for PR 3 (v_89).** One mailbox per member pair
is right while only the host and its GFS-only members talk (N members → N
host mailboxes). Member-to-member delivery (v_89) would make it one per
member PAIR across the space — N² mailboxes, sockets and tickets (200
members ≈ 40 000). PR 3 must re-evaluate this scaling before it builds on
it (e.g. per-pair only toward the host, one per member for member traffic,
or a different budget model). Two GFS caps already bound per-pair today and
must be in that review: `mailbox_max_sockets_per_ip` (512 claim sockets
per address) — past it a claim gets `429 too_many_sockets`, which the
household treats as a plain backoff, so a host with more than ~500 pairs
loses frames past the GFS queue without noticing — and `mailbox_max_per_ip`
(200 new mailboxes per address and day).

**Not in this step (PR 3+).** Passing entries to all members (v_89),
member-to-member mesh, a host `SPACE_ROUTED` hop, a generic
`SPACE_MEDIA_WANTED`, collapsing paths, a media-field registry, the key
handoff for a GFS-only admin, and a parity check.

```mermaid
sequenceDiagram
    participant E as Joiner E (GFS only)
    participant G as GFS
    participant A as Host A
    E->>E: mint (space, gen): keys from the mailbox root
    E->>G: POST /gfs/mailbox/register {…, ticket} (anonymous)
    E->>G: GET /gfs/mailbox/ws — claim mailbox E
    E->>G: POST /gfs/envelope {to_instance: A, sealed redeem + entry E}
    G->>A: envelope (A's /gfs/ws)
    A->>A: verify entry E (identity sig, key-wrap binding, space)
    A->>G: POST /gfs/mailbox/register (one per member pair)
    A->>G: GET /gfs/mailbox/ws — claim mailbox A
    A->>G: POST /gfs/envelope {to_instance: E, sealed ACK + entry A}
    G->>E: envelope (E's /gfs/ws)
    E->>E: seat; space_instances.mailbox = {peer: entry A, own}
    Note over A,E: later, a space post
    A->>G: POST /gfs/envelope {to_instance: mailbox E, sealed to kem_pk E} (anonymous session)
    G->>E: frame on E's mailbox claim socket
    E->>E: open with the mailbox key → §24.11 (gfs_mailbox, check_mailbox_space)
```

Implementation: `socialhome/domain/space_mailbox.py` (entry and stored
shape, suites), `socialhome/federation/space_mailbox_entry.py`
(`build_entry`, `verify_entry`),
`socialhome/services/space_mailbox_service.py` (`SpaceMailboxService`:
root, tickets, `mint`, `remember`, `target_for`, the sweep,
`release_unused`, claim sockets),
`socialhome/federation/invite_token_redeem.py` (the entry in the sealed
redeem / ACK), `FederationTransport._mailbox_target`,
`GfsRelayTransport.send_mailbox` / `send_media_mailbox` (`MailboxTarget`),
`GfsEnvelopeSender.send_to_mailbox`, `GfsRelayInbound.handle_mailbox_frame`,
`inbound_validator.TRANSPORT_GFS_MAILBOX` / `make_check_mailbox_space`,
`SpaceService.revoke_space_session_if_orphaned` (release), migration 0144.

## Privacy — who learns what

| Party | Learns |
|---|---|
| A (prober) | Which of ITS OWN servers B also uses (the intersection) — nothing about B's other servers. |
| B (receiver) | The same intersection, from which of its own sockets the probe arrived on — nothing about A's other servers. |
| A server both use (Y) | Relay bodies `{to_instance, sealed}` both ways, padded to a size bucket like any relayed envelope: recipient, time, size bucket. The content is opaque, but a probe followed within seconds by a small blob in the other direction — and a probe back — may be recognisable as a probe / ack exchange by timing and size alone; that reveals only that the two households exchange relayed envelopes through it, which the relay fallback concedes anyway (below). |
| A server only A uses (X) | One relay body addressed to B's instance id, which it queues and never delivers (B is not its client). It learns that *someone* addressed B — the same as any relayed envelope. |
| A server only B uses (Z) | The same, mirrored: B's probe back, addressed to A, queued and never delivered. |

### Residuals

- **A malicious server cannot plant a route.** A server one household
  uses (X) may re-post that household's sealed probe onto another server
  the peer uses (Z). The peer then acks through Z, where the prober never
  reads, and records nothing — at most it is made to spend one ack (and
  its throttled probe back) on a server that leads nowhere. Pinned by
  `test_a_malicious_gfs_replaying_a_probe_elsewhere_creates_no_route`.

- **A paired peer can test a server it is not on.** A malicious paired
  peer could `POST /gfs/envelope` a probe through an arbitrary server it is
  *not* registered with (the relay body is identity-free, so nothing stops
  it) — our ack, queued there for it, tells it that we use that server
  once it connects to collect it. We also record that server as a route
  to the peer until it expires. This is inherent to any intersection
  scheme that lets two parties confirm a shared server, and is limited to
  households we paired with and opted into the relay with (anyone else's
  probe is refused by the §24.11 pipeline before the handler runs). Our
  own sender never does this.
- **A server can correlate the posting IP.** The relay body is
  identity-free, but the HTTP request that carries it still comes from the
  sender's address, and the sender holds its own WebSocket to the same
  server — so the server can link a `POST /gfs/envelope` to the household
  that owns that socket. Accepted as a documented limit (owner decision
  2026-10-08); see [`principles.md`](../principles.md) for the relay's
  metadata concessions.

## Implementation

- `socialhome/services/peer_gfs_relay_service.py` — `PeerGfsRelayService`:
  the per-connection switch (on: opt in, send our key, probe; off: opt
  out, drop routes) and the probe on a newly learned peer key (v_54).
- `socialhome/services/capabilities_outbound.py` — our key-wrap fields
  in `INSTANCE_CAPABILITIES_UPDATED` for opted-in paired peers;
  `socialhome/services/federation_inbound/pairing.py` —
  `_apply_peer_keywrap` on the receiving side.
- `socialhome/services/gfs_route_discovery_service.py` —
  `GfsRouteDiscoveryService`: `probe_peer` / `probe_all`, the probe and
  ack handlers (registered in `attach_to`), pending-nonce map, throttles,
  `expire_stale_routes`.
- `socialhome/infrastructure/gfs_route_discovery_scheduler.py` —
  `GfsRouteDiscoveryScheduler`: 24 h ± 1 h timer, coalesced `trigger()`,
  `_stop: asyncio.Event` lifecycle.
- `socialhome/federation/federation_service.py` —
  `send_event_via_gfs` (seal like `send_event`, deliver via one server).
- `socialhome/federation/transport.py` — `FederationTransport
  .send_via_gfs_url` (one named server) and the round-robin relay tier.
- `socialhome/services/gfs_relay_inbound.py` — `RELAY_DELIVERED_VIA`;
  relayed pairing bodies (`pairing_peer_accept` / `_confirm`).
- `socialhome/federation/pairing_coordinator.py` /
  `socialhome/federation/pairing_gfs_reach.py` — the bootstrap route
  seeded from a pairing code (`_seat_relay`).
- `socialhome/federation/inbound_validator.py` —
  `make_check_relay_opt_in`.
- `socialhome/repositories/federation_repo.py` — `upsert_gfs_route`,
  `list_gfs_routes`, `delete_gfs_routes_older_than`
  (`peer_gfs_routes`, migration 0081).
- `socialhome/app.py` — `_build_gfs_route_discovery`; the GFS
  `on_connected` hook calls `trigger()`.
- v_87 binary media frames: `socialhome/domain/relay_media_frame.py`
  (outer shape), `socialhome/federation/gfs_relay_media.py` (seal / open),
  `GfsRelayTransport.send_media`, `FederationService._send_media_relay` /
  `handle_inbound_media_relay`, `GfsRelayInbound._handle_media`; GFS side
  `global_server/envelope_relay.py` (`submit_media`, `accept_media`,
  `SlidingByteBudget`), `global_server/routes/envelope.py`,
  `gfs_media_queue` (GFS migration 0028).

## Spec references

§24.11 (inbound pipeline), §24.12 (transport tiers), §D2b (connection-server
envelope relay), §25.8.21 (encryption-first).
