---
name: federation-demo
description: Boots five Social Home households (a / b / c / d / e) on adjacent ports, walks the §11 QR handshake (a↔b, b↔c, a↔c, b↔d) plus the §11 simple-pairing trust-relay flow (a auto-pairs with d via b without a QR scan), exercises the federation surface end-to-end (profile sync, posts, moments, highlights, cross-household DMs, multi-household space with remote invites, space-calendar event + cross-household RSVP) under the real WebRTC transport, and asserts that every household sees the others' federated content. The fifth household, e, is paired with nobody but the connection server, so the opt-in gfs-* chain can prove the §D2b invite-link bootstrap redeem and the GFS-relay delivery tier on the real wire. Use when validating an end-to-end federation change, smoke-testing a new ``aiolibdatachannel`` release, or reproducing a multi-household sync bug.
---

## When to invoke this skill

Run this skill whenever you need to confirm the full HFS↔HFS federation
path is intact:

- Validating a federation, pairing, sync, DM-routing or space-invite
  change before merging.
- Smoke-testing a new ``aiolibdatachannel`` release before bumping the
  pin in this repo.
- Reproducing a multi-household sync bug a user has reported.

The skill expects WebRTC to work natively — no ``SH_DISABLE_RTC=1``
fallback. If the constructor segfaults on first use, your
``aiolibdatachannel`` wheel is the wrong one for the running Python /
OpenSSL combination; rebuild it from source (``pip install -e
../aiolibdatachannel --no-build-isolation``) before retrying.

## Topology

```
            ┌──── a ────┐                     e
            │           │      a ↔ d is *not* a QR handshake — it is
            │           │      established via the §11 trust-relay
            b ◄───────► c       flow ("simple pairing" via b).
            │
            │                  e is paired with NOBODY — its only
            d                  link is to the connection server.
```

- **a** — Alpha House @ ``127.0.0.1:18001`` (admin: ``alice``)
- **b** — Beta House  @ ``127.0.0.1:18002`` (admin: ``bob``)
- **c** — Gamma House @ ``127.0.0.1:18003`` (admin: ``carol``)
- **d** — Delta House @ ``127.0.0.1:18004`` (admin: ``dave``)
- **e** — Epsilon House @ ``127.0.0.1:18005`` (admin: ``emma``)

The inner ring (a / b / c) is fully connected via the §11 QR
handshake. **d is deliberately not paired with a directly** — only
b↔d is a QR pair. The skill then exercises the §11 simple-pairing /
trust-relay flow: Alpha asks Beta to vouch for an introduction to
Delta, Delta's admin one-clicks "accept", and the a ↔ d pair lands
without anyone scanning a QR code.

**e is the stranger.** It is paired with nobody: no QR handshake in
``pair``, no introduction in ``relay-pair``, and therefore no mesh
route either (route discovery only walks CONFIRMED peers). Its single
federation relationship is with the GFS, wired in ``gfs-pair``. That
isolation is the whole point of it: a / b / c / d are all
mesh-reachable from one another, so a token redeem between any two of
them takes the ordinary direct or ``SPACE_ROUTED`` path and the §D2b
**bootstrap** path (sealed redeem via ``POST /gfs/envelope``, a
``space_session`` seat, ``GfsRelayTransport`` for everything after)
never runs. e is the only household that can prove it — see
``gfs-invite-link``. Pair e with anyone and that step silently stops
testing what it is named after.

## Prereqs

- Run from the repo root (``/workspaces/social-home/repos/socialhome``).
- The Python venv (or system Python) used to launch ``socialhome`` must
  have a working ``aiolibdatachannel`` import — i.e. ``python -c "from
  aiolibdatachannel._core import PeerConnection;
  PeerConnection(ice_servers=['stun:stun.l.google.com:19302'])"``
  succeeds without segfaulting.
- Ports ``18001`` / ``18002`` / ``18003`` / ``18004`` / ``18005``
  (households) and ``18765`` (GFS) must be free. Kill a squatter **by
  port** (``lsof -ti :18005 | xargs -r kill -9``) — never
  ``pkill -f socialhome``, which takes out every unrelated checkout on
  the box too.
- ``/tmp/sh-demo`` will be wiped and re-created.

## Run it

```bash
python .claude/skills/federation-demo/harness.py all
```

That single command runs the full sequence:

1. ``up`` — wipe ``/tmp/sh-demo``, write per-instance ``socialhome.toml``
   (configures ``[standalone].external_url`` so peers can reach each
   other), launch all five backends, and walk the
   ``/api/setup/standalone`` wizard so each gets a bearer token.
2. ``pair`` — four QR handshakes (a↔b, b↔c, a↔c, b↔d). After this
   ``/api/pairing/connections`` returns the expected confirmed-peer
   counts on each instance (a:2, b:3, c:2, d:1, **e:0** — e stays
   unpaired on purpose; see Topology).
3. ``relay-pair`` — §11 simple-pairing dry run.
   - ``POST /api/pairing/auto-pair-via {via_instance_id, target_instance_id}``
     on Alpha asks Beta to vouch for an introduction to Delta. Beta
     forwards the request to Delta over federation; no admin click on
     Beta's side.
   - Delta's admin sees the pending request in
     ``GET /api/pairing/auto-pair-requests`` and approves it via
     ``POST /api/pairing/auto-pair-requests/{id}/approve`` —
     one-click, no QR scan.
   - Both a and d now show each other as ``CONFIRMED``.
