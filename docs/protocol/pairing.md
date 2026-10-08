# Pairing

The one-time handshake that establishes an end-to-end encrypted trust
relationship between two HFS instances. All subsequent federation
traffic between the pair rides on the directional session keys derived
here.

## Scope

- **HFS**: full participant. Scans / presents a QR code, runs the
  three-message DH handshake, stores the resulting session keys.
- **GFS**: uninvolved for a classic code (`reach = url`, the default) —
  pairing is strictly peer-to-peer. A code with a **GFS reach**
  (`url_gfs` / `gfs`, [below](#reach--pairing-through-a-gfs)) names ONE
  connection server both households use; it relays the sealed
  peer-accept / -confirm as opaque, identity-free blobs and learns
  nothing else.

**Not a pairing:** the §D2b invite-link bootstrap redeem
([`invites.md`](./invites.md)) also short-circuits the §24.11 pipeline
with a self-signed, TOFU-verified body, but it is not a handshake — it
creates a **space-scoped** `remote_instances` row
(`InstanceSource.space_session`) that is excluded from DMs, the user
roster, presence, the friends constellation and the auto-pair vouching
relay, and it publishes no `PairingConfirmed`. It runs over the
connection server rather than the inbox URL, and it never creates a
paired (`manual`) row — a GFS-reach pairing does.

## Event types

`PAIRING_INTRO`, `PAIRING_INTRO_RELAY`, `PAIRING_INTRO_AUTO`,
`PAIRING_INTRO_AUTO_ACK`, `PAIRING_INTRO_AUTO_ACK_VIA`,
`PAIRING_ACCEPT`, `PAIRING_CONFIRM`, `PAIRING_PEER_ACCEPT`,
`PAIRING_PEER_CONFIRM`, `PAIRING_ABORT`, `UNPAIR`, `URL_UPDATED`.

## Receiver rules

- An inbox `PAIRING_CONFIRM` never confirms a pairing. A pairing becomes
  `CONFIRMED` only through this household's own verification step (the
  admin entering the code, the token-bound peer-confirm, or the vouched
  auto-pair finalisation); the event is logged at WARNING and ignored.
- `PAIRING_ABORT` cancels a pending session only when the peer identity
  stored on it derives to the envelope's `from_instance` — a household
  can cancel its own handshake, never another's.

## Flow — direct QR handshake

The bootstrap handshake rides the **federation inbox URL** as two
plaintext, Ed25519-signed federation events: `PAIRING_PEER_ACCEPT`
(B → A) and `PAIRING_PEER_CONFIRM` (A → B). The receiving
federation-inbox view peeks the body's `event_type` and dispatches
pairing events directly to the pairing coordinator — short of the
§24.11 pipeline, which assumes a confirmed `RemoteInstance` row that
doesn't exist until pairing completes.

The federation inbox path is the only public surface peers reach
through the HA / HAOS Supervisor Ingress proxy, so anchoring the
bootstrap there is what keeps QR pairing working under those modes.
Auth is the body's Ed25519 signature (TOFU on first contact, plus
the SAS round-trip to close the MITM window).

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(inviter)
    participant B as HFS B<br/>(scanner)

    A->>A: generate QR<br/>(own_inbox_id, identity_pk,<br/>dh_pk, inbox_url, token, expiry)
    Note over A,B: user shows QR to B

    B->>B: scan QR → accept_pairing()<br/>derives shared DH secret,<br/>stores local RemoteInstance for A
    B->>A: POST {A.inbox_url}<br/>{event_type: PAIRING_PEER_ACCEPT,<br/>B.identity_pk, B.dh_pk,<br/>B.inbox_url, B.display_name,<br/>token, SAS, home_lat?, home_lon?,<br/>Ed25519 signature}

    A->>A: federation-inbox view dispatches<br/>PAIRING_PEER_ACCEPT → handle_peer_accept:<br/>TOFU verify sig, derive shared secret,<br/>KEK-encrypt keys, save RemoteInstance for B,<br/>publish PairingAcceptReceived
    A-->>A: WS pairing.accept_received →<br/>admin UI auto-fills SAS digits

    Note over A,B: admins compare SAS<br/>out-of-band

    A->>A: admin enters SAS → confirm_pairing()<br/>flips local RemoteInstance → CONFIRMED
    A->>B: POST {B.inbox_url}<br/>{event_type: PAIRING_PEER_CONFIRM,<br/>token, A.instance_id, Ed25519 signature}

    B->>B: federation-inbox view dispatches<br/>PAIRING_PEER_CONFIRM → handle_peer_confirm:<br/>verify sig with stored A.identity_pk,<br/>flip local RemoteInstance → CONFIRMED,<br/>publish PairingConfirmed

    Note over A,B: both sides hold CONFIRMED pair;<br/>normal §24.11 federation starts here.
    A-->>B: URL_UPDATED<br/>(if URL changes later)
