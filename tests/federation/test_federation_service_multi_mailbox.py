"""v_91 — ``broadcast_to_space_members`` hands the mailbox seats of a space
behind one GFS to the outbox as ONE multi payload, and the outbox uploads it
ONCE.

The grouping rules (version, seat kind, GFS capability, identical bytes,
event type, audience, group size) are checked against a recording transport;
the last test runs the real :class:`FederationTransport` +
:class:`GfsRelayTransport` and :class:`OutboxProcessor` on the sending side
and the real receive path (``GfsRelayInbound`` + the §24.11 pipeline, from
``test_gfs_relay_inbound``) on two receivers.
"""

from __future__ import annotations

import base64
import dataclasses
import os
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.outbox import DeliveryOutcome
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.federation_service import MULTI_MAX_TRANSIENT
from socialhome.federation.gfs_relay_transport import (
    GfsRelayTransport,
    MailboxTarget,
)
from socialhome.federation.multi_recipient import (
    MULTI_TO_INSTANCE,
    multi_audience_includes,
    open_multi,
)
from socialhome.federation.sync.space.exporter import ChunkBuilder
from socialhome.federation.sync.space.provider import SpaceSyncService
from socialhome.federation.transport import FederationTransport, _TransportSendResult
from socialhome.infrastructure.outbox_processor import OutboxProcessor
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
    _make_kek_manager,
    _make_remote_instance,
    _make_service,
)
from tests.services.test_gfs_relay_inbound import _SPACE, _MultiHousehold

GFS_A = "https://gfs-a.example.org"
GFS_B = "https://gfs-b.example.org"
V90 = FederationCapability.MIN_FOR_MULTI_MAILBOX
POST = FederationEventType.SPACE_POST_CREATED


class _Transport:
    """Records per-recipient sends and multi uploads."""

    def __init__(self) -> None:
        self.targets: dict[str, MailboxTarget] = {}
        self.capacity: dict[str, int] = {}
        self.sends: list[tuple[str, dict]] = []
        self.multi: list[tuple[list[tuple[MailboxTarget, dict]], dict]] = []
        #: mailbox id → its key-wrap private key (to open an upload).
        self.kem_private: dict[str, bytes] = {}
        self.multi_result: tuple[bool, int | None, float | None] | Exception = (
            True,
            None,
            None,
        )
        self.send_ok = True
        self.capacity_asks: list[str] = []

    async def send(self, *, instance, envelope_dict):
        self.sends.append((instance.id, envelope_dict))
        via = (
            "gfs_relay" if instance.source is InstanceSource.SPACE_SESSION else "https"
        )
        return _TransportSendResult(ok=self.send_ok, via=via)

    async def mailbox_reachable(self, instance, space_id) -> bool:
        return True

    async def multi_mailbox_target(self, instance, space_id, *, caps=None):
        if instance.source is not InstanceSource.SPACE_SESSION:
            return None
        target = self.targets.get(instance.id)
        if target is None:
            return None
        cap = caps.get(target.gfs_url) if caps is not None else None
        if cap is None:
            cap = await self.multi_capacity(target.gfs_url)
            if caps is not None:
                caps[target.gfs_url] = cap
        return target if cap >= 1 else None

    async def multi_capacity(self, gfs_url: str) -> int:
        self.capacity_asks.append(gfs_url)
        return self.capacity.get(gfs_url, 500)

    async def send_multi_mailbox(self, *, to, body):
        self.multi.append((list(to), body))
        if isinstance(self.multi_result, Exception):
            raise self.multi_result
        return self.multi_result

    def per_recipient_ids(self) -> list[str]:
        return [iid for iid, _env in self.sends]


