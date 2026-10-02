"""Inbound coverage for authority-signed space roster gossip (v_23).

``SPACE_MEMBER_JOINED`` / ``SPACE_MEMBER_LEFT`` peer-replicate one roster
mutation to every member household so every household's roster converges.
The handler is SECURITY-SENSITIVE: trust is in the SIGNATURE (the space's
Ed25519 seed), not the sender — any seed-holder may emit, and the space
public key is the trust root. It must fail closed:

* unknown space locally → drop;
* signature absent / forged / signed by the wrong key → drop;
* unknown suite → drop;
* a stale (lower member_version) event → ignored by the version guard, so a
  removed member is never resurrected.

A verified JOINED applies (upserts) the member; a verified LEFT tombstones
them. The merge is idempotent + order-insensitive (the repo's version
guard), so out-of-order delivery converges deterministically.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from socialhome.crypto import generate_space_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)


OWNER = "owner-instance"
RELAY = "some-relay-instance"
SPACE_ID = "sp-roster"
MEMBER_INSTANCE = "peer-x"
MEMBER_USER = "ru1"


def _event(event_type, payload: dict, *, from_instance: str = OWNER):
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=SPACE_ID,
    )


def _signed_payload(
    event_type,
    *,
    seed: bytes,
    member_version: int,
    role: str = "member",
    user_id: str = MEMBER_USER,
    instance_id: str = MEMBER_INSTANCE,
):
    """Build a roster-gossip payload signed with ``seed``."""
    bare = {
        "space_id": SPACE_ID,
        "user_id": user_id,
        "instance_id": instance_id,
        "display_name": "R",
        "user_pk": None,
        "role": role,
        "member_version": member_version,
        "roster_version": member_version,
    }
    signed = sign_authority_event(
        event_type=event_type.value,
        space_id=SPACE_ID,
        payload=strip_authority_sig_fields(bare),
        space_seed=seed,
    )
    return {**bare, **signed}


async def _make_handler(tmp_dir):
    """Handler over a real space repo + remote-member repo, seeded with a
    local copy of the space whose ``identity_public_key`` matches a known
    space keypair. Returns ``(handler, space_repo, remote_members, db, seed)``.
    """
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.key_manager import KeyManager

    kp = generate_space_keypair()
    db = AsyncDatabase(tmp_dir / "roster.db", batch_timeout_ms=10)
    await db.startup()
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x07" * 32))
    await space_repo.save(
        Space(
            id=SPACE_ID,
            name="S",
            owner_instance_id=OWNER,
            owner_username="anna",
            identity_public_key=kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    remote_members = SqliteSpaceRemoteMemberRepo(db)
    h = PrivateSpaceInviteHandler(
        bus=AsyncMock(),
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )
    return h, space_repo, remote_members, db, kp.private_key


# ── Happy paths ──────────────────────────────────────────────────────────


async def test_verified_joined_applies_member(tmp_dir):
    """A JOINED signed by the space seed seats the member in the roster."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED,
                    seed=seed,
                    member_version=1,
                ),
            )
        )
        got = await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
        assert got is not None
        assert got.member_version == 1
        assert got.tombstoned is False
    finally:
        await db.shutdown()


async def test_verified_left_tombstones_member(tmp_dir):
    """A LEFT signed by the space seed tombstones the member."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED,
                    seed=seed,
                    member_version=1,
                ),
            )
        )
        await h._on_space_member_left(
            _event(
                FederationEventType.SPACE_MEMBER_LEFT,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_LEFT,
                    seed=seed,
                    member_version=2,
                ),
            )
        )
        # Live read sees them as gone; the tombstone persists.
        assert await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is None
        ghost = await rm.get_including_tombstones(
            SPACE_ID, MEMBER_INSTANCE, MEMBER_USER
        )
        assert ghost is not None and ghost.tombstoned is True
    finally:
        await db.shutdown()


async def test_relayed_event_trusted_by_signature_not_sender(tmp_dir):
    """The event is trusted by its SIGNATURE, not from_instance — a JOINED
    relayed by a non-owner household but signed with the space seed applies."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED,
                    seed=seed,
                    member_version=1,
                ),
                from_instance=RELAY,  # NOT the owner
            )
        )
        assert await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is not None
    finally:
        await db.shutdown()


# ── Security drops ─────────────────────────────────────────────────────────


