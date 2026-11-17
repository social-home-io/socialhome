"""End to end: two households that share a space through a GFS pair.

Real crypto, real SQLite, real HTTP inboxes, the real §24.11 pipeline —
on the mailbox fixture of ``test_invite_bootstrap_gfs_e2e``: host ``b``,
and ``a`` / ``c`` which reach b's space only through the GFS and hold a
lazy **directory seat** for each other (a confirmed ``space_session`` row,
no address, the space sealed under its static-static keys).

A seat is not a pairing. Before this, ``a`` and ``c`` could not pair:

* the trust relay answered "already paired" on both sides;
* a pairing code wrote its pending half over the seat — the space between
  the two stopped until the people confirmed, and a cancelled handshake
  lost the seat for good.

The rule now, for both handshakes: the pending half waits BESIDE the seat,
the seat keeps carrying the space, and only the confirmed pairing replaces
it. After an unpair the plain seat is back.
"""

# ruff: noqa: F811 — the fixtures imported below are used as test arguments
from __future__ import annotations

from types import SimpleNamespace

import orjson
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    PendingPairingNotFoundError,
    RemoteInstance,
    is_paired,
    is_space_seat,
)
from socialhome.federation.auto_pair_coordinator import AutoPairCoordinator
from socialhome.federation.transport import FederationTransport
from socialhome.repositories.dm_media_outbox_repo import SqliteDmMediaOutboxRepo
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.repositories.space_media_outbox_repo import SqliteSpaceMediaOutboxRepo
from socialhome.services.auto_pair_inbox import AutoPairInbox
from socialhome.services.federation_inbound import PairingInboundHandlers
from socialhome.services.pairing_cancel_service import PairingCancelService
from socialhome.services.peer_unpair_service import PeerUnpairService
from tests.federation.test_invite_bootstrap_gfs_e2e import (  # noqa: F401 — fixtures
    _household,
    _mailbox_of,
    _post_payload,
    gfs,
    http_session,
    three_mailbox_households,
)

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass

_PAIRING_BODIES = {
    FederationEventType.PAIRING_PEER_ACCEPT.value: "handle_peer_accept",
    FederationEventType.PAIRING_PEER_CONFIRM.value: "handle_peer_confirm",
}


async def _wire(h, session) -> SimpleNamespace:
    """Give household *h* what a running app adds on top of the fixture: an
    HTTPS inbox (the route's dispatch: a pairing bootstrap body, else the
    §24.11 pipeline), the trust-relay coordinator, the inbound pairing
    handlers, unpair (with the member directory's re-seat hook) and the
    pending-pairing cancel."""
    refused: list[tuple[str, str]] = []
    # The plaintext pairing bodies (peer-accept / peer-confirm) go out on
    # the service's own HTTP client.
    h.federation.attach_session(session)

    async def _inbox(request: web.Request) -> web.Response:
        inbox_id = request.match_info["inbox_id"]
        raw = await request.read()
        body = orjson.loads(raw)
        event_type = str(body.get("event_type") or "")
        try:
            if event_type in _PAIRING_BODIES:
                handler = getattr(h.federation, _PAIRING_BODIES[event_type])
                result = await handler(body, expected_local_inbox_id=inbox_id)
            else:
                result = await h.federation.handle_inbound_envelope(inbox_id, raw)
        except ValueError as exc:
            refused.append((event_type, str(exc)))
            status = 404 if "No instance found" in str(exc) else 403
            return web.json_response({"error": "refused"}, status=status)
        return web.json_response(result)

    app = web.Application()
    app.router.add_post("/inbox/{inbox_id}", _inbox)
    server = TestServer(app)
    await server.start_server()

    unpair = PeerUnpairService(
        bus=h.bus,
        federation=h.federation,
        federation_repo=h.federation_repo,
        outbox_repo=SqliteOutboxRepo(h.db),
        routing_repo=SqliteDmRoutingRepo(h.db),
        dm_media_outbox_repo=SqliteDmMediaOutboxRepo(h.db),
        space_media_outbox_repo=SqliteSpaceMediaOutboxRepo(h.db),
    )
    if h.directory is not None:
        unpair.attach_after_purge(h.directory.reseat)
    PairingInboundHandlers(
        bus=h.bus, federation_repo=h.federation_repo, peer_unpair=unpair
    ).attach_to(h.federation)

    auto_inbox = AutoPairInbox(bus=h.bus)
    auto = AutoPairCoordinator(
        federation_repo=h.federation_repo,
        key_manager=h.key_manager,
        bus=h.bus,
        federation_service=h.federation,
        own_identity_seed=h.identity_seed,
        own_identity_pk=h.identity_pk,
        inbox=auto_inbox,
        pending_pairings=h.federation.pending_pairings,
    )
    registry = h.federation._event_registry
    registry.register(FederationEventType.PAIRING_INTRO_AUTO, auto.on_intro_at_target)
    registry.register(
        FederationEventType.PAIRING_INTRO_AUTO_ACK, auto.on_ack_at_originator
    )
    registry.register(
        FederationEventType.PAIRING_INTRO_AUTO_ACK_VIA, auto.on_ack_via_at_relay
    )
    confirmed: list[str] = []

    async def _on_confirmed(event: PairingConfirmed) -> None:
        confirmed.append(event.instance_id)

    h.bus.subscribe(PairingConfirmed, _on_confirmed)
    return SimpleNamespace(
        h=h,
        server=server,
        base=str(server.make_url("")).rstrip("/") + "/inbox",
        refused=refused,
        unpair=unpair,
        auto=auto,
        auto_inbox=auto_inbox,
        confirmed=confirmed,
        cancel=PairingCancelService(
            bus=h.bus,
            federation=h.federation,
            federation_repo=h.federation_repo,
            auto_pair=auto,
            pending_pairings=h.federation.pending_pairings,
        ),
    )


