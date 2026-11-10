"""v_90 — ``broadcast_to_space_members`` uploads ONE multi-recipient frame
for the mailbox seats of a space behind one GFS.

The grouping rules (version, seat kind, GFS capability, identical bytes,
event type, group size) are checked against a recording transport; the last
test runs the real :class:`FederationTransport` + :class:`GfsRelayTransport`
on the sending side and the real receive path (``GfsRelayInbound`` + the
§24.11 pipeline, from ``test_gfs_relay_inbound``) on two receivers.
"""

from __future__ import annotations

import dataclasses
import os
from collections import deque
from datetime import timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_RELAY_THROTTLED,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation import federation_service
from socialhome.federation.gfs_relay_transport import (
    RELAY_STATUS_THROTTLED,
    GfsRelayTransport,
    MailboxTarget,
)
from socialhome.federation.multi_recipient import (
    MULTI_TO_INSTANCE,
    derive_mr_key,
    multi_audience_includes,
    open_multi,
)
from socialhome.federation.transport import FederationTransport, _TransportSendResult
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.services import space_crypto_service as scs
from socialhome.services.space_crypto_service import SpaceContentEncryption
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
    _make_kek_manager,
    _make_remote_instance,
    _make_service,
)
from tests.services.test_gfs_relay_inbound import (
    _CONTENT_KEYS,
    _SPACE,
    _MultiHousehold,
)

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
        self.multi: list[tuple[list[MailboxTarget], dict]] = []
        self.multi_result: tuple[bool, int | None] | Exception = (True, None)
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

    async def send_multi_mailbox(self, *, targets, sealed):
        self.multi.append((list(targets), sealed))
        if isinstance(self.multi_result, Exception):
            raise self.multi_result
        return self.multi_result

    def per_recipient_ids(self) -> list[str]:
        return [iid for iid, _env in self.sends]


class _Sender:
    """A sending household: FederationService + recording transport."""

    def __init__(self, *, mr: bool = True) -> None:
        self.km = _make_kek_manager()
        self.repo = InMemoryFederationRepo()
        self.outbox = InMemoryOutboxRepo()
        self.svc, _kp = _make_service(
            federation_repo=self.repo, outbox_repo=self.outbox, key_manager=self.km
        )
        self.transport = _Transport()
        self.svc.attach_transport(self.transport)
        self.mr_asks: list[str] = []

        async def _mr_key(space_id: str):
            self.mr_asks.append(space_id)
            if not mr or space_id not in _CONTENT_KEYS:
                return None
            kid, key = derive_mr_key(_CONTENT_KEYS[space_id])
            return 7, kid, key

        self.svc.attach_member_mesh(
            host_hop=None,  # type: ignore[arg-type]
            mailbox_sender_check=_never,
            mr_key=_mr_key,
        )

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
            self.transport.targets[inst.id] = MailboxTarget(
                mailbox_id=inst.id[::-1], kem_pk=b"k" * 32, gfs_url=gfs
            )
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
    return sorted(t.mailbox_id for t in upload[0])


def _boxes(s: _Sender, ids) -> list[str]:
    return sorted(s.transport.targets[i].mailbox_id for i in ids)


async def test_three_seats_on_one_gfs_go_up_once_and_the_paired_peer_alone():
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    peer = await s.paired()
    result = await s.broadcast()
    assert result.attempted == 4 and result.succeeded == 4
    [upload] = s.transport.multi
    assert _mailboxes(upload) == _boxes(s, seats)
    assert set(upload[1]) == {"mr_suite", "kid", "ciphertext"}
    assert s.transport.per_recipient_ids() == [peer]
    by_id = {r.instance_id: r for r in result.results}
    assert all(by_id[i].ok and by_id[i].via == "gfs_relay" for i in seats)
    # Accepted by the relay, never "reachable" on that alone.
    assert s.svc.last_relay_accepted_at(seats[0]) is not None
    assert s.repo.reachable_calls == [peer]


async def test_a_seat_below_v90_goes_per_recipient():
    s = _Sender()
    new = [await s.seat() for _ in range(2)]
    old = await s.seat(version=V90 - 1)
    await s.broadcast()
    [upload] = s.transport.multi
    assert _mailboxes(upload) == _boxes(s, new)
    assert s.transport.per_recipient_ids() == [old]


async def test_a_gfs_without_envelope_multi_goes_per_recipient():
    s = _Sender()
    s.transport.capacity[GFS_A] = 0
    seats = [await s.seat() for _ in range(3)]
    await s.broadcast()
    assert s.transport.multi == []
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)
    assert s.mr_asks == []  # no group, no key lookup


async def test_a_single_seat_is_never_a_multi_of_one():
    s = _Sender()
    seat = await s.seat()
    await s.seat(gfs=GFS_B)
    await s.paired()
    await s.broadcast()
    assert s.transport.multi == []
    assert seat in s.transport.per_recipient_ids()


