"""v_87 — a space event too large for a JSON envelope on the GFS relay
(~191 KiB) reaches a household behind a GFS as ONE binary relay frame (the
v_87 frame): the same event type, the same signature and encryption, the
payload as the frame's bytes — host → member, member → host, mailbox seats,
live and from the outbox, and for a §25.6 sync chunk.

Both ends are real here: :class:`FederationService`,
:class:`FederationTransport` + :class:`GfsRelayTransport` on the sender,
:class:`GfsRelayInbound` + the §24.11 pipeline on the receiver. Only the GFS
is a stand-in — it records what it is handed (the recipient and the bytes),
applies the relay's own shape checks and pushes each frame on.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import os
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from socialhome.app import _redeliver_envelope
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.errors import SpaceItemTooLargeError
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DELIVERY_ERROR_RELAY_THROTTLED,
    DELIVERY_ERROR_RELAY_TOO_LARGE,
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.outbox import DeliveryOutcome
from socialhome.domain.relay_media_frame import (
    RELAY_EVENT_FRAME_ALLOWANCE_BYTES,
    RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES,
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES,
    RELAY_MEDIA_MAX_BODY_BYTES,
    RELAY_MEDIA_MAX_PLAINTEXT_BYTES,
    SPACE_TEXT_MAX_BYTES_BEHIND_GFS,
    SYNC_CHUNK_WRAP_ALLOWANCE_BYTES,
    parse_relay_media_body,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_mailbox import derive_mailbox_keys
from socialhome.federation.gfs_relay_media import seal_relay_media
from socialhome.federation.gfs_relay_transport import (
    RELAY_MAX_BODY_BYTES,
    RELAY_MAX_ENVELOPE_BYTES,
    GfsRelayTransport,
    MailboxTarget,
)
from socialhome.federation.invite_bootstrap import EnvelopeRelayThrottled
from socialhome.federation.media_envelope import (
    RELAY_FRAME_EVENT_TYPES,
    build_media_envelope,
    encode_media_body,
)
from socialhome.federation.relay_event_body import (
    EVENT_BODY_SUITE_FIELD,
    EVENT_BODY_SUITE_JSON,
    UnsupportedEventBodySuite,
)
from socialhome.federation.transport import FederationTransport
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.outbox_processor import OutboxProcessor
from socialhome.services.gfs_relay_inbound import GfsRelayInbound
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
    _make_kek_manager,
)
from tests.services.test_gfs_relay_inbound import _GfsRepo, _Invite

GFS = "https://gfs.example"
#: Long and distinctive: the leak check searches what the GFS was handed.
SPACE = "space-large-events-leak-check-0001"
PAGE = FederationEventType.SPACE_PAGE_CREATED
CHUNK = FederationEventType.SPACE_SYNC_CHUNK
V87 = FederationCapability.MIN_FOR_RELAY_MEDIA


def _page(kib: int = 240) -> dict:
    """A page as ``SPACE_PAGE_CREATED`` carries it: the content is
    ciphertext, base64'ed (so *kib* of page text is 4/3 of it here)."""
    return {
        "space_id": SPACE,
        "page_id": "page-large-1",
        "title": "The long page",
        "content": base64.b64encode(os.urandom(kib * 1024)).decode(),
    }


def _sync_chunk(kib: int = 240) -> dict:
    """A §25.6 chunk over the relay: the serialised chunk envelope (one
    record — a page of *kib* — encrypted and base64'ed) as a string."""
    record = base64.b64encode(os.urandom(kib * 1024)).decode()
    return {
        "sync_id": "sync-1",
        "chunk": orjson.dumps(
            {"resource": "pages", "encrypted_payload": record}
        ).decode(),
    }