class _Sender:
    """A sending household: FederationService + recording transport."""

    def __init__(self) -> None:
        self.km = _make_kek_manager()
        self.repo = InMemoryFederationRepo()
        self.outbox = InMemoryOutboxRepo()
        self.svc, _kp = _make_service(
            federation_repo=self.repo, outbox_repo=self.outbox, key_manager=self.km
        )
        self.transport = _Transport()
        self.svc.attach_transport(self.transport)
        self.wakes = 0

        def _wake() -> None:
            self.wakes += 1

        self.svc.attach_outbox_wake(_wake)

    async def seat(
        self, *, gfs: str | None = GFS_A, version: int = V90, mailbox: bool = True
    ) -> str:
        """A ``space_session`` member household with a mailbox on *gfs*."""
        inst, _ = _make_remote_instance(self.km, session_key=os.urandom(32))
        inst = dataclasses.replace(
            inst,
            source=InstanceSource.SPACE_SESSION,
            remote_inbox_url="",
            relay_via=gfs,
            proto_version=version,
        )
        await self.repo.save_instance(inst)
        self.repo.add_space_member(_SPACE, inst.id)
        if mailbox and gfs is not None:
            kem = generate_x25519_keypair()
            self.transport.targets[inst.id] = MailboxTarget(
                mailbox_id=inst.id[::-1], kem_pk=kem.public_key, gfs_url=gfs
            )
            self.transport.kem_private[inst.id[::-1]] = kem.private_key
        return inst.id

    async def paired(self) -> str:
        inst, _ = _make_remote_instance(self.km, session_key=os.urandom(32))
        inst = dataclasses.replace(inst, proto_version=V90)
        await self.repo.save_instance(inst)
        self.repo.add_space_member(_SPACE, inst.id)
        return inst.id

    async def broadcast(self, event_type=POST, **kw):
        return await self.svc.broadcast_to_space_members(
            _SPACE, event_type, kw.pop("payload", {"post_id": "p-1"}), **kw
        )


async def _never(_sender: str, _space: str) -> bool:
    return False


def _mailboxes(upload) -> list[str]:
    return sorted(t.mailbox_id for t, _wrap in upload[0])


def _opened(s: _Sender, upload) -> list[dict]:
    """Every listed mailbox opens the one body with its own wrap."""
    to, body = upload
    return [
        open_multi({**body, "wrap": wrap}, s.transport.kem_private[t.mailbox_id])
        for t, wrap in to
    ]


def _boxes(s: _Sender, ids) -> list[str]:
    return sorted(s.transport.targets[i].mailbox_id for i in ids)


def _queued(s: _Sender) -> dict[str, list[str]]:
    """Each stored multi payload's id → the households it has a row for."""
    out: dict[str, list[str]] = {}
    for row in s.outbox.enqueued:
        if s.outbox.payloads[row["payload_id"]].msg_id is not None:
            out.setdefault(row["payload_id"], []).append(row["instance_id"])
    return {k: sorted(v) for k, v in out.items()}


