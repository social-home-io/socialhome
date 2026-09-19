"""Release-blocker protocol tests for mesh-routed space content (SPACE_ROUTED).

Marked ``@pytest.mark.security`` — CLAUDE.md requires these to run
before every commit touching federation code.

This file closes a real gap: ``tests/protocol/`` had **no** SPACE_ROUTED
coverage at all, which is why #648 (a mesh-joined member silently
receiving no post metadata) shipped. The unit suites exercise the happy
path; nothing pinned the invariants below.

Coverage:
* **A relay can never unseal.** The end-to-end seal is bound to the
  target's ephemeral X25519 pub, so a household sitting between host and
  member holds ciphertext only — the encryption-first hard rule.
* **An unknown / dead target ephemeral drops, silently and safely.** The
  requester-restart case behind #648: the private half died with the
  process, so the pub is unknown. It must drop, must not raise, and must
  not be rescued by extending some other key's life.
* **Using an ephemeral does not extend its life.** The documented
  forward-secrecy bound is mint + TTL. Refreshing on use would have been
  a one-line "fix" for #648 and is deliberately rejected.
* **The origin's route-cache window closes before the target's key
  does.** If it can outlive the key, the origin keeps sealing under a
  private half the target already dropped — #648's mechanism.
* **``SPACE_ROUTE_STALE`` relays are opaque; the origin verifies.** A
  forged nack carrying the target's identity pk passes relay structural
  checks and is forwarded, and ``verify_route_stale`` rejects it — the
  enforcement point is the origin. A nack whose identity pk doesn't
  derive to ``path[-1]`` is shed at the relay.
* **The origin rejects a forged / tampered / unknown-suite nack without
  touching its route cache.** A nack the target did not sign must not
  invalidate a working route (a relay-driven denial of service), a nack
  whose ``stale_eph_pk`` was swapped fails verification, and an unknown
  ``sig_suite`` is a hard reject — never a fallback to a default algorithm.
* **A relay cannot use the target as a signing oracle.** Substituting the
  envelope's ``target_eph_pk`` makes the target genuinely sign a nack over
  a key the origin never sealed under. The origin binds ``stale_eph_pk`` to
  the key IT sealed under for that ``route_id`` — before the signature
  check — so the target-signed nack is inert and the live route stays.
* **A non-member cannot dispatch space content as a member (#692, v_31).**
  ``path[0]`` is relay-supplied and becomes the inner event's
  ``from_instance``, the field every §24.11 post-decrypt gate judges. The
  household at ``path[0]`` signs the routing claim + the sealed material
  with its Ed25519 identity key and the endpoint verifies it before the
  dispatcher — against the key it already pinned, or against the shipped
  pub bound by ``derive_instance_id`` when there is no row. The signature
  covers only bytes the relay already sees, so it hands no confirmation
  oracle to a relay guessing at a low-entropy payload.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation import routed_crypto
from socialhome.federation.route_discovery import (
    ROUTE_CACHE_SAFETY_MARGIN_S,
    ROUTE_CACHE_TTL_S,
)
from socialhome.federation.routed_envelope import SpaceRoutedHandler

pytestmark = pytest.mark.security


ROUTE_ID = "route-0123456789abcdef"
INNER_EVENT = "space_post_created"


def _seal(payload: dict, *, target_pub: str) -> dict:
    """Seal ``payload`` as the origin would, for ``target_pub``."""
    origin_priv, origin_pub = routed_crypto.generate_ephemeral_keypair()
    return routed_crypto.seal_inner_payload(
        inner_payload_json=orjson.dumps(payload).decode(),
        origin_eph_priv_b64=origin_priv,
        origin_eph_pub_b64=origin_pub,
        target_eph_pub_b64=target_pub,
        route_id=ROUTE_ID,
        inner_event_type=INNER_EVENT,
    )


def test_relay_cannot_unseal_routed_space_content():
    """A non-member relay holds ciphertext only.

    CLAUDE.md hard rule: a household that isn't a space member but sits
    on the mesh between host and remote member is a routing relay and
    must not read any space content. The relay has its own X25519
    keypair; it must not decrypt with it, and the plaintext must not be
    recoverable from the sealed blob.
    """
    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    relay_priv, _relay_pub = routed_crypto.generate_ephemeral_keypair()

    secret = "sekrit-dinner-plans-2026"
    sealed = _seal({"body": secret, "post_id": "p1"}, target_pub=target_pub)

    # The relay's own key must not open it.
    with pytest.raises(Exception):
        routed_crypto.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=relay_priv,
            route_id=ROUTE_ID,
            inner_event_type=INNER_EVENT,
        )

    # And the plaintext must not be sitting in the blob in any form.
    blob = repr(sealed)
    assert secret not in blob
    assert "dinner" not in blob

    # The intended target still opens it — the test is about the relay,
    # not about a broken seal.
    opened = routed_crypto.unseal_inner_payload(
        sealed=sealed,
        target_eph_priv_b64=target_priv,
        route_id=ROUTE_ID,
        inner_event_type=INNER_EVENT,
    )
    assert secret in opened


def test_seal_is_bound_to_route_id_and_inner_event_type():
    """AAD binding — a relay can't replay a sealed payload onto another
    route or relabel the inner event type it claims to carry."""
    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    sealed = _seal({"post_id": "p1"}, target_pub=target_pub)

    with pytest.raises(Exception):
        routed_crypto.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=target_priv,
            route_id="route-a-different-one",
            inner_event_type=INNER_EVENT,
        )
    with pytest.raises(Exception):
        routed_crypto.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=target_priv,
            route_id=ROUTE_ID,
            inner_event_type="space_member_left",
        )


def test_wrong_ephemeral_private_half_cannot_unseal():
    """The requester-restart case (#648).

    After a restart the target holds none of the ephemeral privates it
    minted before, so the pub the origin sealed under is unknown to it.
    A *different* private half must not open the blob — the recovery is
    re-discovery (which rotates the key), never a fallback that tries
    other keys.
    """
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    fresh_priv, _fresh_pub = routed_crypto.generate_ephemeral_keypair()

    sealed = _seal({"post_id": "p1"}, target_pub=dead_pub)

    with pytest.raises(Exception):
        routed_crypto.unseal_inner_payload(
            sealed=sealed,
            target_eph_priv_b64=fresh_priv,
            route_id=ROUTE_ID,
            inner_event_type=INNER_EVENT,
        )


def test_origin_route_cache_window_closes_before_target_key_expires():
    """The #648 ordering invariant, pinned as a release blocker.

    The origin seals under the ``target_eph_pk`` its route cache holds;
    the matching private half lives in the target's memory on its own
    timer. If the origin's window can outlive the target's, the origin
    keeps sealing under a dead key and the target discards every
    envelope in silence — no NACK exists, and the send already reported
    success.
    """
    assert ROUTE_CACHE_TTL_S < routed_crypto.DEFAULT_TARGET_EPH_TTL_S
    assert ROUTE_CACHE_SAFETY_MARGIN_S > 0
    assert (
        routed_crypto.DEFAULT_TARGET_EPH_TTL_S - ROUTE_CACHE_TTL_S
        == ROUTE_CACHE_SAFETY_MARGIN_S
    )


def test_target_ephemeral_ttl_is_not_extended_by_use():
    """Forward secrecy is bounded at mint + TTL, not last-use + TTL.

    Sliding the window forward on each successful unseal would have made
    #648's long-stream symptom disappear in one line, and is rejected on
    purpose: it converts "no forward secrecy beyond the discovery
    window" (``docs/crypto.md``) into "none while traffic flows". Forcing
    re-discovery rotates the ephemeral instead, which is the
    FS-*positive* direction.
    """
    from types import SimpleNamespace

    from socialhome.federation.route_discovery import RouteDiscoveryService

    svc = RouteDiscoveryService(
        federation_service=SimpleNamespace(own_instance_id="self"),  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
    )
    pub = svc._generate_target_eph(time.monotonic())
    _priv, expiry_at_mint = svc._target_eph_state[pub]

    for _ in range(5):
        assert svc.lookup_target_eph_priv(pub) is not None

    _priv2, expiry_after_use = svc._target_eph_state[pub]
    assert expiry_after_use == expiry_at_mint, (
        "repeated use extended the ephemeral's life — forward-secrecy bound lost"
    )


# ── SPACE_ROUTE_STALE (route-stale nack) ─────────────────────────────


class _RecordingFed:
    """Minimal stand-in for ``FederationService`` as a relay sees it."""

    def __init__(self, own_instance_id: str) -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[dict] = []
        self.identity = generate_identity_keypair()
        #: ``instance_id -> Ed25519 identity pk`` this node has pinned,
        #: read by the v_31 routed origin-authentication check.
        self.identity_pks: dict[str, bytes] = {}

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    @property
    def own_identity_seed(self) -> bytes:
        return self.identity.private_key

    @property
    def own_identity_pk(self) -> bytes:
        return self.identity.public_key

    async def peer_identity_public_key(self, instance_id: str) -> bytes | None:
        return self.identity_pks.get(instance_id)

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        return True

    async def send_event(self, *, to_instance_id, event_type, payload, **_kw):
        self.sent.append(
            {"to": to_instance_id, "event_type": event_type, "payload": payload}
        )
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


def _relay_between(origin_id: str, target_id: str):
    """A relay handler sitting at index 1 of ``[origin, relay, target]``."""
    fed = _RecordingFed("relay-instance")

    async def _noop(_ev) -> None:
        pass

    handler = SpaceRoutedHandler(
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
        event_dispatcher=_noop,
        target_eph_lookup=lambda _p: None,
    )
    return fed, handler, [origin_id, fed.own_instance_id, target_id]


def _nack_event(payload: dict, *, from_instance: str, to_instance: str):
    return FederationEvent(
        msg_id="m-nack",
        event_type=FederationEventType.SPACE_ROUTE_STALE,
        from_instance=from_instance,
        to_instance=to_instance,
        timestamp="2026-05-22T00:00:00Z",
        payload=payload,
    )


async def test_forged_route_stale_passes_relay_but_fails_origin_verify():
    """Relays are opaque forwarders — the signature is the ORIGIN's check.

    A nack signed by a non-target key but carrying the *target's* identity
    pk passes every relay structural check (it derives to ``path[-1]``)
    and is forwarded verbatim. ``verify_route_stale`` over the very same
    payload is False — pinning that the enforcement point is the origin
    (T3), which holds nothing more than the relay does but is the only
    party with something to lose (its route cache). A relay that verified
    would add no security (the same bytes reach the origin either way) and
    would let an on-path peer decide for the origin which nacks it sees.
    """
    target = generate_identity_keypair()
    attacker = generate_identity_keypair()
    origin_id = derive_instance_id(generate_identity_keypair().public_key)
    target_id = derive_instance_id(target.public_key)
    fed, relay, path = _relay_between(origin_id, target_id)
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    forged = {
        "route_id": ROUTE_ID,
        "path": path,
        "position": 2,
        "target_identity_pk": target.public_key.hex(),
        "stale_eph_pk": dead_pub,
        "sig": routed_crypto.sign_route_stale(
            seed=attacker.private_key, route_id=ROUTE_ID, stale_eph_pk_b64=dead_pub
        ),
        "sig_suite": routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
    }
    await relay._on_route_stale(
        _nack_event(forged, from_instance=target_id, to_instance=fed.own_instance_id)
    )
    # Relay forwarded it, opaque and verbatim (only position moved).
    assert len(fed.sent) == 1
    assert fed.sent[0]["to"] == origin_id
    assert fed.sent[0]["event_type"] is FederationEventType.SPACE_ROUTE_STALE
    assert fed.sent[0]["payload"] == {**forged, "position": 1}
    # ...and the origin's verification rejects it.
    assert not routed_crypto.verify_route_stale(
        identity_pk=bytes.fromhex(forged["target_identity_pk"]),
        route_id=forged["route_id"],
        stale_eph_pk_b64=forged["stale_eph_pk"],
        sig_b64=forged["sig"],
        sig_suite=forged["sig_suite"],
    )
    # Sanity: a genuine nack over the same fields verifies, so the False
    # above is about the forged key, not a broken verifier.
    genuine_sig = routed_crypto.sign_route_stale(
        seed=target.private_key, route_id=ROUTE_ID, stale_eph_pk_b64=dead_pub
    )
    assert routed_crypto.verify_route_stale(
        identity_pk=target.public_key,
        route_id=ROUTE_ID,
        stale_eph_pk_b64=dead_pub,
        sig_b64=genuine_sig,
        sig_suite=routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
    )


async def test_route_stale_with_foreign_identity_pk_is_dropped_at_relay():
    """A nack whose ``target_identity_pk`` doesn't derive to ``path[-1]``
    cannot be a nack for this route — the relay sheds it (no key material
    needed) instead of spending a hop on it."""
    target = generate_identity_keypair()
    impostor = generate_identity_keypair()
    origin_id = derive_instance_id(generate_identity_keypair().public_key)
    target_id = derive_instance_id(target.public_key)
    fed, relay, path = _relay_between(origin_id, target_id)
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    nack = {
        "route_id": ROUTE_ID,
        "path": path,
        "position": 2,
        # Self-consistent (signed by the impostor's own key) but the pk
        # doesn't derive to path[-1].
        "target_identity_pk": impostor.public_key.hex(),
        "stale_eph_pk": dead_pub,
        "sig": routed_crypto.sign_route_stale(
            seed=impostor.private_key, route_id=ROUTE_ID, stale_eph_pk_b64=dead_pub
        ),
        "sig_suite": routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
    }
    await relay._on_route_stale(
        _nack_event(nack, from_instance=target_id, to_instance=fed.own_instance_id)
    )
    assert fed.sent == []


# ── SPACE_ROUTE_STALE at the origin: enforcement point ───────────────


class _RecordingRouteService:
    def __init__(self) -> None:
        self.invalidated: list[str] = []
        self.discover_calls: list[str] = []

    async def invalidate_if_eph(
        self, target_instance_id: str, *, target_eph_pk: str
    ) -> bool:
        self.invalidated.append(target_instance_id)
        return True

    async def discover_route(self, target_instance_id: str):
        self.discover_calls.append(target_instance_id)
        return None

    def cached_target_identity_pk(self, target_instance_id: str) -> str | None:
        return None

    def cooldown_remaining(self, target_instance_id: str) -> float:
        return 0.0


def _cancel_deferred(handler: SpaceRoutedHandler) -> None:
    """Tear down any deferred-retransmit task the test provoked so it
    doesn't outlive the test loop."""
    for task in list(handler._deferred_retransmits.values()):
        task.cancel()


async def _origin_with_pending_send():
    """An origin that has just shipped one routed envelope to ``target``
    (pinning the target's identity pk) and holds a recording route service.
    Returns ``(handler, route_service, target_keypair, path, route_id,
    dead_pub)``."""
    target = generate_identity_keypair()
    origin_id = derive_instance_id(generate_identity_keypair().public_key)
    target_id = derive_instance_id(target.public_key)
    fed = _RecordingFed(origin_id)

    async def _noop(_ev) -> None:
        pass

    handler = SpaceRoutedHandler(
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
        event_dispatcher=_noop,
        target_eph_lookup=lambda _p: None,
    )
    rs = _RecordingRouteService()
    handler.attach_route_service(rs)  # type: ignore[arg-type]
    path = [origin_id, "relay-instance", target_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = await handler.send_routed(
        path=path,
        target_eph_pk_b64=dead_pub,
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload={"post_id": "p1"},
        target_identity_pk=target.public_key.hex(),
    )
    return handler, rs, target, path, route_id, dead_pub


def _nack_payload(
    *, route_id: str, path: list[str], target_pk_hex: str, stale_pub: str, sig: str
) -> dict:
    return {
        "route_id": route_id,
        "path": list(path),
        "position": 1,
        "target_identity_pk": target_pk_hex,
        "stale_eph_pk": stale_pub,
        "sig": sig,
        "sig_suite": routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
    }


async def test_forged_route_stale_at_origin_does_not_invalidate_the_route():
    """A relay (or anyone holding the route_id) forging a nack under its
    own key must not be able to evict a working route from the origin's
    cache — that would turn the nack into a relay-driven DoS on every
    route through it. The pending entry survives so the genuine nack, if
    one follows, still lands."""
    handler, rs, target, path, route_id, dead_pub = await _origin_with_pending_send()
    attacker = generate_identity_keypair()
    forged = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=attacker.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    await handler._on_route_stale(
        _nack_event(forged, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == [], "forged nack evicted the route"
    assert rs.discover_calls == []
    assert route_id in handler._pending_routed
    # Genuine nack over the same route: accepted, route invalidated.
    handler._seen_nacks.clear()  # hop-level dedup already saw route_id
    genuine = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    try:
        await handler._on_route_stale(
            _nack_event(genuine, from_instance=path[1], to_instance=path[0])
        )
        assert rs.invalidated == [path[-1]]
        assert route_id not in handler._pending_routed
    finally:
        _cancel_deferred(handler)


async def test_forged_route_stale_never_creates_a_deferred_retransmit():
    """The deferred re-attempt (GAP 1) sits strictly AFTER signature
    verification and the pending pop: a relay forging a nack must not be
    able to make the origin schedule a retransmit (a delayed flood + a
    re-send it can trigger at will). Nothing is scheduled, nothing probed."""
    handler, rs, target, path, route_id, dead_pub = await _origin_with_pending_send()
    attacker = generate_identity_keypair()
    forged = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=attacker.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    await handler._on_route_stale(
        _nack_event(forged, from_instance=path[1], to_instance=path[0])
    )
    assert handler._deferred_retransmits == {}
    assert rs.discover_calls == []
    assert route_id in handler._pending_routed


async def test_replayed_genuine_route_stale_after_pop_does_not_defer_again():
    """One nack, one deferred re-attempt. Replaying the very same genuine
    nack after the pending record was consumed (even past the hop-level
    dedup) must not schedule a second deferral or probe again — the
    pending pop is the authority, not the deferral table."""
    handler, rs, target, path, route_id, dead_pub = await _origin_with_pending_send()
    genuine = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    await handler._on_route_stale(
        _nack_event(genuine, from_instance=path[1], to_instance=path[0])
    )
    try:
        assert rs.discover_calls == [path[-1]]
        assert list(handler._deferred_retransmits) == [route_id]
        first = handler._deferred_retransmits[route_id]
        assert route_id not in handler._pending_routed
        handler._seen_nacks.clear()  # bypass hop-level dedup on purpose
        await handler._on_route_stale(
            _nack_event(genuine, from_instance=path[1], to_instance=path[0])
        )
        assert rs.discover_calls == [path[-1]], "replay must not probe again"
        assert handler._deferred_retransmits == {route_id: first}
        assert not first.done()
    finally:
        _cancel_deferred(handler)


async def test_route_stale_naming_an_eph_never_sealed_under_is_rejected_before_signature_check():
    """A nack that names an eph pk the origin never sealed this route under
    is dropped at the eph-binding step, *before* ``verify_route_stale``
    runs — so the handler-level assertions below prove the binding check,
    not the signature. That the signature itself covers ``stale_eph_pk``
    (swapping the pk under a genuine signature must not verify, or a relay
    could re-point a real nack at a different key) is asserted directly
    against ``verify_route_stale``."""
    handler, rs, target, path, route_id, dead_pub = await _origin_with_pending_send()
    _other_priv, other_pub = routed_crypto.generate_ephemeral_keypair()
    tampered = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=other_pub,  # swapped
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    assert not routed_crypto.verify_route_stale(
        identity_pk=target.public_key,
        route_id=route_id,
        stale_eph_pk_b64=other_pub,
        sig_b64=tampered["sig"],
        sig_suite=tampered["sig_suite"],
    )
    await handler._on_route_stale(
        _nack_event(tampered, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == []
    assert route_id in handler._pending_routed


async def test_route_stale_unknown_sig_suite_is_rejected_with_no_fallback():
    """An unknown ``sig_suite`` raises inside ``verify_route_stale`` (no
    default-algorithm fallback) and the origin drops the nack without
    touching the route cache — even though the signature itself is the
    genuine Ed25519 one."""
    handler, rs, target, path, route_id, dead_pub = await _origin_with_pending_send()
    nack = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    nack["sig_suite"] = "ed25519+mldsa65"
    with pytest.raises(routed_crypto.UnsupportedRouteStaleSuite):
        routed_crypto.verify_route_stale(
            identity_pk=target.public_key,
            route_id=route_id,
            stale_eph_pk_b64=dead_pub,
            sig_b64=nack["sig"],
            sig_suite=nack["sig_suite"],
        )
    await handler._on_route_stale(
        _nack_event(nack, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == []
    assert route_id in handler._pending_routed


async def test_relay_substituted_eph_cannot_tear_down_a_live_route():
    """Signing-oracle closure. Relay M forwards ``SPACE_ROUTED`` with
    ``sealed.target_eph_pk`` replaced by garbage P'. Target T holds no
    private half for P' and — correctly, from its point of view — signs a
    nack over ``(route_id, P')``. That signature is genuine, the identity
    pk is the pinned one, the route_id is live: everything the origin
    checked before this fix passes. The origin must still drop it, because
    P' is not the key it sealed under for this route_id — otherwise any
    on-path relay could evict any route through it at will and trigger a
    flood + retransmit per envelope. The genuine nack over the sealed key
    is unaffected."""
    handler, rs, target, path, route_id, sealed_pub = await _origin_with_pending_send()
    _garbage_priv, garbage_pub = routed_crypto.generate_ephemeral_keypair()
    oracle = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=garbage_pub,
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=garbage_pub
        ),
    )
    # The signature is real — this is not the tampered-sig case.
    assert routed_crypto.verify_route_stale(
        identity_pk=target.public_key,
        route_id=route_id,
        stale_eph_pk_b64=garbage_pub,
        sig_b64=oracle["sig"],
        sig_suite=oracle["sig_suite"],
    )
    await handler._on_route_stale(
        _nack_event(oracle, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == [], (
        "target-signed nack over a substituted eph evicted a live route"
    )
    assert rs.discover_calls == []
    assert route_id in handler._pending_routed
    # The real nack — over the key the origin actually sealed under — lands.
    handler._seen_nacks.clear()  # hop-level dedup already saw route_id
    genuine = _nack_payload(
        route_id=route_id,
        path=path,
        target_pk_hex=target.public_key.hex(),
        stale_pub=sealed_pub,
        sig=routed_crypto.sign_route_stale(
            seed=target.private_key, route_id=route_id, stale_eph_pk_b64=sealed_pub
        ),
    )
    try:
        await handler._on_route_stale(
            _nack_event(genuine, from_instance=path[1], to_instance=path[0])
        )
        assert rs.invalidated == [path[-1]]
        assert route_id not in handler._pending_routed
    finally:
        _cancel_deferred(handler)


# ── Origin authentication on the mesh (#692, v_31) ────────────────────


class _MemberTargetFed(_RecordingFed):
    """The victim household's federation service: it knows the member
    household ``H`` (pinned identity key, current wire) and the relay
    ``F`` that sits next to it, exactly as ``remote_instances`` would."""

    def __init__(self, own_instance_id: str) -> None:
        super().__init__(own_instance_id)
        self.gate_calls: list[dict] = []

    def post_decrypt_gate_steps(self, *, include_ban_check: bool = False) -> list:
        async def _record(ctx):
            self.gate_calls.append(dict(ctx.envelope))

        return [_record]


async def test_non_member_cannot_dispatch_space_content_as_a_member(caplog):
    """#692 — the release-blocking shape of the hole.

    ``F`` is a household on the mesh that is **not** a member of the
    space. It probes ``V`` for a route (so it legitimately holds ``V``'s
    ephemeral pub and can seal something ``V`` decrypts), then ships
    ``SPACE_ROUTED{path: [H, F, V], position: 1}`` — ``H`` being a real
    member household. Both hop checks pass: ``path[1] == F`` is the
    authenticated sender and ``path[2] == V`` is us.

    Pre-v_31 ``V`` synthesised the inner event with
    ``from_instance = H`` and handed it to the dispatcher, so the post
    ended up persisted as ``H``'s — and every §24.11 post-decrypt gate
    (ban check, Follower write gate) judged the forged field. Nothing may
    reach the dispatcher, and the gates must not even be consulted: they
    are not the defence here, the origin signature is.
    """
    member = generate_identity_keypair()
    member_id = derive_instance_id(member.public_key)
    relay = generate_identity_keypair()
    relay_id = derive_instance_id(relay.public_key)

    fed = _MemberTargetFed("victim-instance")
    fed.identity_pks[member_id] = member.public_key
    fed.identity_pks[relay_id] = relay.public_key
    dispatched: list = []

    async def _dispatch(ev) -> None:
        dispatched.append(ev)

    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    handler = SpaceRoutedHandler(
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
        event_dispatcher=_dispatch,
        target_eph_lookup=lambda pub: target_priv if pub == target_pub else None,
    )
    path = [member_id, relay_id, fed.own_instance_id]
    sealed = _seal(
        {"space_id": "sp-1", "post_id": "p-forged", "body": "not from the member"},
        target_pub=target_pub,
    )
    # Sanity: the forgery IS decryptable — the seal alone proves nothing
    # about who wrote it, which is the whole bug.
    assert "not from the member" in routed_crypto.unseal_inner_payload(
        sealed=sealed,
        target_eph_priv_b64=target_priv,
        route_id=ROUTE_ID,
        inner_event_type=INNER_EVENT,
    )
    ev = FederationEvent(
        msg_id="m-forged",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance=relay_id,
        to_instance=fed.own_instance_id,
        timestamp="2026-09-19T00:00:00Z",
        payload={
            "route_id": ROUTE_ID,
            "path": path,
            "position": 1,
            "direction": "forward",
            "inner_event_type": INNER_EVENT,
            "sealed": sealed,
        },
    )
    await handler._on_routed(ev)
    assert dispatched == [], "a non-member dispatched space content as a member"
    assert fed.gate_calls == [], (
        "the forged origin reached the post-decrypt gates — they judge "
        "from_instance, so they are downstream of this check, not a substitute"
    )
    # And the forgery did not turn the target into a route-stale signing
    # oracle either: nothing at all went back out.
    assert fed.sent == []


async def test_the_genuine_member_still_reaches_the_dispatcher():
    """The same shape, signed by the household it claims to be: the post
    lands, attributed to the member, and the §24.11 gates run on it."""
    member = generate_identity_keypair()
    member_id = derive_instance_id(member.public_key)
    relay_id = "relay-instance"

    fed = _MemberTargetFed("victim-instance")
    fed.identity_pks[member_id] = member.public_key
    dispatched: list = []

    async def _dispatch(ev) -> None:
        dispatched.append(ev)

    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    handler = SpaceRoutedHandler(
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=SimpleNamespace(),  # type: ignore[arg-type]
        event_dispatcher=_dispatch,
        target_eph_lookup=lambda pub: target_priv if pub == target_pub else None,
    )
    path = [member_id, relay_id, fed.own_instance_id]
    sealed = _seal({"space_id": "sp-1", "post_id": "p-real"}, target_pub=target_pub)
    sealed["origin_identity_pk"] = member.public_key.hex()
    sealed["origin_sig_suite"] = routed_crypto.ROUTED_ORIGIN_SIG_SUITE_ED25519
    sealed["origin_sig"] = routed_crypto.sign_routed_origin(
        seed=member.private_key,
        route_id=ROUTE_ID,
        direction="forward",
        path=path,
        inner_event_type=INNER_EVENT,
        sealed=sealed,
    )
    ev = FederationEvent(
        msg_id="m-real",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance=relay_id,
        to_instance=fed.own_instance_id,
        timestamp="2026-09-19T00:00:00Z",
        payload={
            "route_id": ROUTE_ID,
            "path": path,
            "position": 1,
            "direction": "forward",
            "inner_event_type": INNER_EVENT,
            "sealed": sealed,
        },
    )
    await handler._on_routed(ev)
    assert len(dispatched) == 1
    assert dispatched[0].from_instance == member_id
    assert fed.gate_calls == [{"space_id": "sp-1", "from_instance": member_id}]


async def test_origin_signature_cannot_be_lifted_onto_another_route():
    """A relay that saw a genuine signed envelope for ``V`` must not be
    able to replay the signature onto a different route, leg, or target —
    every one of those is inside the signed bytes."""
    member = generate_identity_keypair()
    member_id = derive_instance_id(member.public_key)
    path = [member_id, "relay-instance", "victim-instance"]
    _priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    sealed = _seal({"space_id": "sp-1"}, target_pub=target_pub)
    sig = routed_crypto.sign_routed_origin(
        seed=member.private_key,
        route_id=ROUTE_ID,
        direction="forward",
        path=path,
        inner_event_type=INNER_EVENT,
        sealed=sealed,
    )
    common = {
        "identity_pk": member.public_key,
        "sealed": sealed,
        "sig_b64": sig,
        "sig_suite": routed_crypto.ROUTED_ORIGIN_SIG_SUITE_ED25519,
    }
    assert routed_crypto.verify_routed_origin(
        route_id=ROUTE_ID,
        direction="forward",
        path=path,
        inner_event_type=INNER_EVENT,
        **common,
    )
    # Another route_id.
    assert not routed_crypto.verify_routed_origin(
        route_id="route-something-else",
        direction="forward",
        path=path,
        inner_event_type=INNER_EVENT,
        **common,
    )
    # The reply leg.
    assert not routed_crypto.verify_routed_origin(
        route_id=ROUTE_ID,
        direction="reply",
        path=path,
        inner_event_type=INNER_EVENT,
        **common,
    )
    # A different target at the end of the path.
    assert not routed_crypto.verify_routed_origin(
        route_id=ROUTE_ID,
        direction="forward",
        path=[member_id, "relay-instance", "another-victim"],
        inner_event_type=INNER_EVENT,
        **common,
    )
    # A relabelled inner event type.
    assert not routed_crypto.verify_routed_origin(
        route_id=ROUTE_ID,
        direction="forward",
        path=path,
        inner_event_type="space_member_left",
        **common,
    )


def test_origin_signature_never_covers_the_plaintext():
    """Encryption-first (§25.8.21): the signed bytes are derived from
    fields the relay already sees, so a relay holding a *guess* at a
    low-entropy inner payload gets no oracle to confirm it with. Two
    different plaintexts sealed under the same key must produce signing
    bytes that differ only because the ciphertext differs — never because
    the plaintext is in them."""
    secret = "dinner at seven"
    _priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    sealed = _seal({"body": secret}, target_pub=target_pub)
    signed = routed_crypto.routed_origin_signing_bytes(
        route_id=ROUTE_ID,
        direction="forward",
        path=["a", "b", "c"],
        inner_event_type=INNER_EVENT,
        sealed=sealed,
    )
    assert secret.encode() not in signed
    assert b"dinner" not in signed
    # Every byte hashed into it is a field on the wire the relay can read.
    stripped = {
        k: v
        for k, v in sealed.items()
        if k in ("kem_suite", "origin_eph_pk", "target_eph_pk", "nonce", "ciphertext")
    }
    assert (
        routed_crypto.routed_origin_signing_bytes(
            route_id=ROUTE_ID,
            direction="forward",
            path=["a", "b", "c"],
            inner_event_type=INNER_EVENT,
            sealed=stripped,
        )
        == signed
    )