class _Gfs:
    """The relay: sees a recipient id and bytes, pushes them on unchanged."""

    def __init__(self) -> None:
        self.houses: dict[str, _House] = {}
        self.mailboxes: dict[str, _House] = {}
        self.media_capable = True
        self.throttle_media = False
        #: ``(kind, recipient, wire bytes)`` of everything accepted.
        self.seen: list[tuple[str, str, bytes]] = []
        self._queue: deque = deque()

    # ── RelayEnvelopeSender ──
    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        raw = orjson.dumps(envelope)
        assert len(raw) <= RELAY_MAX_BODY_BYTES  # the GFS would answer 413
        self.seen.append(("json", to_instance_id, raw))
        self._queue.append((to_instance_id, {"type": "envelope", **envelope}, False))
        return True

    async def media_connection(self, gfs_url):
        return object() if self.media_capable else None

    async def send_media_body(self, *, body, gfs_url):
        if self.throttle_media:
            raise EnvelopeRelayThrottled("slow down")
        to, _sealed = parse_relay_media_body(body)  # the GFS's own check
        self.seen.append(("media", to, body))
        self._queue.append((to, {"type": "envelope_media", "body": body}, False))
        return True

    async def mailbox_connection(self, gfs_url):
        return object()

    async def send_to_mailbox(
        self, *, mailbox_id, gfs_url, sealed=None, media_body=None
    ):
        if media_body is not None:
            if self.throttle_media:
                raise EnvelopeRelayThrottled("slow down")
            to, _sealed = parse_relay_media_body(media_body)
            assert to == mailbox_id
            self.seen.append(("media", mailbox_id, media_body))
            self._queue.append((mailbox_id, media_body, True))
        else:
            raw = orjson.dumps({"to_instance": mailbox_id, "sealed": sealed})
            assert len(raw) <= RELAY_MAX_BODY_BYTES
            self.seen.append(("json", mailbox_id, raw))
            self._queue.append(
                (mailbox_id, {"type": "envelope", "sealed": sealed}, True)
            )
        return True

    async def multi_connection(self, gfs_url):
        return None

    def multi_max_recipients(self, conn) -> int:
        return 0

    # ── delivery ──
    async def deliver(self) -> list:
        """Push every queued frame to its recipient; the answers (a
        refusal is the raised ``ValueError``)."""
        answers: list = []
        while self._queue:
            to, frame, mailbox = self._queue.popleft()
            try:
                if mailbox:
                    house = self.mailboxes[to]
                    answers.append(
                        await house.inbound.handle_mailbox_frame(
                            frame, keys=house.mailbox
                        )
                    )
                else:
                    answers.append(
                        await self.houses[to].inbound.handle_frame(frame, gfs_url=GFS)
                    )
            except ValueError as exc:
                answers.append(exc)
        return answers


class _House:
    """One household: real service, real relay transport, real relay
    inbound, its key-wrap key and its GFS mailbox."""

    def __init__(self, gfs: _Gfs) -> None:
        self.kp = generate_identity_keypair()
        self.id = derive_instance_id(self.kp.public_key)
        self.km = _make_kek_manager()
        self.repo = InMemoryFederationRepo()
        self.outbox = InMemoryOutboxRepo()
        self.svc = FederationService(
            db=None,  # type: ignore[arg-type]
            federation_repo=self.repo,
            outbox_repo=self.outbox,
            key_manager=self.km,
            bus=EventBus(),
            own_instance_id=self.id,
            own_identity_seed=self.kp.private_key,
            own_identity_pk=self.kp.public_key,
            http_client=None,
        )
        self.kw = generate_x25519_keypair()
        self.mailbox = derive_mailbox_keys(os.urandom(32), "g" * 32, 0)
        self.cards: set[tuple[str, str]] = set()
        self.targets: dict[str, MailboxTarget] = {}
        self.received: list = []

        async def _card(sender: str, space_id: str) -> bool:
            return (sender, space_id) in self.cards

        self.svc._mailbox_sender_check = _card

        async def _record(event) -> None:
            self.received.append(event)

        for event_type in (PAGE, CHUNK, FederationEventType.SPACE_MEDIA_BLOB):
            self.svc._event_registry._handlers[event_type] = [_record]

        async def _resolve(instance_id, space_id):
            return self.targets.get(instance_id)

        async def _signal(*_a, **_kw):  # pragma: no cover - no RTC here
            raise AssertionError("no signalling in a relay test")

        self.transport = FederationTransport(
            own_instance_id=self.id,
            https_inbox=object(),  # type: ignore[arg-type]
            gfs_relay=GfsRelayTransport(relay_sender=gfs),
            mailbox_targets=_resolve,
            signaling_send=_signal,
        )
        self.svc.attach_transport(self.transport)
        self.inbound = GfsRelayInbound(
            federation=self.svc,
            keywrap_private_key=self.kw.private_key,
            invite_coordinator=_Invite(),  # type: ignore[arg-type]
            gfs_connection_repo=_GfsRepo(),  # type: ignore[arg-type]
        )
        gfs.houses[self.id] = self
        gfs.mailboxes[self.mailbox.mailbox_id] = self

    def seat(self, peer: _House, session: bytes, *, mailbox: bool, version: int):
        """Our row for *peer*: a household we reach only through the GFS —
        by its relay (``relay_via``) or, seated from a host's directory, by
        its mailbox."""
        self.repo._instances[peer.id] = RemoteInstance(
            id=peer.id,
            display_name="peer",
            remote_identity_pk=peer.kp.public_key.hex(),
            key_self_to_remote=self.km.encrypt(session),
            key_remote_to_self=self.km.encrypt(session),
            remote_inbox_url="",
            local_inbox_id="l-" + peer.id[:8],
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.SPACE_SESSION,
            relay_via="" if mailbox else GFS,
            remote_keywrap_pk=peer.kw.public_key.hex(),
            proto_version=version,
        )
        self.repo.add_space_member(SPACE, peer.id)
        if mailbox:
            self.targets[peer.id] = MailboxTarget(
                mailbox_id=peer.mailbox.mailbox_id,
                kem_pk=peer.mailbox.kem_public,
                gfs_url=GFS,
            )
            self.cards.add((peer.id, SPACE))