```

### `PAIRING_PEER_ACCEPT` body fields

| Field | Required | Notes |
|---|---|---|
| `event_type` | yes | `"PAIRING_PEER_ACCEPT"` |
| `identity_pk` | yes | B's Ed25519 public key (base64). |
| `dh_pk` | yes | B's X25519 ephemeral DH public key (base64). |
| `inbox_url` | yes | B's federation inbox URL. May be `""` only on a GFS-reach pairing when B carries `keywrap_*` (B has no address; A reaches it through the bootstrap GFS). |
| `display_name` | no | B's household display name. |
| `token` | yes | The pairing token from A's QR / copy code. |
| `verification_code` | yes | SAS digits to be verified out-of-band. |
| `sig_suite` | no | `"ed25519+mldsa65"` when B supports hybrid PQ signatures. |
| `pq_identity_pk` | no | B's ML-DSA-65 public key (when `sig_suite` is hybrid). |
| `pq_algorithm` | no | PQ algorithm name (when `sig_suite` is hybrid). |
| `home_lat` | no | B's household latitude, truncated to 4 decimal places. Sent only when both `home_lat` and `home_lon` are available (HA / HAOS mode, or operator-configured). See [home-location.md](./home-location.md). |
| `home_lon` | no | B's household longitude, truncated to 4 decimal places. |
| `keywrap_pk` | GFS reach | B's static X25519 key-wrap public key (hex) — what A seals relayed traffic to. |
| `keywrap_sig` | GFS reach | Ed25519 self-signature of `keywrap_pk` by B's identity key (b64url). A verifies the binding and refuses the accept on a mismatch (fail closed). |
| `keywrap_suite` | GFS reach | `"x25519"` (`keywrap_seal.KEM_SUITE_X25519`). Unknown or missing → refused, never defaulted. |
| `proto_version` | GFS reach | B's protocol version, recorded as a high-water mark so A can probe relay routes as soon as the pair confirms. |
| `signature` | yes | Ed25519 signature over the body (TOFU auth). |

### Household inbox URLs

Every inbox URL a household learns from outside — the QR / copy code's
`inbox_url`, the `PAIRING_PEER_ACCEPT` body's `inbox_url`, the simple-pairing
`a_inbox_url` / `from_a_inbox_url` / `c_inbox_url`, and `URL_UPDATED` — is
checked where it enters, before anything is stored or sent
(`socialhome/peer_url.py::validate_peer_url`):

- scheme `http://` or `https://`;
- a host is present;
- no credentials (`user@` / `user:pass@`) in the URL;
- no whitespace or control characters, a valid port, at most 2048 characters.

Plain `http://` stays allowed on any host: households pair across a LAN by
address or name (`homeassistant.local`), and envelope content is protected by
the pairing keys rather than the transport. The outbound pairing client
re-checks the URL right before each POST and does not follow redirects;
the envelope transport that later POSTs to the stored inbox URL follows at
most one same-host hop (see `docs/architecture.md`, Redirects).
A scanned code that fails the check is refused by `POST /api/pairing/accept`
with `422 INVALID_PEER_URL`; a peer-accept body that fails it gets `400`.

Connection-server (GFS) URLs apply the same rules plus a TLS requirement:
`https://`, or plain `http://` only on loopback / a private network. In the
other direction there is nothing to check: a household registers no address
with a connection server (`POST /gfs/register` carries none — the GFS holds
no household address and its relay fan-out is WebSocket-only).

When `home_lat` / `home_lon` are present, A records them on B's newly-created
`remote_instances` row immediately — the map pin is available as soon as the
pair is confirmed, without waiting for a separate `LOCAL_HOME_LOCATION_CHANGED`
broadcast.

## Reach — pairing through a GFS

A household with no reachable URL — or an admin who wants a fallback for
when its URL is down — issues a code with a **reach**
(`POST /api/pairing/initiate {reach, gfs_id?}`, see [`api.md`](../api.md)):

| `reach` | Code carries | When |
|---|---|---|
| `url` (default) | `inbox_url` — no GFS information anywhere | today's code; needs a federation base |
| `url_gfs` | `inbox_url` + the bootstrap GFS | needs a base and a relay-capable GFS connection |
| `gfs` | the bootstrap GFS only (`inbox_url = ""`) | needs a relay-capable GFS connection |

The bootstrap GFS is one of the code owner's own active connections whose
server proved `envelope_relay` on its signed `/gfs/info` (the requested
`gfs_id` when it qualifies, else the first). It is the only server ever
revealed, and only to whoever holds the code.

### Extra code fields (`url_gfs` / `gfs` only)

| Field | Notes |
|---|---|
| `reach` | `"url_gfs"` or `"gfs"`. An unknown value is refused (`INVALID_REACH`), never defaulted. |
| `gfs` | `{url, instance_id}` — the bootstrap server's base URL and the `gfs_instance_id` the owner pinned for it. |
| `keywrap_pk` / `keywrap_sig` / `keywrap_suite` | The owner's static key-wrap key (the one `/gfs/info` serves — never a fresh key), its identity binding signature, and the `"x25519"` suite tag. |
| `proto_version` | The owner's protocol version (see `PAIRING_PEER_ACCEPT` above). |

### Rules