4. ``traffic`` — for each household (a / b / c — d stays out of the
   common content fan-out so the inner-ring assertions still bound at
   three viewers):
   - **a → b** and **c → b** ``POST /api/moments/follows`` so Beta's
     moment in the next step lands in Alpha's and Carol's inboxes.
     The follow itself federates as ``USER_FOLLOW``.
   - ``PATCH /api/me`` (display name + bio) → federates ``USER_UPDATED``
     to every paired peer.
   - ``POST /api/feed/posts`` (household-scoped, never federates — sanity
     baseline that local writes still work).
   - ``POST /api/moments`` (one per household; the public-moment ladder
     allows one new moment per 15 min). Beta's moment id is stashed in
     ``state.json`` so ``verify`` can grep it on Alpha's and Carol's
     inboxes.
   - ``POST /api/highlights/frames`` with ``audience_kind=all_paired``
     → fans out the highlight + frame to every paired peer.
   - 1:1 DM from **a → c** (cross-instance conversation; the message
     rides the federation envelope path).
   - 1:1 **media** DM from **a → c** (v_3 ``type='image'``): Alice
     uploads a tiny WebP via ``/api/media/upload``, posts a DM with
     ``media_url`` + ``file_name`` + ``mime_type``. ``DmService``
     embeds a 320 px preview in the outbound ``DM_MESSAGE``; the
     :class:`DmMediaSyncService` scheduler ships the full bytes via
     a follow-up ``DM_MEDIA_BLOB`` event. ``verify`` asserts both
     legs landed on Carol's instance.
   - **b** creates a space (``invite_only``) and mints a ``remote-invites``
     token for each of **a**'s and **c**'s admin users.
   - **b** posts a ``mode=fixed`` bazaar listing in the salon space;
     **a** opens a DM to Beta quoting the listing id ("interested in
     your bazaar listing X"). The listing itself is HFS-local
     (bazaar rows are space-scoped), but the inquiry DM rides the
     usual cross-instance ``DM_RELAY`` path.
5. ``calendar`` — cross-household space-calendar + RSVP federation.
   - Alpha and Carol accept Beta's pending remote-invites
     (``POST /api/remote_invites/{token}/accept``); both become space
     members on Beta's side.
   - Beta creates a calendar event in the space
     (``POST /api/spaces/{id}/calendar/events``); the event federates
     as ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Gamma.
   - Alpha and Carol RSVP "going"
     (``POST /api/calendars/events/{id}/rsvp``); each RSVP federates
     back to Beta as ``SPACE_CALENDAR_RSVP``.
   - Beta's ``GET /api/calendars/events/{id}/rsvps`` is asserted to
     show both Alpha and Carol as ``going``.

4. ``verify`` — assertions across all three households:
   - Every confirmed peer advertises the build's current ``OURS``
     ``proto_version``, **and** — when the ``gfs-invite-link`` chain has run
     — both §D2b ``space_session`` seats carry at least
     ``FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM`` (v_29). That
     second assertion exists because v_29 is the one capability that does
     NOT ride ``INSTANCE_CAPABILITIES_UPDATED``: on the bootstrap path
     there is no peer row to read it off, which is exactly why the invite
     blob carries ``issuer_proto_version`` (what the redeemer gates its
     attempt on) and the sealed redeem body carries the redeemer's (what
     the issuer stamps on the seat). Both land on the seats, so the seats
     are where the round-trip is provable.
     (The capability-bump tripwire — v_24, which makes
     ``SPACE_CONFIG_CHANGED`` space-authority-signed so a seed-holding delegated
     admin can change a space's config with the owner offline and every member
     household (incl. the offline owner on reconnect) accepts it by verifying
     the signature, not ``from_instance == owner``. v_23 adds
     peer-replicated, space-authority-signed roster gossip:
     ``SPACE_MEMBER_JOINED`` / ``SPACE_MEMBER_LEFT`` broadcast on every roster
     mutation so every member household converges its roster, verified against
     the space's public key. v_22 ships the delegated-admin signing-seed share;
     v_21 adds authenticated mesh route discovery: a v_21 target signs the
     ``target_eph_pk`` it ships in ``SPACE_ROUTE_FOUND`` so the origin won't
     seal space content under a relay-substituted key. v_20 guards the
     ``SPACE_SYNC_REJECTED`` reconnect-reconcile backstop: a host only sends
     that reply to peers it sees at >= v_20), and an
     ``INSTANCE_RESYNC_REQUEST`` (capabilities scope, v_19, #319 ¶6) to a
     v_19+ peer via ``POST /api/admin/federation/resync`` is accepted —
     proving the new event type + operator endpoint + ``peer_supports``
     gate round-trip (a sub-v_19 peer would 409).
   - Every household sees the other two's display names via
     ``/api/friends`` (peer-directory snapshot delivered).
   - Every household sees the other two's ``all_paired`` highlights
     under ``/api/highlights``.
   - **c** has the a→c DM in ``/api/conversations`` and the message body
     round-tripped.
   - **c** also has the v_3 cross-household media DM from **a**:
     after a short scheduler-flush grace period the row's
     ``type=image``, ``media_sync_status`` is cleared, and the
     receiver-side ``media_url`` resolves on Carol's instance —
     i.e. the ``DM_MESSAGE`` preview arrived AND the follow-up
     ``DM_MEDIA_BLOB`` landed the full bytes under Carol's
     ``media_dir``.
   - **b** has the a→b bazaar-inquiry DM in ``/api/conversations``,
     and the message body still quotes the listing id (i.e. the body
     made it through ``DM_RELAY`` decryption intact).
   - **a** and **c** have **b**'s moment in their ``/api/moments``
     inbox (validates ``MOMENT_CREATED`` outbound + the inbox-
     fan-out mirror against ``moment_follows``).
   - **b**'s space membership is queryable (Alice / Carol show as
     pending or joined depending on whether the invitation flow has
     auto-accepted by the time the assertion runs).
   - Every backend process is still alive (no WebRTC native crash).
   - **Home-location propagation** (v5): each of a / b / c sees every
     confirmed peer's ``home_lat`` / ``home_lon`` populated in
     ``/api/friends``. Coordinates are seeded in ``cmd_up`` (Berlin /
     Hamburg / Frankfurt / Munich) and carried during the §11
     pairing handshake via the ``peer-accept`` body. A NULL coord
     on the peer row means the pairing carry-through regressed.
   - **share_home toggle** (per-peer home-sharing control): flips Alpha's
     ``share_home`` for Bob to OFF via
     ``PATCH /api/pairing/connections/{bob_id}`` ``{"share_home": false}``,
     waits ~1 s for the null-coord ``LOCAL_HOME_LOCATION_CHANGED`` envelope
     to land, then asserts Bob's ``remote_instances.home_lat`` / ``home_lon``
     for Alpha's row are NULL in Bob's DB. Flips back ON and asserts the
     coords are restored. Exercises ``PeerHomeSharingService.set_share_home``
     end-to-end.
   - **User preferences round-trip**: PATCHes ``/api/me/preferences``
     with ``{hide_highlights: true}`` as Alice, re-fetches
     ``GET /api/me/preferences``, and asserts the value persisted.
     Then checks Bob's preferences via his own token and asserts
     ``hide_highlights`` is still false — no cross-talk between users.
     Alice's preference is restored to false before the step exits.
   - **Log audit**: every backend's ``log.txt`` is split into
     per-record blocks (one block per non-indented line plus its
     indented traceback frames), then scanned for ``Traceback`` /
     ``ERROR:`` / ``WARNING:`` / ``Exception:`` markers. A block
     stays suppressed only when its full text matches at least one
     substring in ``_LOG_BENIGN`` (the harness's allow-list, which
     is comment-heavy so a future contributor can see why each
     pattern is intentional). New unexpected logs surface in the
     next run as ``<inst>: log audit — …`` and become verify
     failures — either fix the root cause OR add an allow-list
     entry with a comment explaining why it's benign.

     The block splitter explicitly distinguishes Python logging
     records (``LEVEL:logger:msg``) from exception summaries
     (``ValueError: …``) so consecutive ``ERROR:`` /
     ``WARNING:`` lines each become their own block. Without that
     guard the whole log collapses to one block and the audit
     misses real per-line errors — pinned by
     ``tests/skills/test_federation_demo_audit.py`` so a future
     refactor can't silently regress the splitter again.

     What the audit currently accepts as benign (each with an
     in-source comment): STUN-server status chatter, outbox-retry
     on briefly-unreachable peers, outbox terminal drops on HTTP
     410 (dual-transport replay-cache hit), HTTPS-inbox transient
     failures during the inner-ring settle window, DTLS handshake
     timeouts on loopback (perfect-negotiation glare aborts one
     side mid-handshake, federation falls through to HTTPS-inbox),
     ICE candidate drops after the 30 s buffer window, and the
     rate-limit middleware's own announcement when ``relay-pair``
     waits the 65 s window. If any of these patterns START coming
     with a per-step content-check failure, that's the real bug
     — the log line on its own is just the symptom.