@dataclasses.dataclass
class _World:
    gfs: _Gfs
    host: _House
    member: _House

    async def send(self, sender: _House, to: _House, event_type, payload):
        return await sender.svc.send_event(
            to_instance_id=to.id,
            event_type=event_type,
            payload=payload,
            space_id=SPACE,
        )


def _world(
    *, mailbox: bool = False, host_version: int = OURS, member_version: int = OURS
) -> _World:
    """A host and a member that reach each other only through the GFS."""
    gfs = _Gfs()
    host, member = _House(gfs), _House(gfs)
    session = os.urandom(32)
    host.seat(member, session, mailbox=mailbox, version=member_version)
    member.seat(host, session, mailbox=mailbox, version=host_version)
    return _World(gfs=gfs, host=host, member=member)


def test_the_gate_is_the_v87_relay_frame_capability():
    assert V87 == 87 and V87 <= OURS


# ── The gap, closed: host → member, member → host, relay seat and mailbox ──


@pytest.mark.parametrize("mailbox", [False, True], ids=["relay-seat", "mailbox"])
@pytest.mark.parametrize("direction", ["host-to-member", "member-to-host"])
async def test_a_240_kib_page_crosses_the_gfs_as_one_frame(mailbox, direction):
    w = _world(mailbox=mailbox)
    sender, to = (
        (w.host, w.member) if direction == "host-to-member" else (w.member, w.host)
    )
    page = _page()
    result = await w.send(sender, to, PAGE, page)
    assert result.ok and result.via == "gfs_relay"
    [(kind, recipient, wire)] = w.gfs.seen
    assert kind == "media" and len(wire) <= RELAY_MEDIA_MAX_BODY_BYTES
    assert recipient == (to.mailbox.mailbox_id if mailbox else to.id)
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = to.received
    assert event.event_type is PAGE and event.from_instance == sender.id
    assert event.space_id == SPACE
    # Byte-identical, and a handler sees an ordinary event: no frame bytes,
    # no frame bookkeeping in the payload.
    assert event.payload == page
    assert event.media_bytes is None


async def test_a_small_page_keeps_the_json_envelope():
    w = _world()
    assert (await w.send(w.host, w.member, PAGE, _page(8))).ok
    assert [kind for kind, _to, _wire in w.gfs.seen] == ["json"]
    assert await w.gfs.deliver() == [{"status": "ok"}]
    assert w.member.received[0].payload["page_id"] == "page-large-1"


@pytest.mark.parametrize("mailbox", [False, True], ids=["relay-seat", "mailbox"])
async def test_a_sync_chunk_over_the_envelope_cap_crosses_as_one_frame(mailbox):
    """§25.6: a catch-up whose chunk holds one large record."""
    w = _world(mailbox=mailbox)
    chunk = _sync_chunk()
    assert len(orjson.dumps(chunk)) > RELAY_MAX_ENVELOPE_BYTES
    result = await w.send(w.host, w.member, CHUNK, chunk)
    assert result.ok and result.via == "gfs_relay"
    assert [kind for kind, _to, _wire in w.gfs.seen] == ["media"]
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = w.member.received
    assert event.event_type is CHUNK and event.payload == chunk


# ── What the GFS learns: a recipient and a size bucket ────────────────────


async def test_the_gfs_sees_a_recipient_and_a_bucket_nothing_else():
    w = _world()
    await w.send(w.host, w.member, PAGE, _page(200))
    await w.send(w.host, w.member, PAGE, _page(240))
    sizes = {len(wire) for _kind, _to, wire in w.gfs.seen}
    assert len(sizes) == 1  # 200 and 240 KiB of page: one bucket
    for _kind, _to, wire in w.gfs.seen:
        for needle in (w.host.id, SPACE, PAGE.value, "page-large-1"):
            assert needle.encode() not in wire


