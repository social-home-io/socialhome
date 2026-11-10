"""Migration 0145 — a failed send stores the event payload ONCE (KEK-wrapped)
plus one ref row per recipient; a ref row is sealed for its one peer when it
is sent, and due ref rows of one payload whose peers are mailbox seats on one
GFS go up as ONE multi upload (v_90)."""

from __future__ import annotations

import dataclasses
import os

import orjson

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_relay_transport import RELAY_STATUS_THROTTLED
from socialhome.federation.multi_recipient import (
    decrypt_mr_payload,
    derive_mr_key,
    open_multi,
)
from socialhome.infrastructure import DeliveryOutcome, EventBus, RetryAfter
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
    _make_kek_manager,
    _make_remote_instance,
)
from tests.federation.test_federation_service_multi_mailbox import (
    POST,
    _Sender,
)
from tests.services.test_gfs_relay_inbound import _CONTENT_KEYS, _SPACE

_PAYLOAD = {"post_id": "p-1", "body": "the secret recipe"}


async def _failing_broadcast(s: _Sender, *, seats: int = 0, paired: int = 0):
    ids = [await s.seat() for _ in range(seats)]
    ids += [await s.paired() for _ in range(paired)]
    s.transport.send_ok = False
    s.transport.multi_result = (False, None)
    await s.broadcast(payload=dict(_PAYLOAD))
    return ids


async def test_three_failed_recipients_store_one_payload_and_three_refs():
    s = _Sender()
    peers = await _failing_broadcast(s, paired=3)
    rows = s.outbox.enqueued
    assert sorted(r["instance_id"] for r in rows) == sorted(peers)
    assert {r["payload_json"] for r in rows} == {""}
    [stored] = s.outbox.payloads.values()
    assert {r["payload_id"] for r in rows} == {stored.id}
    assert stored.event_type is POST and stored.space_id == _SPACE
    # KEK-wrapped, bound to its id, event type and space: nothing readable
    # in the database.
    assert b"secret" not in stored.payload_enc.encode()
    ad = f"sh-outbox-payload:v1\n{stored.id}\n{POST.value}\n{_SPACE}".encode()
    assert orjson.loads(s.km.decrypt(stored.payload_enc, associated_data=ad)) == (
        _PAYLOAD
    )
    # The id is a keyed MAC, not a digest anyone can recompute.
    assert len(stored.id) == 64 and _PAYLOAD["post_id"] not in stored.id


async def test_two_broadcasts_of_other_content_store_two_payloads():
    s = _Sender()
    await _failing_broadcast(s, paired=2)
    await s.broadcast(payload={"post_id": "p-2"})
    assert len(s.outbox.payloads) == 2


async def test_a_ref_row_seals_into_an_envelope_the_receiver_accepts():
    km_a, km_b = _make_kek_manager(), _make_kek_manager()
    kp_a, kp_b = generate_identity_keypair(), generate_identity_keypair()
    key = os.urandom(32)
    b_at_a, _ = _make_remote_instance(km_a, peer_kp=kp_b, session_key=key)
    a_at_b, _ = _make_remote_instance(km_b, peer_kp=kp_a, session_key=key)
    repo_a, repo_b = InMemoryFederationRepo(), InMemoryFederationRepo()
    await repo_a.save_instance(b_at_a)
    await repo_b.save_instance(a_at_b)
    outbox = InMemoryOutboxRepo()

    def _svc(repo, km, kp, outbox_repo):
        return FederationService(
            db=None,  # type: ignore[arg-type]
            federation_repo=repo,
            outbox_repo=outbox_repo,
            key_manager=km,
            bus=EventBus(),
            own_instance_id=derive_instance_id(kp.public_key),
            own_identity_seed=kp.private_key,
            own_identity_pk=kp.public_key,
        )

    class _Down:
        def post(self, *a, **kw):
            raise ConnectionError("peer is away")

    sender = _svc(repo_a, km_a, kp_a, outbox)
    sender._http_client = _Down()
    receiver = _svc(repo_b, km_b, kp_b, InMemoryOutboxRepo())
    got: list[dict] = []

    async def _on(event):
        got.append(event.payload)

    receiver._event_registry.register(FederationEventType.USER_UPDATED, _on)

    result = await sender.send_event(
        to_instance_id=b_at_a.id,
        event_type=FederationEventType.USER_UPDATED,
        payload={"user_id": "u-1"},
    )
    assert not result.ok
    [entry] = outbox.entries()
    assert entry.payload_id is not None and entry.payload_json == ""

    body = await sender.seal_outbox_entry(entry, b_at_a)
    envelope = orjson.loads(body)
    assert envelope["msg_id"] == entry.id  # replay-dedup keys on the row
    assert envelope["to_instance"] == b_at_a.id
    assert await receiver.handle_inbound_envelope(
        inbox_id=a_at_b.local_inbox_id, raw_body=body.encode()
    ) == {"status": "ok"}
    assert got == [{"user_id": "u-1"}]