async def _drain(svc, outbox) -> MagicMock:
    """One pass of the real :class:`OutboxProcessor` over every queued row;
    a row in no group is 'delivered' by a recording single path."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=outbox.entries())
    repo.mark_delivered = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    repo.single = AsyncMock(return_value=DeliveryOutcome.SUCCESS)
    await OutboxProcessor(repo, repo.single, multi=svc).drain_once()
    return repo


async def test_three_seats_on_one_gfs_are_one_payload_and_one_upload():
    """One broadcast to three seats: ONE stored payload + three ref rows and
    a wake; the outbox's next pass makes ONE upload. The paired peer goes
    per recipient at once."""
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    peer = await s.paired()
    result = await s.broadcast()
    by_id = {r.instance_id: r for r in result.results}
    assert all(by_id[i].error == DELIVERY_ERROR_QUEUED for i in seats)
    assert result.terminal_failures == ()  # self-healing, never a loss
    assert s.transport.per_recipient_ids() == [peer]
    assert s.transport.multi == []  # the outbox is the only multi sender
    [(payload_id, rows)] = _queued(s).items()
    assert rows == sorted(seats)
    assert s.wakes == 1

    repo = await _drain(s.svc, s.outbox)
    [upload] = s.transport.multi
    assert _mailboxes(upload) == _boxes(s, seats)
    assert set(upload[1]) == {"mr_suite", "ciphertext"}
    opened = _opened(s, upload)
    # One body, its key wrapped three times: every wrap opens the same one.
    assert len(opened) == 3 and all(b == opened[0] for b in opened)
    envelope = opened[0]["envelope"]
    assert envelope["msg_id"] == s.outbox.payloads[payload_id].msg_id
    assert repo.mark_delivered.await_count == 3
    repo.single.assert_not_awaited()
    # Accepted by the relay, never "reachable" on that alone.
    assert s.svc.last_relay_accepted_at(seats[0]) is not None
    assert s.repo.reachable_calls == [peer]


async def test_a_seat_below_v90_goes_per_recipient():
    s = _Sender()
    new = [await s.seat() for _ in range(2)]
    old = await s.seat(version=V90 - 1)
    await s.broadcast()
    assert list(_queued(s).values()) == [sorted(new)]
    assert s.transport.per_recipient_ids() == [old]


async def test_a_gfs_without_envelope_multi_goes_per_recipient():
    s = _Sender()
    s.transport.capacity[GFS_A] = 0
    seats = [await s.seat() for _ in range(3)]
    await s.broadcast()
    assert _queued(s) == {} and s.wakes == 0
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)


async def test_a_single_seat_is_never_a_multi_of_one():
    s = _Sender()
    seat = await s.seat()
    await s.seat(gfs=GFS_B)
    await s.paired()
    await s.broadcast()
    assert _queued(s) == {}
    assert seat in s.transport.per_recipient_ids()


async def test_different_payload_bytes_split_the_groups():
    s = _Sender()
    seats = [await s.seat() for _ in range(4)]

    async def _per_peer(iid: str, payload: dict) -> dict:
        # Two households get one copy, two another.
        return {**payload, "cert": "x" if iid in seats[:2] else "y"}

    await s.broadcast(per_peer=_per_peer)
    assert sorted(_queued(s).values()) == sorted([sorted(seats[:2]), sorted(seats[2:])])
    assert s.transport.sends == []


async def test_a_relay_payload_variant_rides_its_own_group():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    peer = await s.paired()
    await s.broadcast(payload={"big": "x" * 10}, relay_payload={"big": "small"})
    [(payload_id, rows)] = _queued(s).items()
    assert rows == sorted(seats)
    _stored, payload_json = await s.svc._outbox_payload_json(payload_id)
    assert orjson.loads(payload_json) == {"big": "small"}
    assert s.transport.per_recipient_ids() == [peer]


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.SPACE_MEMBER_CARD,
        FederationEventType.SPACE_MAILBOX_DIRECTORY,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
    ],
)
async def test_event_types_outside_the_multi_set_never_go_multi(event_type):
    s = _Sender()
    for _ in range(3):
        await s.seat()
    await s.broadcast(event_type)
    assert _queued(s) == {}
    assert len(s.transport.sends) == 3


async def test_a_narrowed_broadcast_goes_multi_to_its_narrowed_audience():
    """The audience is exactly the planned targets: a space-chat-style
    ``only_instances`` goes multi to those households only — the body key
    is wrapped to their mailboxes and the left-out one is not in the signed
    audience."""
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    await s.broadcast(only_instances=frozenset(seats[:2]))
    assert list(_queued(s).values()) == [sorted(seats[:2])]
    assert s.transport.sends == []
    await _drain(s.svc, s.outbox)
    [upload] = s.transport.multi
    assert _mailboxes(upload) == _boxes(s, seats[:2])
    to = _opened(s, upload)[0]["envelope"]["to_instance"]
    assert all(multi_audience_includes(to, i) for i in seats[:2])
    assert not multi_audience_includes(to, seats[2])


async def test_a_failing_multi_queue_goes_per_recipient():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]

    async def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    s.outbox.enqueue_refs = _boom
    result = await s.broadcast()
    assert result.succeeded == 2
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)


async def test_key_order_does_not_split_a_group():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]

    async def _per_peer(iid: str, payload: dict) -> dict:
        if iid == seats[0]:
            return {"a": 1, "b": 2}
        return {"b": 2, "a": 1}

    await s.broadcast(per_peer=_per_peer)
    assert list(_queued(s).values()) == [sorted(seats)]
    assert s.transport.sends == []


def _count_reads(s: _Sender) -> dict[str, int]:
    """Wrap every read of the sender's federation repo with a counter."""
    counts: dict[str, int] = {}
    for name in dir(s.repo):
        if not (name.startswith(("get_", "list_"))):
            continue
        real = getattr(s.repo, name)

        def _wrap(real=real, name=name):
            async def _counted(*a, **kw):
                counts[name] = counts.get(name, 0) + 1
                return await real(*a, **kw)

            return _counted

        setattr(s.repo, name, _wrap())
    return counts