async def _pair(x, y) -> None:
    """*x* and *y* (wired) as an ordinary confirmed pair over HTTPS."""
    key_xy, key_yx = bytes([7]) * 32, bytes([8]) * 32
    for me, other, out_key, in_key in ((x, y, key_xy, key_yx), (y, x, key_yx, key_xy)):
        inbox_id = f"inbox-{me.h.name}-for-{other.h.name}"
        await me.h.federation_repo.save_instance(
            RemoteInstance(
                id=other.h.instance_id,
                display_name=other.h.name,
                remote_identity_pk=other.h.identity_pk.hex(),
                key_self_to_remote=me.h.key_manager.encrypt(out_key),
                key_remote_to_self=me.h.key_manager.encrypt(in_key),
                remote_inbox_url=(f"{other.base}/inbox-{other.h.name}-for-{me.h.name}"),
                local_inbox_id=inbox_id,
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            )
        )


@pytest.fixture
async def world(three_mailbox_households, gfs, tmp_path, http_session, monkeypatch):
    """``a`` and ``c`` share b's space and hold a directory seat for each
    other; ``m`` is a household both are paired with (the mutual friend
    that can introduce them)."""

    async def _no_rtc(self, instance) -> None:
        """Paired households here talk over their HTTPS inboxes only: the
        fixture has no signalling, and a DataChannel that "opens" without a
        remote end would swallow every envelope after the first."""

    monkeypatch.setattr(FederationTransport, "_ensure_handshake", _no_rtc)
    a, b, c, space = three_mailbox_households
    m = await _household(tmp_path, "friend", gfs, http_session)
    wa = await _wire(a, http_session)
    wc = await _wire(c, http_session)
    wm = await _wire(m, http_session)
    await _pair(wa, wm)
    await _pair(wc, wm)
    yield SimpleNamespace(
        a=wa,
        c=wc,
        m=wm,
        b=b,
        space=space,
        gfs=gfs,
        tmp_path=tmp_path,
        http_session=http_session,
    )
    await gfs.drain()
    for w in (wa, wc, wm):
        await w.server.close()
    await m.db.shutdown()


async def _row(w, other) -> RemoteInstance | None:
    return await w.h.federation_repo.get_instance(other.h.instance_id)


async def _seat_keys(w, other) -> tuple[bytes, bytes]:
    row = await _row(w, other)
    assert is_space_seat(row) and not row.relay_via and not row.remote_inbox_url
    return (
        w.h.key_manager.decrypt(row.key_self_to_remote),
        w.h.key_manager.decrypt(row.key_remote_to_self),
    )