# ── Bounds: what cannot fit is refused at the sender ──────────────────────


async def test_an_event_no_frame_can_carry_is_refused_at_the_sender(caplog):
    w = _world()
    with caplog.at_level(logging.WARNING):
        result = await w.send(w.host, w.member, PAGE, _page(480))
    assert not result.ok and result.error == DELIVERY_ERROR_RELAY_TOO_LARGE
    assert w.gfs.seen == [] and w.host.outbox.enqueued == []
    assert "does not fit" in caplog.text and PAGE.value in caplog.text


# ── Versioning: an older peer or GFS keeps today's refusal, loudly ────────


@pytest.mark.parametrize("why", ["older-peer", "gfs-without-envelope-media"])
async def test_without_v87_or_envelope_media_the_large_event_is_refused(why, caplog):
    w = _world(member_version=V87 - 1 if why == "older-peer" else OURS)
    w.gfs.media_capable = why == "older-peer"
    with caplog.at_level(logging.WARNING):
        result = await w.send(w.host, w.member, PAGE, _page())
    assert not result.ok and result.error == DELIVERY_ERROR_RELAY_TOO_LARGE
    assert w.gfs.seen == [] and w.host.outbox.enqueued == []
    assert w.member.id in caplog.text
    assert not await w.host.svc.relay_event_frames_ok(
        w.host.repo._instances[w.member.id]
    )


async def test_large_event_reaches_says_whether_a_frame_would_carry_it():
    """The check a local action runs before it stores anything."""
    w = _world(host_version=V87 - 1)
    assert await w.host.svc.large_event_reaches(w.member.id)
    assert not await w.member.svc.large_event_reaches(w.host.id)
    # A household that is not behind a GFS takes large events as ever.
    assert await w.member.svc.large_event_reaches("z" * 32)


# ── A throttled frame waits in the outbox and goes out as a frame again ───


async def test_a_throttled_frame_is_queued_and_redelivered_as_a_frame():
    w = _world()
    page = _page()
    w.gfs.throttle_media = True
    result = await w.send(w.host, w.member, PAGE, page)
    assert not result.ok and result.error == DELIVERY_ERROR_RELAY_THROTTLED
    assert w.gfs.seen == []
    [entry] = w.host.outbox.entries()
    w.gfs.throttle_media = False
    outcome = await _redeliver_envelope(w.host.svc, w.host.repo, entry)
    assert outcome is DeliveryOutcome.SUCCESS
    assert [kind for kind, _to, _wire in w.gfs.seen] == ["media"]
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = w.member.received
    assert event.payload == page
    # The same row again (the GFS's answer was lost): one message to the
    # receiver's replay cache.
    assert (
        await _redeliver_envelope(w.host.svc, w.host.repo, entry)
        is DeliveryOutcome.SUCCESS
    )
    [answer] = await w.gfs.deliver()
    assert isinstance(answer, ValueError) and "Replay" in str(answer)
    assert len(w.member.received) == 1


async def test_a_queued_large_row_for_an_older_peer_is_dropped_not_retried():
    w = _world()
    w.gfs.throttle_media = True
    await w.send(w.host, w.member, PAGE, _page())
    [entry] = w.host.outbox.entries()
    row = w.host.repo._instances[w.member.id]
    w.host.repo._instances[w.member.id] = dataclasses.replace(
        row, proto_version=V87 - 1
    )
    assert (
        await _redeliver_envelope(w.host.svc, w.host.repo, entry)
        is DeliveryOutcome.PERMANENT
    )


# ── Receive: the frame is a second door for nothing ───────────────────────


def _frame(sender: _House, to: _House, event_type, payload: dict, **meta) -> dict:
    """A relay frame built by hand: *payload* as the frame's bytes."""
    row = sender.repo._instances[to.id]
    header, sealed = build_media_envelope(
        encoder=sender.svc._encoder,
        session_key=sender.km.decrypt(row.key_self_to_remote),
        from_instance=sender.id,
        to_instance=to.id,
        sig_suite=row.sig_suite or sender.svc._encoder.sig_suite,
        event_type=event_type,
        payload={EVENT_BODY_SUITE_FIELD: EVENT_BODY_SUITE_JSON, **meta},
        raw_chunk=orjson.dumps(payload),
        space_id=SPACE,
    )
    body = seal_relay_media(
        to_instance=to.id,
        media_body=encode_media_body(header, sealed),
        recipient_keywrap_pub=to.kw.public_key,
    )
    return {"type": "envelope_media", "body": body}