async def test_wrong_key_signature_dropped(tmp_dir):
    """SECURITY: an event signed with a key OTHER than the space seed is
    dropped — the roster is unchanged."""
    h, _sr, rm, db, _seed = await _make_handler(tmp_dir)
    try:
        wrong = generate_space_keypair().private_key
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED,
                    seed=wrong,
                    member_version=1,
                ),
            )
        )
        assert (
            await rm.get_including_tombstones(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
            is None
        )
    finally:
        await db.shutdown()


async def test_missing_signature_dropped(tmp_dir):
    """An event with no authority_sig is dropped."""
    h, _sr, rm, db, _seed = await _make_handler(tmp_dir)
    try:
        payload = {
            "space_id": SPACE_ID,
            "user_id": MEMBER_USER,
            "instance_id": MEMBER_INSTANCE,
            "role": "member",
            "member_version": 1,
            "roster_version": 1,
        }
        await h._on_space_member_joined(
            _event(FederationEventType.SPACE_MEMBER_JOINED, payload)
        )
        assert (
            await rm.get_including_tombstones(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
            is None
        )
    finally:
        await db.shutdown()


async def test_unknown_suite_dropped(tmp_dir):
    """An unrecognised authority_sig_suite is dropped (crypto-suite rule —
    no default fallback)."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        payload = _signed_payload(
            FederationEventType.SPACE_MEMBER_JOINED, seed=seed, member_version=1
        )
        payload["authority_sig_suite"] = "rsa-2048"
        await h._on_space_member_joined(
            _event(FederationEventType.SPACE_MEMBER_JOINED, payload)
        )
        assert (
            await rm.get_including_tombstones(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
            is None
        )
    finally:
        await db.shutdown()


async def test_unknown_space_dropped(tmp_dir):
    """A gossip for a space we hold no local copy of is dropped."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        payload = _signed_payload(
            FederationEventType.SPACE_MEMBER_JOINED, seed=seed, member_version=1
        )
        ev = _event(FederationEventType.SPACE_MEMBER_JOINED, payload)
        # Point both the envelope + payload at an unknown space.
        ev.space_id = "no-such-space"
        ev.payload["space_id"] = "no-such-space"
        await h._on_space_member_joined(ev)
        # The real space's roster stays empty.
        assert await rm.list_for_space(SPACE_ID) == []
    finally:
        await db.shutdown()


# ── Convergence ──────────────────────────────────────────────────────────


async def test_stale_joined_does_not_resurrect_removed_member(tmp_dir):
    """SECURITY/convergence: a removed (tombstoned) member is NOT resurrected
    by a replayed lower-version JOINED."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        # Seat (v1), remove (v2).
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED, seed=seed, member_version=1
                ),
            )
        )
        await h._on_space_member_left(
            _event(
                FederationEventType.SPACE_MEMBER_LEFT,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_LEFT, seed=seed, member_version=2
                ),
            )
        )
        # A stale JOINED at v1 arrives late — must be ignored.
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED, seed=seed, member_version=1
                ),
            )
        )
        assert await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is None
    finally:
        await db.shutdown()


async def test_out_of_order_left_then_stale_joined_converges_removed(tmp_dir):
    """Delivery order LEFT(v2) → JOINED(v1) converges to removed — the
    version guard + removal-wins-tie make the merge order-insensitive."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    try:
        # LEFT (v2) arrives first — applies as a fresh tombstone.
        await h._on_space_member_left(
            _event(
                FederationEventType.SPACE_MEMBER_LEFT,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_LEFT, seed=seed, member_version=2
                ),
            )
        )
        # Then a stale JOINED (v1) — must NOT resurrect.
        await h._on_space_member_joined(
            _event(
                FederationEventType.SPACE_MEMBER_JOINED,
                _signed_payload(
                    FederationEventType.SPACE_MEMBER_JOINED, seed=seed, member_version=1
                ),
            )
        )
        assert await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is None
    finally:
        await db.shutdown()


# ── Unsigned-injection regression (no legacy mutation path) ─────────────────