5. ``visibility`` — per-pair user-visibility filter. Provisions a
   second local user on **a** (``ada``), confirms **b** mirrors her
   in ``/api/friends``, then hides ada from **b** via
   ``PATCH /api/pairing/connections/{b_id}/visible-users``. Asserts
   **b**'s mirror drops ada. **While ada is hidden** the step also
   fires a DM (ada → bob), a moment (ada), and an ``audience_kind=
   all_paired`` highlight (ada) and asserts **none** of them reach
   **b** — exercising the DM_MESSAGE, MOMENT_CREATED, and HIGHLIGHT_*
   outbound gates. Gamma is the positive control: ada is **not**
   hidden from **c**, so the same highlight is asserted to land on
   Gamma's ``/api/highlights``. Then flips ada back to visible and
   asserts **b** sees her again.

6. ``invite-redeem`` — cross-instance space-invite token redeem
   over federation. Carol creates a private space on **c**, mints
   an invite token, Alice POSTs the token to her own
   ``/api/spaces/join`` with ``issuer_instance_id=<carol's id>``.
   The backend routes the redeem over
   ``SPACE_INVITE_TOKEN_REDEEM``; Carol's instance validates the
   token, seats Alice as a remote space member, ACKs back. The
   harness asserts both legs: Alice's HTTP response carries the
   right ``space_id`` (proves the ACK round-tripped), and Carol's
   ``space_remote_members`` DB row exists (direct check —
   ``/api/spaces/{id}/members`` only surfaces local members).
   PR 1 baseline for the relayed case (a wants to join d's space
   via b) that lands as ``invite-redeem-routed`` in step 7.

7. ``invite-redeem-routed`` (PR 2, v_6 mesh) — c (receiver) joins
   a space hosted on d (issuer) but is **not** directly paired with
   d; the only path is c↔b↔d. The receiver-side coordinator runs
   ``SPACE_FIND_ROUTE`` to discover the path, gets back d's per-
   route X25519 ephemeral pub via ``SPACE_ROUTE_FOUND``, and ships
   the redeem as a ``SPACE_ROUTED`` envelope with the inner
   payload sealed end-to-end (HKDF-derived directional AES-256-GCM
   key, AAD bound to ``route_id`` + ``inner_event_type``). b
   forwards the opaque ciphertext without decrypting it; d unseals,
   seats c as a remote member, and ships the ACK back as
   ``SPACE_ROUTED(direction=reply)``. Asserts: c's
   ``POST /api/spaces/join`` resolved 200/201 (full round-trip
   completed), ``d.space_remote_members`` contains c (inner REDEEM
   dispatched at the target after unseal), AND b's log contains
   ``SPACE_ROUTED`` but **not** ``SPACE_INVITE_TOKEN_REDEEM``
   (encryption invariant — relays never see the inner event_type).

8. ``remote-invite-routed`` (PR 3, v_6 mesh-private-invite) — c
   (admin) invites dave on d through the unpaired path c↔b↔d.
   Backend ``SpaceService.invite_remote_user`` runs route discovery
   and ships ``SPACE_PRIVATE_INVITE`` via ``SPACE_ROUTED(forward)``;
   b forwards the opaque blob without decrypting. d's inbox surfaces
   the invite via ``GET /api/remote_invites``; d accepts;
   ``SpaceService.accept_remote_invite`` runs a *fresh* discovery
   (the original reply-leg ephemerals have expired in the user-time
   gap) and ships ``SPACE_PRIVATE_INVITE_ACCEPT`` as a new
   ``SPACE_ROUTED(forward)`` leg back through b. Asserts: invite
   POST returns 201 (mesh send succeeded), d's inbox surfaces the
   row, accept returns 200/204, ``c.space_remote_members`` shows
   dave, AND b's log contains ``SPACE_ROUTED`` but **not**
   ``SPACE_PRIVATE_INVITE_ACCEPT`` (encryption invariant — relays
   cannot read the admin-initiated flow either).

9. ``remote-invite-decline`` (PR 3, decline-path coverage) — c
   invites alice (direct pair), alice declines via
   ``POST /api/remote_invites/{token}/decline``,
   ``SPACE_PRIVATE_INVITE_DECLINE`` round-trips to c. Asserts the
   ``space_invitations`` row on c moves to ``status='declined'``
   AND alice is NOT seated in ``c.space_remote_members``
   (defensive: a decline must not accidentally seat the user).
   Complements the accept-only path that
   ``cmd_traffic`` + ``cmd_calendar`` exercise today.

10. ``replay`` — outbox redelivery resilience. Kills **c**, has **a**
   post one ``audience_kind=all_paired`` highlight while **c** is
   offline, restarts **c**, waits across the second outbox-backoff
   slot (~35 s), and asserts the queued highlight lands. Validates
   the §24 ``ResilientFederationOutbox`` flush-on-reachable path.

8. The harness exits non-zero if any assertion fails or any process
   crashed during the run.

The canonical ``all`` sequence runs ``up → pair → traffic →
calendar → verify → relay-pair → visibility → invite-redeem →
invite-redeem-routed → remote-invite-routed → space-post-routed →
space-media-blob → space-gallery-media-blob →
space-sync-catchup-media → sync-https-fallback → admin-promote-kick →
app-session → remote-invite-decline → replay`` in that order.
The whole ``gfs-*`` chain (``gfs-up`` / ``gfs-pair`` / ``gfs-traffic``
/ ``gfs-replay`` / ``gfs-space-subscribe`` / ``gfs-space-post`` /
``gfs-space-rotate`` / ``gfs-space-no-subscribers`` / ``gfs-down``)
stays
opt-in — it spins up a
separate GFS process and isn't required to validate the HFS↔HFS
surface.