def test_a_frame_carries_media_chunks_and_the_seat_vocabulary_only():
    assert RELAY_FRAME_EVENT_TYPES == SPACE_SESSION_ALLOWED_EVENT_TYPES | {
        FederationEventType.DM_MEDIA_BLOB
    }
    assert FederationEventType.SPACE_MEDIA_BLOB in RELAY_FRAME_EVENT_TYPES


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.DM_MESSAGE,
        FederationEventType.PRESENCE_UPDATED,
        FederationEventType.SPACE_CREATED,
        FederationEventType.USER_UPDATED,
    ],
)
async def test_a_frame_of_an_event_outside_the_seat_vocabulary_is_refused(event_type):
    w = _world()
    with pytest.raises(ValueError, match="not allowed here"):
        await w.member.inbound.handle_frame(
            _frame(w.host, w.member, event_type, {"x": 1}), gfs_url=GFS
        )
    assert w.member.received == []


async def test_a_framed_event_runs_the_ban_gate_on_its_real_payload():
    """The gates after the decrypt judge the payload in the frame's bytes —
    here the ban check, which reads the payload's own ``space_id``."""
    w = _world()
    w.member.repo._bans.add(("space-the-sender-is-banned-from", w.host.id))
    page = {**_page(100), "space_id": "space-the-sender-is-banned-from"}
    frame = _frame(w.host, w.member, PAGE, page)
    assert await w.member.inbound.handle_frame(frame, gfs_url=GFS) == {"status": "ok"}
    assert w.member.received == []  # dropped by the gate, answered like any


async def test_a_framed_event_is_deduplicated_by_its_idempotency_key():
    w = _world()

    class _Cache:
        def __init__(self) -> None:
            self.seen: set = set()

        def check_and_mark(self, key) -> bool:
            fresh = key not in self.seen
            self.seen.add(key)
            return fresh

    w.member.svc.attach_idempotency_cache(_Cache())
    page = {**_page(100), "idempotency_key": "once"}
    for _ in range(2):
        frame = _frame(w.host, w.member, PAGE, page)
        assert await w.member.inbound.handle_frame(frame, gfs_url=GFS) == {
            "status": "ok"
        }
    assert len(w.member.received) == 1


@pytest.mark.parametrize(
    "raw", [b"x" * 100_000, b"[" + b"1," * 50_000 + b"1]", b'"' + b"t" * 100_000 + b'"']
)
async def test_a_frame_whose_bytes_are_not_a_json_object_is_refused(raw):
    w = _world()
    row = w.host.repo._instances[w.member.id]
    header, sealed = build_media_envelope(
        encoder=w.host.svc._encoder,
        session_key=w.host.km.decrypt(row.key_self_to_remote),
        from_instance=w.host.id,
        to_instance=w.member.id,
        sig_suite=row.sig_suite or w.host.svc._encoder.sig_suite,
        event_type=PAGE,
        payload={EVENT_BODY_SUITE_FIELD: EVENT_BODY_SUITE_JSON},
        raw_chunk=raw,
        space_id=SPACE,
    )
    body = seal_relay_media(
        to_instance=w.member.id,
        media_body=encode_media_body(header, sealed),
        recipient_keywrap_pub=w.member.kw.public_key,
    )
    with pytest.raises(ValueError, match="not JSON|not a JSON object"):
        await w.member.inbound.handle_frame(
            {"type": "envelope_media", "body": body}, gfs_url=GFS
        )
    assert w.member.received == []


async def test_a_frame_whose_bytes_were_swapped_is_refused():
    w = _world()
    row = w.host.repo._instances[w.member.id]
    session = w.host.km.decrypt(row.key_self_to_remote)
    header, _sealed = build_media_envelope(
        encoder=w.host.svc._encoder,
        session_key=session,
        from_instance=w.host.id,
        to_instance=w.member.id,
        sig_suite=row.sig_suite or w.host.svc._encoder.sig_suite,
        event_type=PAGE,
        payload={EVENT_BODY_SUITE_FIELD: EVENT_BODY_SUITE_JSON},
        raw_chunk=orjson.dumps(_page(4)),
        space_id=SPACE,
    )
    other = w.host.svc._encoder.encrypt_bytes(
        orjson.dumps({**_page(4), "title": "swapped"}), session
    )
    body = seal_relay_media(
        to_instance=w.member.id,
        media_body=encode_media_body(header, other),
        recipient_keywrap_pub=w.member.kw.public_key,
    )
    with pytest.raises(ValueError, match="sha256 mismatch"):
        await w.member.inbound.handle_frame(
            {"type": "envelope_media", "body": body}, gfs_url=GFS
        )
    assert w.member.received == []