async def test_a_paired_only_space_pays_no_per_target_reads_for_multi():
    """With the grouping or without it, a paired-only space costs the same
    per-target reads; the grouping adds one batched read and no capability
    lookup."""
    costs = []
    for grouped in (False, True):
        s = _Sender()
        for _ in range(4):
            await s.paired()
        counts = _count_reads(s)
        if grouped:
            await s.broadcast()
        else:
            with patch.object(
                type(s.svc), "_queue_multi_groups", AsyncMock(return_value={})
            ):
                await s.broadcast()
        costs.append(counts)
        if grouped:
            assert s.transport.capacity_asks == []
    unwired, wired_counts = costs
    extra = {
        k: wired_counts.get(k, 0) - unwired.get(k, 0)
        for k in set(wired_counts) | set(unwired)
    }
    assert {k: v for k, v in extra.items() if v} == {"list_instances_in_space": 1}


async def test_a_seat_row_is_read_once_and_each_gfs_probed_once():
    s = _Sender()
    for _ in range(3):
        await s.seat(gfs=GFS_A)
    for _ in range(2):
        await s.seat(gfs=GFS_B)
    counts = _count_reads(s)
    s.transport.capacity_asks.clear()
    await s.broadcast()
    assert len(_queued(s)) == 2
    assert sorted(s.transport.capacity_asks) == [GFS_A, GFS_B]
    assert counts.get("list_instances_in_space") == 1
    # No per-target row read by the grouping.
    assert counts.get("get_instance", 0) == 0


async def test_two_gfss_are_two_payloads_and_two_uploads():
    s = _Sender()
    a = [await s.seat(gfs=GFS_A) for _ in range(2)]
    b = [await s.seat(gfs=GFS_B) for _ in range(2)]
    await s.broadcast()
    assert sorted(_queued(s).values()) == sorted([sorted(a), sorted(b)])
    await _drain(s.svc, s.outbox)
    assert sorted(_mailboxes(u) for u in s.transport.multi) == sorted(
        [_boxes(s, a), _boxes(s, b)]
    )
    assert s.transport.sends == []


async def test_one_gfs_spelled_two_ways_is_one_group():
    s = _Sender()
    await s.seat(gfs=GFS_A)
    await s.seat(gfs=GFS_A.upper() + "/")
    await s.broadcast()
    assert len(_queued(s)) == 1 and s.transport.sends == []


async def test_a_group_over_the_gfs_cap_is_chunked_and_a_lone_rest_goes_alone():
    s = _Sender()
    for _ in range(5):
        await s.seat()
    await s.broadcast()
    s.transport.capacity[GFS_A] = 2
    groups = await s.svc.outbox_multi_groups(s.outbox.entries())
    assert sorted(len(g) for g in groups) == [2, 2]
    repo = await _drain(s.svc, s.outbox)
    assert [len(u[0]) for u in s.transport.multi] == [2, 2]
    assert repo.single.await_count == 1  # the lone rest, per row


async def test_a_deferred_member_and_a_mailbox_less_seat_stay_per_recipient():
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    loner = await s.seat(mailbox=False)
    s.svc._deferred_mesh_queues[seats[0]] = deque()
    result = await s.broadcast()
    assert list(_queued(s).values()) == [sorted(seats[1:])]
    assert s.transport.per_recipient_ids() == [loner]
    assert len(result.results) == 4


async def test_the_audience_tags_name_exactly_the_group():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    outsider = await s.paired()
    await s.broadcast()
    await _drain(s.svc, s.outbox)
    [upload] = s.transport.multi
    env = _opened(s, upload)[0]["envelope"]
    assert env["to_instance"].startswith(MULTI_TO_INSTANCE + ":")
    assert all(multi_audience_includes(env["to_instance"], i) for i in seats)
    assert not multi_audience_includes(env["to_instance"], outsider)


# ── End to end: real sending transport, real receivers ──────────────────


class _MultiSender:
    """The ``RelayEnvelopeSender`` seam: a GFS proving ``envelope_multi``."""

    def __init__(self) -> None:
        self.uploads: list[tuple[list[tuple[str, dict]], str, dict]] = []

    async def mailbox_connection(self, gfs_url):
        return object()

    async def multi_connection(self, gfs_url):
        return object()

    def multi_max_recipients(self, conn) -> int:
        return 500

    async def send_to_mailboxes(self, *, entries, gfs_url, body):
        self.uploads.append((list(entries), gfs_url, body))
        return 202

    async def send_to_mailbox(self, **_kw):  # pragma: no cover - not reached
        raise AssertionError("per-recipient send in a multi test")


