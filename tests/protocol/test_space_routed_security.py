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

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

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
    await handler._on_route_stale(
        _nack_event(genuine, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == [path[-1]]
    assert route_id not in handler._pending_routed


async def test_route_stale_with_tampered_stale_eph_pk_fails_verification():
    """The signature binds ``(route_id, stale_eph_pk)``. Swapping the eph
    pk under a genuine signature must not verify — otherwise a relay could
    re-point a real nack at a different key."""
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
    await handler._on_route_stale(
        _nack_event(genuine, from_instance=path[1], to_instance=path[0])
    )
    assert rs.invalidated == [path[-1]]
    assert route_id not in handler._pending_routed
