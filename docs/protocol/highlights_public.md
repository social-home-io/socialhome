# Highlights — public sharing via GFS

A highlight author can opt a single highlight into public sharing through a
paired Global Federation Server (GFS). The GFS mints a revocable URL
the author can paste into Twitter / email / SMS; anyone visiting the
URL gets a public landing page. The page bootstraps a WebRTC
DataChannel **directly to the author's instance**; GFS only relays SDP/ICE
during the handshake. When WebRTC can't connect, the GFS pipes the stream —
and on both paths every byte of the highlight is **sealed end to end** to
the viewer's browser, under a secret that rides only in the link's
fragment, so the GFS can't read it even if it opens the link itself (see
"End-to-end sealed stream").

This is the only path in Social Home where a non-Social-Home browser
can view a highlight. The author's existing audience and retention rules
still apply: a publication can never outlive `highlights.expires_at`, and
revoking a token (or unpublishing) takes effect on the next request.

## Scope

- **HFS** runs :class:`HighlightPublicationService` (publish / revoke /
  unpublish) and (PR2) :class:`HighlightSignalingHandler` for the WebRTC
  answerer side.
- **GFS** runs :class:`HighlightPublicationRegistry` and serves the
  ``/highlight/{instance}/{highlight}/{token}`` public landing page. PR2 adds
  the public ``/gfs/highlight_rtc/*`` signalling endpoints.

## URL shape

```
https://{gfs_host}/highlight/{instance_id}/{highlight_id}/{token}#k={link_secret}
```

* ``instance_id`` — author's HFS instance id.
* ``highlight_id`` — opaque highlight id on that instance.
* ``token`` — 32-byte urlsafe-base64. Revocable, multiple per
  publication. Authoritative; the GFS resolves it against
  ``gfs_highlight_tokens`` and rejects mismatched ``(instance_id, highlight_id)``
  tuples in the URL.
* ``k`` (fragment) — the 32-byte link secret, base64url. Browsers never send
  a fragment to a server (nor in ``Referer``), so the GFS never gets it. The
  author's household derives it as ``HKDF-SHA256(identity seed,
  info="sh-highlight-link:v1" ‖ 0 ‖ highlight_id ‖ 0 ‖ token)`` and appends it
  to the URL the GFS returned — the secret itself is never stored, and only
  the author's household can make one.
  Because it derives from the **long-term identity seed**, an identity
  rotation (a new seed) changes every secret: every public highlight link
  made before it stops opening (the stream no longer opens under the old
  `#k`), and the author makes new links from the share menu. Nothing on the
  GFS has to change; the old tokens just open nothing.

## Wire endpoints

Author SH → GFS (Ed25519-signed body, same `_rtc_authenticate`
middleware as `/gfs/rtc/*`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/gfs/highlights/{highlight_id}/publish` | Record a publication and mint the first share token. Body: `{highlight_id, instance_id, expires_at, label?, signature}`. Returns `201 {token, url, label}`. |
| POST | `/gfs/highlights/{highlight_id}/tokens` | Mint another token under an existing publication. Body: `{label?, signature}`. Returns `201 {token, url, label}`. |
| POST | `/gfs/highlight_tokens/{token}/revoke` | Revoke a single token. Body must come from the publishing instance — guards against cross-instance revoke. |
| POST | `/gfs/highlights/{highlight_id}/unpublish` | Drop the publication; CASCADE revokes every token under it. |

Public (no signature):

| Method | Path | Purpose |
|---|---|---|
| GET | `/highlight/{instance_id}/{highlight_id}/{token}` | SSR landing page. Always `200` with the same viewer shell and `Cache-Control: no-store` (the URL carries the share token). The GFS does not look up the token or check whether the author is online here (see "Not a presence oracle" below). The viewer's offer call is the first point that consults state. |

Public-viewer WebRTC signalling (added in PR2):

| Method | Path | Purpose |
|---|---|---|
| GET | `/gfs/highlights/ice-servers` | Anonymous list of STUN/TURN URLs the browser bootstrap feeds into `RTCPeerConnection`. |
| POST | `/gfs/highlight_rtc/offer` | Anonymous. Body: `{instance_id, highlight_id, token, sdp, vk}` (`vk`: the viewer's ephemeral P-256 key the stream is sealed to, see "End-to-end sealed stream"; forwarded to the author with the token only when shaped like one, never stored or logged — without it the author streams nothing). GFS verifies the token, stores the offer in :class:`GfsRtcSession`, and pushes a `highlight_signal` WS frame to the author's instance. Returns `201 {session_id}`. Every non-success state (unknown / revoked / expired token, URL mismatch, unpublished, author offline) gets the one uniform `503 {"error":"unavailable"}`. |
| GET | `/gfs/highlight_rtc/session/{session_id}` | Anonymous. Browser polls until `answer_sdp` and any author-side ICE candidates land. |
| POST | `/gfs/highlight_rtc/ice/viewer` | Anonymous. Body: `{session_id, candidate}`. Forwards to the author's WS as a `highlight_signal kind=ice` frame. |
| POST | `/gfs/highlight_rtc/answer` | Author SH only (Ed25519-signed). Body: `{instance_id, session_id, sdp, signature}`. Authority guard: `session.initiator_id` must match the signing instance. |
| POST | `/gfs/highlight_rtc/ice/author` | Author SH only (signed). Same authority guard; appends to `ice_candidates` so the next viewer poll sees it. |

GFS-relay fallback (used when the direct DataChannel can't connect — see
"GFS-relay fallback" below):

| Method | Path | Purpose |
|---|---|---|
| GET | `/gfs/highlight_rtc/relay/{instance_id}/{highlight_id}?token=...&vk=...` | Anonymous, token-gated (same token as the offer flow); `vk` as on the offer (without it the author streams one `error: unsupported` frame). Chunked `application/octet-stream`: the GFS pushes a `highlight_signal kind=relay_offer` to the author and pipes the framed bytes the author streams back. Every non-success state (bad / expired token, URL mismatch, author offline, relay capacity, author never connects) gets the uniform `503 {"error":"unavailable"}`, sent at once (only the never-connects branch waits its 30 s budget). `422` (missing token). |
| POST | `/gfs/highlight_rtc/relay-stream/{relay_id}` | Author SH only. Header-auth: `X-SH-Instance` + `X-SH-Signature` (Ed25519 over canonical `{"instance_id","relay_id"}`); body is the raw framed byte stream. `403` (relay's target instance != signer), `404` (unknown relay), `401` (bad signature), `422` (missing headers). |

The viewer DataChannel has label `highlight-public-v1` and uses the
length-prefixed JSON-header / binary-payload framing detailed below. The
GFS-relay fallback streams the **identical** framed bytes over HTTP.

## Retention

* Per-publication: `gfs_highlight_publications.expires_at` is a unix
  epoch mirroring the author's `highlights.expires_at`. The GFS cron
  call `prune_expired(now)` drops past-cap rows; `lookup_active`
  filters live too.
* Per-token: `revoked_at` is `NULL` while active; setting it to a
  unix epoch makes `resolve_token` return `None` immediately.
* Author can revoke any individual token without affecting other
  tokens under the same publication.
* No follower / viewer extension — the same retention rule that
  governs the in-mesh viewer governs the public viewer.

## Not a presence oracle

A publication can only be streamed while the author's instance has a live
SH↔GFS WebSocket, because the offer and the relay are pushed over it. The
GFS must not let an outsider use that as an "is this household online?"
probe ([`principles.md`](../principles.md#the-gfs-is-not-an-author-presence-oracle)):

* The landing page is the same shell for every URL. It does no token lookup
  and no `is_connected` check.
* The offer and the relay GET fold token resolution and the author's
  connection into one boolean
  (`routes/highlight_rtc.py:_servable`). Both checks run on every branch,
  and every failure gets `global_server/public_unavailable.py`'s uniform
  `503 {"error":"unavailable"}` + `Cache-Control: no-store`. Unknown,
  revoked and offline can't be told apart.
  Failures are answered at once, with no added delay. A delay would hide
  nothing, because the matching offer already answers `201` or `503`
  immediately, and it would let anonymous callers hold a socket open for
  30 s. Only the relay's never-streams branch waits, for its 30 s connect
  budget.
* The landing page itself is sent with `Cache-Control: no-store`, because
  its URL carries the share token.
* The viewer renders it as "This isn't available right now." in a
  `role="alert"` region and moves focus to a *Try again* button. It retries
  on its own after 10 s, 30 s and 60 s, then stops. The old offline page
  reloaded every 10 s forever, and that page was itself the oracle.

**Residual:** success shows that the author's household was connected at
that moment. That means a `201` offer (answered or not), or a relay that
streams bytes. Live content from the author's household can't hide this.

## GFS-relay fallback

The direct DataChannel is always tried first. When WebRTC can't connect
(symmetric NAT, blocked UDP, a browser that never gathers a usable
candidate) the viewer bootstrap falls back to a chunked HTTP GET against
`/gfs/highlight_rtc/relay/{instance_id}/{highlight_id}`:

1. The GFS resolves the token, checks that the author's SH↔GFS WS is
   live, and registers a transient in-memory `RelayBridge` keyed by a fresh
   `relay_id`.
2. It pushes a `highlight_signal` WS frame `kind: "relay_offer"`
   (`{relay_id, highlight_id, token}`) to the author.
3. The author opens `POST /gfs/highlight_rtc/relay-stream/{relay_id}` and
   streams the **byte-identical** framed payload (same
   `highlight-public-v1` framing as the DataChannel) back to the GFS.
4. The GFS pipes those bytes straight through to the still-open viewer
   GET and tears the bridge down at `stream_end`.

The GFS stores **zero** content bytes — the `RelayBridge` is purely an
in-memory pipe between the inbound author POST and the outbound viewer
GET. If the token doesn't resolve, the author is offline (no live WS),
relay capacity is full, or the author never connects the relay-stream,
the viewer GET returns the same uniform `503`. It is sent at once, except
on the never-connects branch, which first waits the 30 s budget. See "Not a
presence oracle" above.

## DataChannel framing

Single ordered DataChannel labelled `highlight-public-v1`. Every frame on
the wire is:

```
[u32 header_len BE][header_json][u32 payload_len BE][payload_bytes]
```

`header_json` is UTF-8 JSON with a `kind` field. Reserved kinds (v1):

| `kind` | Direction | Header fields | Payload |
|---|---|---|---|
| `highlight_meta` | author → viewer (first frame) | `highlight` (full Highlight dict), `frames` (manifest: `[{frame_id, sequence, content_type, byte_length, caption_text, caption_emoji, duration_ms}, …]`) | empty |
| `stream_seal` | author → viewer (first frame, plaintext) | `seal_suite`, `epk` | empty — opens the sealed stream (see "End-to-end sealed stream") |
| `sealed` | author → viewer (every frame after `stream_seal`) | `kind` only | one whole inner frame (any kind below), AES-256-GCM sealed |
| `author_profile` | author → viewer (inner, right after `highlight_meta`) | `seal_suite`, `epk`, `nonce` | the author's `{display_name, bio, picture}` sealed to `vk` (see "Who shared it") |
| `frame_chunk` | author → viewer | `frame_id`, `sequence`, `chunk_index`, `is_last_chunk`, `byte_length` | up to `CHUNK_SIZE` (64 KiB) bytes |
| `stream_end` | author → viewer (terminator) | `kind` only | empty |
| `error` | author → viewer (plaintext, content-free) | `error` (one of `expired`, `unauthorized`, `backpressure`, `unsupported` — no `vk` or token to seal to) | empty |

The rows after `sealed` are the **inner** frames: they only ever travel
inside a `sealed` frame. The viewer never shows a content frame that
arrived in the clear.

Backpressure: author waits on `RTCDataChannel.bufferedAmount <
SEND_HWM_BYTES` (1 MiB) before pushing the next chunk. Reference
encoder + decoder: `socialhome/services/highlight_public_framing.py`;
golden-bytes test in `tests/protocol/test_highlight_public_framing.py`
(release-blocker per CLAUDE.md §27.9).

## End-to-end sealed stream

The GFS mints the share token, so it could open the link's path like any
viewer, and on the relay fallback it pipes every byte. Neither shows it the
highlight:

1. The share URL carries the **link secret** in its fragment (`#k=`, see
   "URL shape"). The viewer bundle reads it from `location.hash`; it is never
   part of a request.
2. The viewer makes an ephemeral **P-256** key pair per view and sends the
   public half as `vk` with its offer / relay GET. The GFS forwards `vk` and
   the token to the author in the `offer` / `relay_offer` frame.
3. The author's household derives the link secret from the token, makes its
   own ephemeral pair and sends one plaintext `stream_seal` frame
   `{seal_suite, epk}`. Every other frame goes out as `sealed`:
   `AES-256-GCM(key, nonce = 96-bit big-endian frame counter, AAD = highlight
   id)` over the whole encoded inner frame, with
   `key = HKDF-SHA256(ikm = ECDH(epk, vk), salt = link secret,
   info = "sh-highlight-public-stream:v1")`. Suite
   `p256+link-hkdf-sha256-aes256gcm` (`SUPPORTED_STREAM_SEAL_SUITES`); the
   viewer refuses any other.
4. The viewer opens the frames in arrival order; a dropped, reordered,
   tampered or foreign frame fails its tag and the page says the highlight
   couldn't be opened.

Both halves are needed. Without the link secret — the GFS, which only ever
sees the path, the token and `vk` — nothing opens, even if it swaps in its
own `vk`. Without this view's private key, someone who has the link still
can't read another viewer's stream off the relay. An offer or relay request
with no `vk` or no token is never streamed (the relay answers one plaintext
`error: unsupported`).

**Only our own live links are streamed.** The GFS forwards the token
with every offer / relay request, but the household does not take its word
for it: it streams only when the token is one it minted itself — it keeps a
SHA-256 of each token the GFS returned at publish
(`highlight_public_tokens`, migration 0116), drops it on revoke (before it
even calls the GFS, so a revoke holds even if that call fails) and drops all
of them on unpublish or when the GFS connection is removed — for a highlight
that is still published on that same GFS and not past `expires_at`. Anything
else gets no stream (a direct offer is refused; the relay answers one
plaintext `error: expired`). A token longer than 256 characters (the GFS
mints 43) is refused before any lookup or HKDF (`error: unsupported`).
Revocation is therefore enforced by the author's household, not by the GFS.
Unpublish clears all of them even when the GFS connection is suspended or
gone. A highlight has one publication: publishing it on another GFS first
pulls the previous one and its tokens, so no earlier link can come back.
Relay uploads in flight are capped like the WebRTC viewers (50 per
household).

**Links made before this** have no fragment. The page detects that before
contacting anyone and says *"This link can't be opened … Ask whoever shared
it for a new link."*; the author makes a new one from the highlight's share
menu (each publish mints a new token, now with its secret). The old token
stays valid on the GFS until revoked or the highlight expires, but no
household streams a highlight in the clear any more, so it opens nothing.

### Rollout: both sides must be new

The sealed stream changes what the household sends and what the GFS's
viewer page must open, so it only works when both are new:

| Household | GFS viewer page | What happens |
|---|---|---|
| new | new | Sealed link (`#k=`) plays. |
| new | old (can't open `sealed` frames) | **Refused at publish**: the GFS's signed `/gfs/info` capability block must carry `sealed_highlight_stream: true` (verified against the GFS key pinned at pairing, like every other GFS capability — an unsigned or stripped flag counts as "no"). Without it `POST /api/highlights/{id}/publish` answers **409 `GFS_TOO_OLD`** (`params.feature = "public_highlight"`) before anything is sent to that GFS, and the share dialog says, next to the GFS picker, that this GFS can't share encrypted highlight links yet and its operator needs to update it. A dead link is never minted. |
| old | new | The old household's links carry no `#k`: the new page shows *"This link can't be opened … Ask whoever shared it for a new link."* without contacting anyone. If someone adds a `#k` by hand, the old household's plaintext frames arrive, are never shown, and the page says it couldn't be opened. |
| old | old | Unchanged (plaintext, as before). |

A household learns the capability from the same `/gfs/info` fetches as the
others (pairing, reconnect, the hourly refresh, or once on demand at
publish); a proven `true` sticks for the process. No household↔household
federation shape changed, so `OURS` is not bumped (the GFS leg has no
`proto_version`; see [capabilities.md](./capabilities.md#gfs-capabilities)).

**Residuals:**

- The GFS serves the viewer page and its script, so a GFS that rewrote
  that script could read the fragment in the browser. Sealing removes
  everything the GFS sees on the wire and at rest; it cannot remove trust in
  the code a web page is served with (a pinned viewer is the follow-up).
- The stream's integrity rests on the link secret alone; frames are not
  signed by the household. A GFS that ALSO holds the full link (say, it was
  posted publicly where the GFS saw it) can swap `vk` and `epk` and read or
  rewrite that view's stream, or show a highlight under the author's name.
  Treat a link as a bearer secret: whoever has it, and runs the relay, is
  trusted for that link. Reference:
`socialhome/services/highlight_public_seal.py`,
`client/gfs/stream_seal.ts` (interop test with a Python-sealed golden
vector).

## Who shared it (sealed author profile)

The page names the author above the highlight: display name, bio and a
small picture (WebP ≤ 256 px / 24 KB, re-encoded by the author's
household). They travel **inside the highlight stream**, sealed to the
viewer, so the GFS stores nothing new and relays only ciphertext on both
transports:

1. The viewer bundle makes an ephemeral **P-256 ECDH** key pair for this
   view (WebCrypto; X25519 isn't in every supported browser yet) and sends
   the raw public point (base64url) as `vk` with its offer and its relay
   GET. Without WebCrypto it can't open the sealed stream either, so it
   asks nothing and says the browser can't open encrypted highlights.
2. The GFS forwards `vk` in the `highlight_signal` `offer` /
   `relay_offer` frame when it is shaped like a key; it never stores or
   logs it.
3. The author's household makes its own ephemeral pair, derives
   `HKDF-SHA256(ECDH, salt="", info="sh-highlight-public-profile:v1")` and
   seals the profile JSON with AES-256-GCM, the highlight id as associated
   data. Frame header: `seal_suite` (`p256-hkdf-sha256-aes256gcm`,
   `SUPPORTED_PROFILE_SEAL_SUITES`), `epk`, `nonce`; the viewer refuses an
   unknown suite and shows no author.
4. The DataChannel is DTLS end to end anyway; on the relay fallback the
   GFS pipes the sealed frame like every other byte.

A protected (child) account cannot publish a highlight (§CP.R); if one
streams anyway, the profile carries the name only. The profile frame keeps its own seal and now also rides inside the sealed
stream, so it opens only with the link secret too.
Reference: `socialhome/services/highlight_public_profile.py`,
`client/gfs/sealed_profile.ts` (interop test with a Python-sealed golden
vector).

## Sequence (public viewer flow)

```mermaid
sequenceDiagram
    autonumber
    participant V as Viewer<br/>(browser)
    participant G as GFS
    participant A as Author SH

    V->>G: GET /highlight/{i}/{s}/{t}  (#k=secret stays in the browser)
    G-->>V: 200 SSR landing + bootstrap.js (same shell for every URL)
    V->>V: no #k → "ask for a new link", stop (no further request)
    V->>G: GET /gfs/highlights/ice-servers
    G-->>V: { servers: [...] }
    V->>V: ephemeral P-256 key pair (vk)
    V->>G: POST /gfs/highlight_rtc/offer {sdp, vk}
    G->>G: resolve_token + author_online (both, every request)
    Note over G,V: any failure → uniform 503 {"error":"unavailable"}
    G->>G: store offer in GfsRtcSession
    G->>A: WS push { type:"highlight_signal", kind:"offer", session_id, sdp, token, vk }
    G-->>V: 201 { session_id }
    A->>A: token one of our live links? (SHA-256 in highlight_public_tokens,<br/>published on this GFS, unexpired) — else no stream
    A->>A: open answerer PeerConnection
    A->>G: POST /gfs/highlight_rtc/answer (signed) {session_id, sdp}
    A->>G: POST /gfs/highlight_rtc/ice/author (signed) (xN)
    V->>G: POST /gfs/highlight_rtc/ice/viewer (xN)
    G->>A: WS push { kind:"ice", candidate } (xN)
    V->>G: GET /gfs/highlight_rtc/session/{id} (poll)
    G-->>V: { answer_sdp, ice_candidates }
    alt direct DataChannel connects
        V-->>A: DataChannel "highlight-public-v1" opens (direct, no GFS)
        A->>A: secret = HKDF(identity seed, highlight, token)
        A->>V: stream_seal {seal_suite, epk}
        A->>V: sealed(highlight_meta), sealed(author_profile)
        A->>V: sealed(frame_chunk) × N
        A->>V: sealed(stream_end)
    else WebRTC fails — GFS-relay fallback
        V->>G: GET /gfs/highlight_rtc/relay/{i}/{s}?token={t}&vk={vk}
        G->>G: register transient RelayBridge {relay_id}
        G->>A: WS push { kind:"relay_offer", relay_id, highlight_id, token, vk }
        A->>A: same live-link check — else one error: expired frame
        A->>G: POST /gfs/highlight_rtc/relay-stream/{relay_id} (header-signed)
        Note over G: GFS pipes byte-identical sealed frames<br/>(no secret, can't open them; stores nothing)
        G-->>V: chunked octet-stream (stream_seal → sealed × N)
    end
    V->>V: open each sealed frame in order (ECDH(vk) + #k secret)
```

## Public viewer bundle

The viewer that the GFS landing serves is a Preact component built
from `client/gfs/public_highlight.tsx` via a separate Vite config
(`client/vite.gfs.config.ts`) that emits a single self-contained
IIFE bundle to `socialhome/global_server/static/highlight_public_viewer.js`.
Sharing the SPA's Preact + Vite tooling gives us one component model
across both surfaces; future GFS UI work (admin portal port, public
global-space pages) plugs in as additional rollup inputs without a
second toolchain. Run `pnpm build:gfs` from `client/` to rebuild the
GFS bundle alone, or `pnpm build` for both.

## Implementation pointers

- Schema (SH side): `socialhome/migrations/0001_initial.sql` —
  `highlights.public_gfs_id`, `highlights.public_published_at`;
  `socialhome/migrations/0116_highlight_public_tokens.sql` — the SHA-256 of
  each live share token this household minted.
- Schema (GFS side): `socialhome/global_server/migrations/0001_initial.sql`
  — `gfs_highlight_publications`, `gfs_highlight_tokens`.
- Domain: `socialhome/domain/highlight.py` (`Highlight.public_*` fields);
  `socialhome/global_server/domain.py` (`GfsHighlightPublication`,
  `GfsHighlightToken`).
- Repos: `socialhome/repositories/highlight_repo.py`
  (`mark_published` / `mark_unpublished` / `list_published_for`,
  `add_public_token` / `remove_public_token` / `is_live_public_token`);
  `socialhome/global_server/repositories.py`
  (`SqliteGfsHighlightPublicationRepo`, `SqliteGfsHighlightTokenRepo`).
- Services: `socialhome/services/highlight_publication_service.py` (SH);
  `socialhome/global_server/highlight_publications.py` (GFS registry).
- Routes: `socialhome/routes/highlight_publications.py` (SH);
  `socialhome/global_server/routes/highlights.py` (GFS);
  `socialhome/global_server/routes/highlight_rtc.py` (public + author RTC).
- Author-side answerer: `socialhome/services/highlight_signaling_handler.py`.
- Framing: `socialhome/services/highlight_public_framing.py` +
  `tests/protocol/test_highlight_public_framing.py` (golden-bytes test).
- Public viewer: `client/gfs/public_highlight.tsx` + the matching Vite
  config at `client/vite.gfs.config.ts`.

## Security notes

- Encryption-first carve-out: this is the one Social Home surface
  where content is intentionally readable without a household
  identity — by whoever holds the full link. The carve-out is
  per-highlight and author-initiated; nothing becomes public without the
  author flipping the toggle, and the GFS between them is never a reader.
- Signed publish: every `/gfs/highlights/*` mutation is Ed25519-signed
  by the author's instance and verified against the GFS-side
  `client_instances.public_key`. A bad actor cannot publish someone
  else's highlight.
- Token-vs-URL match: the landing handler rejects URLs whose
  `(instance_id, highlight_id)` segment doesn't agree with the resolved
  token, so a stolen token cannot be used to enumerate other
  highlights on the same instance.
- Per-IP rate-limit on the public landing path uses the existing
  `build_listing_rate_limit()` middleware (30/min/IP).
- Author can pull every token instantly via `unpublish`. There's no
  revoke-key-rotation step — the row's deletion is the revoke, and the
  author's household enforces it itself: it streams only for a token it
  minted and still holds (see "Only our own live links are streamed"), so a
  GFS that keeps forwarding a revoked token gets nothing.
- The GFS-relay carries **only sealed** frames (see "End-to-end sealed
  stream"): the GFS has the token but never the link secret, so it can't
  open them, even by opening the link itself. It is a transient in-memory
  pipe (`RelayBridge`) and **persists nothing**. The relay-stream POST is still Ed25519 header-signed and the
  bridge rejects a signer whose instance != the relay's target, so a
  third party can't inject bytes into someone else's stream.