async def test_a_frame_built_from_the_broadcast_is_taken_by_every_receiver_once():
    """Broadcast → outbox → ONE ``send_to_mailboxes``; every receiver takes
    the frame, and a retry of the same payload (a lost 202) carries the same
    ``msg_id`` — the receivers' replay cache drops it."""
    receivers = [_MultiHousehold(), _MultiHousehold()]
    kp = generate_identity_keypair()
    sender_id = derive_instance_id(kp.public_key)
    km = _make_kek_manager()
    repo = InMemoryFederationRepo()
    outbox = InMemoryOutboxRepo()
    svc, _ = _make_service(federation_repo=repo, outbox_repo=outbox, key_manager=km)
    # The sender's identity as the receivers pinned it, with its card.
    svc._own_instance_id = sender_id
    svc._encoder = type(svc._encoder)(kp.private_key)
    for rx in receivers:
        await rx.repo.save_instance(
            RemoteInstance(
                id=sender_id,
                display_name="sender",
                remote_identity_pk=kp.public_key.hex(),
                key_self_to_remote="unused",
                key_remote_to_self="unused",
                remote_inbox_url="",
                local_inbox_id="l-" + sender_id[:8],
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.SPACE_SESSION,
            )
        )
        rx.cards.add((sender_id, _SPACE))
        seat, _ = _make_remote_instance(km, session_key=os.urandom(32))
        await repo.save_instance(
            dataclasses.replace(
                seat,
                id=rx.own_id,
                source=InstanceSource.SPACE_SESSION,
                remote_inbox_url="",
                relay_via=GFS_A,
                proto_version=V90,
            )
        )
        repo.add_space_member(_SPACE, rx.own_id)

    targets = {
        rx.own_id: MailboxTarget(
            mailbox_id=rx.mailbox.mailbox_id,
            kem_pk=rx.mailbox.kem_public,
            gfs_url=GFS_A,
        )
        for rx in receivers
    }

    async def _resolve(instance_id, space_id):
        return targets.get(instance_id) if space_id == _SPACE else None

    async def _signal(*_a, **_kw):  # pragma: no cover - no RTC here
        raise AssertionError("no signalling in a relay test")

    gfs = _MultiSender()
    transport = FederationTransport(
        own_instance_id=sender_id,
        https_inbox=object(),  # type: ignore[arg-type]
        gfs_relay=GfsRelayTransport(relay_sender=gfs),
        mailbox_targets=_resolve,
        signaling_send=_signal,
    )
    svc.attach_transport(transport)

    result = await svc.broadcast_to_space_members(
        _SPACE, POST, {"post_id": "p-9", "content": "hi all"}
    )
    assert {r.error for r in result.results} == {DELIVERY_ERROR_QUEUED}
    assert gfs.uploads == []
    await _drain(svc, outbox)
    [(entries, url, body)] = gfs.uploads
    assert sorted(m for m, _wrap in entries) == sorted(
        t.mailbox_id for t in targets.values()
    )
    assert url == GFS_A

    def _pushed(upload, rx) -> dict:
        """What the GFS pushes to *rx*'s mailbox: body + its own wrap."""
        entries, _url, body = upload
        wrap = dict(entries)[rx.mailbox.mailbox_id]
        return {"type": "envelope", "sealed": {**body, "wrap": wrap}}

    for rx in receivers:
        assert await rx.frame(_pushed(gfs.uploads[0], rx)) == {"status": "ok"}
        [event] = rx.dispatched
        assert event.from_instance == sender_id
        assert event.payload == {"post_id": "p-9", "content": "hi all"}

    # The GFS took it but the answer was lost: the retry is a new frame of
    # the same message.
    await _drain(svc, outbox)
    assert gfs.uploads[-1][2] != body  # a fresh K, a fresh body
    for rx in receivers:
        with pytest.raises(ValueError, match="Replay"):
            await rx.frame(_pushed(gfs.uploads[-1], rx))
        assert len(rx.dispatched) == 1


# ── Per-recipient FIFO: a live send never overtakes a queued multi row ───


async def test_a_live_send_to_a_seat_behind_a_queued_row_is_queued_after_it():
    """A seat with any row still pending: a live content send would reach it
    before that row — it goes to the outbox instead, where the ordering gate
    hands it out only after every older row of the seat."""
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    result = await s.svc.send_event(
        to_instance_id=seats[0],
        event_type=POST,
        payload={"post_id": "p-2"},
        space_id=_SPACE,
    )
    assert result.error == DELIVERY_ERROR_QUEUED and not result.ok
    assert s.transport.sends == []
    row = s.outbox.enqueued[-1]
    assert row["instance_id"] == seats[0]
    assert s.outbox.payloads[row["payload_id"]].msg_id is None  # per recipient
    assert s.outbox.rescheduled == []  # due now: the gate holds it