async def test_a_mailbox_frame_from_a_sender_without_a_card_is_refused():
    w = _world(mailbox=True)
    w.member.cards.clear()
    assert (await w.send(w.host, w.member, PAGE, _page())).ok
    [answer] = await w.gfs.deliver()
    assert isinstance(answer, ValueError) and "no member card" in str(answer)
    assert w.member.received == []


async def test_a_media_chunk_still_arrives_with_its_bytes():
    """The frame's first use is unchanged: a chunk's bytes stay bytes."""
    w = _world()
    raw = os.urandom(300 * 1024)
    result = await w.host.svc.send_media_chunk(
        to_instance_id=w.member.id,
        event_type=FederationEventType.SPACE_MEDIA_BLOB,
        payload={"filename": "img.webp", "transfer_id": "t1"},
        raw_chunk=raw,
        space_id=SPACE,
        mesh_fallback=True,
    )
    assert result.ok
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = w.member.received
    assert event.media_bytes == raw and event.payload["filename"] == "img.webp"


async def test_queued_results_keep_their_error_for_small_events():
    """A small event to a seat whose relay is down still queues as ever."""
    w = _world()

    async def _down(**_kw):
        return False

    w.gfs.send_sealed_envelope = _down  # type: ignore[method-assign]
    result = await w.send(w.host, w.member, PAGE, _page(4))
    assert result.error == DELIVERY_ERROR_QUEUED


# ── v_91: a large event is not grouped — one frame per recipient ──────────


async def test_a_large_broadcast_to_mailbox_seats_goes_per_recipient_by_frame():
    """Two mailbox seats behind one GFS that takes multi-recipient uploads:
    a small event is ONE upload for both; a large one cannot ride that JSON
    upload, is ungrouped and reaches each seat as its own binary frame."""
    gfs = _Gfs()
    uploads: list = []

    async def _multi_connection(gfs_url):
        return object()

    async def _send_to_mailboxes(*, entries, gfs_url, body):
        uploads.append(entries)
        return 202

    gfs.multi_connection = _multi_connection  # type: ignore[method-assign]
    gfs.multi_max_recipients = lambda conn: 500  # type: ignore[method-assign]
    gfs.send_to_mailboxes = _send_to_mailboxes  # type: ignore[attr-defined]
    host, one, two = _House(gfs), _House(gfs), _House(gfs)
    for member in (one, two):
        session = os.urandom(32)
        host.seat(member, session, mailbox=True, version=OURS)
        member.seat(host, session, mailbox=True, version=OURS)

    async def _single(entry):
        return await _redeliver_envelope(host.svc, host.repo, entry)

    async def _drain() -> None:
        repo = MagicMock()
        repo.list_due = AsyncMock(return_value=host.outbox.entries())
        repo.mark_delivered = AsyncMock()
        repo.mark_failed = AsyncMock()
        repo.reschedule = AsyncMock()
        await OutboxProcessor(repo, _single, multi=host.svc).drain_once()

    page = _page()
    result = await host.svc.broadcast_to_space_members(SPACE, PAGE, page)
    assert {r.error for r in result.results} == {DELIVERY_ERROR_QUEUED}
    await _drain()  # the multi frame cannot be built: ungrouped
    await _drain()  # each row on its own
    assert uploads == []
    assert sorted(to for kind, to, _wire in gfs.seen if kind == "media") == sorted(
        m.mailbox.mailbox_id for m in (one, two)
    )
    assert [kind for kind, _to, _wire in gfs.seen] == ["media", "media"]
    await gfs.deliver()
    for member in (one, two):
        [event] = member.received
        assert event.payload == page


async def test_a_framed_event_that_fits_a_json_envelope_is_refused():
    """The frame is no second lane for ordinary events."""
    w = _world()
    with pytest.raises(ValueError, match="small enough for a JSON envelope"):
        await w.member.inbound.handle_frame(
            _frame(w.host, w.member, PAGE, _page(4)), gfs_url=GFS
        )
    assert w.member.received == []


@pytest.mark.parametrize("decoy", ["space_id", "idempotency_key", "page_id"])
async def test_a_framed_event_with_fields_next_to_its_bytes_is_refused(decoy):
    """One place for the payload: the signed metadata holds the binding
    fields only, so no early gate can be shown a different payload."""
    w = _world()
    with pytest.raises(ValueError, match="more than the binding"):
        await w.member.inbound.handle_frame(
            _frame(w.host, w.member, PAGE, _page(100), **{decoy: "decoy"}),
            gfs_url=GFS,
        )
    assert w.member.received == []


