# Design principles

These are the load-bearing decisions Social Home is built on. Every
feature decision, federation event, and data-storage choice is checked
against this list. Distilled from §2 of `spec_work.md`.

## Households first

Social Home runs **inside the household** — as a Home Assistant add-on
or as a standalone container the operator owns. There is no SaaS tier
and no centrally-hosted account system. A household's data lives on
the household's disk; nothing leaves except encrypted federation
envelopes, addressed to peers the household has chosen to pair with.

## Identity model — household + per-user (§2)

Every identity is bound to an Ed25519 public key, and identifiers are
deterministic digests of that key (no central registry). Two layers
exist:

- The **instance** key is the household's transport + trust root: it
  signs every federation envelope and anchors pairing.
- Each user **also** has their **own** Ed25519 keypair (independent user
  identity, **Phase 1** — capability v_25; **Phase 2** — the immutable
  `identity_anchor` at v_26). The private seed is KEK-wrapped and never
  federates; the public half plus a dual-signed binding (the household
  vouches for the specific key, the user proves possession) rides the
  existing user roster (`USERS_SYNC` / `USER_UPDATED`).

**`user_id` is anchored to an opaque per-user value, not the human name
(Phase 2 — capability v_26).** For a user created on v_26+ (standalone),
`user_id = derive_user_id(home_instance_pk, identity_anchor)` where
`identity_anchor` is an immutable uuid4 minted once at provision — so the
identifier is bound to an opaque value, **not** the mutable human name, and
a rename never re-keys the user. **Existing users keep their
username-derived id**: their `identity_anchor` is frozen to `= username`
(haos users likewise), so `user_id` is byte-for-byte the legacy value and
nothing about pre-v_26 accounts churns. The anchor is committed into both
signatures of the user binding; a sub-v_26 peer falls back to the
username-derived id. The per-user **key** remains a *soft alias* — additive,
behaviour-neutral metadata that does not replace `user_id` for addressing,
display, or routing. The remaining roadmap intent is later-phase: make the
user identity fully portable (mutable login + `@handle`, member move-out).
Any change that makes the per-user key *authoritative* over `user_id` —
relaxes the dual-sig / sender-pinned-key verification, or changes how the
`identity_anchor` derives `user_id` — is a §2 identity-model change that
needs explicit reviewer sign-off. See
[`protocol/user-identity.md`](protocol/user-identity.md).

## Encryption-first (§25.8.21)

Every field in every outgoing federation event is encrypted unless the
federation service genuinely needs it in plaintext to route or
validate. Only `event_type`, `from_instance`, `to_instance`,
`space_id`, and `epoch` stay plaintext; everything else — names,
counts, choices, message bodies — sits inside the AES-256-GCM
payload. There is no `"payload": plaintext_fallback` pattern, and
there is no "trusted instance" mode that skips encryption.

## Fail closed on crypto

If `SpaceContentEncryption` isn't configured at runtime, the outbound
federation path raises `RuntimeError`. Social Home does not degrade
silently — it stops sending. The same posture applies to signature
verification on inbound: a bad signature drops the envelope on the
floor, no exceptions for "trusted" peers.

## No third-party trust

The Global Federation Server (GFS) sees **routing metadata only** —
which space an event belongs to, which peer is online for push fan-out,
which SDP/ICE candidates need relaying. It never sees plaintext content,
votes, names, or messages, and it cannot forge content, because every payload
it relays is signed by a key it does not hold and every receiver re-verifies
that signature itself. A compromised or malicious GFS can disrupt discovery
and push, but cannot read or forge content.

*Which* key signs depends on the path, and the difference matters:

- **Household-to-household** federation envelopes are signed with the
  **originating instance's** Ed25519 key (optional ML-DSA-65 hybrid), so the
  GFS cannot impersonate a household there.
- **`POST /gfs/publish`** carries **no originating-instance signature at all**
  — that is the point of the identity-free relay below. Forgery is prevented
  by the **space-authority** signature sealed inside the payload: only a
  holder of the space seed can mint one, and every subscriber verifies it
  against the space public key it already mirrors. A GFS that fabricated a
  relay would have to forge that signature, which it cannot. What the GFS
  *can* do on this path is relay a captured payload again (the relay is
  deliberately at-least-once; subscriber-side `post_id` dedupe is the
  backstop) — it cannot author one.

**Strengthened:** the GFS no longer learns *which household relayed* a
public/global space event either. `POST /gfs/publish` carries exactly
`{space_id, event_type, payload}` and is authorized purely by the
space-authority signature sealed inside the opaque payload; the fan-out
frame to subscribers is identity-free too. The relaying household's
identity is not **required, stored, logged or forwarded**.

The fallback to the older identified body is itself authenticated away: a
household relays identity-free only while the GFS's `GET /gfs/info`
capability block verifies against the GFS identity key that household pinned
at pair time, the answer **ratchets** (a capability cannot be un-advertised
mid-life), and a public GFS URL must be `https://`. Stripping the capability
on-path would otherwise force the identified body back — whose household
signature is exactly the third-party-provable artefact this section removes.