async def _space_reaches(world, sender, receiver, marker: str) -> list[str]:
    """*sender* posts to the shared space; returns which carrier the GFS
    saw for *receiver* (its mailbox id, or nothing when it went direct),
    after asserting the post arrived and decrypted there."""
    receiver.h.received.clear()
    world.gfs.mailbox.clear()
    await sender.h.federation.broadcast_to_space_members(
        world.space.id, FederationEventType.SPACE_POST_CREATED, _post_payload(marker)
    )
    await world.gfs.drain()
    got = [
        (e.from_instance, e.payload["content"])
        for e in receiver.h.received
        if e.event_type is FederationEventType.SPACE_POST_CREATED
    ]
    assert got == [(sender.h.instance_id, marker)], (
        f"{sender.h.name} → {receiver.h.name}: {got!r} (refused: {receiver.refused!r})"
    )
    receiver_mailbox = _mailbox_of(receiver.h, world.gfs)
    return [to for to, _ in world.gfs.mailbox if to == receiver_mailbox]


async def _assert_space_over_the_seat(world, tag: str) -> None:
    """Both ways, at each other's GFS mailbox, under the seat's keys."""
    a, c = world.a, world.c
    assert await _space_reaches(world, a, c, f"{tag}-a-to-c")
    assert await _space_reaches(world, c, a, f"{tag}-c-to-a")
    assert is_space_seat(await _row(a, c)) and is_space_seat(await _row(c, a))


async def _assert_space_over_the_pairing(world, tag: str) -> None:
    """Both ways, straight to each other's inbox — nothing for the two on
    the GFS any more — and both rows are the pairing."""
    a, c = world.a, world.c
    assert not await _space_reaches(world, a, c, f"{tag}-a-to-c")
    assert not await _space_reaches(world, c, a, f"{tag}-c-to-a")
    for me, other in ((a, c), (c, a)):
        row = await _row(me, other)
        assert is_paired(row) and row.source is InstanceSource.MANUAL
        assert row.remote_inbox_url.startswith(other.base + "/")


# ── Trust relay (a asks the mutual friend m to introduce it to c) ─────────


async def _request_and_approve(world) -> RemoteInstance:
    """a requests, m vouches, c's admin approves; the ack travels back
    through m and confirms a. Returns c's approval (still pending)."""
    a, c, m = world.a, world.c, world.m
    await a.auto.request_via(
        via_instance_id=m.h.instance_id,
        target_instance_id=c.h.instance_id,
        target_display_name="C",
        own_inbox_base_url=a.base,
    )
    [request] = c.auto_inbox.list_pending()
    assert request.from_a_id == a.h.instance_id
    return await c.auto.finalize_pending(
        request.request_id, own_inbox_base_url=c.base, actor_user_id=c.h.user_id
    )


async def test_a_trust_relay_request_over_a_seat_reaches_the_other_household(world):
    """The reported bug, requester side: "already paired" (422) for a
    household we only share a space with — and, approver side, the vouched
    introduction dropped for the same reason."""
    a, c, m = world.a, world.c, world.m
    seat_before = await _seat_keys(a, c)

    await a.auto.request_via(
        via_instance_id=m.h.instance_id,
        target_instance_id=c.h.instance_id,
        target_display_name="C",
        own_inbox_base_url=a.base,
    )

    assert [r.from_a_id for r in c.auto_inbox.list_pending()] == [a.h.instance_id]
    # Asking changed nothing about the space between the two.
    assert await _seat_keys(a, c) == seat_before
    await _assert_space_over_the_seat(world, "requested")


async def test_the_space_keeps_flowing_until_the_trust_relay_pair_confirms(world):
    a, c = world.a, world.c
    approval = await _request_and_approve(world)

    # a confirmed when the ack verified: its seat is the pairing now.
    assert a.confirmed == [c.h.instance_id]
    a_row = await _row(a, c)
    assert is_paired(a_row) and a_row.relay_via == world.m.h.instance_id
    assert a_row.remote_inbox_url == f"{c.base}/{approval.local_inbox_id}"
    # c approved, but a pairing needs both: c's row is still the seat, the
    # approval waits beside it — and c keeps taking the space from a, whose
    # envelopes now arrive under the NEW keys on the approval's inbox.
    assert approval.status is PairingStatus.PENDING_SENT
    assert is_space_seat(await _row(c, a)) and c.confirmed == []

    # a's first envelope under the new keys: it verifies against the vouched
    # identity, decrypts under the approved session key, confirms the pair
    # on c BEFORE it is dispatched — and is delivered.
    assert not await _space_reaches(world, a, c, "first-under-new-keys")
    assert c.confirmed == [a.h.instance_id]
    c_row = await _row(c, a)
    assert is_paired(c_row) and c_row.local_inbox_id == approval.local_inbox_id
    assert c.refused == [] and a.refused == []

    await _assert_space_over_the_pairing(world, "paired")