async def test_a_framed_event_from_a_paired_peer_needs_the_relay_opt_in():
    """Not a household behind a GFS: the relay opt-in gate judges it, as
    for a JSON envelope on the relay."""
    w = _world()
    row = w.member.repo._instances[w.host.id]
    w.member.repo._instances[w.host.id] = dataclasses.replace(
        row, source=InstanceSource.MANUAL, gfs_relay=False
    )
    with pytest.raises(ValueError, match="GFS relay is not enabled"):
        await w.member.inbound.handle_frame(
            _frame(w.host, w.member, PAGE, _page(100)), gfs_url=GFS
        )
    assert w.member.received == []


# ── One bound: what goes live also heals by sync ──────────────────────────


def _payload_of(size: int) -> dict:
    """A page payload whose JSON is exactly *size* bytes."""
    base = {"space_id": SPACE, "page_id": "page-at-the-bound", "content": ""}
    payload = {**base, "content": "x" * (size - len(orjson.dumps(base)))}
    assert len(orjson.dumps(payload)) == size
    return payload


def _chunk_of(record: dict) -> dict:
    """The §25.6 chunk event that carries *record* as its one record, the
    way ``ChunkBuilder`` and ``encrypt_chunk`` build it: the records JSON
    AES-GCM-sealed, ``b64url(nonce):b64url(ciphertext)`` inside a signed
    chunk envelope, serialised into the event."""
    ciphertext = orjson.dumps({"records": [record], "chunk_index": 3}) + os.urandom(16)
    envelope = {
        "sync_id": "s" * 32,
        "resource": "pages",
        "space_id": SPACE,
        "epoch": 7,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": b64url_encode(os.urandom(12))
        + ":"
        + b64url_encode(ciphertext),
        # A hybrid signature set: the larger of the two shapes.
        "signatures": {"ed25519": "s" * 88, "mldsa65": "p" * 4412},
    }
    return {"sync_id": "s" * 32, "chunk": orjson.dumps(envelope).decode()}


@pytest.mark.parametrize("mailbox", [False, True], ids=["relay-seat", "mailbox"])
async def test_a_payload_at_the_bound_goes_live_and_heals_by_sync(mailbox):
    """ONE bound: the live bound is derived from the frame's so that the
    same item fits the sync chunk. The GFS drops the live frame (its media
    queue keeps a frame one hour); the catch-up still brings the item — its
    record's ciphertext rides the frame raw, so the chunk costs only its
    wrapper."""
    w = _world(mailbox=mailbox)
    assert RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES >= 500 * 1024
    page = _payload_of(RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES)
    assert (await w.send(w.host, w.member, PAGE, page)).ok
    w.gfs._queue.clear()  # accepted with a 202, then dropped by the GFS
    assert w.member.received == []
    # The record of the same page, with the fields a record adds.
    record = {**page, "version_hash": "h" * 64, "updated_at": "t" * 32, "seq": 9}
    chunk = _chunk_of(record)
    # As JSON the chunk would not fit any frame; raw it does.
    assert len(orjson.dumps(chunk)) > RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES
    result = await w.send(w.host, w.member, CHUNK, chunk)
    assert result.ok and result.via == "gfs_relay"
    assert len(w.gfs.seen[-1][2]) <= RELAY_MEDIA_MAX_BODY_BYTES
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = w.member.received
    assert event.event_type is CHUNK and event.payload == chunk
    assert event.media_bytes is None


async def test_a_sync_chunk_to_a_paired_peer_is_not_touched():
    """The raw layout exists inside a relay frame only: toward a paired
    peer (DataChannel / inbox) the rule does not apply at all."""
    w = _world()
    row = dataclasses.replace(
        w.host.repo._instances[w.member.id], source=InstanceSource.MANUAL
    )
    chunk = _chunk_of(_payload_of(300 * 1024))
    envelope = w.host.svc._seal_envelope(
        row, event_type=CHUNK, payload=chunk, space_id=SPACE
    )
    assert (
        await w.host.svc.relay_event_frame(
            row,
            envelope,
            event_type=CHUNK,
            payload_json=lambda: orjson.dumps(chunk).decode(),
            space_id=SPACE,
        )
        is None
    )
    assert w.gfs.seen == []