Phases added after the initial publish are documented inline in
``harness.py`` (each ``cmd_*`` has its own docstring):

* ``space-post-routed`` — mesh-routed SPACE_POST_CREATED via
  SPACE_ROUTED through a relay that never decrypts the inner
  payload.
* ``space-media-blob`` — bytes for a posted image actually reach
  the remote member's media path (the SpaceMediaSyncService
  outbox + chunked SPACE_MEDIA_BLOB stream).
* ``space-gallery-media-blob`` — same as above, exercised through
  the gallery upload path. Thumbnail + full bytes both land on
  the remote member's media path via the shared media outbox.
* ``space-sync-catchup-media`` — newcomer joining a long-running
  space gets the historical post + gallery bytes too, not just
  the metadata rows. Catch-up enqueues happen after the §25.6
  metadata sentinel; assertion proves both surfaces land on the
  joiner's media path.
* ``admin-promote-kick`` — cross-household role promotion lands on
  the affected member's household via SPACE_MEMBER_ROLE_CHANGED, **and
  it is the live proof of the v_28 ``SPACE_ROUTE_STALE`` nack**. c and
  d are not paired, so the role change rides ``SPACE_ROUTED`` via b. The
  step warms c's mesh route to d (a post d must receive), then **kills
  d** — d's ephemeral private halves live only in RAM, so the key c
  just cached is dead — and has c PATCH dave's role *while d is down*:
  cache hit, c seals under the dead key, b accepts the outer envelope
  (c sees ``ok``) and b's durable outbox holds the hop to d. d is
  respawned on the same data dir; b redelivers; d cannot open the
  envelope and answers with a signed ``SPACE_ROUTE_STALE`` that b walks
  back to c; c verifies it against the identity key it pinned at
  discovery, invalidates the route, rediscovers and retransmits the
  identical inner event once; d applies the role. All three are hard
  assertions: d's ``space_members.role == 'admin'`` (polled ≤ 90 s),
  d's log carries ``no cached target_eph_priv … nacked to``, and c's
  log carries ``rediscovered, retransmitted`` naming d's instance id and the `space_member_role_changed` event (not just any retransmit to d) —
  preceded by ``invalidated`` (c's cache still held the dead key) or
  ``already rebuilt`` (d's catch-up ``SPACE_SYNC_BEGIN`` had refreshed
  it first); both are real recoveries and the step prints which fired.
  The nack has to land inside c's 270 s pending-record window
  (``routed_envelope._PENDING_ROUTED_TTL_S``), which outlasts b's whole
  5/10/20/40 s outbox ladder; if c's one-shot rediscovery races d's boot
  and finds no route, c logs ``deferring one retransmit`` and retries
  exactly once past the discovery negative cooldown (the success line
  then carries a ``(deferred attempt)`` suffix), and a second miss logs
  ``still no route … giving up``. A ``broadcast_to_space_members … did
  not reach`` WARNING on c fails the step early — it means the cache was
  not warm and the nack path was never exercised. (The step previously
  relied on the respawn ``sync-https-fallback`` performs, but the
  respawned d BEGINs a mesh catch-up sync and c re-discovers its route
  on admitting it, so
  whether c still held a stale route at PATCH time was a race — which
  is also why the step used to fail intermittently pre-v_28, when a
  stale seal was dropped in silence.) ``sync-https-fallback``'s own
  #648 tripwire counts only silent drops (``…; dropping``), not nacked
  ones.
* ``app-session`` — app-to-app federation (v_17+/v_18): opens an
  ``APP_SESSION`` from a to b via the legacy ``peer_instance_id``
  path, sends an ``APP_MESSAGE``, and asserts both REST calls return
  2xx.  Also exercises the v_18 additions: probes
  ``GET /api/apps/{id}/contacts`` and validates the contact shape;
  when remote contacts on b are present, opens a person-routed
  session via the new ``target`` body and asserts the 201 response.
  Skips gracefully when no common installed app is present in the
  demo environment (unit tests in
  ``tests/services/test_app_federation_service.py`` cover the full
  delivery path with a WS mock).

To iterate faster you can run the steps individually (``python
harness.py up`` / ``pair`` / ``traffic`` / ``calendar`` / ``verify``
/ ``relay-pair`` / ``visibility`` / ``invite-redeem`` /
``invite-redeem-routed`` / ``remote-invite-routed`` /
``space-post-routed`` / ``space-media-blob`` /
``space-gallery-media-blob`` / ``space-sync-catchup-media`` /
``sync-https-fallback`` / ``admin-promote-kick`` / ``app-session`` /
``remote-invite-decline`` / ``replay``);
state is persisted to ``/tmp/sh-demo/state.json`` between calls.

## Owner-offline delegated authority — opt-in (``owner-offline``)

Proves the keystone of the owner-offline-spaces epic across live nodes: a
**delegated admin moderates a space with the owning household stopped**, and
another member converges. HFS-only (no GFS needed). Run after ``up`` + ``pair``:

```bash
python .claude/skills/federation-demo/harness.py owner-offline
```

Topology: **a** = owner, **b** = delegated admin, **c** = plain member. The step:

1. a creates a private space + enables ``delegated_admin_authority`` (owner-only).
2. a §D1b-invites b and c; both accept (stub + content key).
3. a promotes b to admin → ``SPACE_ADMIN_KEY_SHARE`` ships b the space SIGNING SEED.
4. Asserts b now holds the seed (``spaces.identity_private_key`` non-NULL) —
   *admin authority = holding the private key* (§4.2.3).
5. **Stops a** (SIGTERM the process group). The owner is offline.
6. b renames the space — b executes LOCALLY and authority-signs
   (``_executes_locally_as_delegated_admin``), broadcasting ``SPACE_CONFIG_CHANGED``.
7. Asserts **c converges on the rename while a is offline** (c verifies the
   space-authority signature, not ``from_instance``). ← the headline proof.
8. Restarts a; asserts a reconciles to b's offline change (b's outbox redelivers
   the authority-signed config; a applies it by LWW).

This step found two real bugs the unit suite missed (it crosses the HTTP + wire
boundary the unit tests bypass): ``delegated_admin_authority`` was dropped at
every hand-rolled ``features`` wire dict (route parser, federation metadata, stub
builder) so the flag never persisted/federated; and ``_apply_roster_gossip`` never
registered a gossip-learned member in ``space_instances`` so a delegated admin's
``broadcast_to_space_members`` couldn't reach them. Both are fixed (with CI
regression tests). NOTE on step 8: b's offline edit must out-sequence the owner's
last config edit; if ``config_sequence`` collides, the ``(config_sequence, author)``
LWW tie-break decides — the step waits for b to sync first, but a host config bump
that doesn't federate its sequence to members can still force a collision (a
documented convergence follow-up). Steps 1–7 are the deterministic core.