async def _wire_both_handlers(tmp_dir):
    """Build BOTH the authority gossip handler AND the legacy inbound service
    over the SAME real repos, registered on ONE real EventDispatchRegistry —
    exactly the production wiring (the registry fires every handler bound to an
    event type). Returns ``(registry, remote_members, db, own_instance)``.
    """
    from socialhome.federation.event_dispatch_registry import EventDispatchRegistry
    from socialhome.services.federation_inbound_service import (
        FederationInboundService,
    )

    h, space_repo, remote_members, db, _seed = await _make_handler(tmp_dir)
    inbound = FederationInboundService(
        bus=AsyncMock(),
        conversation_repo=AsyncMock(),
        space_post_repo=AsyncMock(),
        space_repo=space_repo,
        user_repo=AsyncMock(),
        space_remote_member_repo=remote_members,
    )
    own_instance = "this-household"
    registry = EventDispatchRegistry()
    # ``attach_to`` stashes the federation service (read for ``own_instance_id``
    # in the legacy handler), so the SAME object must carry both the registry
    # and the instance id.
    fed = SimpleNamespace(_event_registry=registry, own_instance_id=own_instance)
    inbound.attach_to(fed)
    h.attach_to(fed)
    return registry, space_repo, remote_members, db, own_instance


def _full_event(event_type, payload: dict, *, from_instance: str = RELAY):
    from socialhome.domain.federation import FederationEvent

    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="this-household",
        timestamp="2026-06-10T00:00:00+00:00",
        payload=payload,
        space_id=SPACE_ID,
    )