async def test_an_approved_pair_does_not_confirm_on_seat_traffic(world):
    """Only an envelope under the APPROVED keys confirms. The requester
    cancelled before the ack: its space traffic keeps coming under the
    seat's keys, and must neither confirm the approval nor be refused."""
    a, c, m = world.a, world.c, world.m
    await a.auto.request_via(
        via_instance_id=m.h.instance_id,
        target_instance_id=c.h.instance_id,
        target_display_name="C",
        own_inbox_base_url=a.base,
    )
    [request] = c.auto_inbox.list_pending()
    await a.cancel.cancel_pending(c.h.instance_id)  # a changed its mind
    await c.auto.finalize_pending(request.request_id, own_inbox_base_url=c.base)

    assert is_space_seat(await _row(a, c)) and a.confirmed == []
    await _assert_space_over_the_seat(world, "one-sided")
    assert c.confirmed == []
    assert c.h.federation.pending_pairings.staged(a.h.instance_id) is not None

    # c's admin withdraws the approval: the seat is still there.
    await c.cancel.cancel_pending(a.h.instance_id)
    assert c.h.federation.pending_pairings.staged(a.h.instance_id) is None
    await _assert_space_over_the_seat(world, "withdrawn")


async def test_an_envelope_under_other_keys_on_the_approval_inbox_is_refused(world):
    """The approval's inbox resolves to the approved keys only: somebody
    who knows the inbox id but not the session key gets nothing — neither
    a confirmed pair nor a dispatched event."""
    a, c = world.a, world.c
    approval = await _request_and_approve(world)
    # a's envelope sealed under the SEAT's keys, posted to the approval's
    # inbox: authentic sender, wrong keys for this door.
    seat_row = RemoteInstance(
        id=c.h.instance_id,
        display_name="",
        remote_identity_pk=c.h.identity_pk.hex(),
        key_self_to_remote=a.h.key_manager.encrypt(bytes(32)),
        key_remote_to_self=a.h.key_manager.encrypt(bytes(32)),
        remote_inbox_url=f"{c.base}/{approval.local_inbox_id}",
        local_inbox_id="x",
    )
    c.h.received.clear()
    result = await a.h.federation.send_event_once(
        instance=seat_row,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload=_post_payload("wrong-keys"),
    )

    assert not result.ok
    assert [reason for _t, reason in c.refused] == ["Failed to decrypt payload: "]
    assert c.h.received == [] and c.confirmed == []
    assert is_space_seat(await _row(c, a))


# ── Pairing code (a shows it, c scans it) ─────────────────────────────────


async def _scan(world) -> tuple[dict, dict]:
    """a mints a code, c scans it; c's signed peer-accept reaches a."""
    a, c = world.a, world.c
    code = await a.h.federation.initiate_pairing(a.base, share_home=False)
    accepted = await c.h.federation.accept_pairing(
        code, own_inbox_base_url=c.base, share_home=True
    )
    return code, accepted


async def test_a_pairing_code_over_a_seat_does_not_pause_the_space(world):
    """The reported bug, QR side: the scan (and the peer-accept on the code
    owner) wrote a pending row over the seat, so the space between the two
    stopped until the people had compared the code."""
    a, c = world.a, world.c
    seats = (await _seat_keys(a, c), await _seat_keys(c, a))

    code, _accepted = await _scan(world)

    # Both halves of the handshake exist — beside the seats, not over them.
    for me, other, status in (
        (c, a, PairingStatus.PENDING_RECEIVED),
        (a, c, PairingStatus.PENDING_RECEIVED),
    ):
        half = me.h.federation.pending_pairings.staged(other.h.instance_id)
        assert half is not None and half.status is status
        assert half.source is InstanceSource.MANUAL
    assert (await _seat_keys(a, c), await _seat_keys(c, a)) == seats
    assert await a.h.federation_repo.get_pairing(code["token"]) is not None
    await _assert_space_over_the_seat(world, "code-out")


