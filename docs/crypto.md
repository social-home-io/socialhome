# Cryptography

This document describes every cryptographic primitive Social Home uses,
where each one lives in the code, how keys are stored at rest, what the
wire formats look like, and the path forward to post-quantum (PQ)
cryptography.

> **Status (2026-04-18):** classical crypto is in production shape.
> The hybrid Ed25519 + ML-DSA-65 signature path is wired end-to-end
> behind `Config.federation_sig_suite = "ed25519+mldsa65"` and a
> manually-installed `liboqs-python` package (PyPI rejects the direct
> URL ref so it can't ship as a `socialhome[pq]` extra — see Operator
> checklist below for the install command).
> Further PQ work (ML-KEM for pairing, PQ VAPID) is tracked in
> [§ Post-quantum migration path](#post-quantum-migration-path) below.

## Threat model

Social Home is a federated social network for households. Each
deployment (a household's instance) is a trust boundary. The crypto
layer protects three things:

1. **Authenticity** — a paired peer's admin can prove that a federation
   envelope really came from them. Ed25519 identity signatures today.
2. **Confidentiality** — post bodies, DM contents, space content, and
   pairing key exchanges never appear in plaintext on the network.
   Per-pair AES-256-GCM session keys today.
3. **Forward secrecy (bounded)** — a compromised peer's keys can't
   retroactively decrypt envelopes sent by other pairs. Each pair has
   its own directional keys; the KEK on disk encrypts them at rest.

Explicit non-goals:

* **Deniability / OTR-style forward secrecy** — we don't rotate per-
  message keys. §12.5 DM relay is the closest thing to deniability.
* **Hiding federation metadata from a global adversary** — the GFS
  necessarily sees `space_id` (to fan out), `event_type`, payload size and
  timing, the subscriber set (it is the directory) and source IPs. Content
  stays opaque to it.

  Hiding the *relaying household's identity* from the GFS is **a goal, and
  it is now met on the wire**: `POST /gfs/publish` carries
  `{space_id, event_type, payload}` and nothing else, and the fan-out frame
  carries no `from_instance` either. The guarantee is precise and worth
  stating honestly: the GFS does not **require, store, log or forward** the
  relaying household's identity. It is *not* "the GFS cannot learn it" — a
  household normally holds an authenticated WebSocket to the same GFS from
  the same IP, so an operator can correlate a publish's source IP, timing
  and size with that session. No protocol change closes that short of a
  mix/onion egress, which is out of scope.
* **Anonymity** — instance IDs are derived from identity public keys
  and are persistent. Users consent to this by accepting the pairing.

The fourth concern that showed up recently — **quantum adversaries
harvesting traffic now to decrypt later** — is what the PQ migration
addresses. See the last section of this document.

## Primitives in use

| Primitive | Algorithm | Key size | Purpose | Source |
|-----------|-----------|----------|---------|--------|
| Identity signatures | Ed25519 | 256-bit | Federation envelopes, user-identity assertions, space config, SDP | `socialhome/crypto.py` |
| Identity signatures (PQ, optional) | ML-DSA-65 (FIPS 204) | 1952-byte pk / 4032-byte sk | Hybrid signature when suite is `ed25519+mldsa65` | `socialhome/federation/pq_signer.py` |
| Key agreement | X25519 (ECDH) | 256-bit | Pairing-time session key derivation | `socialhome/federation/pairing_coordinator.py` |
| Symmetric AEAD | AES-256-GCM | 256-bit | Federation payloads, space content, KEK wrap | `cryptography.hazmat.primitives.ciphers.aead.AESGCM` |
| KDF | HKDF-SHA256 | 32-byte output | Session-key derivation, KEK derivation | `cryptography.hazmat.primitives.kdf.hkdf` |
| Hash | SHA-256 | 256-bit | Instance/space/user ID derivation, token hashing, space-page version hashes (`sha256:` suite-prefixed over title + content + cover — they recognise versions, never order them; receivers reject any other prefix, v_48) | `hashlib.sha256`, `socialhome/domain/page_version.py` |
| MAC | HMAC-SHA256 | 256-bit | Relay path selection (`keyed_hash`) | `socialhome/crypto.py` |
| MAC | HMAC-SHA1 | 160-bit | TURN credential generation (coturn REST API) | `socialhome/routes/calls.py` |
| Password hash | scrypt | N=2^14, r=8, p=1 | Standalone-mode user passwords | `socialhome/platform/standalone/adapter.py` |
| Web Push | VAPID (P-256 ECDSA) | P-256 | Push-notification JWT signing | `socialhome/services/push_service.py` |

### Quantum safety at a glance

- **Quantum-safe today:** AES-256-GCM (Grover ⇒ effective 128-bit),
  SHA-256, HKDF-SHA256, HMAC-SHA256, scrypt.
- **Vulnerable to Shor's algorithm:** Ed25519 (signatures), X25519
  (key agreement), P-256 ECDSA (VAPID).

## Where each primitive is used

**Identity** — every instance mints an Ed25519 keypair at first start
(`identity_bootstrap.py`). The seed is KEK-encrypted and stored in
`instance_identity.identity_private_key`; the public key is the basis
for `instance_id = base32(SHA256(pk)[:20])`. When
`federation_sig_suite = "ed25519+mldsa65"`, a second (ML-DSA-65) key is
minted and stored in `pq_private_key` / `pq_public_key`.

**Federation envelopes** — `FederationService.send_event` builds an
envelope, AES-256-GCM-encrypts the payload under the per-pair session
key, and signs the envelope bytes with every algorithm in the peer's
negotiated `sig_suite`. See `federation/encoder.py`. The wire format is:

```json
{
  "msg_id":            "<uuid4>",
  "event_type":        "dm_message",
  "from_instance":     "<own instance_id>",
  "to_instance":       "<peer instance_id>",
  "timestamp":         "2026-04-18T12:34:56+00:00",
  "encrypted_payload": "<b64url(nonce)>:<b64url(ct+tag)>",
  "space_id":          null,
  "proto_version":     1,
  "sig_suite":         "ed25519" | "ed25519+mldsa65",
  "signatures": {
    "ed25519":  "<b64url Ed25519 sig>",
    "mldsa65":  "<b64url ML-DSA-65 sig>"
  }
}
```

The `signatures` map's key set is enforced to equal the algorithms
parsed from `sig_suite`. Verification is **AND across all algorithms**
(see `encoder.verify_signatures_all`): an attacker must break every
algorithm in the suite, not just one.

**Redelivery re-signs (fresh `timestamp`, same `msg_id`).** The §24.11
pipeline rejects any envelope whose `timestamp` is outside ±300 s. A
queued event redelivered from the outbox days later would fail that gate,
so `FederationService.resign_for_redelivery` rebuilds the envelope with a
fresh `timestamp` and re-signs it (same `msg_id`, same `encrypted_payload`,
same `sig_suite`) on every retry — otherwise structural / security events
in `NEVER_DROP` (bans, key revocations, `SPACE_DISSOLVED`) would be silently
lost to any peer offline more than 5 minutes. The `msg_id` is preserved, so
the receiver's replay cache still dedupes a redelivery whose `2xx` ack was
lost. Because re-signed retries pass the timestamp gate, the replay-dedup
window (`REPLAY_CACHE_WINDOW`, 24 h) is deliberately sized to outlast the
outbox's max jittered redelivery interval (~5.2 h) so a lost-ack retry is
caught by replay rather than applied twice. The re-sign uses the sender's
own signing key — an attacker who captures the bytes still cannot move the
timestamp window themselves (the signature covers `timestamp`).

**Pairing** — `PairingCoordinator.initiate/accept/confirm` runs a QR-
flow with an ephemeral X25519 ECDH. Two directional keys fall out via
HKDF-SHA256; each is AES-256-GCM-wrapped under the local KEK and
stored in `remote_instances.key_self_to_remote` /
`key_remote_to_self`. The QR payload carries `sig_suite` and
optionally `pq_identity_pk`; the receiver negotiates the intersection
(`crypto_suite.negotiate`) so a classical peer talking to a hybrid peer
settles on classical for that pair.

**Space content** — `SpaceContentEncryption` in
`services/space_crypto_service.py` owns an AES-256-GCM key per space
per epoch. Epochs rotate on membership change; old keys are retained
for historical decryption. `space_id` is the GCM AAD so a key lifted
from one space can't decrypt another. Each epoch row also records
`rotated_by` — the household that minted it — so that when delegation
is enabled two admins can rotate to the same epoch concurrently (owner
offline) and still converge: `import_key` keeps the key whose
`rotated_by` sorts lexicographically smallest at a given
`(space_id, epoch)`, deterministically on every receiver. A NULL
`rotated_by` (legacy/owner-minted, or a pre-rotation peer) never
clobbers a stamped row; both-NULL degrades to last-writer-wins.
Because a rekey *pins* the epoch onto its key, the `SPACE_KEY_EXCHANGE_REKEY`
that ships it is **space-authority-signed** (`authority_sig` over the inner
`space_content_key` meta, same `sign_authority_event` helper as
`SPACE_CONFIG_CHANGED`): the receiver imports only when the sender is the
space owner (`from_instance == owner_instance_id`, back-compat) or the
signature verifies against `spaces.identity_public_key` — so a relay or
removed ex-member can't hijack the content key. An authority-signed rekey
with a blank `rotated_by` is rejected (the smallest-wins tiebreak compares
only authenticated, non-empty minter ids).

**GFS-relayed public/global content** (`services/space_public_outbound.py`
/ `space_public_inbound.py`) — the post inner is encrypted under the
space's epoch content key (`SpaceContentEncryption.encrypt`) and signed
per author (`space_public_author.build_signed_author_inner`); the
envelope around it is **space-authority-signed**
(`authority_sig.sign_authority_event`) and relayed by the content-blind
GFS, which verifies only that signature against the space pubkey it
already pins. The relay is **anonymous**: the household sends no
`from_instance` and no household transport signature, because the
authority signature already authorizes the relay and `space_id` already
routes it. The fan-out therefore reaches *every* subscriber (the GFS can
no longer exclude the publisher); a subscriber that gets its own post back
drops it on the post-id / self-echo guard. A current household never sends
the legacy identified body (a GFS that cannot prove `anonymous_publish` gets
no publish at all); one from an older household is still accepted by the
GFS — verified, then discarded: it never
authorizes, is never forwarded and is never logged. Receivers take
attribution (`origin_instance_id`) from the encrypted, authority-signed
inner only; an outer `from_instance` from an old GFS is never read.

The `space_subscriber_key_handoff` payload is identity-free too: it is
exactly `{space_id, sealed, authority_sig, authority_sig_suite}` and names
neither the relaying household nor the household being onboarded. The
**seal is the gate** — the GFS fans the same bytes to every subscriber and
only the one holding the matching key-wrap private key can `open_keywrap`
it; everyone else drops it quietly at DEBUG. Receivers still accept the
legacy `target_instance_id`-bearing shape from an older seed-holder (the
explicit gate then applies), so nobody is stranded.

The residual limits, stated plainly: the GFS still sees `space_id`,
`event_type`, payload size and timing; it can correlate a publish with a
household's own authenticated WebSocket session by IP/timing; and a
per-instance GFS ban cannot gate an anonymous relay, so the space-level
ban is the only moderation lever on the relay path.

The whole shape is pinned by the §27.9 release blocker
`tests/protocol/test_gfs_payload_minimization.py`. See
`docs/protocol/discovery.md` for the shipped flow.

**Routed-envelope seal** (`federation/routed_crypto.py`) — for
multi-hop `SPACE_ROUTED` events the inner payload is sealed with a
per-route ephemeral X25519+HKDF directional key (HKDF info
``socialhome/space_routed/{origin-to-target,target-to-origin}``).
The target generates a fresh ephemeral on each `SPACE_FIND_ROUTE`
probe and ships the public via `SPACE_ROUTE_FOUND`; the origin
generates its own ephemeral on `send_routed` and stashes the priv
for the matching reply. Relays only ever see the opaque ciphertext.

**Routed-origin signature (v_31, #692).** The seal proves *confidentiality*
to the target, not *authorship*: the ephemeral X25519 exchange is anonymous,
so any peer that learned the target's `target_eph_pk` can produce a blob the
target decrypts. `path[0]` — which the unwrap turns into the inner event's
`from_instance` — is therefore signed. The authoring household stamps three
sibling fields onto `sealed`:

```
origin_identity_pk : "<64 hex>"   # its Ed25519 identity public key
origin_sig         : "<b64url>"   # Ed25519 over the signing bytes below
origin_sig_suite   : "ed25519"    # suite tag; unknown value → reject
```

```
b"space-routed-origin:v1:" + direction + b":" + route_id
  + b":" + "|".join(path) + b":" + inner_event_type
  + b":" + sha256(kem_suite|origin_eph_pk|target_eph_pk|nonce|ciphertext).hex()
```

`routed_crypto.sign_routed_origin` / `verify_routed_origin`. The endpoint
verifies against the key it already pins for `path[0]`, or — for a mesh
origin it has never paired with, which has no `remote_instances` row — the
shipped pub bound by `derive_instance_id(pk) == path[0]` (§4.1.2). Both legs
are covered; `direction` separates them, so a forward signature cannot be
lifted onto a reply, and `path` binds the target, so a signature captured en
route to one household will not verify at another.

The digest deliberately covers the **ciphertext**, never the plaintext or a
digest of it: a relay already holds every byte that goes into it, whereas
signing the plaintext would hand a relay with a guess at a low-entropy inner
payload a deterministic oracle to confirm the guess with — which §25.8.21
rules out. Authenticity is unaffected: the AEAD tag binds ciphertext to
plaintext under a key the forger does not share with the target.

A target that cannot open a routed envelope (its private half died with
a restart) answers with a `SPACE_ROUTE_STALE` nack whose `sig` is an
Ed25519 signature by the target's **identity** key over
``b"space-route-stale:v1:" + route_id + b":" + stale_eph_pk``
(`routed_crypto.sign_route_stale` / `verify_route_stale`), tagged
`sig_suite` (`ROUTE_STALE_SIG_SUITE_ED25519`; receivers check it against
`SUPPORTED_ROUTE_STALE_SIG_SUITES` and reject anything unknown — no
default). Like `target_eph_sig` on `SPACE_ROUTE_FOUND` and the per-user
`user_sig_suite` binding it is Ed25519-only today even where
`federation_sig_suite` runs the hybrid; the tag plus the hard reject make
the `"ed25519+mldsa65"` sibling a drop-in.

Both halves live **in memory only** and are bounded by
`DEFAULT_TARGET_EPH_TTL_S` (300 s) measured from the moment the key is
*minted* — never extended on use. That is a deliberate forward-secrecy
bound: a long-lived routed interaction re-keys by running a fresh
discovery, which rotates the ephemeral, rather than by keeping one warm.
Two invariants follow, and both were violated before #648:

* The origin's route cache (which holds the `target_eph_pk` it seals
  under) MUST expire *before* the target's private half. Its TTL is
  therefore derived — `ROUTE_CACHE_TTL_S = DEFAULT_TARGET_EPH_TTL_S -
  ROUTE_CACHE_SAFETY_MARGIN_S` — and anchored on the moment the probe
  was *sent*, since the target minted its key strictly after that. If
  the origin's window can outlive the target's, the origin keeps sealing
  under a key the target has dropped, and the target discards every
  envelope in silence (there is no NACK, and the send already reported
  success).
* Because the private half dies with the process, a target that
  **restarts** invalidates every pub an origin holds for it. Recovery is
  re-discovery, not a longer-lived key: a host admitting a
  `SPACE_SYNC_BEGIN` from a mesh-only requester drops its cached route
  to that requester first, so the stream is sealed under a key minted by
  the requester's current process.
The wire carries a `kem_suite` field (today only `"x25519"`) so a
future hybrid (`x25519+mlkem768`, Phase 2) is a suite-bump rather
than a wire-format break — see [`protocol/spaces.md` → "Mesh
routing"](protocol/spaces.md#mesh-routing-space_routed).

**Static-recipient key-wrap seal** (`federation/keywrap_seal.py`) — a
classic sealed-box (ECIES) for sealing to a household that is **not**
a paired peer and without online ephemeral discovery: the recipient
publishes a long-lived X25519 *key-wrap* public key (at GFS
registration → `instance_identity.keywrap_public_key` /
`ClientInstance.keywrap_public_key`), and the sender does
`DH(fresh-ephemeral, recipient-keywrap-pub)` → HKDF-SHA256 (info
``space-keywrap:v1``) → AES-256-GCM, binding the ephemeral pub as AAD
so a relay can't swap it. A fresh ephemeral per seal means no key
reuse. Same `kem_suite` contract as the routed seal (`"x25519"` today,
reject-unknown, PQ migration is a suite-bump). This is the channel the
Phase-5b-b subscriber content-key handoff
(`space_subscriber_key_handoff`, `services/space_subscriber_key_outbound.py`
→ `space_subscriber_key_inbound.py`) uses to hand the per-space content
key to an unpaired GFS subscriber, GFS-blind: the seed-holder seals the
`{space_content_key:{…}}` meta to the subscriber's key-wrap pubkey and
authority-signs the sealed envelope so the content-blind GFS relays it
without learning the key.

**Moment origin signature (v_35)** (`federation/moment_origin.py`) — a
moment travels up to three hops, and each relay re-sends it under its own
envelope signature, so the envelope proves only the last hop. The origin
household therefore signs every `MOMENT_CREATED` / `MOMENT_DELETED` it
authors with its Ed25519 **identity** key (no new key), and three sibling
fields ride inside the encrypted payload:

```
origin_identity_pk : "<64 hex>"   # the origin's Ed25519 identity public key
origin_sig         : "<b64url>"   # Ed25519 over the signing bytes below
origin_sig_suite   : "ed25519"    # suite tag; unknown value → reject
```

```
b"moment-origin:v1:" + canonical_json({
    event_type, sig_suite, moment_id, author_user_id, origin_instance_id,
    occurred_at,
    # MOMENT_CREATED only:
    content, media_url, media_type, duration_ms, parent_moment_id, expires_at,
})   # sort_keys, separators=(",", ":"), UTF-8
```

A `no_relay` moment (v_38 — a protected account's, delivered to the
origin's paired households only) signs the same object plus
`"no_relay": true` under the disjoint domain `b"moment-origin:v2:"`, so a
relay can neither strip the mark (the v1 bytes then don't verify) nor add
it. A moment without the mark signs exactly the v1 bytes.

`hop_count` is outside the signature (each relay bumps it) and relays
forward the three fields verbatim. The event type is inside the signed
object, so a signed create cannot be replayed as a delete. A receiver
handed a moment by a household other than its claimed origin verifies it
against the key it pins for that origin, or — for a friend-of-friend
origin it has never paired with — against the shipped key, accepted only
when `derive_instance_id(pk) == origin_instance_id` (§4.1.2). Receivers
check `origin_sig_suite` against `SUPPORTED_MOMENT_ORIGIN_SIG_SUITES` and
raise `UnsupportedMomentOriginSuite` on anything else — no default. Like
the routed-origin signature it is Ed25519-only today; the tag plus the
hard reject make the `"ed25519+mldsa65"` sibling a drop-in. Direct
deliveries need no origin signature: there the envelope signer *is* the
origin.

**Link-preview author signature (GFS public relay)**
(`services/space_public_author.py`) — a public/global space post's
author-built link card rides inside the encrypted relay inner, but outside
the main per-author signature's fixed field set (that relay has no version
negotiation; widening the set would make every pre-preview subscriber drop
posts that carry a card). The author's identity seed signs it separately:

```
link_preview           : {url, title, description, site_name, thumbnail_url}
link_preview_sig       : "<b64url>"   # Ed25519 over the bytes below
link_preview_sig_suite : "ed25519"    # unknown value → card rejected
```

```
b"space-post-link-preview:v1:" + canonical_json({
    post_id, space_id, author_user_id, author_pk,
    link_preview, link_preview_sig_suite,
})   # sort_keys, separators=(",", ":"), UTF-8
```

Verified against the same `author_pk` the main author signature already
proved (`verified_link_preview`); a missing / bad signature drops the card,
never the post, so a relayer can strip a card but cannot forge, alter or
move one to another post. `SUPPORTED_LINK_PREVIEW_SIG_SUITES` +
`UnsupportedLinkPreviewSigSuite`, no default — the PQ sibling is a suite
bump.

The key-wrap pubkey is **self-signed by the identity** so the seal path
never trusts the GFS-served value. At identity setup each household
produces `keywrap_sig = b64url(sign_ed25519(identity_seed,
keywrap_public_key))` (stored `instance_identity.keywrap_sig`, published
at GFS registration → `ClientInstance.keywrap_sig`). Before sealing, the
sender calls `verify_keywrap_binding(instance_id, identity_pub,
keywrap_pub, keywrap_sig)` on the GFS-served
`{instance_id, public_key, keywrap_public_key, keywrap_sig}`, which
returns true only when **all** hold (fail-closed, never raises):
`derive_instance_id(identity_pub) == instance_id` (binds the identity
key to the claimed id — 160-bit, the GFS can't forge it), the key-wrap
pub is 32 bytes, and the Ed25519 signature over the key-wrap pub
verifies against `identity_pub`. A malicious GFS that substitutes a
key-wrap pubkey it controls has no valid signature from the real
identity → rejected, so it can't read the sealed content key. This
mirrors the #596 `derive_instance_id`-binding pattern: authorize against
a key the receiver can bind first-hand, never the directory-served
value. An older HFS that published no `keywrap_sig` is unsealable
(graceful degrade).

**Space content key delivery** (`services/space_crypto_service.py`)
ships the per-space AES-256-GCM key to a new remote member via the
§D1b invite/redeem envelope. The payload wears a `key_suite` field
(today only `"aesgcm-256"`) so future variants (e.g. ChaCha20-Poly1305
for low-power receivers, or a PQ-protected wrapping when Phase 2
lands a hybrid asymmetric channel for it) are wire-additive. AES-256
is Grover-resistant on the symmetric side, so the PQ migration here
is about strengthening the *delivery channel*, not the AEAD primitive.
Receivers reject unknown `key_suite` values rather than fall back —
see `apply_space_content_key_from_metadata` for the validator.

**Delegated-admin signing-seed delivery** (`SPACE_ADMIN_KEY_SHARE`,
`services/space_service.py`, v_22) — when the owner opts into
`SpaceFeatures.delegated_admin_authority`, the space's Ed25519 signing
*seed* (the private half of `identity_public_key`) is shipped to a
remote admin household so it can sign space-authority events offline.
The seed rides the encrypted peer-pair path (the directional session
key, same AES-256-GCM channel as every other federation event to that
peer) addressed to that one household — **never broadcast**. The
payload carries a `seed_suite` field (today only `"ed25519-seed"`,
validated against `SUPPORTED_SEED_SUITES`, rejected-not-defaulted on
unknown) so a future hybrid PQ signing key is a wire-additive suite
bump. The receiver fails closed — it accepts the seed only from the
authentic owner instance into a space whose *local* copy has
delegation enabled, and only when the b64url payload decodes to
exactly 32 bytes — and, from v_44, only when the seed is the private half
of the key it pins. See `_share_admin_signing_seed` (sender) /
`PrivateSpaceInviteHandler._on_admin_key_share` (receiver).

**Space authority key certificate** (`authority_cert.py`, v_44) — the
space authority key rotates when an admin household is revoked, and the
rotation is announced by a cert signed with the OWNER HOUSEHOLD's identity
key, never with the old space key (which the revoked household holds). Wire
shape `{space_id, owner_instance_id, owner_pk, authority_pk,
authority_key_suite, key_epoch, issued_at, cert_sig_suite, cert_sig}`;
signing bytes `b"space-authority-cert:v1:"` + canonical JSON (sorted keys,
compact) of the cert minus `cert_sig`, so both suite tags are signed. Two
suite tags, each validated against its own frozenset with no default:
`cert_sig_suite` (`SUPPORTED_AUTHORITY_CERT_SUITES`, today `"ed25519"`) for
the owner's signature, `authority_key_suite`
(`SUPPORTED_AUTHORITY_KEY_SUITES`, today `"ed25519"`) for the certified key —
the owner identity and the space key migrate independently, so Phase-2 grows
each set separately (e.g. `"ed25519+mldsa65"` with concatenated key
material). Unknown or missing → `UnsupportedAuthorityCertSuite`. Receivers
bind the cert with keys they already hold — `derive_instance_id(owner_pk)
== owner_instance_id`, plus the stored host / registered key when there is
one — and apply it only at a strictly higher `key_epoch` (an int in
`1..2^63−1`; anything else is refused), so a replayed older cert can never
restore a revoked key. The owner picks `max(current + 1, unix seconds)`. No new long-lived key exists:
the cert is signed by the household identity every peer already pins.

**Space writer certificate** (`writer_cert.py`, domain shape in
`domain/writer_cert.py`, v_49) — the space AUTHORITY key's per-epoch
statement that one household may write in a space, so a member's own signed
item can be authorized without a host signature on every post. Issued by a
seed holder (owner or delegated admin, and only while its seed still matches
the pinned space key) once per writer household per content epoch: `scope:
"write"` for owner / admin / moderator / member seats, `"comment"` for a
follower seat while `allow_subscriber_comment` is on; a household with
several seats gets one cert at its strongest scope. Wire shape (every field
always present) `{cert_suite, space_id, epoch, instance_pk, scope,
issued_at, cert_sig}` — `instance_pk` is the household's Ed25519 identity
public key (b64url), `issued_at` unix seconds. Signing bytes
`b"space-writer-cert:v1:"` + canonical JSON (sorted keys, compact) of the
cert minus `cert_sig`, so the suite tag is signed; the prefix is distinct
from `space-authority:v1:` so neither signature can be lifted onto the
other. Suite `cert_suite` validated against `SUPPORTED_WRITER_CERT_SUITES`
(`WRITER_CERT_SUITE_ED25519 = "ed25519"`); unknown →
`UnsupportedWriterCertSuite`, never a default. `verify_writer_cert(cert, *,
space_pubkey, space_id, epoch, author_pk, required_scope)` checks the suite,
the signature against the pinned space key, `space_id` and `epoch` equal,
`instance_pk == author_pk`, and that `scope` permits the action (`write`
implies `comment`); every failure raises. Receivers additionally require
the item's epoch to be the newest they hold, or the previous one for
`WRITER_CERT_EPOCH_GRACE_S` (600 s) after the newest key arrived, so an
old epoch's cert cannot authorize items forever; any scope-reducing change
rotates the content epoch. A cert is not secret. It is delivered to each
household inside encrypted payloads and rides inside the encrypted item to
every member and subscriber — with one deliberate exception: a trusted-mode
member publish (below) carries it in PLAINTEXT to the connection server and
in the fan-out frame, because the server authorizes the publish with it. No
new key: it is
signed by the space authority key every member already pins, and binds the
household identity key every peer already pins.

**Writer-cert user binding (v2)** — the household's writer users
(`writer_user_ids`, sorted, at most 64) signed by the space authority seed
in a SECOND Ed25519 signature: `users_sig` over
`b"space-writer-cert:v2:users:"` + canonical JSON of `{cert_sig, space_id,
epoch, instance_pk, writer_user_ids, users_sig_suite}`. Binding the cert's own
signature ties it to exactly this cert (it cannot be lifted onto another).
Its own suite tag `users_sig_suite` (`WRITER_USERS_SUITE_ED25519 =
"ed25519"`, `SUPPORTED_WRITER_USERS_SUITES`, unknown →
`UnsupportedWriterUsersSuite`, never a default). Kept apart from `cert_sig`
on purpose: the v1 signing bytes (`space-writer-cert:v1:` over the six v1
fields) are unchanged, so a v1 verifier — which ignores the extra fields —
still accepts a bound cert. `verify_writer_users` requires the binding and
the author's user id in it; a member-published `space_item` must carry it,
the host-relay path does not. **The binding never travels in plaintext**:
it names the household's users, so it rides only inside the encrypted inner.
The `writer_cert` a connection server sees (request, fan-out frame, queue)
is the v1 fields alone — the codec serializes nothing else, and the GFS
refuses a request whose cert carries any other key; the receiver checks
that the frame's v1 cert equals the inner cert's v1 fields and verifies the
binding on the inner copy. A household with more than 64 writer users gets
the first 64 bound (logged once per space and household); the rest post
through the host.

**Author-bound item type (v_49 `space_item`)** — an inner built for a
member publish signs two extra fields with the author signature,
`item_type` and `item_target` (the id the item acts on — the post itself for
a post), present-or-absent like `identity_anchor` so host-relay inners keep
their exact v_25 bytes. Receivers require both to match the decrypted item,
so nobody holding the content key can re-wrap a signed post as another kind
of item. A `post_edit` inner (the author's full snapshot of the edited post)
also signs `edited_at` (tz-aware ISO 8601, present-or-absent, so every other
inner keeps its bytes) — the stamp receivers order edits by.

**Generic member item inner** (`services/space_item_author.py`, v_49) —
comments, comment edits / deletes, post deletes and reactions carry their
own author-signed inner: Ed25519 by the author's household identity key
over `b"space-item-author:v1:"` + canonical JSON (sorted keys, compact) of
`{item_type, item_target, space_id, post_id, author_user_id, author_pk,
author_username, identity_anchor?, origin_instance_id, ts, comment_type,
parent_id, content, media_url, created_at, emoji, author_sig_suite}` —
every field always present (`null` when unused) except `identity_anchor`,
present-or-absent as in the post inner. The prefix differs from
`space-post-author:v1:`, so neither inner can be passed off as the other.
Suite tag `author_sig_suite` (`ITEM_AUTHOR_SIG_SUITE_ED25519 = "ed25519"`,
`SUPPORTED_ITEM_AUTHOR_SIG_SUITES`, unknown →
`UnsupportedItemAuthorSigSuite`, never a default). Self-cert as for posts:
`derive_user_id(author_pk, identity_anchor or author_username) ==
author_user_id`. No new key: the household identity key every peer pins.

**Trusted-mode member publish** (`domain/gfs_member_publish.py`,
`global_server/member_publish.py`, v_49) — `POST /gfs/member-publish`. The
household signs canonical JSON (sorted keys, compact — the encoding every
signed household→GFS request uses) of `{action: "gfs-member-publish:v1",
instance_id, gfs_instance_id, ts, target, event_type: "space_item", epoch, writer_cert,
payload}` with its Ed25519 identity seed; the GFS verifies it against the
registered `client_instances.public_key` with the ±300 s `ts` window. The
`action` value is the domain separator — distinct from `subscribe` /
`unsubscribe` / `unpublish` — so the signature cannot be replayed as any
other request, and `gfs_instance_id` (the server id the household pinned from `/gfs/info`, refused unless it is the receiving server's) binds it to one server so it cannot be replayed to another; no new key or suite (it is the household identity signature,
`ed25519`). The GFS then runs `verify_writer_cert` against the pinned space
key with `author_pk` = that same registered key and scope `comment` (the
item type is hidden in the ciphertext; receivers enforce the real scope).
`payload` is AES-256-GCM under the existing per-space epoch content key —
the GFS holds no key. **Content-epoch notice** — `POST
/gfs/spaces/{id}/epoch`, two forms. The OWNER's: its household identity
signature (Ed25519, the registered key) over canonical JSON of `{action:
"gfs-owner-epoch-notice:v1", owning_instance, gfs_instance_id, space_id,
epoch, ts}` — only this form may move the epoch by more than one. A
delegated admin's: a space-authority signature (`authority_sig.py`, suite
`authority_sig_suite`) over `{space_id, epoch}` under the new event type
`space_epoch_notice`, which is deliberately outside
`AUTHORITY_RELAY_EVENT_TYPES` so neither a notice nor a relay payload can be
lifted onto the other path; it raises the epoch by +1 at most. No new key
or suite.

**GFS capability block** (`capabilities_sig.py`) — `GET
/gfs/info` is unauthenticated, so the capability that decides whether a
household may relay identity-free (`anonymous_publish`) is signed with the
GFS's own Ed25519 identity key, whose public half the household pinned at
pair time (TOFU) and which the same response publishes as `public_key`. No
new key is minted. Wire fields: `capabilities` (the map), `capabilities_sig`
(b64url Ed25519) and `capabilities_sig_suite`
(`CAPS_SIG_SUITE_ED25519 = "ed25519"`, validated against
`SUPPORTED_CAPS_SIG_SUITES`; unknown → `UnsupportedCapsSigSuite`, never a
default). Signing bytes are `b"gfs-capabilities:v1:"` + canonical JSON
(`sort_keys`, compact separators) of `{gfs_instance_id, capabilities}` — the
instance id inside the signed bytes stops a block from being replayed by
another server, the prefix stops it from being lifted onto another statement
that key signs. The household additionally **ratchets** a verified `true`
(in-process) so a stripped-on-path response can't stop the relay mid-life.
There is no identified fallback: a GFS without a verified `true` receives no
publish.

The suite-tag retrofit promised in earlier revisions of this doc is
shipped — every cryptographic wire format in the federation surface
carries a ``*_suite`` identifier (signatures, mesh KEM, key-wrap KEM,
content-key delivery, GFS capability block, writer certificates). A future ChaCha20-Poly1305 or
PQ-protected variant
is therefore a wire-additive change. (An earlier *sealed-sender*
primitive carried its own ``aead_suite``; it was never wired into any
federation path and has been deleted — the shipped GFS relay is the
per-space AES-GCM + space-authority construction described above.)

**Per-user identity binding** (`crypto.py`, independent user identity
Phase 1) — each household member has an Ed25519 **user** key separate
from the household's instance key. When a household publishes one of its
users (`USERS_SYNC` / `USER_UPDATED`, capability v_25) the per-user entry
may carry a *dual-signed* binding, verified by
`verify_user_identity_assertion`:

- The **USER self-signature** (`user_signature`, base64url) covers
  `user_identity_signed_bytes` — a length-prefixed (`_lv`) encoding under
  the `sh/user-identity/v1` domain tag binding `user_id`, `instance_id`,
  `username`, the user public key, and the suite. It proves the user
  holds their own key, independent of the hosting instance.
- The **INSTANCE signature** (`user_assertion_signature`, base64url)
  covers `instance_assertion_signed_bytes`: the legacy
  `user_assertion_signed_bytes` (byte-for-byte back-compat) **extended**,
  when a binding is present, under a `user-binding` domain separator with
  the user pubkey + suite. So the instance signature *commits to the
  specific user key* — verified against the envelope sender's pinned
  instance key, this closes the key-transplant flaw (a swapped user key
  can't reuse the household's instance signature).

The suite tag is `user_sig_suite` (today only
`USER_SIG_SUITE_ED25519 = "ed25519"`, validated against
`SUPPORTED_USER_SIG_SUITES`); a present-but-unknown suite raises
`UnsupportedUserSigSuite` with **no default fallback** (a first-revision
payload that omits the suite defaults to `ed25519`, the documented
migration tripwire). Only the public half ever federates; the private
seed is KEK-wrapped in `users.user_identity_private_key`. See
[`protocol/user-identity.md`](protocol/user-identity.md).

**Binary media chunks** (`federation/media_framing.py`,
`federation/encoder.py:encrypt_bytes`) — media on the `fed-media-v1`
DataChannel ships the raw chunk as `nonce(12) ‖ AES-256-GCM(ct+tag)`,
**not base64**. The chunk's wrapping header is an ordinary signed
federation envelope, so origin auth + replay are the §24.11 envelope
guarantees unchanged. The binary payload is bound to that signed
envelope by a `chunk_sha256` field — the b64url SHA-256 of the
**plaintext** chunk — carried *inside* the AES-GCM-encrypted,
signature-covered envelope metadata. The receiver verifies
`sha256(plaintext) == chunk_sha256` (constant-time) after decryption,
so the hash commits to the exact bytes written to disk; tampering with
the payload fails the hash, tampering with the hash fails the GCM tag
or the signature. The metadata carries a `media_aead_suite` tag (today
only `"aesgcm-256"`, validated against `SUPPORTED_MEDIA_AEAD_SUITES`,
rejected-not-defaulted on unknown). **Nonce budget:** both the metadata
AEAD and the chunk AEAD draw a fresh random 96-bit nonce under the same
directional session key already used for every JSON envelope to that
peer. Chunking raises the messages-per-key count (a 200 MiB video ≈ 400
chunks × 2 GCM ops), but stays ~7–8 orders of magnitude under the NIST
SP 800-38D random-nonce birthday bound (~2³² messages/key) for any
realistic peer lifetime — never introduce a deterministic/counter nonce
that could collide with this random-nonce stream. See
[`protocol/media.md`](protocol/media.md).

**Binary app frames** (`federation/app_framing.py`) — app messages on the
`fed-app-v1` DataChannel (capability v_17) follow the same binary wire layout
as the media channel: `nonce(12) ‖ AES-256-GCM(ct+tag)` in the payload bytes,
wrapping header is a signed federation envelope, payload is bound to the header
by a `payload_sha256` field carried inside the encrypted envelope metadata
(identical pattern to `chunk_sha256` on the media channel).  Constants:
`APP_AEAD_SUITE_AESGCM_256 = "aesgcm-256"`, validated against
`SUPPORTED_APP_AEAD_SUITES`; unknown suites raise `UnsupportedAppAeadSuite`
with no default fallback.  Payload ceiling: 1 MiB (tighter than the 4 MiB
media ceiling — app messages are chess moves and whiteboard deltas, not media
blobs).  The JSON `APP_MESSAGE` event fallback uses the same per-pair AES-256-GCM
session key as every other federation envelope (no additional symmetric key
material is needed).  See [`protocol/apps.md`](protocol/apps.md).

**WebRTC SDP signing** (`federation/sdp_signing.py`) — Ed25519
signature over `<sdp_type>:<sdp>` so a MITM can't swap DTLS endpoints.

**Owner-bound row ids** (`federation/owner_bound_id.py`, v_34 albums,
v_36 every other federated row with an owner — space posts, comments,
gallery items, calendar events, tasks, pages, stickies and moments; v_39
space timetables, bound with no legacy window) — a
new row's id commits to its creator, so no other household can claim it
first. The kind (`gallery-album`, `space-post`, `space-comment`,
`gallery-item`, `space-calendar-event`, `space-task`, `space-page`,
`space-sticky`, `space-timetable`, `moment`) domain-separates the commitment per table; the
`moment` kind is unscoped (`UNSCOPED_KINDS`) — its space component is the
empty string, and a scoped kind with no space (or an unscoped one with a
space) never verifies. 32 hex characters in the UUIDv8 layout: `id[0:16]` is a random
nonce (with the UUID version nibble `8`), `id[16]` the suite nibble in the
variant position (`8` = `OWNER_BOUND_ID_SUITE_SHA256`; receivers check it
against `SUPPORTED_OWNER_BOUND_ID_SUITES` and refuse anything else, no
default) and `id[17:32]` the first 60 bits of
SHA-256(`"socialhome/owner-bound-id/v1"` ‖ kind ‖ space_id ‖
owner_user_id ‖ nonce, NUL-separated). Forging a claim for another owner
means a 60-bit preimage per attempt, and the claimed owner must still be
seated on the signing household. A commitment, not a signature — hash-based,
so no PQ migration is needed beyond a suite bump if SHA-256 ever is.

**Standalone auth** — `StandaloneAdapter` hashes passwords with scrypt
and embeds parameters in the stored hash: `scrypt$16384$8$1$<salt
hex>$<hash hex>`. Parameters can be bumped without a schema change.

## Key storage

### Database

| Table | Column | Content | Wrapped? | AAD |
|-------|--------|---------|----------|-----|
| `instance_identity` | `identity_private_key` | 32-byte Ed25519 seed | KEK AES-256-GCM | — |
| `instance_identity` | `identity_public_key` | 32-byte Ed25519 public key (hex) | no | — |
| `instance_identity` | `pq_private_key` | ML-DSA-65 secret key | KEK AES-256-GCM | — |
| `instance_identity` | `pq_public_key` | ML-DSA-65 public key (hex) | no | — |
| `instance_identity` | `routing_secret` | 32-byte HMAC key (hex) | no *(local-only, never transmitted)* | — |
| `users` | `user_identity_private_key` | 32-byte Ed25519 user seed (per-user identity, Phase 1) | KEK AES-256-GCM | — |
| `users` | `user_identity_public_key` | 32-byte Ed25519 user public key (hex) | no | — |
| `users` | `user_pq_private_key` | ML-DSA-65 user secret key (reserved for PQ; NULL in Phase 1) | KEK AES-256-GCM | — |
| `users` | `user_pq_public_key` | ML-DSA-65 user public key (hex; reserved for PQ; NULL in Phase 1) | no | — |
| `remote_users` | `user_identity_public_key` | Remote user's Ed25519 public key (hex), stored only after the dual-signed binding verifies | no | — |
| `remote_instances` | `key_self_to_remote` | 32-byte session key | KEK AES-256-GCM | — |
| `remote_instances` | `key_remote_to_self` | 32-byte session key | KEK AES-256-GCM | — |
| `remote_instances` | `remote_identity_pk` | Peer Ed25519 public key (hex) | no | — |
| `remote_instances` | `remote_pq_identity_pk` | Peer ML-DSA-65 public key (hex) | no | — |
| `remote_instances` | `sig_suite` | Negotiated per-peer suite | no | — |
| `space_keys` | `content_key_hex` | 32-byte space content key | KEK AES-256-GCM | `space_id` |
| `pending_pairings` | `own_dh_sk` | X25519 ephemeral secret | KEK AES-256-GCM | — |
| `platform_users` | `password_hash` | `scrypt$N$r$p$salt$hash` | self-contained (params + salt embedded) | — |
| `api_tokens` | `token_hash` | `sha256(token)` | no | — |

### Filesystem

| Path | Content | Permissions |
|------|---------|-------------|
| `{data_dir}/.kek_salt` | 32-byte random salt (input to KEK HKDF) | `0600` |
| `{data_dir}/.vapid_private.pem` | P-256 ECDSA private key (PKCS8 PEM) | `0600` |
| `{data_dir}/.vapid_public.txt` | P-256 public key (base64url uncompressed point) | `0644` |

On a **GFS** (the separate relay deploy artifact) one more file lives beside
its database:

| Path | Content | Permissions |
|------|---------|-------------|
| `{data_dir}/gfs_identity.seed` | 32-byte Ed25519 seed — the GFS's identity key: signs cluster gossip and the `/gfs/info` capability block, and its public half is what households pin at pair time | `0600` |

Minted randomly on first boot (`secrets.token_bytes(32)`) and read back on
every later boot, or replaced by `[server] signing_seed_hex` / `GFS_SIGNING_SEED`
(64 hex chars) when the operator injects it from a vault. It is never derived
from configuration: deriving it from the publicly-served `gfs_instance_id`
would let anyone recompute the private key and forge a signed capability
block. Losing the file changes the server's identity, so every paired
household must re-pair.

The KEK itself is never stored — it's re-derived from the salt on each
startup via `KeyManager.from_data_dir`. Passphrase-mode deployments
use `KeyManager.from_passphrase(passphrase, salt)`; losing the
passphrase bricks the instance's existing wrapped keys.

### Recovery Kit (`.shrk`)

A **Recovery Kit** is a passphrase-sealed off-box export of the household's
**trust layer** — the at-rest key material that the regular shareable backup
deliberately excludes — so the SAME `instance_id` can be reconstituted on
fresh hardware after disk loss without re-pairing. It captures the
`instance_identity`, `remote_instances`, `spaces`, and `space_keys` rows
(KEK-wrapped values dumped **verbatim**, still wrapped) plus the `.kek_salt`
that re-derives the runtime KEK — so a restored host decrypts those wrapped
values natively. (`spaces` captures every space row; for owned spaces it
also carries the KEK-wrapped signing seed, so you remain the owner after
recovery.) Impl: `services/recovery_crypto.py` (codec) +
`services/recovery_kit_service.py` (build/restore). In `haos` mode the HA
Supervisor backup already captures `{data_dir}` (DB + `.kek_salt`), so the
Kit is the path for `standalone` / `ha` deployments.

The file is one JSON object: a **clear header** + a sealed body. The header
is the AEAD associated data, so tampering any field (e.g. swapping
`instance_id` onto another household, or downgrading a suite) breaks
decryption.

| Field | Content |
|-------|---------|
| `kit_version` | `1` (receivers reject any other) |
| `kdf_suite` | `scrypt-n16384-r8-p1` (`SUPPORTED_RECOVERY_KDF_SUITES`) |
| `aead_suite` | `aesgcm-256` (`SUPPORTED_RECOVERY_AEAD_SUITES`) |
| `instance_id` | the sealing household (clear, for pre-decrypt display) |
| `created_at` | seal time (ISO-8601) |
| `seal_salt` | b64url, 32-byte scrypt salt for the passphrase |
| `nonce` | b64url, 12-byte AES-GCM nonce |
| `ciphertext` | b64url, `AES-256-GCM(payload)`, AAD = canonical header |

- **KDF:** `scrypt(N=2^14, r=8, p=1)` over the UTF-8 passphrase + `seal_salt`
  → 32-byte AES key. Distinct from the KEK's HKDF — the Kit's passphrase seal
  is a **second** layer protecting the `.kek_salt` + wrapped rows off-box.
- **Suite tags** follow the project's `*_suite` contract: validate against the
  `SUPPORTED_RECOVERY_*_SUITES` frozensets, **no default fallback** — an
  unknown suite raises `UnsupportedRecoverySuite`.
- **Fail-closed restore:** writes `.kek_salt`, then a **KEK self-test**
  (decrypt the wrapped identity seed) runs *before* any row is inserted;
  restore refuses a non-empty instance and rolls back atomically on failure.

## Derivation chains

- `KEK = HKDF-SHA256(salt, length=32, info=b"socialhome/kek/from-data-dir")`
- Pairing directional keys (post-ECDH):
  - `key_self_to_remote = HKDF-SHA256(shared_secret, info=b"socialhome/session/self-to-remote")`
  - `key_remote_to_self = HKDF-SHA256(shared_secret, info=b"socialhome/session/remote-to-self")`
- ID derivation (public, deterministic, no secret):
  - `instance_id = base32(SHA256(identity_pk)[:20]).lower()`
  - `space_id    = base32(SHA256(space_pk)[:20]).lower()`
  - `user_id     = base32(SHA256(instance_pk ‖ 0x00 ‖ username)[:20]).lower()`

## Post-quantum migration path

### Threat: harvest-now-decrypt-later

A quantum adversary can record federation traffic today and decrypt it
years later once a fault-tolerant quantum computer exists. What's at
risk depends on the primitive:

| Primitive | Risk tomorrow | Mitigation in v1 |
|-----------|---------------|------------------|
| Ed25519 identity signatures | Forgeable — attacker can impersonate any peer in replays | **Hybrid Ed25519+ML-DSA-65** (this doc) |
| X25519 pairing ECDH | Shared secret recoverable; all historical envelopes readable | Phase 2 — ML-KEM-768 for pairing |
| AES-256-GCM payload | Safe (Grover ⇒ ~128-bit) | No change needed |
| P-256 VAPID | VAPID JWT forgeable | Phase 3 — pending Web Push spec update |

### The suite contract

The wire-format building block is the `sig_suite` field. Its grammar:
`<algo>` or `<algo>+<algo>+…`, where each `<algo>` appears in
`socialhome/federation/crypto_suite.KNOWN_ALGORITHMS`. Current
registry:

- `ed25519` — classical.
- `mldsa65` — ML-DSA-65 (FIPS 204, NIST security level 3).

A hybrid envelope's `signatures` map must contain exactly one entry
per algorithm in the suite. Verification is AND — missing or invalid
entries reject the envelope.

Extending the suite with a third algorithm (e.g. SLH-DSA for
long-term archival signatures) is a three-step change:

1. Add the identifier to `KNOWN_ALGORITHMS` + `SUPPORTED_SUITES`.
2. Add a signer class alongside `PqSigner`.
3. Extend `FederationEncoder.sign_envelope_all` /
   `verify_signatures_all` to dispatch on the new identifier.

No wire-format or schema change is needed — the `signatures` map
grows naturally.

### Phase 1 — hybrid signatures (done)

- Config: `federation_sig_suite = "ed25519+mldsa65"`.
- Library: `liboqs-python` via the `pq` optional extra.
- Identity: on startup, `ensure_instance_identity` mints an ML-DSA-65
  keypair and persists the secret KEK-encrypted in
  `instance_identity.pq_private_key`. On an existing deployment that
  later enables hybrid, the bootstrap upgrades the row in place.
- Pairing: QR payload carries `pq_identity_pk` + `sig_suite`. The
  receiver runs `crypto_suite.negotiate`; a classical peer paired
  with a hybrid peer runs classical for that pair.
- Wire: every outbound envelope emits the `signatures` map with the
  per-peer suite's algorithms. Every inbound envelope is rejected
  unless every entry verifies.
- User identity: the per-user binding's `user_sig_suite` follows the
  same suite contract. The PQ migration is wire-additive — grow
  `SUPPORTED_USER_SIG_SUITES` with a sibling
  `USER_SIG_SUITE_*` constant for the hybrid `"ed25519+mldsa65"` variant
  and ship the parallel ML-DSA-65 user signature alongside the Ed25519
  one (the reserved `user_pq_public_key` / `users.user_pq_*` columns
  already hold the material). The signed-bytes layout is unchanged; a
  v_25 receiver that only knows `"ed25519"` rejects the new suite via
  `UnsupportedUserSigSuite` (no default fallback), so the variant ships
  only once both sides advertise it.

### Phase 2 — post-quantum key agreement

Not done in this repo yet. The work is to introduce a `kem_suite`
field to the pairing QR payload and replace X25519 with a hybrid
X25519+ML-KEM-768 KEM (Signal's PQXDH-style). Directional keys would
be derived from the concatenated X25519 + ML-KEM shared secrets
through HKDF. The AES-256-GCM-wrapped session keys on the
`remote_instances` rows stay — only their derivation changes.

### Phase 3 — Web Push VAPID

Blocked on the IETF: Web Push (RFC 8292) currently mandates ECDSA
P-256 for VAPID. When a PQ signature variant lands in the spec,
`services/push_service.py`'s `load_or_create_vapid` grows a PQ branch.
Until then we rely on the transport (browser → push server) being
TLS-protected.

### Phase 4 — retire classical halves

Only once every peer in the federation has rotated to a hybrid suite.
Retirement is a flag on `Config` (`federation_require_pq = True`)
rather than a wire-format change — hybrid receivers start rejecting
classical envelopes. The admin UI should warn about any paired peers
still on `sig_suite = "ed25519"` before the flag is flipped.

## Operator checklist

To enable the hybrid signature suite on a deployment:

1. Install `liboqs-python` next to the existing `socialhome` install:
   `pip install 'oqs @ git+https://github.com/open-quantum-safe/liboqs-python@0.10.0'`
   (PyPI doesn't accept direct URL refs so we can't bundle this as a
   `socialhome[pq]` extra; install it manually). Requires the native
   `liboqs` C library on the host.
2. Set `federation_sig_suite = "ed25519+mldsa65"` in `socialhome.toml`
   (or `SH_FEDERATION_SIG_SUITE=ed25519+mldsa65` as an env var).
3. Restart. `identity_bootstrap` detects the suite change and mints
   the ML-DSA keypair on startup.
4. Re-pair any existing paired peers. Newly paired peers automatically
   negotiate the hybrid suite if they also have it enabled.

`liboqs-python` must be compatible with the host's liboqs C library —
mismatched versions will fail at import. Test this end to end in a
staging deployment before flipping a production instance.

## References

- **NIST FIPS 203** (ML-KEM, standardises CRYSTALS-Kyber).
- **NIST FIPS 204** (ML-DSA, standardises CRYSTALS-Dilithium).
- **NIST FIPS 205** (SLH-DSA, standardises SPHINCS+).
- **Signal "PQXDH"** (2023) — the canonical hybrid KEM protocol
  reference.
- **liboqs-python** — <https://github.com/open-quantum-safe/liboqs-python>.
- **RFC 8292** (VAPID for Web Push).
- **RFC 5869** (HKDF).
- **RFC 7914** (scrypt).