async def test_unsigned_joined_seats_nobody_through_any_handler(tmp_dir):
    """SECURITY: an UNSIGNED ``SPACE_MEMBER_JOINED`` (no ``authority_sig``)
    from a confirmed peer must NOT seat any roster member through ANY handler.

    Regression for the dormant legacy ``_on_space_member_joined`` in
    FederationInboundService: it only ``return``ed when ``authority_sig`` was
    PRESENT, so an unsigned event slipped its guard and called ``add`` with no
    authority verification — letting any confirmed peer forge a roster entry.
    Dispatch through the real registry (both handlers fire); the roster must be
    unchanged.
    """
    registry, space_repo, rm, db, _own = await _wire_both_handlers(tmp_dir)
    try:
        payload = {
            "space_id": SPACE_ID,
            "user_id": MEMBER_USER,
            "instance_id": MEMBER_INSTANCE,
            "display_name": "Forged",
            "role": "member",
            "member_version": 1,
            "roster_version": 1,
        }
        await registry.dispatch(
            _full_event(FederationEventType.SPACE_MEMBER_JOINED, payload)
        )
        assert (
            await rm.get_including_tombstones(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
            is None
        )
        assert await rm.list_for_space(SPACE_ID) == []
        # No local member seated either.
        assert await space_repo.get_member(SPACE_ID, MEMBER_USER) is None
    finally:
        await db.shutdown()


async def test_unsigned_left_evicts_nobody_through_any_handler(tmp_dir):
    """SECURITY: an UNSIGNED ``SPACE_MEMBER_LEFT`` must NOT evict/tombstone a
    seated member through ANY handler. Seat a member via the verified path,
    then dispatch an unsigned LEFT — the member must remain.
    """
    registry, space_repo, rm, db, _own = await _wire_both_handlers(tmp_dir)
    try:
        # Seat a real member directly so there is something to (illegitimately)
        # try to evict.
        await rm.apply_member_event(
            space_id=SPACE_ID,
            user_id=MEMBER_USER,
            instance_id=MEMBER_INSTANCE,
            display_name="R",
            user_pk=None,
            role="member",
            member_version=1,
            tombstoned=False,
        )
        payload = {
            "space_id": SPACE_ID,
            "user_id": MEMBER_USER,
            "instance_id": MEMBER_INSTANCE,
            "role": "member",
            "member_version": 2,
            "roster_version": 2,
        }
        await registry.dispatch(
            _full_event(FederationEventType.SPACE_MEMBER_LEFT, payload)
        )
        still = await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
        assert still is not None
        assert still.tombstoned is False
    finally:
        await db.shutdown()


# ── Registration ───────────────────────────────────────────────────────────


async def test_handlers_registered(tmp_dir):
    """attach_to wires the gossip handlers for both event types."""
    h, _sr, _rm, db, _seed = await _make_handler(tmp_dir)
    try:
        registered: dict = {}

        class _Reg:
            def register(self, et, fn):
                registered.setdefault(et, []).append(fn)

        fed = SimpleNamespace(_event_registry=_Reg())
        h.attach_to(fed)
        assert (
            h._on_space_member_joined
            in registered[FederationEventType.SPACE_MEMBER_JOINED]
        )
        assert (
            h._on_space_member_left in registered[FederationEventType.SPACE_MEMBER_LEFT]
        )
    finally:
        await db.shutdown()


# ── v_32 roster snapshot ──────────────────────────────────────────────


def _snapshot(entries, *, from_instance=OWNER):
    return _event(
        FederationEventType.SPACE_ROSTER_SNAPSHOT,
        {"space_id": SPACE_ID, "entries": entries},
        from_instance=from_instance,
    )


def _entry(event_type, payload):
    return {"event_type": event_type.value, "payload": payload}


async def test_a_roster_snapshot_seats_every_signed_entry(tmp_dir):
    """An empty mirror (a household that joined before roster gossip, or
    missed it) is filled by one snapshot — and learns the removals too."""
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    left = FederationEventType.SPACE_MEMBER_LEFT
    await h._on_space_roster_snapshot(
        _snapshot(
            [
                _entry(j, _signed_payload(j, seed=seed, member_version=4)),
                _entry(
                    left,
                    _signed_payload(
                        left,
                        seed=seed,
                        member_version=6,
                        user_id="gone",
                        instance_id="p-g",
                    ),
                ),
            ]
        )
    )
    assert await remote.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is not None
    gone = await remote.get_including_tombstones(SPACE_ID, "p-g", "gone")
    assert gone is not None and gone.tombstoned
    await db.shutdown()


async def test_a_roster_snapshot_skips_forged_foreign_and_unknown_entries(tmp_dir):
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    forged = _signed_payload(j, seed=b"\x01" * 32, member_version=3, user_id="u-f")
    other_space = dict(
        _signed_payload(j, seed=seed, member_version=3, user_id="u-o"),
        space_id="sp-else",
    )
    await h._on_space_roster_snapshot(
        _snapshot(
            [
                _entry(j, forged),
                _entry(j, other_space),
                {"event_type": "space_member_banned", "payload": {}},
                "not-a-dict",
                _entry(j, _signed_payload(j, seed=seed, member_version=3)),
            ]
        )
    )
    rows = await remote.list_for_space(SPACE_ID)
    assert [r.user_id for r in rows] == [MEMBER_USER]
    await db.shutdown()


async def test_a_roster_snapshot_is_verified_off_the_event_loop(tmp_dir, monkeypatch):
    """Up to thousands of signature checks per snapshot run in a worker
    thread, not on the event loop."""
    import threading

    from socialhome.federation import private_invite_handler as handler_mod

    real_verify = handler_mod.verify_authority_event
    threads: list[int] = []

    def _spy(**kwargs):
        threads.append(threading.get_ident())
        return real_verify(**kwargs)

    monkeypatch.setattr(handler_mod, "verify_authority_event", _spy)
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    await h._on_space_roster_snapshot(
        _snapshot(
            [
                _entry(j, _signed_payload(j, seed=seed, member_version=3)),
                _entry(
                    j,
                    _signed_payload(
                        j, seed=seed, member_version=3, user_id="u2", instance_id="p2"
                    ),
                ),
            ]
        )
    )
    assert len(threads) == 2
    assert threading.get_ident() not in threads
    assert len(await remote.list_for_space(SPACE_ID)) == 2
    await db.shutdown()


async def test_a_roster_snapshot_for_an_unknown_space_is_dropped(tmp_dir, caplog):
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    payload = dict(_signed_payload(j, seed=seed, member_version=3), space_id="sp-nope")
    event = _event(
        FederationEventType.SPACE_ROSTER_SNAPSHOT,
        {"space_id": "sp-nope", "entries": [_entry(j, payload)]},
    )
    event.space_id = "sp-nope"
    await h._on_space_roster_snapshot(event)
    assert "unknown space sp-nope" in caplog.text
    assert await remote.list_for_space("sp-nope") == []
    await db.shutdown()


async def test_a_roster_snapshot_drops_unsigned_and_unknown_suite_entries(
    tmp_dir, caplog
):
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    unsigned = {
        k: v
        for k, v in _signed_payload(
            j, seed=seed, member_version=3, user_id="u-n"
        ).items()
        if k != "authority_sig"
    }
    odd_suite = dict(
        _signed_payload(j, seed=seed, member_version=3, user_id="u-s"),
        authority_sig_suite="rot13",
    )
    await h._on_space_roster_snapshot(
        _snapshot([_entry(j, unsigned), _entry(j, odd_suite)])
    )
    assert "missing authority signature" in caplog.text
    assert "unknown authority_sig_suite" in caplog.text
    assert await remote.list_for_space(SPACE_ID) == []
    await db.shutdown()


async def test_a_roster_snapshot_never_regresses_a_newer_seat(tmp_dir):
    h, _spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    left = FederationEventType.SPACE_MEMBER_LEFT
    await h._on_space_member_left(
        _event(left, _signed_payload(left, seed=seed, member_version=9))
    )
    await h._on_space_roster_snapshot(
        _snapshot([_entry(j, _signed_payload(j, seed=seed, member_version=5))])
    )
    assert await remote.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER) is None
    await db.shutdown()