## Owner-offline delegated BAN converges (``owner-offline-ban``)

Sibling of ``owner-offline``, but exercises a **roster** mutation (a ban /
removal) rather than a config edit — the path the **#618** bug lived on. A
delegated admin's offline ban gossips a ``SPACE_MEMBER_LEFT`` tombstone whose
``member_version`` is sourced from the space's dedicated ``roster_sequence``.
Pre-#618 that sequence wasn't anchored above the victim's last-seen version, so
other households (holding the victim at a HIGH ``member_version`` from a real
seat) DROPPED the tombstone as stale via the version-guarded CRDT merge — the
banned member stayed live. HFS-only. Run after ``up`` + ``pair``:

```bash
python .claude/skills/federation-demo/harness.py owner-offline-ban
```

Topology: **a** = owner / host, **b** = delegated admin + seed-holder, **c** =
the member who gets banned. The step:

1. a creates a private space + enables ``delegated_admin_authority``.
2. a §D1b-invites b AND c; both accept (stub + content key).
3. a promotes b to admin → ``SPACE_ADMIN_KEY_SHARE`` ships b the space SIGNING
   SEED; polls until b holds it (``spaces.identity_private_key`` non-NULL).
4. **Settles until a, b AND c all agree c is a LIVE member** at a real
   ``member_version`` in their ``space_remote_members`` roster — the #618
   pre-condition: every household holds c at a high version an un-anchored ban
   gossip would fail to beat. Prints the converged state.
5. **Stops a** (SIGTERM the process group). The owner is offline.
6. b bans c offline-of-owner via
   ``DELETE /api/spaces/{id}/remote-members/{c_inst}/{c_user}``
   (``SpaceService.remove_remote_member``). b holds the seed, so the
   ``SPACE_MEMBER_LEFT`` roster gossip it fans out to every member household
   (incl. the offline host) is space-authority-signed; the per-member
   ``member_version`` is anchored on ``roster_sequence`` (the #618 fix). Asserts
   b's own roster shows c tombstoned immediately (local write).
7. Restarts a; polls until a's ``space_remote_members`` row for c is
   ``tombstoned=1`` — a applied b's authority-signed offline ban on reconnect.
   ← the headline #618 proof. Pre-#618 a would have dropped it as stale and c
   would still be a live member.
8. Secondary (non-fatal): notes whether c's own household saw itself removed
   (``SPACE_REMOTE_MEMBER_REMOVED`` cascaded the local stub away).

Re-runnable (each run mints a fresh space + a ``time.time_ns()`` marker).

NOTE on the environment: the offline-owner only reconciles the tombstone after
restart once b's outbox redelivers the authority-signed ``SPACE_MEMBER_LEFT``
over the HTTP inbox (the realtime RTC channel is gone while a is down). In the
no-TURN loopback sandbox a's event loop can occasionally stall under the
post-restart ICE/STUN reconnection storm (low CPU, growing accept backlog) —
when that happens the HTTP inbox times out and the step's 60 s convergence poll
can miss. Always run ``owner-offline-ban`` against a **fresh** ``up`` + ``pair``
(a clean RTC topology); a leftover wedged backend holding port 18001 from a
prior aborted run is the usual cause of a spurious failure — kill by port and
``rm -rf /tmp/sh-demo`` before retrying. On a clean topology it converges
reliably.

## GFS (Global Federation Server) — opt-in subcommands

The skill also wires up the **GFS** path as a separate, opt-in flow
on top of the canonical HFS-only ``all`` run. Boot a GFS, pair Alpha
+ Delta + Epsilon with it, and tear down — the full HFS↔GFS pair
handshake runs end-to-end against a real GFS process.

The canonical chain, in order:

```bash
python .claude/skills/federation-demo/harness.py up
python .claude/skills/federation-demo/harness.py pair
python .claude/skills/federation-demo/harness.py traffic
python .claude/skills/federation-demo/harness.py gfs-up
python .claude/skills/federation-demo/harness.py gfs-pair
python .claude/skills/federation-demo/harness.py gfs-traffic
python .claude/skills/federation-demo/harness.py gfs-space-subscribe
python .claude/skills/federation-demo/harness.py gfs-space-post
python .claude/skills/federation-demo/harness.py gfs-space-rotate
python .claude/skills/federation-demo/harness.py gfs-space-no-subscribers
python .claude/skills/federation-demo/harness.py gfs-invite-link
python .claude/skills/federation-demo/harness.py gfs-invite-link-content
python .claude/skills/federation-demo/harness.py verify
python .claude/skills/federation-demo/harness.py gfs-down
```

``gfs-replay`` slots in anywhere after ``gfs-traffic`` and is optional
(it restarts Alpha); the content steps only need ``gfs-traffic`` to
have published the space.

The topology is what makes the content steps meaningful:

- ``gfs-pair`` pairs **a**, **d** and **e** with the GFS. a and d are
  **not** QR-paired with each other (d pairs only with b; a↔d exists
  only after the separate ``relay-pair`` step, which is not part of
  this chain), so anything d receives from a's space can only have
  travelled through the GFS. **c** is paired with neither and is the
  negative control.
- **e** is paired with *nobody* — not even by mesh. It is the only
  household for which the §D2b bootstrap redeem and the
  ``GfsRelayTransport`` delivery tier are the ONLY way to reach a's
  space, which is what ``gfs-invite-link`` /
  ``gfs-invite-link-content`` exercise.

### ``gfs-up``

Starts a Global Federation Server on ``127.0.0.1:18765``. Bypasses
the interactive ``socialhome-global-server --init / --set-password``
CLI: writes the example TOML, points ``base_url`` and ``data_dir`` at
the local sandbox, seeds a bcrypt admin-password hash, and spawns
``python -c "from socialhome.global_server.server import main; main()"``
under the hood. Verifies the GFS is reachable by polling ``/healthz``.

The subprocess calls ``logging.basicConfig(level=DEBUG)`` **before**
``main()`` (whose own ``basicConfig(level=INFO)`` is then a no-op), so the
GFS runs at DEBUG. ``gfs-space-post``'s anonymity assertion needs it: the
GFS's relay bookkeeping (``GFS: relaying <event> for space <id> to N
subscriber(s)``) is a DEBUG record, and at INFO there would be no relay
line to scan for a leaked household id.

State is persisted under ``/tmp/sh-demo/gfs/`` (same parent as the
HFS sandboxes); ``gfs-down`` (or the broader ``down``) tears it down.

### ``gfs-pair``

Walks the §24 GFS pairing handshake for Alpha, Delta and Epsilon
against the running GFS (Epsilon's only federation relationship of any
kind — same three calls, because a household that joined a space from
a public link is not a special kind of client):