async def test_a_cancelled_pairing_code_keeps_the_seat(world):
    a, c = world.a, world.c
    seats = (await _seat_keys(a, c), await _seat_keys(c, a))
    code, _accepted = await _scan(world)

    await c.cancel.cancel_pending(a.h.instance_id)

    # c's half and its session are gone; its seat never was touched.
    assert c.h.federation.pending_pairings.staged(a.h.instance_id) is None
    assert await c.h.federation_repo.get_pairing(code["token"]) is None
    assert (await _seat_keys(a, c), await _seat_keys(c, a)) == seats
    await _assert_space_over_the_seat(world, "cancelled")
    # A second cancel finds no pending pairing — the seat is not one.
    with pytest.raises(PendingPairingNotFoundError):
        await c.cancel.cancel_pending(a.h.instance_id)
    # The code owner heard the cancel: its half went too, so it has nothing
    # left to cancel (or to confirm into a one-sided pairing).
    with pytest.raises(PendingPairingNotFoundError):
        await a.cancel.cancel_pending(c.h.instance_id)
    await _assert_space_over_the_seat(world, "both-cancelled")


async def test_a_cancel_is_heard_by_the_household_that_showed_the_code(world):
    """c scanned a's code and then cancelled. Its ``PAIRING_ABORT`` is
    sealed with the keys of the half and goes to the inbox the code gave
    out — which, over a seat, no row holds. a must hear it all the same:
    otherwise a's admin enters the code a minute later, a replaces its
    seat with a pairing c refused, and the space between the two is dead
    (review finding; without a seat the abort always arrived)."""
    a, c = world.a, world.c
    seats = (await _seat_keys(a, c), await _seat_keys(c, a))
    code, accepted = await _scan(world)

    assert await c.cancel.cancel_pending(a.h.instance_id) is True

    assert a.refused == []
    assert a.h.federation.pending_pairings.staged(c.h.instance_id) is None
    assert await a.h.federation_repo.get_pairing(code["token"]) is None
    with pytest.raises(ValueError, match="No pending pairing"):
        await a.h.federation.confirm_pairing(
            code["token"], accepted["verification_code"]
        )
    assert a.confirmed == [] and c.confirmed == []
    assert (await _seat_keys(a, c), await _seat_keys(c, a)) == seats
    await _assert_space_over_the_seat(world, "c-cancelled")


async def test_a_pairing_code_never_confirms_a_trust_relay_approval(world):
    """a compares a pairing code with c. Meanwhile an introduction "from
    c" arrives through a household a trusts and a's admin approves it —
    the two are pairing, after all. The approval carries the keys and the
    address the INTRODUCER vouched for, which the code never covered:
    entering the code must not confirm it (review finding — the half was
    replaced in place and promoted with the introducer's keys)."""
    a, c, m = world.a, world.c, world.m
    code, accepted = await _scan(world)
    await c.auto.request_via(
        via_instance_id=m.h.instance_id,
        target_instance_id=a.h.instance_id,
        target_display_name="A",
        own_inbox_base_url=c.base,
    )
    [request] = a.auto_inbox.list_pending()
    approval = await a.auto.finalize_pending(
        request.request_id, own_inbox_base_url=a.base
    )
    # A handshake of its own: never the inbox the code gave out.
    assert not code["inbox_url"].endswith("/" + approval.local_inbox_id)

    with pytest.raises(ValueError, match="No pending pairing"):
        await a.h.federation.confirm_pairing(
            code["token"], accepted["verification_code"]
        )

    assert is_space_seat(await _row(a, c)) and a.confirmed == []


async def test_a_pairing_code_never_confirms_our_own_keyless_request(world):
    """a holds c's code half and also asks m to introduce it to c: the
    request (no key at all) took the half's place. Entering the code must
    not turn the seat into a confirmed row nobody can talk to."""
    a, c, m = world.a, world.c, world.m
    seat = await _seat_keys(a, c)
    code, accepted = await _scan(world)
    await a.auto.request_via(
        via_instance_id=m.h.instance_id,
        target_instance_id=c.h.instance_id,
        target_display_name="C",
        own_inbox_base_url=a.base,
    )

    with pytest.raises(ValueError, match="No pending pairing"):
        await a.h.federation.confirm_pairing(
            code["token"], accepted["verification_code"]
        )

    assert await _seat_keys(a, c) == seat and a.confirmed == []
    await _assert_space_over_the_seat(world, "still-a-seat")


