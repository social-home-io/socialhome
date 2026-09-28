# Social Home Apps — cross-household federation

Federated app-to-app messaging that lets an installed Social Home App exchange
state with the same app running in a paired household.  A chess game, shared
whiteboard operation, or custom mini-app payload sent by Alice's household
arrives at Bob's household as an `app.message` WebSocket frame that the
host bridge relays into the running iframe — all without the app or the frame
content ever appearing in plaintext on the network.

## Scope

- **HFS**: full participant.  The `AppFederationService` drives session
  lifecycle (`APP_SESSION`) and message delivery (`APP_MESSAGE` / binary
  frames).  The `FederationService` handles encryption, signing, and the
  §24.11 inbound validation pipeline identically to every other event type.
- **GFS**: uninvolved.  App federation is strictly peer-to-peer between
  directly confirmed households.

## Event types

`APP_SESSION`, `APP_MESSAGE`.

## Transport selection

Two transport paths carry app messages; the same `session_id` scopes both:

| Path | Condition | Notes |
|---|---|---|
| **`fed-app-v1` binary DataChannel** | peer is CONFIRMED **and** channel is open **and** `peer_supports(MIN_FOR_APP_CHANNEL)` (v_17) | Fast path — no base64 overhead; high-frequency app traffic does not head-of-line-block control envelopes on `fed-v1`. **No `to_user` routing slot** — the binary frame format (v1) carries no per-user routing field. Binary inbound always falls back to the household fan-out; the receiver disambiguates by `session_id` (documented v1 limitation). |
| **`APP_MESSAGE` JSON event over `fed-v1` / HTTPS** | anything else | Fallback — identical encryption and §24.11 validation; lower throughput. Carries `to_user`/`from_user` when the peer is v_18+ (see capability v_18). |

Session control (`APP_SESSION`) always uses the JSON event path regardless of
peer version, so sessions open and close correctly even against pre-v17 peers.

## Flow — open session and send a message

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A<br/>(initiator)
    participant B as HFS B<br/>(receiver)
    participant WS_B_user as B target user<br/>(WebSocket)
    participant WS_B_all as B all users<br/>(WebSocket, legacy fallback)

    A->>A: AppFederationService.open_session(app_id, target={instance_id:B, user_ref:"bob", is_local:false})<br/>→ _assert_target_allowed (roster check)<br/>→ allocate session_id, _require_enabled check

    alt peer B is v_18+ (MIN_FOR_APP_USER_ROUTING)
        A->>B: APP_SESSION event<br/>{app_id, session_id, verb:"open",<br/>to_user:"bob", from_user:"alice"}<br/>encrypted + Ed25519-signed
        B->>B: §24.11 pipeline → on_inbound_event()<br/>→ _deliver(to_user="bob") → resolve to local user
        B->>WS_B_user: app.message WS frame<br/>{type:"app.message", app_id, session_id,<br/>from_instance, from_user:"alice", kind:"session", payload}
        B->>B: AppChallengeReceived → bell row + title-only push (§25.3) for bob only
    else peer B is sub-v_18 (legacy household-addressed)
        A->>B: APP_SESSION event<br/>{app_id, session_id, verb:"open"}<br/>(no to_user / from_user)<br/>encrypted + Ed25519-signed
        B->>B: §24.11 pipeline → on_inbound_event()<br/>→ _deliver(to_user=None) → household fan-out
        B->>WS_B_all: app.message WS frame (all users with app enabled)
    end

    Note over A,B: session open; moves/ops follow

    alt fed-app-v1 binary path (v_17+ CONFIRMED direct peer — no per-user routing slot)
        A->>A: FederationService.send_app_message()<br/>→ encrypt payload (AES-256-GCM)<br/>→ sign envelope<br/>→ build binary frame [u8 type][u32 hlen][header][u32 plen][payload]
        A->>B: binary frame on fed-app-v1 DataChannel
        B->>B: iter_complete_frames → §24.11 header re-validation<br/>→ decrypt payload → verify payload_sha256
        B->>B: on_inbound_message() — no to_user slot; household fan-out
        B->>WS_B_all: app.message WS frame
    else JSON path (sub-v17 or channel unavailable) — carries to_user for v_18+
        A->>A: FederationService.send_app_message()<br/>→ APP_MESSAGE JSON event (+ to_user/from_user when peer is v_18+)<br/>encrypted + Ed25519-signed
        A->>B: APP_MESSAGE event over fed-v1 / HTTPS inbox
        B->>B: §24.11 pipeline → on_inbound_event()
        B->>WS_B_user: app.message WS frame (routed to to_user if v_18+, else all users)
    end

    A->>B: APP_SESSION event {verb:"close"}<br/>(always JSON, always encrypted)
    B->>WS_B_all: app.message WS frame {verb:"close"}