1. Mint a one-time pair token via the GFS landing page (the QR
   token; rendered as ``data-pair-token`` on the ``<code>`` element
   so it's scrape-friendly).
2. POST it to the SH side's ``/api/gfs/connections``. The SH then
   ``GET {gfs_url}/gfs/info`` to pull the GFS's Ed25519 ``public_key``
   and ``POST /gfs/register`` with the SH's ``{instance_id,
   public_key, inbox_url, token}`` body.
3. Assert all three households now show the GFS connection as
   ``status="active"`` (auto-accept is on by default for fresh
   deployments).

### ``gfs-traffic`` — global-space publish round-trip

Validates the **publish** wire end-to-end: Alpha creates a
``space_type=global`` space, ``SpaceService._auto_publish_on_type``
fires, ``GfsConnectionService.publish_space`` builds a metadata
payload, signs it with Alpha's identity key, and POSTs to
``/gfs/spaces/{space_id}/publish``. The GFS verifies the
signature against the registered ``ClientInstance.public_key``,
upserts a ``GlobalSpace`` row at ``status='active'``, and the
harness then asserts the space appears on ``GET /gfs/spaces``
with the right name + owning instance.

Prerequisites: ``up`` + ``gfs-up`` + ``gfs-pair`` (Alpha must be
a paired client of the GFS so the registration sig verifies).

The step then PATCHes the space to turn ``features.allow_subscribers``
**on**. That flag — not ``join_mode`` — is what makes a public/global
space publicly readable, and it defaults **off**, so a fresh space relays
nothing and seats no subscriber; the downstream readable chain
(``gfs-space-subscribe`` → ``gfs-space-post`` → ``gfs-space-rotate``)
would correctly relay nothing without it. ``POST /api/spaces`` takes no
``features`` block, hence the PATCH — and since a PATCH replaces the whole
features dict, the step reads the current one back first. The create body
still passes ``join_mode="open"``, but that is now purely about who may
JOIN. The not-readable rule gets its own step:
``gfs-space-no-subscribers``.

### ``gfs-replay`` — owning-HFS downtime survives the publish

Validates that a GFS publication is durable across an owning-HFS
restart. Runs after ``gfs-traffic`` (which already published Alpha's
global space). Sequence:

1. Pre-check: ``GET /gfs/spaces`` lists Alpha's space, and Alpha's
   ``/api/gfs/publications`` mirrors the same row.
2. SIGTERM Alpha; wait for the process to exit.
3. While Alpha is offline, the GFS continues to list the space — the
   GFS does not proactively unpublish on owning-instance disconnect.
4. Respawn Alpha on the same data_dir; wait for
   ``/api/instance/config`` to answer 200.
5. Wait ~8 s for the ``GfsWebSocketSupervisor`` to reconcile its
   per-connection clients and the ``GfsWebSocketClient`` to reopen
   ``wss://gfs/gfs/ws``.
6. Re-assert: Alpha's ``/api/gfs/connections`` shows the connection
   active, ``/api/gfs/publications`` still lists the space, and
   ``GET /gfs/spaces`` still lists it.

Failure modes this catches:

- The GFS-side ``GlobalSpace`` row gets garbage-collected on
  owning-HFS disconnect (regression).
- ``GfsWebSocketSupervisor.start()`` is no longer fired from
  ``_on_startup`` (regression — the WS would never reconnect).
- The publication mirror in ``gfs_publications`` gets wiped by a
  migration or a misfired ``unpublish_space_from_all`` on shutdown.

### ``gfs-space-subscribe`` — discovery → mirror → GFS subscriber set