async def test_a_confirmed_pairing_code_replaces_the_seat_on_both_sides(world):
    a, c = world.a, world.c
    code, accepted = await _scan(world)

    await a.h.federation.confirm_pairing(code["token"], accepted["verification_code"])

    # a entered the code; its signed peer-confirm confirmed c.
    assert a.confirmed == [c.h.instance_id] and c.confirmed == [a.h.instance_id]
    a_row, c_row = await _row(a, c), await _row(c, a)
    # Each side's row holds the inbox the handshake gave the other — never
    # the seat's — and the per-pair choice made in THIS handshake.
    assert code["inbox_url"] == f"{a.base}/{a_row.local_inbox_id}"
    assert c_row.local_inbox_id == accepted["local_inbox_id"]
    assert (a_row.share_home, c_row.share_home) == (False, True)
    assert a.h.federation.pending_pairings.list_staged() == []
    assert c.h.federation.pending_pairings.list_staged() == []
    assert a.refused == [] and c.refused == []

    await _assert_space_over_the_pairing(world, "qr-paired")


async def test_an_unverified_pairing_code_opens_no_inbox(world):
    """Fail closed: until OUR people confirmed, nothing sealed under a
    pairing code's keys is taken — the household's row is its seat, and a
    handler would read "confirmed" off it."""
    a, c = world.a, world.c
    code, accepted = await _scan(world)
    half = a.h.federation.pending_pairings.staged(c.h.instance_id)
    assert half is not None and code["inbox_url"].endswith(half.local_inbox_id)
    c_half = c.h.federation.pending_pairings.staged(a.h.instance_id)
    a.h.received.clear()

    # c, not yet confirmed by a, talks to a under the code's keys.
    result = await c.h.federation.send_event_once(
        instance=c_half,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload=_post_payload("too-early"),
    )

    assert not result.ok and a.h.received == []
    assert [t for t, _ in a.refused] == [FederationEventType.SPACE_POST_CREATED.value]
    assert "No instance found" in a.refused[0][1]
    assert accepted["verification_code"]  # (the handshake itself is intact)


# ── Unpair: the plain seat is back ────────────────────────────────────────


async def test_after_an_unpair_the_plain_seat_is_back_on_both_sides(world):
    a, c = world.a, world.c
    seats = (await _seat_keys(a, c), await _seat_keys(c, a))
    code, accepted = await _scan(world)
    await a.h.federation.confirm_pairing(code["token"], accepted["verification_code"])
    await _assert_space_over_the_pairing(world, "paired")

    # a's admin removes the connection: a signed UNPAIR, then forget.
    assert await a.unpair.unpair(c.h.instance_id) is True

    # Both households are back to exactly the seat they held before the
    # people paired — re-derived from the member card, same keys — and
    # the space flows over it again.
    assert (await _seat_keys(a, c), await _seat_keys(c, a)) == seats
    await _assert_space_over_the_seat(world, "unpaired")
    # Still members of the space, and still not paired: a new handshake
    # starts from scratch.
    assert not is_paired(await _row(a, c)) and not is_paired(await _row(c, a))


# ── Households that share no space: nothing changed ───────────────────────


async def test_without_a_seat_both_handshakes_write_the_row_as_ever(world):
    """What a v_80 peer — or anybody we share no space with — sees and
    what we store for it is what it always was: the pending half IS the
    row and nothing waits in memory. (The wire bodies are built by the
    same code with or without a seat; their fields are pinned in
    ``test_auto_pair_coordinator`` / ``test_pairing_coordinator``.)"""
    a, m = world.a, world.m
    stranger = await _wire(
        await _household(world.tmp_path, "stranger", world.gfs, world.http_session),
        world.http_session,
    )
    try:
        await _pair(stranger, m)
        # Trust relay: stranger asks m for an introduction to a.
        await stranger.auto.request_via(
            via_instance_id=m.h.instance_id,
            target_instance_id=a.h.instance_id,
            target_display_name="A",
            own_inbox_base_url=stranger.base,
        )
        row = await _row(stranger, a)
        assert row.status is PairingStatus.PENDING_SENT and not row.remote_identity_pk
        assert stranger.h.federation.pending_pairings.list_staged() == []
        [request] = a.auto_inbox.list_pending()
        approval = await a.auto.finalize_pending(
            request.request_id, own_inbox_base_url=a.base
        )
        # The approver's half is its row too (it held none for the stranger).
        assert a.h.federation.pending_pairings.list_staged() == []
        assert (await _row(a, stranger)).status is PairingStatus.PENDING_SENT
        assert is_paired(await _row(stranger, a))
        # The stranger's first envelope confirms the row, as ever.
        await stranger.h.federation.send_event(
            to_instance_id=a.h.instance_id,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload=_post_payload("hello"),
        )
        row = await _row(a, stranger)
        assert is_paired(row) and row.local_inbox_id == approval.local_inbox_id
    finally:
        await stranger.server.close()
        await stranger.h.db.shutdown()