async def test_different_payload_bytes_split_the_groups():
    s = _Sender()
    seats = [await s.seat() for _ in range(4)]

    async def _per_peer(iid: str, payload: dict) -> dict:
        # Two households get one copy, two another.
        return {**payload, "cert": "x" if iid in seats[:2] else "y"}

    await s.broadcast(per_peer=_per_peer)
    assert len(s.transport.multi) == 2
    assert sorted(len(u[0]) for u in s.transport.multi) == [2, 2]
    assert s.transport.sends == []


async def test_a_relay_payload_variant_rides_its_own_group():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    peer = await s.paired()
    await s.broadcast(payload={"big": "x" * 10}, relay_payload={"big": "small"})
    [upload] = s.transport.multi
    assert len(upload[0]) == len(seats)
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
    assert s.transport.multi == []
    assert len(s.transport.sends) == 3


async def test_a_refused_upload_falls_back_per_recipient_for_each():
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    s.transport.multi_result = (False, 400)
    s.transport.send_ok = False
    result = await s.broadcast()
    assert len(s.transport.multi) == 1
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)
    # The per-recipient path queues each in the outbox.
    assert {r.error for r in result.results} == {DELIVERY_ERROR_QUEUED}


async def test_a_throttled_upload_queues_every_seat_without_a_live_retry():
    """The GFS just said it is busy: N per-recipient sends at it now would
    be a storm. Each seat goes straight to the outbox — ONE stored payload,
    one ref row per seat — and reports the waitable throttle."""
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    s.transport.multi_result = (False, RELAY_STATUS_THROTTLED)
    result = await s.broadcast(payload={"post_id": "p-1"})
    assert len(s.transport.multi) == 1
    assert s.transport.sends == []  # no live attempt
    by_id = {r.instance_id: r for r in result.results}
    assert sorted(by_id) == sorted(seats)
    assert all(
        not r.ok
        and r.error == DELIVERY_ERROR_RELAY_THROTTLED
        and r.retry_after_s is not None
        for r in by_id.values()
    )
    rows = s.outbox.enqueued
    assert sorted(r["instance_id"] for r in rows) == sorted(seats)
    [stored] = s.outbox.payloads.values()
    assert {r["payload_id"] for r in rows} == {stored.id}
    assert stored.event_type is POST and stored.space_id == _SPACE
    # Back-pressure on us, not news about the households.
    assert s.repo.unreachable_calls == []


@pytest.mark.parametrize("where", ["build", "upload"])
async def test_a_raising_multi_chunk_falls_back_per_recipient(where, monkeypatch):
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    boom = RuntimeError("boom")
    if where == "build":

        def _raise(*_a, **_kw):
            raise boom

        monkeypatch.setattr(federation_service, "build_multi_envelope", _raise)
    else:
        s.transport.multi_result = boom
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
    assert len(s.transport.multi) == 1 and s.transport.sends == []


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
    """Multi wired or not, a paired-only space costs the same per-target
    reads; the grouping adds one batched read and no capability or key
    lookup."""
    costs = []
    for wired in (False, True):
        s = _Sender(mr=wired)
        if not wired:
            s.svc._mr_key = None
        for _ in range(4):
            await s.paired()
        counts = _count_reads(s)
        await s.broadcast()
        costs.append(counts)
        if wired:
            assert s.transport.capacity_asks == []
            assert s.mr_asks == []
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
    assert len(s.transport.multi) == 2
    assert sorted(s.transport.capacity_asks) == [GFS_A, GFS_B]
    assert counts.get("list_instances_in_space") == 1
    # No per-target row read by the grouping (the multi frame needs none).
    assert counts.get("get_instance", 0) == 0


async def test_no_space_key_means_per_recipient():
    s = _Sender(mr=False)
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast()
    assert s.transport.multi == []
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)


async def test_an_oversize_frame_goes_per_recipient():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    await s.broadcast(payload={"blob": os.urandom(400_000).hex()})
    assert s.transport.multi == []
    assert sorted(s.transport.per_recipient_ids()) == sorted(seats)


async def test_two_gfss_are_two_uploads():
    s = _Sender()
    a = [await s.seat(gfs=GFS_A) for _ in range(2)]
    b = [await s.seat(gfs=GFS_B) for _ in range(2)]
    await s.broadcast()
    assert sorted(_mailboxes(u) for u in s.transport.multi) == sorted(
        [_boxes(s, a), _boxes(s, b)]
    )
    assert s.transport.sends == []


async def test_one_gfs_spelled_two_ways_is_one_group():
    s = _Sender()
    await s.seat(gfs=GFS_A)
    await s.seat(gfs=GFS_A.upper() + "/")
    await s.broadcast()
    assert len(s.transport.multi) == 1 and s.transport.sends == []


async def test_a_group_over_the_gfs_cap_is_chunked_and_a_lone_rest_goes_alone():
    s = _Sender()
    s.transport.capacity[GFS_A] = 2
    for _ in range(5):
        await s.seat()
    order = await s.repo.list_member_instance_ids(_SPACE)
    await s.broadcast()
    assert [len(u[0]) for u in s.transport.multi] == [2, 2]
    # Member order: the last one is the lone rest.
    assert s.transport.per_recipient_ids() == [order[4]]