Delta finds Alpha's published global space in the GFS directory and
subscribes to it. Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` +
``gfs-traffic`` (Alpha owns the published ``space_type=global`` space).

0. Waits until the GFS has REGISTERED Delta's SH↔GFS WebSocket
   (``gfs.ws.register: instance=<d>`` in the GFS log). Not cosmetic: the
   ``new_subscriber`` → sealed-key handoff triggered by the subscribe is
   fire-and-forget, and if Delta's socket isn't up the GFS falls back to
   Delta's HTTPS ``/federation/inbox``, which answers 401 for relay frames
   — Delta then stays permanently keyless for that epoch. In production
   the socket has been up for hours; in the harness Delta pairs seconds
   earlier and the supervisor's reconcile loop can take ~30 s.
1. ``POST /api/public_spaces/refresh`` on Delta (admin-only, 202) runs
   ``PublicSpaceDiscoveryService.refresh_now`` inline instead of waiting
   for the scheduled poll.
2. Asserts Alpha's space now appears on Delta's ``GET /api/public_spaces``
   with the published ``name`` and with ``instance_id`` equal to Alpha's
   instance id.
3. ``POST /api/spaces/{id}/subscribe`` on Delta.
4. Asserts Delta now holds a local ``spaces`` row whose
   ``identity_public_key`` is byte-identical to Alpha's own pin for the
   same space (read straight out of both SQLite DBs).
5. Asserts the GFS registered **Delta specifically**: ``subscriber_count``
   on ``GET /gfs/spaces/{id}`` moved, and the GFS's own
   ``space_subscribers`` table names Delta's instance id. (The
   ``GET /gfs/spaces/{id}/subscribers`` endpoint would be the pure-REST
   check but it is space-authority-signature gated by design, so the
   harness reads the GFS DB read-only instead.)

Failure modes this catches:

- The discovery poll hits the wrong GFS URL — nothing ever lands in
  ``public_space_cache`` and step 2 times out.
- The listing's ``owning_instance`` is mapped onto the wrong local
  field — the row appears but points at nobody, so a join can never be
  routed. Step 2's ``instance_id`` assertion is the tripwire.
- ``GfsSpaceMirrorService`` never seats the local stub — subscribe still
  returns 200, but with no pinned space-authority key every relayed
  frame is dropped at ``SpacePublicInbound._verify_authority``, at
  WARNING, invisibly. Step 4 is the tripwire.
- The SH side never calls ``POST /gfs/spaces/{id}/subscribe`` — the GFS
  relay fan-out simply never targets this household. Step 5.

### ``gfs-space-post`` — public space content actually crosses the GFS

Prereqs: ``gfs-space-subscribe``. Alpha posts in the global space via
``POST /api/spaces/{id}/posts`` (the space endpoint — the household feed
endpoint ignores ``space_id`` and lands a non-federating household post),
then the harness polls Delta's ``GET /api/spaces/{id}/feed``.

Alpha posts **twice**, deliberately covering both author-id shapes — the
relay fail-closes on a non-derivable author id (``verify_signed_author_inner``
runs the self-cert ``derive_user_id(author_pk, anchor_or_username) ==
author_user_id`` on both the relaying seed-holder and the subscriber), and
the two shapes are minted by different rules:

* a **provisioned** local user (``erin``, seated by ``_seat_local_member``),
  whose id is uuid4-anchored via ``UserService.provision``;
* Alpha's **setup admin**, whose id is username-anchored via
  ``identity_bootstrap.derive_local_user_id`` — the one minting rule now
  shared by ``StandaloneAdapter.provision_admin`` and the ``/api/setup``
  routes. Delta's copy must be attributed to Alpha's own admin ``user_id``,
  so a regression back to the old synthetic ``uid-<username>`` shape (which
  failed the self-cert and silently dropped every first-user post at the
  subscriber; migration 0049 repairs already-deployed installs) fails loudly
  here. ``verify`` re-checks this post separately.

The positive assertion — Delta sees each post **decrypted**, with the right
``author`` user id and the exact body — covers six mechanisms at
once: the GFS relay fan-out, the space-authority signature, the
``new_subscriber`` notify fired when Delta subscribed, the sealed
content-key handoff that notify triggers, the per-author household
signature (``verify_signed_author_inner``), and the AES-GCM decrypt
under the current epoch key.

The negative control is **c**: not a space member, not a subscriber, not
GFS-paired. The harness asserts c holds no ``space_posts`` row for either
post and that c's space feed does not expose them — the
§"non-member households MUST NOT see space content" hard rule, checked
on the real wire rather than in a unit test.

Failure modes this catches: a relay that drops the frame; an authority
signature that no longer verifies against the mirrored pin; a
``new_subscriber`` notify that never fires (Delta holds no content key
and drops everything with "no key for epoch"); a broken author
self-cert; and — on the c side — any regression that fans space content
at non-member households.

The step also asserts the relay is **identity-free** (the "the GFS must not
see who relayed" property): the GFS log names Alpha's instance id on no
relay line (scoped to relay lines — the GFS legitimately knows that id from
``/gfs/register``, the ``/gfs/ws`` hello and the public directory listing's
``owning_instance``) and never mentions ``from_instance`` at all; Delta's
``gfs.relay.received: space=… event=space_post_public`` records carry no
``from=``; and Alpha never logged the legacy downgrade WARNING ("does not
advertise anonymous_publish"). ``verify`` re-checks Delta's log-line shape.

### ``gfs-space-rotate`` — a key rotation must not cut subscribers off

Prereqs: ``gfs-space-post``. Removing a space member rotates the
per-space AES-256 content key (forward secrecy — the removed member must
not read future posts). Members are re-keyed through the
``space_instances`` fan-out, but GFS subscribers hold a read-only
subscription and are **never** in ``space_instances``: they need the
separate ``SpaceSubscriberKeyOutbound`` re-seal.

1. Seats a SECOND provisioned member on Alpha (``frank``) purely as the
   one to remove — the author from ``gfs-space-post`` (``erin``) has to
   survive the rotation to write the post in step 4. Re-run tolerant (an
   existing user / existing membership is reused).
2. ``DELETE /api/spaces/{id}/members/{user_id}`` on frank — which runs
   ``_rotate_and_distribute_space_key``.
3. Settles ~10 s for the new epoch key to be re-sealed to every GFS
   subscriber.
4. Erin posts again, now under the new epoch.
5. Asserts Delta can still read the new post, with the right author.

Failure mode this catches: the rotation re-keys ``space_instances``
members only, so a GFS subscriber silently goes dark after the first
rotation — everything it receives fails to decrypt until its next GFS-WS
reconnect happens to re-trigger a handoff. Before the fix the first post
still read fine and only the post-rotation one vanished, which is
exactly what step 5 pins down.

``verify`` re-asserts all three at the end of a run (gated on the
``gfs_space_id`` state key, so it self-skips when the chain wasn't run):
Delta still sees both posts, c still sees neither, and Delta's mirrored
``identity_public_key`` still matches Alpha's.

### ``gfs-space-no-subscribers`` — listed for discovery, never relayed

Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` (independent of the readable
chain above). A space carries three independent dials: its ``space_type``
(private / household / public / global), its ``join_mode``
(``invite_only`` / ``open`` / ``request``) saying how a person becomes a
posting **member**, and ``features.allow_subscribers`` saying whether
**strangers** may follow it read-only. The product rule: **a public/global
space with ``allow_subscribers`` off is listed in the GFS directory but is
not publicly readable** — its content is never relayed to the GFS and its
per-space content key is never sealed to a subscriber, so a stranger
cannot read a group nobody let them into. Members (including mesh-only
remote members) are unaffected — they are fanned out over
``space_instances``.

The space in this step is deliberately ``join_mode=open``: anyone may
**join** it, and that still buys them nothing to **read**. Under the old
(wrong) model where ``open`` implied readable, this space would have
relayed — so the step doubles as a guard against regressing to it.

1. Alpha creates a second ``space_type=global`` space and leaves
   ``allow_subscribers`` at its default (off).
2. Asserts the GFS directory LISTS it (``GET /gfs/spaces``) and reports
   ``allow_subscribers: false`` alongside ``join_mode: "open"``. Listing
   is how people discover the space and ask for an invite — a "fix" that
   suppressed the metadata publish instead of the content fails here.
3. Asserts Delta discovers it too (``POST /api/public_spaces/refresh``
   then ``GET /api/public_spaces``), flag included, so Delta's browser
   can suppress the Subscribe button.
4. Delta asks to subscribe — and must be **refused**. This is a hard
   assertion now: the mirrored stub carries the owner's truthful flag, so
   Delta refuses locally, and the GFS 403s ``POST /gfs/subscribe``
   independently.
5. Alpha posts into the space; the step settles ~10 s (the readable
   equivalent in ``gfs-space-post`` lands well inside that window).
6. Asserts Delta logged **no** ``gfs.relay.received: space=<id>`` record
   and that neither Delta nor c holds the post by row or in the feed.

The gates live on the host side: ``space_public_outbound`` (both the
local-author and the owner-offline remote-author relay),
``space_subscriber_key_outbound`` (the ``new_subscriber`` handoff and
every reconcile entry point) and ``space_post_outbound`` (the pre-signed
``public_relay`` hint on the member broadcast) — plus
``SpaceService.subscribe_to_space`` and the GFS's own subscribe refusal.
``verify`` re-asserts the directory listing (with both dials), and the two
no-content checks, gated on the ``gfs_no_subscribers_space_id`` state key.