async def test_a_ref_row_whose_payload_is_gone_cannot_be_sealed():
    s = _Sender()
    await _failing_broadcast(s, paired=1)
    [entry] = s.outbox.entries()
    s.outbox.payloads.clear()
    instance = await s.repo.get_instance(entry.instance_id)
    try:
        await s.svc.seal_outbox_entry(entry, instance)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("sealed a row without its payload")


async def test_a_stored_payload_moved_to_another_event_or_space_does_not_unwrap():
    s = _Sender()
    await _failing_broadcast(s, paired=1)
    [entry] = s.outbox.entries()
    [stored] = s.outbox.payloads.values()
    instance = await s.repo.get_instance(entry.instance_id)
    for swapped in (
        dataclasses.replace(stored, event_type=FederationEventType.SPACE_POST_DELETED),
        dataclasses.replace(stored, space_id="sp-other"),
    ):
        s.outbox.payloads[stored.id] = swapped
        try:
            await s.svc.seal_outbox_entry(entry, instance)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"unwrapped a payload moved to {swapped}")


async def test_due_ref_rows_on_one_gfs_go_up_as_one_multi_upload():
    s = _Sender()
    seats = await _failing_broadcast(s, seats=3)
    s.transport.multi.clear()
    entries = s.outbox.entries()
    assert len(entries) == 3

    [group] = await s.svc.outbox_multi_groups(entries)
    assert sorted(e.instance_id for e in group) == sorted(seats)

    s.transport.multi_result = (True, None)
    assert await s.svc.deliver_outbox_multi(group) is DeliveryOutcome.SUCCESS
    [(targets, sealed)] = s.transport.multi
    assert sorted(t.mailbox_id for t in targets) == sorted(
        s.transport.targets[i].mailbox_id for i in seats
    )
    _kid, key = derive_mr_key(_CONTENT_KEYS[_SPACE])
    envelope = open_multi(sealed, key)["envelope"]
    assert orjson.loads(decrypt_mr_payload(envelope["encrypted_payload"], key)) == (
        _PAYLOAD
    )
    assert all(s.svc.last_relay_accepted_at(i) for i in seats)


async def test_a_throttled_group_is_retried_and_a_refused_one_goes_per_recipient():
    s = _Sender()
    await _failing_broadcast(s, seats=2)
    [group] = await s.svc.outbox_multi_groups(s.outbox.entries())
    s.transport.multi_result = (False, RELAY_STATUS_THROTTLED)
    assert isinstance(await s.svc.deliver_outbox_multi(group), RetryAfter)
    s.transport.multi_result = (False, 400)
    assert await s.svc.deliver_outbox_multi(group) is None


async def test_rows_that_are_not_mailbox_seats_or_not_refs_are_never_grouped():
    s = _Sender()
    await _failing_broadcast(s, seats=1, paired=2)
    assert await s.svc.outbox_multi_groups(s.outbox.entries()) == []
    legacy = [
        dataclasses.replace(e, payload_id=None, payload_json="{}")
        for e in s.outbox.entries()
    ]
    assert await s.svc.outbox_multi_groups(legacy) == []


async def test_one_seat_twice_in_a_pass_rides_one_group_only():
    s = _Sender()
    await _failing_broadcast(s, seats=2)
    entries = s.outbox.entries()
    again = [dataclasses.replace(e, id=e.id + "-again") for e in entries]
    groups = await s.svc.outbox_multi_groups(entries + again)
    assert sorted(len(g) for g in groups) == [2, 2]
    for g in groups:
        assert len({e.instance_id for e in g}) == 2