async def test_a_framed_event_without_a_known_body_suite_is_refused():
    w = _world()
    for meta in ({EVENT_BODY_SUITE_FIELD: "json-v9"}, {EVENT_BODY_SUITE_FIELD: None}):
        row = w.host.repo._instances[w.member.id]
        header, sealed = build_media_envelope(
            encoder=w.host.svc._encoder,
            session_key=w.host.km.decrypt(row.key_self_to_remote),
            from_instance=w.host.id,
            to_instance=w.member.id,
            sig_suite=row.sig_suite or w.host.svc._encoder.sig_suite,
            event_type=PAGE,
            payload=meta,
            raw_chunk=orjson.dumps(_page(100)),
            space_id=SPACE,
        )
        body = seal_relay_media(
            to_instance=w.member.id,
            media_body=encode_media_body(header, sealed),
            recipient_keywrap_pub=w.member.kw.public_key,
        )
        with pytest.raises(UnsupportedEventBodySuite):
            await w.member.inbound.handle_frame(
                {"type": "envelope_media", "body": body}, gfs_url=GFS
            )
    assert w.member.received == []


async def test_a_live_payload_over_the_bound_is_refused_though_a_frame_could_take_it(
    caplog,
):
    w = _world()
    page = _payload_of(RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES + 1)
    with caplog.at_level(logging.WARNING):
        result = await w.send(w.host, w.member, PAGE, page)
    assert not result.ok and result.error == DELIVERY_ERROR_RELAY_TOO_LARGE
    assert w.gfs.seen == [] and w.host.outbox.enqueued == []
    assert "does not fit" in caplog.text


async def test_a_sync_chunk_may_fill_the_frame():
    w = _world()
    chunk = {"sync_id": "s" * 32, "chunk": ""}
    chunk["chunk"] = "c" * (
        RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES - len(orjson.dumps(chunk))
    )
    assert (await w.send(w.host, w.member, CHUNK, chunk)).ok
    assert await w.gfs.deliver() == [{"status": "ok"}]
    over = {**chunk, "chunk": chunk["chunk"] + "c"}
    result = await w.send(w.host, w.member, CHUNK, over)
    assert result.error == DELIVERY_ERROR_RELAY_TOO_LARGE


def test_the_live_bound_is_derived_from_the_frame_bound():
    assert RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES == (
        RELAY_MEDIA_MAX_PLAINTEXT_BYTES - RELAY_EVENT_FRAME_ALLOWANCE_BYTES
    )
    assert RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES == (
        RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES - SYNC_CHUNK_WRAP_ALLOWANCE_BYTES
    )
    assert RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES == 515_998  # ~503 KiB


# ── Only a household behind a GFS frames events ───────────────────────────


async def test_a_framed_event_from_a_paired_peer_with_the_opt_in_is_refused():
    """The sender frames only toward a household behind a GFS; the receiver
    takes a framed event only from one — a paired peer on the relay opt-in
    has its inbox for large events."""
    w = _world()
    row = w.member.repo._instances[w.host.id]
    w.member.repo._instances[w.host.id] = dataclasses.replace(
        row, source=InstanceSource.MANUAL, gfs_relay=True
    )
    with pytest.raises(ValueError, match="not behind a GFS"):
        await w.member.inbound.handle_frame(
            _frame(w.host, w.member, PAGE, _page(100)), gfs_url=GFS
        )
    assert w.member.received == []


# ── A local save is refused up front, never shown as saved ────────────────


async def test_a_text_no_frame_carries_is_refused_before_it_is_stored():
    w = _world()
    fits = "x" * (SPACE_TEXT_MAX_BYTES_BEHIND_GFS - 64)
    await w.host.svc.check_space_text_size(SPACE, "Title", fits)
    with pytest.raises(SpaceItemTooLargeError) as refused:
        await w.host.svc.check_space_text_size(SPACE, "Title", fits + "x" * 128)
    assert refused.value.status == 413
    assert refused.value.code == "SPACE_ITEM_TOO_LARGE"
    assert refused.value.params == {"max_kb": SPACE_TEXT_MAX_BYTES_BEHIND_GFS // 1024}
    # Measured as it travels: JSON escapes count.
    with pytest.raises(SpaceItemTooLargeError):
        await w.host.svc.check_space_text_size(
            SPACE, '"' * (SPACE_TEXT_MAX_BYTES_BEHIND_GFS // 2 + 64)
        )


async def test_a_space_without_a_household_behind_a_gfs_keeps_its_limits():
    w = _world()
    row = w.host.repo._instances[w.member.id]
    w.host.repo._instances[w.member.id] = dataclasses.replace(
        row, source=InstanceSource.MANUAL
    )
    await w.host.svc.check_space_text_size(SPACE, "x" * 900_000)
    await w.host.svc.check_space_text_size("another-space", "x" * 900_000)