async def test_a_malformed_roster_snapshot_is_ignored(tmp_dir):
    h, _spaces, remote, db, _seed = await _make_handler(tmp_dir)
    await h._on_space_roster_snapshot(
        _event(FederationEventType.SPACE_ROSTER_SNAPSHOT, {"entries": "x"})
    )
    assert await remote.list_for_space(SPACE_ID) == []
    await db.shutdown()


async def test_an_applied_seat_announces_itself(tmp_dir):
    """A seat learned from gossip releases the writes held for it."""
    from socialhome.domain.events import SpaceRemoteSeatLive

    h, _spaces, _remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    await h._on_space_member_joined(
        _event(j, _signed_payload(j, seed=seed, member_version=2))
    )
    live = [
        (e.space_id, e.instance_id, e.user_id)
        for e in (c.args[0] for c in h._bus.publish.await_args_list)
        if isinstance(e, SpaceRemoteSeatLive)
    ]
    assert live == [(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)]
    await db.shutdown()


# ── v_44: on the HOST, gossip never raises a seat; a lost admin seat is news ──


async def _hosted_handler(tmp_dir):
    """Same as ``_make_handler`` but this household HOSTS the space, and
    the bus is a recorder."""
    h, space_repo, rm, db, seed = await _make_handler(tmp_dir)
    h._own_instance_id = OWNER  # we are the host
    published: list = []

    async def _publish(evt):
        published.append(evt)

    h._bus = SimpleNamespace(publish=_publish)
    return h, space_repo, rm, db, seed, published


async def _gossip(h, seed, *, tombstoned=False, **kw):
    et = (
        FederationEventType.SPACE_MEMBER_LEFT
        if tombstoned
        else FederationEventType.SPACE_MEMBER_JOINED
    )
    payload = _signed_payload(et, seed=seed, **kw)
    if tombstoned:
        await h._on_space_member_left(_event(et, payload, from_instance=RELAY))
    else:
        await h._on_space_member_joined(_event(et, payload, from_instance=RELAY))


async def test_host_ignores_gossip_that_raises_a_seat_to_admin(tmp_dir):
    """SECURITY (v_44): a demoted household still holding the old seed
    signs ``JOINED role=admin`` for its own seat. On the host that would
    re-seat it as admin and hand it the next signing seed. Pinned."""
    h, _sr, rm, db, seed, _pub = await _hosted_handler(tmp_dir)
    try:
        await rm.add(
            space_id=SPACE_ID,
            instance_id=MEMBER_INSTANCE,
            user_id=MEMBER_USER,
            user_pk=None,
            display_name="R",
            role="member",
        )
        await _gossip(h, seed, member_version=50, role="admin")
        got = await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)
        assert got.role == "member"
    finally:
        await db.shutdown()


async def test_host_never_seats_an_invented_admin(tmp_dir):
    """A seat the host never made cannot arrive as admin or moderator: the
    raise is refused outright, so no row is seated at all."""
    h, _sr, rm, db, seed, _pub = await _hosted_handler(tmp_dir)
    try:
        await _gossip(h, seed, member_version=3, role="moderator", user_id="ghost")
        assert (
            await rm.get_including_tombstones(SPACE_ID, MEMBER_INSTANCE, "ghost")
            is None
        )
        # A plain member seat from a seed holder still lands.
        await _gossip(h, seed, member_version=4, role="member", user_id="ghost")
        assert (await rm.get(SPACE_ID, MEMBER_INSTANCE, "ghost")).role == "member"
    finally:
        await db.shutdown()


async def test_member_household_still_mirrors_the_hosts_role(tmp_dir):
    """Only the host pins: a member household mirrors the role the
    authority signed (that is how a promotion reaches it)."""
    h, _sr, rm, db, seed = await _make_handler(tmp_dir)
    h._own_instance_id = "a-member-household"
    try:
        await _gossip(h, seed, member_version=2, role="admin")
        assert (await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)).role == "admin"
    finally:
        await db.shutdown()