async def test_a_deferred_member_and_a_mailbox_less_seat_stay_per_recipient():
    s = _Sender()
    seats = [await s.seat() for _ in range(3)]
    loner = await s.seat(mailbox=False)
    s.svc._deferred_mesh_queues[seats[0]] = deque()
    result = await s.broadcast()
    [upload] = s.transport.multi
    assert len(upload[0]) == 2
    assert s.transport.per_recipient_ids() == [loner]
    assert len(result.results) == 4


async def test_the_audience_tags_name_exactly_the_group():
    s = _Sender()
    seats = [await s.seat() for _ in range(2)]
    outsider = await s.paired()
    await s.broadcast()
    [(_targets, sealed)] = s.transport.multi
    _kid, key = derive_mr_key(_CONTENT_KEYS[_SPACE])
    env = open_multi(sealed, key)["envelope"]
    assert env["to_instance"].startswith(MULTI_TO_INSTANCE + ":")
    assert all(multi_audience_includes(env["to_instance"], key, i) for i in seats)
    assert not multi_audience_includes(env["to_instance"], key, outsider)


# ── End to end: real sending transport, real receivers ──────────────────


class _MultiSender:
    """The ``RelayEnvelopeSender`` seam: a GFS proving ``envelope_multi``."""

    def __init__(self) -> None:
        self.uploads: list[tuple[list[str], str, dict]] = []

    async def mailbox_connection(self, gfs_url):
        return object()

    async def multi_connection(self, gfs_url):
        return object()

    def multi_max_recipients(self, conn) -> int:
        return 500

    async def send_to_mailboxes(self, *, mailbox_ids, gfs_url, sealed):
        self.uploads.append((list(mailbox_ids), gfs_url, sealed))
        return True

    async def send_to_mailbox(self, **_kw):  # pragma: no cover - not reached
        raise AssertionError("per-recipient send in a multi test")


async def test_a_frame_built_by_the_broadcast_is_taken_by_every_receiver():
    receivers = [_MultiHousehold(), _MultiHousehold()]
    kp = generate_identity_keypair()
    sender_id = derive_instance_id(kp.public_key)
    km = _make_kek_manager()
    repo = InMemoryFederationRepo()
    svc, _ = _make_service(federation_repo=repo, key_manager=km)
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
            mailbox_id=rx.mailbox.mailbox_id, kem_pk=b"k" * 32, gfs_url=GFS_A
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

    async def _mr_key(space_id: str):
        kid, key = derive_mr_key(_CONTENT_KEYS[space_id])
        return 7, kid, key

    svc.attach_member_mesh(
        host_hop=None,  # type: ignore[arg-type]
        mailbox_sender_check=_never,
        mr_key=_mr_key,
    )

    result = await svc.broadcast_to_space_members(
        _SPACE, POST, {"post_id": "p-9", "content": "hi all"}
    )
    assert result.succeeded == 2
    [(ids, url, sealed)] = gfs.uploads
    assert sorted(ids) == sorted(t.mailbox_id for t in targets.values())
    assert url == GFS_A
    for rx in receivers:
        assert await rx.frame({"type": "envelope", "sealed": sealed}) == {
            "status": "ok"
        }
        [event] = rx.dispatched
        assert event.from_instance == sender_id
        assert event.payload == {"post_id": "p-9", "content": "hi all"}


# ── v_90: no multi frame right after a key rotation ──────────────────────


async def test_a_broadcast_within_ten_minutes_of_a_rotation_makes_no_multi_upload(
    tmp_dir, monkeypatch
):
    """A member still importing the newest epoch drops a frame sealed under
    it (unknown kid) while the GFS's 202 marks it delivered. Inside the
    grace window everything goes per recipient — live and from the outbox."""
    db = AsyncDatabase(tmp_dir / "keys.db", batch_timeout_ms=10)
    await db.startup()
    try:
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'T', 'inst-1', 'pascal', ?)",
            (_SPACE, "ab" * 32),
        )
        crypto = SpaceContentEncryption(
            SqliteSpaceKeyRepo(db), KeyManager.from_data_dir(tmp_dir)
        )
        await crypto.initialise_for_space(_SPACE)
        await crypto.rotate_epoch(_SPACE)
        s = _Sender()
        s.svc._mr_key = crypto.mr_key
        seats = [await s.seat() for _ in range(3)]
        s.transport.send_ok = False
        await s.broadcast()
        assert s.transport.multi == []
        assert sorted(s.transport.per_recipient_ids()) == sorted(seats)
        entries = s.outbox.entries()
        assert len(entries) == 3
        assert await s.svc.outbox_multi_groups(entries) == []

        # Once the window is over, the same space goes up once again.
        real = scs._utcnow()
        monkeypatch.setattr(
            scs,
            "_utcnow",
            lambda: real + timedelta(seconds=scs.MULTI_EPOCH_GRACE_S + 1),
        )
        [group] = await s.svc.outbox_multi_groups(entries)
        assert len(group) == 3
        s.transport.send_ok = True
        await s.broadcast(payload={"post_id": "p-2"})
        assert len(s.transport.multi) == 1
    finally:
        await db.shutdown()