async def test_a_live_send_to_a_paired_peer_with_a_queued_row_goes_live():
    s = _Sender()
    peer = await s.paired()
    await s.outbox.enqueue(instance_id=peer, event_type=POST, payload_json="{}")
    result = await s.svc.send_event(
        to_instance_id=peer, event_type=POST, payload={"p": 1}, space_id=_SPACE
    )
    assert result.ok and s.transport.per_recipient_ids() == [peer]


async def test_a_live_send_with_nothing_queued_goes_live_and_paired_pay_no_read():
    s = _Sender()
    seat = await s.seat()
    peer = await s.paired()
    asked: list[str] = []
    real = s.outbox.has_pending_row

    async def _counted(instance_id: str):
        asked.append(instance_id)
        return await real(instance_id)

    s.outbox.has_pending_row = _counted
    for iid in (seat, peer):
        await s.svc.send_event(
            to_instance_id=iid, event_type=POST, payload={"p": 1}, space_id=_SPACE
        )
    assert s.transport.per_recipient_ids() == [seat, peer]
    assert asked == [seat]  # one query, only for a space_session seat
    assert s.outbox.rescheduled == []


async def test_the_host_hop_queues_behind_a_seat_s_pending_multi_row():
    """A seat we cannot reach at its mailbox goes through the space's host —
    but not past a multi row it still has pending: that send is queued
    behind the row like a live one."""
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    hop = MagicMock()
    hop.send = AsyncMock()
    s.svc.attach_member_mesh(host_hop=hop, mailbox_sender_check=_never)

    async def _unreachable(instance, space_id) -> bool:
        return False

    s.transport.mailbox_reachable = _unreachable  # type: ignore[method-assign]
    # A directory seat with no GFS relay of ours: the host hop is its path.
    seat = await s.repo.get_instance(seats[0])
    await s.repo.save_instance(dataclasses.replace(seat, relay_via=None))
    result = await s.svc.send_with_mesh_fallback(
        to_instance_id=seats[0],
        event_type=POST,
        payload={"post_id": "p-2"},
        space_id=_SPACE,
    )
    assert result.error == DELIVERY_ERROR_QUEUED and not result.ok
    hop.send.assert_not_awaited()
    assert s.outbox.enqueued[-1]["instance_id"] == seats[0]


async def test_one_ineligible_seat_is_dropped_from_the_upload():
    """A seat that lost its mailbox since the rows were grouped is left out
    (its row goes per row later); the other two still go up as one."""
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    await s.broadcast()
    s.transport.multi.clear()
    [group] = await s.svc.outbox_multi_groups(s.outbox.entries())
    del s.transport.targets[seats[0]]
    outcome, sent = await s.svc.deliver_outbox_multi(group)
    assert outcome is DeliveryOutcome.SUCCESS
    assert sorted(e.instance_id for e in sent) == sorted(seats[1:])
    [upload] = s.transport.multi
    assert _mailboxes(upload) == _boxes(s, seats[1:])


async def test_a_row_moderation_no_longer_wants_is_kept_out_of_every_group():
    """The moderation queue's check runs on rows that would group; a refused
    one goes single (the delivery callback drops it)."""
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    await s.broadcast()
    checked: list[str] = []

    async def _wanted(entry) -> bool:
        checked.append(entry.instance_id)
        return entry.instance_id != seats[0]

    s.svc.attach_outbox_wake(lambda: None, still_wanted=lambda: _wanted)
    [group] = await s.svc.outbox_multi_groups(s.outbox.entries())
    assert sorted(e.instance_id for e in group) == sorted(seats[1:])
    assert sorted(checked) == sorted(seats)
    s.svc.attach_outbox_wake(lambda: None, still_wanted=lambda: None)
    [group] = await s.svc.outbox_multi_groups(s.outbox.entries())
    assert len(group) == 3


async def test_a_group_left_with_one_seat_does_not_go_up():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    s.transport.multi.clear()
    [group] = await s.svc.outbox_multi_groups(s.outbox.entries())
    del s.transport.targets[seats[0]]
    assert await s.svc.deliver_outbox_multi(group) is None
    assert s.transport.multi == []
    # Not refused by the GFS: the payload may still be grouped later.
    assert s.outbox.ungrouped == set()