- **Scanner adopts the code's reach.** It verifies the key-wrap binding
  (`verify_keywrap_binding`; fail closed — `KEYWRAP_INVALID`). When the
  code has no `inbox_url` (`gfs`) or the scanner has no URL of its own,
  it must itself be connected to the bootstrap server — matched by the
  pinned `gfs_instance_id`, falling back to the normalized base URL —
  or the scan is refused with `GFS_NOT_SHARED` **before any state is
  written or anything is sent**.
- **Delivery.** A body goes to the other side's inbox when it has one;
  otherwise — or when that POST did not reach the peer (network error,
  timeout, 5xx; never after a 4xx, which is the peer's own answer) and
  we hold a route to it through the bootstrap server — the *same signed body* is wrapped
  as `{kind: "pairing_peer_accept" | "pairing_peer_confirm", pairing:
  <signed body>}`, padded to a relay size bucket, sealed to the other
  side's key-wrap key and posted to the bootstrap server as the
  identity-free `{to_instance, sealed}`.
- **Receiving a relayed body.** `GfsRelayInbound` opens it, throttles it
  (own bucket, the bootstrap-body budget) and hands the inner body to
  the very handler the inbox uses — token, signature, expiry and
  single-use checks unchanged — with one more: the delivering connection
  (`RELAY_DELIVERED_VIA`) must be the one the pairing session was issued
  with (`pending_pairings.relay_via`); anything else is dropped. A frame
  from a server that is not one of our active connections never reaches
  a handler. Conversely, for a session on which we offered no address
  (`gfs` code / scanner without URL) a body on the inbox path is refused
  — nobody could know our inbox id.
- **Confirm checks** (inbox or relay): the session must not have expired,
  and the signer named in the confirm must be the household the token's
  session was made with (`pending_pairings.peer_identity_pk`).
- **Both sides opt into the relay.** Whoever writes the peer's row —
  the scanner on scan, the code owner on the accept — sets `gfs_relay`,
  stores the verified `remote_keywrap_pk` and **seeds one route**: its
  own connection to the bootstrap server — but only where the peer
  provably reads that server. The scanner always may (the code owner is
  on it by construction). The code owner seeds only when the accept
  arrived through the bootstrap server, or the scanner has no URL (a
  scanner without one refuses a code whose server it is not on). A
  scanner that answered at our inbox with its own URL may not be on that
  server at all; a route there would make every fallback a silent `202`
  and tell the server the scanner's id, so the code owner keeps the
  opt-in and the key and leaves the routes to discovery. This is not the poisoning case
  of [`gfs-relay.md`](./gfs-relay.md): the server was named in the
  out-of-band code and we are ourselves registered there — never seeded
  from where a relayed frame merely arrived. The row exists with the
  opt-in before any relayed message from the peer can arrive (the
  §24.11 relay opt-in gate would refuse it otherwise). `remote_inbox_url`
  stays `""` for a peer with no address; its first `URL_UPDATED` fills it.
- **After confirm** each side asks route discovery to probe the peer;
  the seeded route expires after 72 h like any route no ack refreshes.
- **A classic `url` pairing means no GFS**, also when it replaces an
  earlier GFS pairing with the same household: both sides switch the
  relay opt-in off and drop that peer's routes.
- A `proto_version` above our own is recorded as our own (a newer peer
  is relied on for what we know); a non-integer one is ignored.

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(code owner, no URL)
    participant G as GFS G<br/>(both connected)
    participant B as HFS B<br/>(scanner, no URL)

    A->>A: initiate {reach: gfs}<br/>session.relay_via = conn A·G
    Note over A,B: QR / code: token, identity_pk, dh_pk, inbox_url "",<br/>reach, gfs {url, instance_id}, keywrap_pk/sig/suite
    B->>B: verify key-wrap binding,<br/>match G among own connections (else GFS_NOT_SHARED)
    B->>B: row for A: manual, PENDING_RECEIVED, gfs_relay=1,<br/>keywrap pk, route (A, conn B·G)
    B->>G: POST /gfs/envelope {to_instance: A, sealed}<br/>(PAIRING_PEER_ACCEPT incl. B's keywrap)
    G->>A: WS frame {sealed}
    A->>A: open → handle_peer_accept (token, sig, expiry,<br/>single use, delivered via conn A·G ✓)
    A->>A: row for B: gfs_relay=1, keywrap pk,<br/>route (B, conn A·G); SAS to admin
    Note over A,B: admins compare SAS out-of-band
    A->>A: confirm → CONFIRMED
    A->>G: POST /gfs/envelope {to_instance: B, sealed}<br/>(PAIRING_PEER_CONFIRM)
    G->>B: WS frame {sealed}
    B->>B: handle_peer_confirm (delivered via conn B·G ✓) → CONFIRMED
    Note over A,B: each side: probe_peer → route discovery refines routes;<br/>§24.11 traffic rides the seeded route meanwhile
```

### Older households

No protocol bump: the new code fields and relay kinds reach only a peer
that just showed it has them. An older code owner never issues a reach
code; an older scanner of a `gfs` code fails closed on the empty
`inbox_url` (`validate_peer_url`, as any classic code without a URL); an
older scanner of a `url_gfs` code ignores the extra fields and pairs
URL-only — its accept carries no key-wrap key, so the code owner pairs
URL-only too (no opt-in, no route, no probe).

### Error codes

| Code | Where | Meaning |
|---|---|---|
| `INVALID_REACH` (422) | initiate, accept | Unknown `reach`. |
| `NOT_CONFIGURED` (422) | initiate | `url` / `url_gfs` without a federation base. |
| `GFS_NOT_CONNECTED` (422) | initiate | `url_gfs` / `gfs` without an active, relay-capable GFS connection. |
| `GFS_NOT_SHARED` (422) | accept | The code (or our missing URL) needs its bootstrap GFS, and we are not connected to it. Nothing was sent. |
| `KEYWRAP_INVALID` (422) | accept | The code's key-wrap key is missing, unlabelled or not bound to its identity. |

Implementation: `socialhome/federation/pairing_gfs_reach.py`,
`socialhome/federation/pairing_coordinator.py`,
`socialhome/federation/peer_pairing_client.py` (`build_relay_envelope`),
`socialhome/services/gfs_relay_inbound.py`. Tests:
`tests/protocol/test_gfs_pairing_reach.py`.

## Manual code fallback — `socialhome://` URL scheme

The QR path fails the moment a camera is missing or denied, the QR is
too small to focus on a phone screen, or the two households are
pairing remotely (over chat / SMS / email). To keep pairing possible
in those cases, the SPA exposes a **copy/paste pairing code** as an
equal-weight peer to the QR — not a hidden fallback.

The code is a single-line `socialhome://` URL that survives chat
copy/paste round-trips. Two shapes:

- **Instance pairing (households)** — `socialhome://pair#<base64url(JSON)>`.
  The fragment carries the exact same JSON object the QR encodes
  (`token`, `instance_id`, `identity_pk`, `dh_pk`, `inbox_url`,
  `expires_at`, plus the optional post-quantum `pq_*` fields).
  Encoding the payload in the **URL fragment** (`#…`) — rather than
  a query string — keeps it client-side: a stray paste into a
  browser address bar never sends the secret to a third party's
  server logs, because fragments are not transmitted on HTTP
  requests.

- **GFS pairing** — `socialhome://gfs-pair/{base_url}?token={token}`.
  Direct migration of the GFS landing's previous `sh://gfs-pair/…`
  scheme. The pairing token is single-use with a 10-minute TTL
  (§24.7.4) — short enough to make casual reuse a non-issue, long
  enough for a screenshot ↔ paste handoff in another room.

### Back-compat

The scanner-side paste field accepts both the new `socialhome://pair#…`
URL and **raw multi-field JSON** (what the older QR codes encoded
directly). A code in flight in someone's chat thread keeps working
across the 5-minute token TTL.

### Where it shows up

- **Inviter side** — the modal renders the QR and the
  `socialhome://pair#…` code in a peer card, side-by-side on desktop
  and stacked under an OR divider on mobile. A "Copy code" button
  next to the card writes the URL to the clipboard.
- **Scanner side** — a two-method picker (Scan QR / Paste code) at
  the top of the scan step replaces the previous camera-with-buried-
  fallback layout. Both methods are equal-weight cards. The paste
  textarea decodes whichever shape the user provides.
- **GFS landing page** — the server-rendered HTML (`GET /` on a GFS)
  renders the `socialhome://gfs-pair/…` URL next to the QR with a
  small inline-JS Copy code button. The QR encodes the same URL.

## Flow — auto-pair via relay

When two instances can't scan each other's QR but share a mutual peer
`C`, they can bootstrap trust via `C`. The relay sees only opaque
ciphertext; the two endpoints derive the session keys themselves.

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A
    participant B as HFS B<br/>(relay — vouches for both)
    participant C as HFS C<br/>(target)
    A->>B: PAIRING_INTRO_AUTO<br/>{target_id: C, a_dh_pk, a_inbox_url}
    B->>C: PAIRING_INTRO_AUTO<br/>{from_a_*, vouch_sig over A's identity}
    Note over C: admin one-clicks "approve"<br/>(no QR scan, vouched by B)
    C->>B: PAIRING_INTRO_AUTO_ACK_VIA<br/>{to_a_id, c_pk, c_dh_pk, ack_sig}
    B->>A: PAIRING_INTRO_AUTO_ACK<br/>(forwarded — both hops over established trust)
    Note over A,C: A↔C now CONFIRMED;<br/>federation envelopes flow normally.
```

The ack rides through `B` rather than directly C → A because A
holds only a `PENDING_SENT` row for C (no identity key yet) — a
direct C → A envelope would fail the §24.11 inbound signature
check. Both legs of the ack are over established trust (A↔B and
B↔C are confirmed pairs, set up via QR before the auto-pair flow
started).

## Roster catch-up on pair-confirm — `USERS_SYNC`

Pairing carries the *instance's* metadata but no per-user roster.
Without a roster push, the peer's local ``remote_users`` mirror sits
empty until each member of the freshly-paired household happens to
edit their profile — at which point the existing
``USER_UPDATED`` outbound fans the change. In practice this means
the household sees only the admin (or whoever first touched their
profile) on the Friends dashboard / DM picker.

`UsersSyncOutbound` closes the gap. On `PairingConfirmed` it sends
a single `USERS_SYNC` envelope to the new peer:

```
{"users": [
  {"user_id", "username", "display_name", "bio", "picture_hash",
   "picture_webp_base64": <optional bytes>},
  ...
]}
```

The receiver-side handler (`FederationInboundService._on_users_sync`)
iterates the list and upserts each row through the same
``_upsert_remote_user`` path ``USER_UPDATED`` uses, so the inbound
code is unchanged. The per-pair user-visibility filter applies on
the sender — admins who have hidden a user from a peer keep that
user out of the snapshot.

When every local user is hidden from the peer (or there are no
local users at all), the envelope is suppressed entirely — no
empty-roster sync.

## Per-pair user visibility

The admin Connection-Detail modal toggles which **local** users
surface to a paired peer. Default-visible: a household member shows
up unless an admin has explicitly hidden them via
`PATCH /api/pairing/connections/{instance_id}/visible-users`. State
lives in `peer_user_visibility`; the sender-side gates read it
through the `VisibilityMixin` on each outbound service.

**Semantic: hide = remove.** A hide flips three things in one
admin click:

1. The peer is told to forget the user — `USER_REMOVED` fires and
   the peer's inbound handler marks the row
   `remote_users.deprovisioned_at`, dropping the user from
   `/api/friends`, the DM picker, member lists, and autocomplete.
2. The peer **cascade-purges** every moment, highlight, and DM
   conversation that user is involved in — hard delete on the
   peer's side so no orphan content from the hidden user lingers.
3. Future user-scoped envelopes from that user are dropped two
   different ways (sender + receiver, defence-in-depth — see
   below).

Un-hide fires `USER_UPDATED`, which un-sets `deprovisioned_at` on
the peer's `remote_users` row. The user re-appears, but the
purged content does **not** come back — un-hiding is a fresh
start, not a rewind.

**Sender-side gates** (efficient + privacy-respecting). Every
direct-fan-out user-scoped event is gated through the
`peer_user_visibility` lookup before the envelope is built. The
plaintext never hits the wire and never gets decrypted on the
blocked peer's process:

- `USER_UPDATED`, `USERS_SYNC` (profile catch-up)
- `USER_ONLINE`, `USER_IDLE`, `USER_OFFLINE` (session presence)
- `DM_MESSAGE`, `DM_MESSAGE_DELETED`, `DM_MESSAGE_REACTION`,
  `DM_MEDIA_BLOB`, `DM_USER_TYPING`
- `DM_RELAY` (at the originating node only — forwarding hops
  handle opaque ciphertext and cannot gate)
- `DM_HISTORY_CHUNK` (filtered at the conversation level:
  cross-household DMs are 1:1, so a hidden local participant
  suppresses the entire conversation's catch-up; the
  `DM_HISTORY_COMPLETE` envelope still fires so the requester's
  state machine terminates cleanly)
- `HIGHLIGHT_CREATED`, `HIGHLIGHT_FRAME_APPENDED`,
  `HIGHLIGHT_DELETED`, `HIGHLIGHT_FRAME_DELETED`,
  `HIGHLIGHT_FRAME_VIEWED`, `HIGHLIGHT_FRAME_REACTED`,
  `HIGHLIGHT_FRAME_REACTION_REMOVED`
- `MOMENT_CREATED`, `MOMENT_DELETED`, `MOMENT_REACTED`,
  `MOMENT_REACTION_REMOVED`

**Receiver-side backstop**. `MomentFederationOutbound` runs a 3-hop
relay (`relay_inbound`) — a household that's *not* the originator
re-broadcasts moments to its own peers, so a moment from a hidden
user can leak back to the blocked peer via that relay. The mid-path
relayer has no visibility into the originator's per-pair hide
policy, so the sender-side gate can't help. §24.11 step 11
(`make_check_deprovisioned_author`) closes the hole: every inbound
user-scoped envelope is checked against the receiver's
`remote_users.deprovisioned_at`, and dropped silently if the
author has been purged. The step runs after `persist_replay` so
the relayer's outbox sees a 200 OK and stops redelivering.

The deprovisioned-author filter covers the same event-type list as
the sender gates, keyed off the per-event-type author field
(`sender_user_id` / `reactor_user_id` / `viewer_user_id` /
`author_user_id` / `user_id`).

Space-scoped events (`SPACE_POST_CREATED`, `SPACE_COMMENT_*`,
`SPACE_STICKY_*`, `SPACE_CALENDAR_*`, etc.) are **not** gated by
this toggle — spaces own their own audience model.

Wire compat: zero. No new event types, no payload-field additions,
no `proto_version` bump. The sender simply doesn't emit some
envelopes, and the receiver drops some that arrive via relay.

## Key derivation

Each side holds an **Ed25519 identity key** (long-lived) and generates
a fresh **X25519 DH keypair** per pairing. The shared secret feeds
HKDF-SHA256 to produce two directional AES-256-GCM keys:

- `key_self_to_remote` — encrypts outbound envelopes.
- `key_remote_to_self` — decrypts inbound envelopes.

Both are stored alongside the peer in `remote_instances` with a
`PairingStatus.CONFIRMED` row. See `docs/crypto.md` for the full key
schedule.

## Local alias — household-only rename of a paired peer

The peer's `display_name` is whatever it sent on the federation
handshake. When it's empty or cryptic (e.g. the truncated
`instance_id`, "z7k63zfi") the admin is stuck with that name in
every UI surface — Friends dashboard, Connections list, DM picker.

The `remote_instances.local_alias` column (added in migration
`0005`) lets the admin pick a name they want to see for the
connection without renaming the peer remotely. The choice is
**never federated** — purely local UX state. Storage rules:

- `NULL` (default) → SPA falls back to `display_name`.
- Set via `PATCH /api/pairing/connections/{instance_id}/alias`
  with `{"alias": "Brother's house"}` (admin-only, 80-char cap).
- Cleared by posting `{"alias": null}` or a whitespace-only string.

The HTTP read shapes (`GET /api/pairing/connections`,
`GET /api/friends`) return `display_name` already resolved to the
effective value — the SPA renders one field and gets the alias
preference for free. The raw federated name stays available as
`federated_display_name` so the manage modal can show "They
advertise themselves as <federated_name>" alongside the editable
alias input.

A subsequent `save_instance` (e.g. URL rotation) preserves the
alias because the upsert doesn't touch the `local_alias` column.

## Pairing wizard — "Configure sharing" step

After the SAS verification succeeds and the pair is confirmed, the pairing
wizard advances to a **Configure sharing** step instead of closing
immediately. The step shows the `<ShareHomeToggle/>` component for the
just-paired peer, defaulting to ON (`share_home = true`). Operators can
flip it before hitting the final **Done** button; the change is applied via
`PATCH /api/pairing/connections/{instance_id}` with `{"share_home": false}`
and fires the one-shot null-coord revoke envelope described in
[home-location.md](./home-location.md#revoking-access). Leaving the toggle
ON (the default) requires no extra API call.

This step only appears for household pairings (the QR and copy-code flows);
GFS pairings do not expose the toggle.

## Federation inbox base — per-adapter shape

The pairing coordinator works with a "federation inbox base" URL —
the string a peer would POST to with a per-peer suffix appended.
What that string actually is depends on the platform adapter:

- **Standalone** — adapter owns the route. Returns
  `{external_url}/federation/inbox`; the addon listens on
  `/federation/inbox/{inbox_id}` directly.
- **HA / HAOS** — addon sits behind HA Core. The HA integration
  pushes the bare external URL (Nabu Casa Remote UI or admin-set
  `external_url`) via `PUT /api/ha/integration/federation-base`
  and registers an HA Core HTTP view at
  `/api/socialhome/inbox/{inbox_id}` that forwards into the addon's
  own `/federation/inbox/{inbox_id}`. The adapter splices that
  path onto the pushed value, so callers see
  `{external_url}/api/socialhome/inbox`. The append is idempotent —
  a future integration that ever pushes the full path won't cause
  a double-append.

Every adapter returns `None` until the URL is configured; the
pairing route surfaces that as a 422 `NOT_CONFIGURED` so the admin
knows to wire it up before issuing a QR. This applies to
household↔household pairing only — connecting to a GFS
(`/api/gfs/connections*`) never consults the federation base, so a
Home Assistant App can join the GFS before it has a public URL.

## URL rotation — `URL_UPDATED`

When this instance's externally-reachable inbox URL changes — admin
rotates `external_url` in standalone mode, Nabu Casa Remote UI flips
on/off in HA mode, or a reverse-proxy gets reconfigured — every
confirmed peer is told so their `remote_inbox_url` tracks the move.
Without this, the next envelope delivery silently fails with a
"No instance found" rejection at the stale URL.

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(URL changed)
    participant B as HFS B<br/>(peer)
    A->>A: detect base URL change<br/>(adapter.get_federation_base)
    loop for each confirmed peer
        A->>B: URL_UPDATED<br/>(inbox_url = new_base/peer.local_inbox_id)
        B->>B: update remote_instances.remote_inbox_url<br/>for A
    end
```

Payload: `{"inbox_url": "<full per-peer URL>"}`. The URL is
per-peer: sender appends the recipient's `local_inbox_id` to the new
base, so each `URL_UPDATED` envelope delivers to exactly one peer
with that peer's own secret path.

Who is told: every confirmed **paired** (`manual`) peer — including one
with no address of its own (paired [through a GFS](#reach--pairing-through-a-gfs);
the envelope rides the relay) — and never a household met through an
invite link (`space_session`: the GFS shields both addresses). The
published base is the adapter's **effective** one
(`adapter.get_federation_base()`): under HA that is the pushed URL plus
the HA-hosted forwarder path `/api/socialhome/inbox`, and an admin
override wins over a pushed value — `PUT /api/ha/integration/federation-base`
publishes only when that effective base changed.

Validation at the receiver: the envelope is already signature-verified
by the §24.11 inbound pipeline. The handler additionally rejects
empty URLs and anything that fails the
[household inbox URL rules](#household-inbox-urls). A peer whose stored
URL is `""` (paired through a GFS) takes its first URL this way.

## TTL + cleanup

`PAIRING_TTL_SECONDS = 300` — five minutes from the QR being issued
(or scanned) to the SAS being entered on both sides. Past that, every
handler in the coordinator rejects the in-progress message with
`Pairing session has expired`.

`PairingSessionPruneScheduler` (in
[`socialhome/infrastructure/`](../../socialhome/infrastructure/pairing_session_prune_scheduler.py))
runs once a minute and calls
[`federation_repo.cleanup_expired_pairings`](../../socialhome/repositories/federation_repo.py).
That call deletes the expired `pending_pairings` row AND any
PENDING_SENT / PENDING_RECEIVED `remote_instances` row whose
`local_inbox_id` matched it (with everything queued or learned for that
row, as in [Cleanup](#cleanup), in one transaction) — so the SPA's pending handshake list
self-empties within ~1 minute of expiry. CONFIRMED rows are protected
by an explicit status filter (the real `confirm()` path deletes the
session before flipping the instance, so the scenario is defensive
rather than actual).

## Unpairing

Either admin can end a pairing (`DELETE /api/pairing/connections/{instance_id}`),
and both households forget each other — not just the one that clicked, and
not just when the other household happens to be online.

The household that unpairs sends a signed, encrypted `UNPAIR` to the peer
**before** forgetting it. The order matters: the envelope is encrypted with
the pairwise session key and delivered to the inbox URL stored on the
peer's `remote_instances` row, so it can only go out while that row still
exists. The payload is empty — the receiver unpairs the **signer**
(`from_instance`, bound to the verified signature by the §24.11 pipeline),
never a household named in the body, so one peer can never tear down
another's pairing.

The first attempt is bounded (5 s, `UNPAIR_NOTIFY_TIMEOUT_S`) so an
unreachable peer never blocks the admin's unpair. When it lands, both sides
run the cleanup below and the route reports `peer_notified: true`.

### A peer that is offline at unpair time

When the first attempt does not land, the route reports
`peer_notified: false` and the pairing becomes an **unpair tombstone**
rather than being deleted: the `remote_instances` row stays with
`status = 'unpairing'` (a value the schema has always allowed — no
migration), because outbox redelivery needs its session key and inbox URL.
The tombstone grants **no trust**:

- every ordinary read hides it (`get_instance`, `list_instances`, social /
  space fan-outs), so it is gone from the connections list, rosters, DMs and
  every send path at once (`PeerUnpaired` fires immediately);
- the peer's people (`remote_users`) and our per-peer visibility rows are
  dropped right away, exactly as the row delete would cascade them;
- the §24.11 pipeline refuses everything it sends except its own `UNPAIR`,
  with the same `404 No instance found` a household we never knew gets
  (step 4b, after the signature check);
- the outbox sends it nothing but the `UNPAIR` — and an `UNPAIR` goes only to
  a tombstone, so a stale one can never hit a pairing made later. Queued DM
  and space media for it (`dm_media_outbox`, `space_media_outbox`) is
  dropped when the tombstone is made, and those senders skip tombstones —
  not even the space mesh fallback reaches it.
- the outbox refuses to *queue* anything else for it, too — and anything at
  all for a peer whose row is gone. The check is part of the outbox `INSERT`,
  so a send that was already in flight when the pairing ended (a space-sync
  offer, an ICE candidate) cannot leave a row behind the unpair's purge. The
  DM and space media outboxes carry the same `INSERT` guard: DM media only
  for a live (non-`unpairing`) pairing; space media never for a tombstone,
  and otherwise only for a paired household or a member household of that
  space (a mesh-only member has no `remote_instances` row).

Exactly one `UNPAIR` is queued, with a 30-day `expires_at`
(`UNPAIR_RETRY_MAX_AGE`) and the outbox's normal backoff. A signed envelope
from the tombstoned peer proves it is back online and pulls that retry
forward to the next outbox tick (only when the retry is parked in the
future, so a peer that keeps sending costs one read per envelope, not a
write). A `404` that does not carry our own `unknown_inbox` marker came
from something in front of the peer (e.g. its Home Assistant integration
not loaded yet), so for the `UNPAIR` it stays a retry until the 30-day
expiry instead of ending the tombstone. The tombstone ends when:

- the `UNPAIR` is delivered, or refused for good (the peer already forgot
  us — a 4xx from its Social Home) — the row is purged;
- the peer's own `UNPAIR` reaches us;
- the `UNPAIR` expires — the retention sweep fails it and the tombstone is
  purged; a peer returning after that is refused as a stranger;
- we pair with that household again — the new row (fresh keys, fresh inbox
  id) replaces the tombstone in one transaction.

The retry state lives in that one outbox row, so the tombstone needs no
column of its own.

### Cleanup

Both directions run the same cleanup (`PeerUnpairService.forget`), built on
`PeerUnpairService.purge` — the one helper every service that removes a
`remote_instances` row goes through (unpair, the space-session cleanup of a
link-joined seat, an inbound `PAIRING_ABORT` for a half-paired household).
In order:

- delete the `remote_instances` row first (`remote_users` and
  `peer_user_visibility` cascade) — from then on every outbox `INSERT`
  refuses the household, so the deletes below sweep everything a send still
  in flight could have queued,
- drop every queued outbox envelope for the peer (it can never be
  delivered without the row), and its queued DM / space media,
- drop the mesh topology the peer announced (`network_discovery` rows it
  is the source of — it is no longer a trusted neighbour),
- publish `PeerUnpaired`, which pushes `connection.removed` to every
  household member (`forget` only; the other purge callers publish their
  own event or none).

A crash between the row delete and the outbox delete leaves rows addressed
to nobody; the outbox's retention sweep (`purge_orphaned`, also run at
startup) deletes every `federation_outbox` row whose `remote_instances` row
is gone, whatever its status; the DM and space media outboxes run the same
sweep when their sync services start (space media keeps rows for a member
household of that space). A tombstone still has its row, so its queued
`UNPAIR` survives the sweep. The repository's own bulk housekeeping —
expired pending handshakes, a tombstone replaced by a re-pair — runs the
same cleanup in the same order inside its own transaction: the instance
row first, then its outbox envelopes, DM and space media and the mesh
hints it announced (the re-pair before the new row is written, so nothing
the new pairing queues is touched).

Space membership is **not** touched. A space is shared by its members, not
by the pairing: two households that stop being direct connections stay
co-members of any space they share, and its content keeps arriving over the
mesh.

```mermaid
sequenceDiagram
    participant AdminA as Admin (household A)
    participant A as Household A
    participant B as Household B
    AdminA->>A: DELETE /api/pairing/connections/B
    A->>B: UNPAIR (encrypted, Ed25519-signed; ≤ 5 s)
    alt B online
        B->>B: §24.11 pipeline — verify A's signature
        B->>B: forget(A): outbox, mesh hints, row, PeerUnpaired
        A->>A: forget(B)
        A-->>AdminA: 200 {ok, peer_notified: true}
    else B offline
        A->>A: tombstone B (status 'unpairing'), queue one UNPAIR (30 d)
        A-->>AdminA: 200 {ok, peer_notified: false}
        Note over B: B comes back, still thinks it is paired
        B->>A: any envelope (e.g. INSTANCE_CAPABILITIES_UPDATED)
        A-->>B: 404 No instance found (signature checked, UNPAIR pulled forward)
        A->>B: UNPAIR (outbox retry)
        B->>B: forget(A)
        B-->>A: 2xx
        A->>A: purge tombstone
    end
```

## Implementation

- `socialhome/federation/pairing_coordinator.py` — state machine for
  direct + auto-pair flows, plus `handle_peer_accept` /
  `handle_peer_confirm` for the bootstrap transport.
- `socialhome/federation/peer_pairing_client.py` — outbound HTTP
  client that POSTs `PAIRING_PEER_ACCEPT` / `PAIRING_PEER_CONFIRM`
  bodies directly to the peer's federation `inbox_url`. Signs bodies
  with Ed25519 using this instance's identity seed.
- `socialhome/peer_url.py` — `validate_peer_url`, the shared
  household / connection-server URL rules.
- `socialhome/routes/federation.py` — `FederationInboxView` peeks the
  body's `event_type` and dispatches `PAIRING_PEER_ACCEPT` /
  `PAIRING_PEER_CONFIRM` to the pairing coordinator ahead of the
  §24.11 pipeline.
- `socialhome/routes/pairing.py` — local-only admin routes used by
  the UI (`/api/pairing/initiate`, `/accept`, `/confirm`). All three
  require a signed-in admin (bearer token, or ingress headers under haos);
  none is on the auth middleware's public-path list — the peer's side of
  the handshake only ever arrives through the federation inbox. Managing
  household connections is admin-only throughout: introductions,
  auto-pair via a trusted peer, the approve / decline queues, per-peer
  settings and unpairing all answer `403` to a non-admin. Only the
  connections listing stays readable by every signed-in member.
- `socialhome/services/federation_inbound/pairing.py` — §24.11
  inbound handlers for already-paired peers (covers
  `PAIRING_INTRO_RELAY`, `URL_UPDATED`, `UNPAIR`).
- `socialhome/services/peer_unpair_service.py` — `unpair` (send
  `UNPAIR`, then forget) and the shared `forget` step both directions use.
- `socialhome/services/url_update_outbound.py` — outbound fan-out of
  `URL_UPDATED` when this instance's base URL changes.
- `socialhome/crypto.py` — key derivation primitives.

## Spec references

§11 (Instance Pairing & Encrypted Inboxes),
§25.8.20 (session key derivation),
§S-13/S-14 (SAS verification and answer-origin audits).