async def test_host_publishes_revocation_when_an_admin_seat_is_tombstoned(tmp_dir):
    """A delegated admin removed another admin while the owner was offline:
    the host learns from the LEFT gossip and must rotate."""
    from socialhome.domain.events import SpaceAdminAuthorityRevoked

    h, _sr, rm, db, seed, published = await _hosted_handler(tmp_dir)
    try:
        await rm.add(
            space_id=SPACE_ID,
            instance_id=MEMBER_INSTANCE,
            user_id=MEMBER_USER,
            user_pk=None,
            display_name="R",
            role="admin",
        )
        await _gossip(h, seed, tombstoned=True, member_version=9, role="admin")
        assert published == [
            SpaceAdminAuthorityRevoked(
                space_id=SPACE_ID,
                instance_id=MEMBER_INSTANCE,
                occurred_at=published[0].occurred_at,
            )
        ]
    finally:
        await db.shutdown()


async def test_host_publishes_revocation_when_gossip_lowers_an_admin(tmp_dir):
    """Lowering is allowed (it only takes privilege away) — and it ends an
    admin seat, so it is a revocation."""
    from socialhome.domain.events import SpaceAdminAuthorityRevoked

    h, _sr, rm, db, seed, published = await _hosted_handler(tmp_dir)
    try:
        await rm.add(
            space_id=SPACE_ID,
            instance_id=MEMBER_INSTANCE,
            user_id=MEMBER_USER,
            user_pk=None,
            display_name="R",
            role="admin",
        )
        await _gossip(h, seed, member_version=9, role="member")
        assert (await rm.get(SPACE_ID, MEMBER_INSTANCE, MEMBER_USER)).role == "member"
        assert [type(e) for e in published if not hasattr(e, "user_id")] == [
            SpaceAdminAuthorityRevoked
        ]
    finally:
        await db.shutdown()


async def test_no_revocation_for_a_non_admin_seat_or_a_mirror(tmp_dir):
    from socialhome.domain.events import SpaceAdminAuthorityRevoked

    h, _sr, rm, db, seed, published = await _hosted_handler(tmp_dir)
    try:
        await rm.add(
            space_id=SPACE_ID,
            instance_id=MEMBER_INSTANCE,
            user_id=MEMBER_USER,
            user_pk=None,
            display_name="R",
            role="member",
        )
        await _gossip(h, seed, tombstoned=True, member_version=9)
        assert not [e for e in published if isinstance(e, SpaceAdminAuthorityRevoked)]
    finally:
        await db.shutdown()


async def test_the_hosts_snapshot_names_its_owner_seat(tmp_dir):
    """Migration 0070: the host ships its owner as ``owner``; the stub
    records that seat (and still mirrors it as a plain ``member`` row)."""
    h, spaces, remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    owner_entry = _signed_payload(
        j,
        seed=seed,
        member_version=2,
        role="owner",
        user_id="u-anna",
        instance_id=OWNER,
    )
    await h._on_space_roster_snapshot(_snapshot([_entry(j, owner_entry)]))
    assert await spaces.get_owner_user_id(SPACE_ID) == "u-anna"
    seat = await remote.get(SPACE_ID, OWNER, "u-anna")
    assert seat is not None and seat.role == "member"
    await db.shutdown()


async def test_only_the_host_can_name_the_owner_seat(tmp_dir):
    """A validly signed snapshot relayed by another household (any seed
    holder can sign) names no owner — only the host's own does."""
    h, spaces, _remote, db, seed = await _make_handler(tmp_dir)
    j = FederationEventType.SPACE_MEMBER_JOINED
    forged_owner = _signed_payload(
        j,
        seed=seed,
        member_version=2,
        role="owner",
        user_id="u-mallory",
        instance_id=OWNER,
    )
    await h._on_space_roster_snapshot(
        _snapshot([_entry(j, forged_owner)], from_instance=RELAY)
    )
    # An "owner" on a household that isn't the host names nobody either.
    elsewhere = _signed_payload(
        j, seed=seed, member_version=3, role="owner", user_id="u-x", instance_id="p-x"
    )
    await h._on_space_roster_snapshot(_snapshot([_entry(j, elsewhere)]))
    assert await spaces.get_owner_user_id(SPACE_ID) is None
    await db.shutdown()