async def test_a_multi_upload_failing_transient_three_times_goes_per_row():
    """Matrix 3: a multi upload that failed TRANSIENT MULTI_MAX_TRANSIENT
    times (by its rows' attempts) is ungrouped — its rows then go per row,
    with the host-hop fallback, and can no longer hold their seats."""
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    s.transport.multi_result = (False, 503, None)

    def _tried(n: int):
        return [dataclasses.replace(e, attempts=n) for e in s.outbox.entries()]

    for n in range(MULTI_MAX_TRANSIENT):
        [group] = await s.svc.outbox_multi_groups(_tried(n))
        outcome, sent = await s.svc.deliver_outbox_multi(group)
        assert outcome is DeliveryOutcome.TRANSIENT and len(sent) == 2
    assert s.outbox.ungrouped == set()  # still one upload so far
    # The next pass after the third failure: ungrouped, no group.
    assert await s.svc.outbox_multi_groups(_tried(MULTI_MAX_TRANSIENT)) == []
    assert s.outbox.ungrouped == {group[0].payload_id}
    # From then on each row takes the per-row path.
    repo = await _drain(s.svc, s.outbox)
    assert sorted(c.args[0].instance_id for c in repo.single.await_args_list) == (
        sorted(seats)
    )


# ── Sync / control events never queue behind a multi row ────────────────


class _SyncExporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id: str) -> list[dict]:
        return list(self._records)


class _SyncCrypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")


async def test_a_catch_up_stream_to_a_seat_behind_a_multi_row_goes_live():
    """Only a multi event type queues behind a seat's pending multi row: a
    newcomer's catch-up (SPACE_SYNC_CHUNK …) goes live. Queued, every chunk
    would count a strike and the provider would abandon the stream after
    three — within milliseconds."""
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    queued_before = len(s.outbox.enqueued)
    provider = SpaceSyncService(
        builder=ChunkBuilder(
            encoder=FederationEncoder(generate_identity_keypair().private_key),
            crypto=_SyncCrypto(),
        ),
        exporters={
            name: _SyncExporter(name, [{"id": f"{name}-{i}"} for i in range(4)])
            for name in ("posts", "members")
        },
        sig_suite="ed25519",
    )
    provider.attach_federation(s.svc)
    session = SimpleNamespace(
        sync_id="sync-1",
        space_id=_SPACE,
        requester_instance_id=seats[0],
        sync_mode="initial",
        have_seq=None,
        since_seq=None,
        rtc=None,
        transport_mode="https",
        touch=lambda: None,
    )
    await provider.stream_initial(session)
    chunks = [
        env
        for iid, env in s.transport.sends
        if iid == seats[0]
        and env["event_type"] == FederationEventType.SPACE_SYNC_CHUNK.value
    ]
    assert len(chunks) == 3  # posts, members, then the sentinel
    assert len(s.outbox.enqueued) == queued_before  # nothing queued behind
    assert s.outbox.rescheduled == []


@pytest.mark.parametrize(
    "event_type",
    [FederationEventType.SPACE_SYNC_BEGIN, FederationEventType.SPACE_SYNC_COMPLETE],
)
async def test_a_sync_control_event_to_a_seat_behind_a_multi_row_goes_live(
    event_type,
):
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    result = await s.svc.send_event(
        to_instance_id=seats[0],
        event_type=event_type,
        payload={"sync_id": "sync-1"},
        space_id=_SPACE,
    )
    assert result.ok
    assert [iid for iid, _env in s.transport.sends] == [seats[0]]


async def test_the_host_hop_carries_a_sync_chunk_past_a_pending_multi_row():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    hop = MagicMock()
    hop.send = AsyncMock(return_value=MagicMock(ok=True))
    s.svc.attach_member_mesh(host_hop=hop, mailbox_sender_check=_never)

    async def _unreachable(instance, space_id) -> bool:
        return False

    s.transport.mailbox_reachable = _unreachable  # type: ignore[method-assign]
    seat = await s.repo.get_instance(seats[0])
    await s.repo.save_instance(dataclasses.replace(seat, relay_via=None))
    await s.svc.send_with_mesh_fallback(
        to_instance_id=seats[0],
        event_type=FederationEventType.SPACE_SYNC_CHUNK,
        payload={"sync_id": "sync-1", "chunk": "{}"},
        space_id=_SPACE,
    )
    hop.send.assert_awaited_once()