```

## Payload field tables

### `APP_SESSION`

| Field | Required | Notes |
|---|---|---|
| `app_id` | yes | Installed app identifier. Must match a locally-enabled app on the receiver or the inbound is silently dropped. |
| `session_id` | yes | Hex UUID allocated by the initiator.  Scopes all subsequent `APP_MESSAGE` frames for this session. |
| `verb` | yes | `"open"` / `"accept"` / `"close"`. |
| `to_user` | no (v_18+) | The target user's username on the receiving household.  When present and non-empty, the receiver delivers the frame only to that user (instead of fanning out to all local users).  Omitted for sub-v_18 peers — gated on `FederationCapability.MIN_FOR_APP_USER_ROUTING`. |
| `from_user` | no (v_18+) | The initiator's username on the sending household.  Used by the receiver to show who sent the challenge.  Omitted for sub-v_18 peers. |

**v1 session lifecycle note:** the host initiates `open`; other verbs (`accept`, `close`) are passed through to the app layer — the iframe app owns session lifecycle.  The server does not enforce session state.

**Challenge notification (v_18+):** when an inbound `APP_SESSION {verb:"open"}` resolves to a single local recipient via `to_user`, an `AppChallengeReceived` domain event is published. The `NotificationService` subscriber seats a bell row and fires a title-only push notification (§25.3) for the addressed user. Idempotent on `session_id` — a double federation delivery (WebRTC + HTTPS fallback) does not produce two notifications.

**Privacy note (§FIX-I2 relaxed for v_18):** `to_user`/`from_user` are permitted
because the challengeable roster is gated to the consensual pairing-scoped
DM/friends set — the same set as `/api/friends` and DMs.  This is not covert
tracking; it is addressed messaging to people who already share a household
relationship.  Sub-v_18 peers still receive no per-user identity on the wire
(legacy household fan-out). See `docs/principles.md` for the formal sign-off.

All fields above are inside the **encrypted payload** (AES-256-GCM).  Only
`event_type`, `from_instance`, `to_instance`, `msg_id`, and `timestamp` are
in plaintext for routing.

### `APP_MESSAGE` (JSON fallback)

| Field | Required | Notes |
|---|---|---|
| `app_id` | yes | Identifies which app the data belongs to. |
| `session_id` | yes | Matches the `APP_SESSION` that opened the channel. |
| `data` | yes | Application-defined dict.  The receiver's `_deliver` passes it as-is to the `app.message` WS frame's `payload` field. |
| `to_user` | no (v_18+) | Per-user routing: the target user's username on the receiving household.  When present and non-empty, the receiver delivers the frame only to that user.  Omitted for sub-v_18 peers. |
| `from_user` | no (v_18+) | The sender's username on the initiating household.  Omitted for sub-v_18 peers. |
| `app_aead_suite` | yes | Suite used for the binary payload seal (`"aesgcm-256"` today).  **Binary path only** — carried inside the envelope's encrypted metadata.  Receivers reject unknown suites via `UnsupportedAppAeadSuite`; no default fallback. |
| `payload_sha256` | yes | Base64url SHA-256 of the **plaintext** application payload.  **Binary path only** — carried inside the encrypted envelope metadata.  Binds the binary payload to the signed header; the receiver verifies `sha256(plaintext) == payload_sha256` after decryption. |

All fields are inside the encrypted payload.

**v1 binary limitation:** the `fed-app-v1` binary frame format carries **no**
`to_user` or `from_user` slot.  Binary inbound messages (`on_inbound_message`)
always fall back to the household fan-out; the receiver disambiguates sessions
by `session_id`.  Per-user routing on the binary fast-path is deferred to a
future frame-format revision.

## Binary frame format (`fed-app-v1`)

```
[u8 frame_type][u32 header_len BE][header_bytes][u32 payload_len BE][payload_bytes]
```

- `frame_type` — `1` (`FRAME_TYPE_APP_MSG`).  Unknown types are skipped for
  forward compatibility.
- `header_bytes` — the signed federation envelope JSON verbatim (the exact
  bytes that were signed).  The §24.11 pipeline re-parses this unchanged.
- `payload_bytes` — `nonce(12) ‖ AES-256-GCM(ct+tag)`.  Bound to the header
  by `payload_sha256` inside the encrypted metadata.

Payload ceiling: **1 MiB** (tighter than the media channel's 4 MiB — app
messages are chess moves and whiteboard deltas, not media blobs).  Bulk data
belongs on `fed-media-v1`.

## Encryption-first

Every application field (`app_id`, `session_id`, `verb`, `data`,
`app_aead_suite`, `payload_sha256`) is inside the AES-256-GCM encrypted
envelope payload — signed by Ed25519 and **never** in plaintext on the wire.
Only the routing envelope fields (`event_type`, `from_instance`,
`to_instance`, `msg_id`, `timestamp`) are cleartext.

For the binary path the `app_aead_suite` and `payload_sha256` binding mirror
the media channel's `media_aead_suite` / `chunk_sha256` pattern
(`federation/media_framing.py`) — tampering with the binary payload fails the
hash check; tampering with the hash fails the GCM tag or the Ed25519
signature.

## `app.message` WebSocket frame

The `AppFederationService._deliver` method delivers a frame to addressed local
WebSocket connections when an inbound app event passes the §24.11 pipeline.
For v_18+ person-routed events the frame goes only to the addressed user; for
legacy/binary events it is broadcast to all local users with the app enabled:

| Field | Type | Notes |
|---|---|---|
| `type` | `"app.message"` | Constant WS frame type identifier. |
| `app_id` | `string` | Identifies the installed app. |
| `session_id` | `string` | Hex UUID that scopes the session — route messages to the correct in-page component using this. |
| `from_instance` | `string` | Instance ID of the sending household — lets the app show who sent a move or issued an invite. |
| `from_user` | `string` \| absent | Username of the initiator on the sending household.  Present for v_18+ person-routed events and local-loopback sessions; absent for legacy household-addressed events and binary inbound. |
| `kind` | `"session"` \| `"message"` | `"session"` for `APP_SESSION` control events (open/close); `"message"` for `APP_MESSAGE` JSON events and binary `fed-app-v1` data frames. |
| `payload` | `object` | Application-defined dict. For `APP_SESSION` events this is the full decrypted payload (includes `verb`); for `APP_MESSAGE` events it is the `data` sub-field. |

### Bridge relay into the iframe

The SPA host bridge (`client/src/features/apps/bridge.ts`) listens on
`app.message` WS frames and, after filtering by `app_id`, relays the full
identity into the sandboxed iframe as a `MessageEvent`:

```ts
{
  type: 'app:event',
  kind: 'session' | 'message',   // forwarded from the WS frame
  sessionId: string,              // WS frame's session_id
  fromInstance: string,           // WS frame's from_instance
  fromUser?: string,              // WS frame's from_user (v_18+ / local loopback; absent for legacy)
  payload: object,                // WS frame's payload
}
```

This lets apps:
- **Route by session** — use `sessionId` to dispatch a message to the correct
  game or whiteboard component when multiple sessions are open simultaneously.
- **Show sender identity** — `fromInstance` names the peer household; `fromUser`
  names the individual who sent the invite or move (available for v_18+ remote
  and all local-loopback sessions).
- **Distinguish invites from moves** — `kind === "session"` signals a
  lifecycle event (open/close); `kind === "message"` is in-game data.

## Inbound delivery

`AppFederationService._deliver` routes inbound app messages:

- **Person-routed (v_18+ JSON path):** when the inbound JSON event carries a
  non-empty `to_user` that resolves to a local user (by username), the frame
  is delivered **only to that user**.  For an `APP_SESSION {verb:"open"}`, an
  `AppChallengeReceived` event is also published (→ bell row + §25.3 push).
- **Legacy / binary household fan-out:** when `to_user` is absent, empty, or
  unresolvable — including all binary `fed-app-v1` frames, which carry no
  routing slot — the frame is broadcast to **every local user** whose
  WebSocket is open and who passes the app's age gate.  The `session_id`
  scopes the semantics so the SPA can dispatch to the correct component.

If the app is not installed or not enabled on the receiving household, the
event is silently dropped with a debug log — the peer does not receive an
error.

## Receiver rules

- A per-user delivery (`to_user` resolves to a local user) must name its
  initiator: `from_user` has to be a user of the sending household
  (v_18+ senders always ship both fields), so the recipient's block list
  always applies. Otherwise nothing is delivered, stored or notified
  (WARNING).
- A pending invite (`app_pending_sessions`) is only refreshed by the
  household that opened it; another household reusing the session id
  changes nothing.
- Re-delivered opens are de-duplicated per `(from_instance, session_id)`,
  so one household can never suppress another's invite.

`tests/protocol/test_app_session_scope.py`.

## Implementation

- `socialhome/services/app_federation_service.py` — `AppFederationService`:
  `list_contacts`, `open_session`, `send_message`, `on_inbound_event`,
  `on_inbound_message`, `_deliver`, `_assert_target_allowed`,
  `_seen_open_session` (idempotency guard).
- `socialhome/federation/app_framing.py` — binary frame encode/decode,
  `APP_AEAD_SUITE_AESGCM_256`, `SUPPORTED_APP_AEAD_SUITES`,
  `UnsupportedAppAeadSuite`, `CHANNEL_LABEL = "fed-app-v1"`.
- `socialhome/federation/federation_service.py` — `FederationService.send_app_message`:
  selects binary or JSON path; passes `to_user`/`from_user` on the JSON path
  when `peer_supports(MIN_FOR_APP_USER_ROUTING)`;
  `_app_inbound_handler` for binary frame dispatch.
- `socialhome/domain/federation_capabilities.py` — `MIN_FOR_APP_CHANNEL = 17`,
  `MIN_FOR_APP_USER_ROUTING = 18`, `OURS = 18`.
- `socialhome/domain/events.py` — `AppChallengeReceived(app_id, session_id,
  to_user_id, from_display)`.
- `socialhome/domain/apps.py` — `AppContactNotFoundError`.
- `socialhome/routes/apps.py` — REST endpoints: `GET /api/apps/{app_id}/peers`,
  `GET /api/apps/{app_id}/contacts`, `POST /api/apps/{app_id}/sessions`
  (accepts `target` body; `peer_instance_id` is back-compat),
  `POST /api/apps/{app_id}/messages` (same `target` shape).
- `attach_apps` in `socialhome/app.py` — registers `APP_SESSION` /
  `APP_MESSAGE` handlers in the event registry and opens the `fed-app-v1`
  DataChannel on the `PeerConnection`.

## Spec references

§25 (Social Home Apps),
§24.11 (inbound federation validation pipeline),
§25.8.21 (encryption-first rule).