The honest residual: this is not "the GFS cannot learn it". A household
normally holds an authenticated WebSocket to the same server from the same
IP, so an operator can correlate a publish's source IP, timing and size
with that session. Closing that would need a mix/onion egress and is out of
scope. One smaller residual is tracked with the relay itself: a per-instance
GFS ban cannot gate an anonymous relay — the space-level ban is the
moderation lever there. See
[`protocol/discovery.md`](./protocol/discovery.md).

### Sign-off: the connection server learns the recipients of a link-joined pair

Two households introduced by an invite link (§D2b) hold no address for
each other — deliberately, because the invite blob is public. Every
federation envelope between them is therefore carried by the connection
server that introduced them (`POST {gfs}/gfs/envelope`,
`federation/gfs_relay_transport.py`), which means a third party is on the
path of ordinary space traffic for the first time. **What it concedes,
exactly:**

- **The GFS sees, per envelope: `to_instance`, a timestamp and a byte
  size.** Nothing else is on the wire. The whole §24.11 envelope —
  including the routing fields that are plaintext on every other
  transport (`from_instance`, `event_type`, `space_id`, `msg_id`,
  `timestamp`) — is sealed to the recipient's static X25519 key-wrap key
  before the relay is handed `{to_instance, sealed}`. The relay cannot
  read the content, the event kind, the space, the sender, or any name.
- **It can infer that a household is receiving traffic, how much and
  when.** Sizes are not padded and timings are not batched.
- **A space fan-out to N link-joined members shows the relay N
  recipients at (almost) the same instant.** Those N ids are thereby
  linkable as "plausibly members of one thing" — the relay does not learn
  *what* thing, or that a space is involved at all, but the correlation
  is real and is the sharpest edge of this concession.
- **It can correlate with the household's own WebSocket session** — same
  server, same IP — exactly as the `/gfs/publish` residual above notes.
- **It cannot forge.** The seal is confidentiality only; authorization is
  the Ed25519 signature under the pair's key, checked by the unmodified
  §24.11 pipeline at the receiver. A relay that substitutes, replays or
  edits a blob is dropped there.
- **It can drop or delay.** An at-least-once, best-effort relay is the
  trust level here; a relay that refuses everything makes the pair mute,
  not readable.

**Why this is accepted:** the alternative is exchanging addresses inside
the sealed ACK, which publishes each household's network address to the
other — a strictly larger and more permanent disclosure, to a party the
user knows even less about than their own connection server. A household
that would rather not concede the timing metadata has two exits that need
no code: pair with the other household normally (QR / trust relay), at
which point the peer row is no longer `space_session` and traffic moves
back to RTC / HTTPS, or decline invite-link joins.

### Sign-off: per-user routing on the app channel (§FIX-I2 relaxed, v_18)

`APP_SESSION` and `APP_MESSAGE` events may carry `to_user` (the target user's
username on the receiving household) and `from_user` (the initiator's
username) since capability v_18.  This is a bounded relaxation of the
§FIX-I2 rule that formerly prohibited any stable per-user identifier from
crossing household boundaries on the app channel.

The relaxation is justified by three constraints:

1. **Roster-gating.** Both `open_session` and `send_message` call
   `_assert_target_allowed`, which checks the caller's contact roster —
   the same block-aware pairing-scoped set as `/api/friends` and DMs.
   Targets outside that roster are rejected with `AppContactNotFoundError`
   (HTTP 403).
2. **Consensual relationship.** The roster is built from households that the
   local instance has explicitly paired with through the §11 QR handshake.
   Every person in it is already reachable via DM; surfacing their username
   on the app channel exposes nothing beyond what DMs already expose.
3. **Fallback for older peers.** Sub-v_18 peers receive the legacy
   household-addressed shape (no `to_user`/`from_user`); the receiver fans
   out to all local users as before — the §FIX-I2 posture is preserved for
   any peer that has not upgraded.

**This was an explicit §2 sign-off.**  Reviewers authorising a change that
widens the audience eligible for per-user routing beyond the pairing-scoped
roster (e.g. adding unauthenticated guest channels, GFS-relayed routing, or
any path that bypasses `_assert_target_allowed`) MUST treat it as a §2
principle change requiring a new sign-off.

### Sign-off: Social Home Apps execute fetched third-party JavaScript

Social Home Apps (PR1 and later) represent an **explicit, bounded
exception** to the "no third-party trust" posture: an admin may install
an app bundle that originates from the `socialhome-apps` GitHub
releases and is therefore third-party code that runs on the
household's server. Three mitigations gate this before any code executes:

1. **sha256 pinning.** The catalog entry for every app includes a
   `sha256` hex digest. `AppService` downloads the bundle tarball and
   verifies the digest before touching the filesystem — a mismatch
   aborts with an error and the bundle is never unpacked.