### ``gfs-invite-link`` — a stranger joins from a published link (§D2b)

Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` + ``gfs-traffic``.

The live proof of the §D2b **bootstrap** redeem — the path taken when
the redeeming household has no relationship with the issuer at all.
**e** is the only household that can prove it: a / b / c / d are all
mesh-reachable from one another, so a redeem between any two of them
takes the direct or ``SPACE_ROUTED`` branch and the bootstrap code
never executes outside its unit tests.

1. **Mint.** a mints a ``member`` link with
   ``publish_to_gfs`` — the blob is parked on the connection server's
   bulletin board and the response carries the shareable ``gfs.url``.
2. **The public page.** The harness fetches that URL with a plain HTTP
   client, exactly as a visitor's browser would: 200, the space's
   name, the ``socialhome://invite#…`` code — and **not** a's
   ``inbox_url``. The page is public, so a household address on it
   would publish a network location to strangers.
3. **The blob.** Decoded (base64url JSON) and asserted to hold public
   keys and ids ONLY — no ``inbox``, no household port, no
   ``external_url``. The only URL in it is ``via_gfs.gfs_url``, the
   relay the redeemer hands its sealed request to.
4. **The redeem.** e posts the decoded fields to its own
   ``POST /api/spaces/join`` — the exact body
   ``client/src/features/spaces/SpaceJoinByCodeDialog.tsx`` sends, so
   the step walks the path a person actually walks.
5. **The seat**, asserted on BOTH sides' ``remote_instances`` rows:
   ``source='space_session'``, an EMPTY ``remote_inbox_url`` (neither
   household learns the other's address, by design), CONFIRMED, and a
   pinned ``remote_keywrap_pk`` (migration 0050 — without it the pair
   is seated but mute). Plus e's ``space_members`` row at role
   ``member``, the seat the ISSUER's token row decided, and the local
   stub the ACK's ``space_meta`` seeded.
6. **The relay stayed a relay** (#677): the GFS log (DEBUG, see
   ``gfs-up``) shows envelopes moving, and **no** line names both
   households, the invite token, or the space name. The routing
   envelope is ``{to_instance, sealed}`` — one id and a ciphertext.
7. **Both households logged it.** a: ``invite bootstrap: seated <e> in
   space <id> as member — the redeem arrived over the connection-server
   relay``. e: ``invite bootstrap: <a> acked our redeem …``. Those two
   INFO lines were added with this step: every FAILING outcome on this
   path already logged, so success was the one thing that left an
   operator nothing to read.
8. **Revoke.** a ``DELETE``s the link; the ``/join`` page becomes the
   styled 404 ("This invite has expired or was revoked") which must
   **not** echo the dead token back, and a fresh redeem of the same
   code is refused (422). e, who already walked through the door,
   keeps its seat — revoke is not eviction.
9. **An ``admin`` link**, minted by the owner on a SECOND space and
   redeemed by e into an ``admin`` seat — with **no** space signing
   seed (``spaces.identity_private_key`` stays NULL and a ships no
   ``SPACE_ADMIN_KEY_SHARE``). A connection server pins a space's
   identity key TOFU-immutably, so that credential could never be
   taken back from a household whose only introduction was a public
   string.

Failure modes this catches: a blob that grows an address field; a
``/join`` page that stops handing over a code; a relay that starts
logging the pair; a redeem that seats a full social peer instead of a
space-scoped one (which would then join every DM / presence / moment
fan-out); a revoked link that still resolves; and a link-joined admin
handed the space authority seed.

### ``gfs-invite-link-content`` — the relay carries ordinary space traffic

Prereqs: ``gfs-invite-link``.

Seating the pair is half the job. e and a hold matching directional
session keys but **no address for each other** and no mesh path, so
every §24.11 envelope between them is sealed a second time to the
peer's key-wrap key and handed to the same ``POST /gfs/envelope``
relay the redeem used (``GfsRelayTransport``, selected in
``federation/transport.py`` for ``source = space_session``).

1. a posts in the space; e sees it **decrypted** in its feed and
   holds the ``space_posts`` row. One assertion over the seal, the
   relay, the §24.11 pipeline under the wider relay timestamp window,
   the pair key and the content key e got in the ACK's ``space_meta``.
2. The GFS carried envelopes addressed to e while that happened — the
   honest form of "it really was relayed": a count and a recipient,
   because the blob itself is opaque.
3. e posts; a receives it. The reply leg is the half that breaks if
   only the host knows how to reach the other side.
4. a removes a member, rotating the per-space content key, and posts
   again — e must decrypt the NEW epoch. A re-key that skips
   link-joined members leaves them dark at the first roster change.
5. ``GET /api/connections`` on e labels a ``source=space_session``
   with transport ``gfs_relay``. ``https`` in particular would be a
   lie: there is no inbox URL to fall back to.

``verify`` re-asserts the durable half of both steps (gated on the
``gfs_invite_space_id`` state key, so it self-skips when the chain was
not run): the addressless ``space_session`` seat on both sides, the
relayed posts still present on e, the revoked ``/join`` page still a
token-free 404, and e's link-admin seat still without a signing seed.
``verify``'s capability block additionally asserts **v_29** round-trips
on the one wire that cannot fall back to a peer row — see below.

### Bazaar / public moment over GFS (TODO)

Still to wire end-to-end:

- ``POST /api/bazaar/listings`` for the Bazaar test path.
- ``POST /api/moments`` with ``is_public=true`` for the public-
  moment / GFS-following test path.

## Troubleshooting

- **PeerConnection segfaults on the very first call** — the
  ``aiolibdatachannel`` wheel installed in the active Python is the
  pre-OpenSSL-3 release. Either upgrade to ``aiolibdatachannel >=
  2026.5.9`` or rebuild from source against the system OpenSSL 3:
  ``pip install -e /workspaces/social-home/repos/aiolibdatachannel --no-build-isolation``.
- **``setup_required: false`` on a fresh DB** — a previous instance is
  still running. Run ``python harness.py down`` first, then ``up``.
- **``federation inbox: rejected reason=Failed to decrypt payload``** —
  one of the three ``[standalone].external_url`` values doesn't match
  what peers can actually reach (the harness uses ``127.0.0.1`` so this
  shouldn't happen unless ports are stomped).
- **DM a→c isn't visible on c after ~15 s** — check ``/tmp/sh-demo/c/log.txt``
  for ``federation inbox: rejected`` lines. The DM message rides
  federation envelopes; a stale outbox entry can stall delivery — the
  harness waits 5 s before asserting which is enough on a quiet host.

## Cleanup

```bash
python .claude/skills/federation-demo/harness.py down
```

Sends ``SIGTERM`` (then ``SIGKILL`` after 1 s) to each process group
and removes ``/tmp/sh-demo``.

## Files

- [`harness.py`](harness.py) — the driver script. Contains all the
  ``up``/``pair``/``traffic``/``verify``/``down`` subcommands plus the
  ``all`` orchestrator. Each subcommand persists / loads state from
  ``/tmp/sh-demo/state.json`` so you can re-run individual steps.