2. **Path-traversal guard + apps-dir containment.** Bundle entries are
   unpacked only when their resolved path stays within
   `apps_path/<app_id>/<version>/` (the dedicated `apps_path`, default
   `<data_dir>/apps`). Any entry that would escape that directory is
   rejected and the whole install is rolled back.
3. **Sandboxed-iframe runtime (shipped in PR3).** App JavaScript is
   loaded into `<iframe sandbox="allow-scripts">`. The absence of
   `allow-same-origin` gives the frame an opaque origin so it cannot
   touch the parent's DOM, localStorage, cookies, or identity. A strict
   `Content-Security-Policy` (`connect-src 'none'`, `worker-src 'none'`,
   `frame-ancestors 'self'`, etc.) on every bundle response prevents
   app code from reaching the network. The host SPA validates
   `event.source === iframe.contentWindow` before processing postMessage
   frames (sandboxed iframes expose an opaque `"null"` origin so origin
   checking is bypassable — only the source reference identifies our
   iframe); the bridge is the only host interface and never exposes the
   bearer token — store reads/writes are proxied as per-user,
   server-scoped KV operations.
   The bundle is served via a signed-URL + HttpOnly path-scoped-cookie
   scheme so no credential leaks into the iframe via URL or header.

Reviewers authorising a change to `AppService` install flow, the
sandbox policy, or the postMessage bridge MUST treat any weakening of
these three mitigations as a §2-principle change requiring explicit
sign-off.

## Plaintext locally, encrypted on the wire

Local SQLite stores plaintext rows — that is your data, on your disk,
in your house. Federation envelopes are encrypted because the network
is not your house. DM end-to-end encryption is **transport-only**: the
local DB stores plaintext like every other surface, but federation
envelopes carrying DMs are encrypted such that no relay or GFS can
read them.

The same rule applies to **space content**. A household that isn't a
member of a space — but happens to be on the federation mesh between
the host and a remote member — is acting as a routing relay. It MUST
NOT be able to read any space content (posts, comments, reactions,
calendar events, location pins, …). Two layers enforce this:

1. **Direct delivery never reaches non-members.**
   `broadcast_to_space_members` targets `space_instances` (households
   with at least one member of the space), never arbitrary paired
   peers. Sending a `SPACE_*` event to a non-member household is a
   bug, not an optimization.
2. **Mesh-routed delivery seals end-to-end.** When a path between
   members crosses a non-member relay, the inner payload is wrapped
   with `SPACE_ROUTED` and sealed under the target's ephemeral
   X25519 public key (see `socialhome/federation/routed_crypto.py`).
   The relay sees the routing metadata but can't derive the shared
   secret — it holds neither ephemeral private half.

The per-space content key the recipient uses to decrypt is itself
delivered through the §D1b invite/redeem envelope (encrypted between
the host and the joining instance) — never to a routing relay.
Removed members lose access on the next epoch rotation (forward
secrecy). Once a member receives content, they store it plaintext
locally, same shape as every other surface.

## Spec is the source of truth, code wins on disagreement

`spec_work.md` is the canonical specification. When code and spec
disagree, fix the code. When the architecture moves the goalposts
during implementation, fix the spec. This rule is mirrored in
`CLAUDE.md` and `AGENTS.md`; doc files (the ones you're reading) are
forward-derived from code with `§NN` backlinks to the spec.

## GPS truncation (§4 dimension)

Latitude and longitude are truncated to **4 decimal places** before
any storage or transmission. `round(float(lat), 4)` — never store raw
device precision, never cap-at-runtime-but-store-precise. Applied
uniformly: presence updates, space zones, household location, public-
space discovery rows.

## One initial migration

v1 ships exactly one schema file: `socialhome/migrations/0001_initial.sql`.
The spec's 33 numbered migrations were collapsed because there is no
migration history to preserve before v1. New schema work after v1
follows the standard `0002_*.sql` pattern.

## Layered architecture

Strict four-layer separation: **domain → repository → service → API**.
Routes are thin `BaseView` subclasses. Services depend on
`Abstract*Repo` Protocols, never on `Sqlite*Repo` concretes. SQL never
appears in services or routes — only in repositories. Domain objects
are pure dataclasses (`@dataclass(slots=True, frozen=True)`) with
behaviour as pure methods.

## Always async

All I/O is `async def`. `time.sleep()` is banned in favour of
`asyncio.sleep()`. Blocking I/O goes through `run_in_executor`. Long-
running schedulers follow the `_stop: asyncio.Event` lifecycle from
`infrastructure/replay_cache_scheduler.py`; the `_running: bool` flag
pattern is gone.

## Spec references

- §2 (design principles)
- §4 (architecture) — for "households first" topology
- §11 (instance pairing) — for the no-third-party-trust posture
- §24.11 (inbound validation pipeline) — for "fail closed on crypto"
- §25.8 / §25.8.21 (post-quantum migration + encryption-first)
