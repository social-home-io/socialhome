"""The size rule measures the WHOLE item, and the worst case still crosses.

A household behind a GFS gets a large item as one relay frame, live and —
when that frame is lost — as the item's §25.6 sync record. Each kind of
space item has one limit on its record as it travels, every field
(``domain/relay_media_frame.py``); only what other people add after it was
saved is held out, under its own hard cap. These tests build each kind at
exactly its limit — every field at its own cap, adversarial shapes (empty
and 1-character ids, control characters that escape to six bytes,
non-ASCII) — and push it through the REAL builders: the live payload
(``PageFederationOutbound``, ``SpaceCalendarService._publish_federation_event_saved``,
``SpaceConfigOutbound``), the sync record (``PagesExporter``,
``CalendarExporter``) and its chunk (``ChunkBuilder`` with a hybrid
Ed25519 + ML-DSA-65 signature set, the raw body suite), then check the
frame.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import orjson
import pytest

from socialhome.crypto import (
    b64url_encode,
    generate_identity_keypair,
    generate_space_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.calendar import (
    LOCATION_MAX_CHARS,
    MAX_EVENT_ATTENDEES,
    MAX_RSVPS_PER_EVENT,
    RSVP_RECORD_MAX_BYTES,
    CalendarEvent,
    calendar_record,
)
from socialhome.domain.emoji import MAX_EMOJI_CODEPOINTS
from socialhome.domain.events import PageUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.file_hashes import FILES_FIELD, FILES_SUITE_FIELD
from socialhome.domain.gfs_server import MAX_GFS_SERVERS_JSON_BYTES
from socialhome.domain.media_constraints import (
    SPACE_COVER_BOOTSTRAP_MAX_BYTES,
    SPACE_ICON_BOOTSTRAP_MAX_BYTES,
)
from socialhome.domain.page import MAX_PAGE_TITLE_LENGTH
from socialhome.domain.page_version import page_record, version_hash
from socialhome.domain.relay_media_frame import (
    RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES,
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES,
    RELAY_SYNC_RECORD_MAX_BYTES,
    SPACE_CALENDAR_ITEM_MAX_BYTES,
    SPACE_CONFIG_ITEM_MAX_BYTES,
    SPACE_PAGE_ITEM_MAX_BYTES,
    space_item_bytes,
)
from socialhome.domain.space import (
    SPACE_NAME_MAX_CHARS,
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.domain.space_media_ref import (
    MAX_PAGE_DESCRIPTOR_ENTRIES,
    MEDIA_REFS_FIELD,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_media import (
    RELAY_MEDIA_MAX_MEDIA_BODY_BYTES,
    seal_relay_media,
)
from socialhome.federation.media_envelope import (
    build_media_envelope,
    encode_media_body,
)
from socialhome.federation.relay_event_body import (
    EVENT_BODY_SUITE_FIELD,
    EVENT_BODY_SUITE_SYNC_CHUNK_RAW,
    pack_event_body,
    unpack_event_body,
)
from socialhome.federation.sync.space.exporter import ChunkBuilder, serialise_chunk
from socialhome.federation.sync.space.exporters import PagesExporter
from socialhome.federation.sync.space.exporters.calendar import CalendarExporter
from socialhome.repositories.page_repo import new_page
from socialhome.services.calendar_service import (
    SpaceCalendarService,
    calendar_event_too_large,
)
from socialhome.services.moderation_release import release_scope
from socialhome.services.page_conflict_service import (
    MAX_CONFLICT_SIDES,
    PageConflictSide,
    Sequenced,
    canonical_extras,
    page_too_large,
)
from socialhome.services.page_federation_outbound import PageFederationOutbound
from socialhome.services.space_config_outbound import SpaceConfigOutbound
from socialhome.services.space_service import (
    ROSTER_SNAPSHOT_PART_BYTES,
    require_config_fits,
    space_metadata_fields,
)
from socialhome.utils.rrule import MAX_RRULE_CHARS, clean_rrule

SPACE = "s" * 32
ID = "i" * 32
CHUNK = FederationEventType.SPACE_SYNC_CHUNK
HYBRID = "ed25519+mldsa65"
#: An ML-DSA-65 signature (FIPS 204, Table 2: 3309 bytes). liboqs is an
#: optional extra, so the PQ half is signed by a stand-in of the real size.
MLDSA65_SIGNATURE_BYTES = 3309
#: Text that costs the most on the wire per character: control characters
#: escape to six bytes (``\\u0001``), quotes and backslashes to two, and
#: multi-byte UTF-8.
ADVERSARIAL = '\x01\x1f"\\é😀\u2028'


class _MlDsa65:
    def sign(self, message: bytes) -> bytes:
        return os.urandom(MLDSA65_SIGNATURE_BYTES)


def _hybrid_encoder() -> FederationEncoder:
    return FederationEncoder(
        generate_identity_keypair().private_key,
        pq_signer=_MlDsa65(),
        sig_suite=HYBRID,
    )


class _Crypto:
    """``encrypt_chunk`` in the real wire form: ``b64url(nonce):b64url(ct)``
    (AES-GCM adds a 16-byte tag)."""

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 7, (
            b64url_encode(os.urandom(12))
            + ":"
            + b64url_encode(plaintext + os.urandom(16))
        )


class _Exporter:
    def __init__(self, resource: str, records: list[dict]) -> None:
        self.resource = resource
        self._records = records

    async def list_records(self, space_id):
        return self._records


async def _chunk_events(resource: str, records: list[dict], **kw) -> list[dict]:
    """The ``SPACE_SYNC_CHUNK`` payloads the provider sends for *records*,
    signed with a hybrid set."""
    builder = ChunkBuilder(encoder=_hybrid_encoder(), crypto=_Crypto())  # type: ignore[arg-type]
    index = iter(range(10_000))
    out = []
    async for envelope in builder.build_chunks(
        exporter=_Exporter(resource, records),  # type: ignore[arg-type]
        space_id=SPACE,
        sync_id="y" * 32,
        sig_suite=HYBRID,
        next_index=lambda: next(index),
        **kw,
    ):
        out.append({"sync_id": "y" * 32, "chunk": serialise_chunk(envelope).decode()})
    return out


async def _assert_record_crosses(resource: str, record: dict) -> None:
    """*record* is within the record bound, and its chunk — built by the
    real builder with a hybrid signature set, laid out raw — fits one
    frame."""
    assert len(orjson.dumps(record)) <= RELAY_SYNC_RECORD_MAX_BYTES
    [payload] = await _chunk_events(
        resource, [record], max_record_bytes=RELAY_SYNC_RECORD_MAX_BYTES
    )
    assert set(orjson.loads(payload["chunk"])["signatures"]) == {"ed25519", "mldsa65"}
    suite, raw = pack_event_body(CHUNK, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    assert len(raw) <= RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload


def _fill(build, limit: int) -> str:
    """The text that makes ``build(text)`` (a record) exactly *limit* bytes:
    an adversarial block, then single-byte padding."""
    block = ADVERSARIAL * 200
    pad = limit - space_item_bytes(build(block))
    assert pad >= 0, pad
    text = block + "x" * pad
    assert space_item_bytes(build(text)) == limit
    return text


def _descriptors(n: int) -> tuple[dict, dict]:
    """*n* file descriptors as the provider adds them, and their hashes."""
    names = [f"sp-{i:02d}-{'f' * 40}.webp" for i in range(n)]
    refs = {
        name: {
            "blob_id": "b" * 43,
            "suite": "space-media-aesgcm-256-v1",
            "epoch": 2**53,
            "size": 10 * 1024 * 1024,
            "scope": "channel",
            "addr_suite": "hmac-sha256-v1",
        }
        for name in names
    }
    return refs, {name: "h" * 43 for name in names}


def _with_files(payload: dict, n: int) -> dict:
    refs, hashes = _descriptors(n)
    return {
        **payload,
        MEDIA_REFS_FIELD: refs,
        FILES_FIELD: hashes,
        FILES_SUITE_FIELD: "sha256-b64url-v1",
    }


async def test_a_real_chunk_is_laid_out_raw_never_silently_as_json():
    """What ``ChunkBuilder`` + ``encrypt_chunk`` produce must take the raw
    suite: a silent fall-back to JSON would cost the third the bound is
    built on."""
    [payload] = await _chunk_events("pages", [{"id": "p", "content": "x" * 300_000}])
    as_json = orjson.dumps(payload)
    suite, raw = pack_event_body(CHUNK, as_json.decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    assert len(raw) < len(as_json) * 0.76


def test_a_full_frame_with_a_hybrid_header_fits_the_relay():
    """The frame's own signed header — a hybrid signature set, measured
    from the encoder, not guessed — fits beside a payload that fills the
    frame bound, and the whole seals into one relay body."""
    encoder = _hybrid_encoder()
    header, sealed = build_media_envelope(
        encoder=encoder,
        session_key=os.urandom(32),
        from_instance="a" * 32,
        to_instance="b" * 32,
        sig_suite=HYBRID,
        event_type=CHUNK,
        payload={EVENT_BODY_SUITE_FIELD: EVENT_BODY_SUITE_SYNC_CHUNK_RAW},
        raw_chunk=b"x" * RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES,
        space_id=SPACE,
        msg_id="m" * 36,
    )
    assert len(base64.urlsafe_b64decode(header["signatures"]["mldsa65"] + "==")) == (
        MLDSA65_SIGNATURE_BYTES
    )
    body = encode_media_body(header, sealed)
    assert len(body) <= RELAY_MEDIA_MAX_MEDIA_BODY_BYTES
    seal_relay_media(
        to_instance="b" * 32,
        media_body=body,
        recipient_keywrap_pub=generate_x25519_keypair().public_key,
    )


# ── Pages: the whole record, every side at its cap, 64 file descriptors ───


def _side(n: int, content: str) -> PageConflictSide:
    title = (ADVERSARIAL * 30)[:MAX_PAGE_TITLE_LENGTH]
    cover = "api/media/" + "d" * 60 + ".webp"
    return PageConflictSide(
        hash=version_hash(title, content, cover),
        title=title,
        content=content,
        by="" if n == 0 else "u",  # empty and one-character ids
        at=datetime.now(UTC).isoformat(timespec="microseconds"),
        cover_image_url=cover,
        base_seq=2**53,
    )


def _page_at_limit():
    """A page whose record — the row and the most sides a conflict keeps,
    each a whole other version — is exactly the limit."""
    sides = [_side(n, ADVERSARIAL * 5000) for n in range(MAX_CONFLICT_SIDES)]
    page = replace(
        new_page(
            title=(ADVERSARIAL * 30)[:MAX_PAGE_TITLE_LENGTH],
            content="",
            created_by="u",
            space_id=SPACE,
        ),
        cover_image_url="api/media/" + "c" * 60 + ".webp",
        last_editor_user_id="",
        locked_by="u" * 32,
        locked_at=datetime.now(UTC).isoformat(),
        lock_expires_at=datetime.now(UTC).isoformat(),
        seq=2**53,
    )
    content = _fill(
        lambda text: page_record(replace(page, content=text), sides),
        SPACE_PAGE_ITEM_MAX_BYTES,
    )
    return replace(page, content=content), sides


class _PageRepo:
    def __init__(self, page, sides) -> None:
        self._page, self._sides = page, sides

    async def list(self, *, space_id, since_seq=None):
        return [self._page]

    async def get_draft_base(self, page_id, *, space_id):
        return None

    async def list_conflict_sides(self, page_id, *, space_id):
        return self._sides


class _Federation:
    """Captures what a builder broadcasts."""

    own_instance_id = "o" * 32

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def broadcast_to_space_members(self, space_id, event_type, payload, **kw):
        self.sent.append((event_type, payload, kw))


async def test_a_page_at_its_limit_crosses_live_and_by_sync():
    page, sides = _page_at_limit()
    assert not page_too_large(page, sides)
    assert page_too_large(replace(page, content=page.content + "x"), sides)
    # Sync: the exporter's record IS the measured item; the provider adds
    # the files' descriptors for a household behind a GFS.
    [record] = await PagesExporter(_PageRepo(page, sides)).list_records(SPACE)  # type: ignore[arg-type]
    assert record == page_record(page, sides)
    lists = {k for k, v in record.items() if isinstance(v, (list, tuple, dict))}
    assert lists == {"conflict"}  # measured with the page
    await _assert_record_crosses(
        "pages", _with_files(record, MAX_PAGE_DESCRIPTOR_ENTRIES)
    )
    # Live: the real outbound's payload for the host's version, a release
    # block and the answer to a proposer included.
    fed = _Federation()
    outbound = PageFederationOutbound(bus=None, federation_service=fed)  # type: ignore[arg-type]
    sequenced = Sequenced(
        proposer_instance="p" * 32,
        proposal_hash=version_hash("t", "c"),
        outcome="refused",
        reason="too_large",
    )
    with release_scope("q" * 32, "u" * 32):
        await outbound._on_updated(
            PageUpdated(
                page_id=page.id,
                space_id=SPACE,
                title=page.title,
                content=page.content,
                actor_user_id="u" * 32,
                canonical=canonical_extras(page, sides, sequenced),
            )
        )
    [(_type, payload, _kw)] = fed.sent
    payload = _with_files(payload, MAX_PAGE_DESCRIPTOR_ENTRIES)
    assert len(orjson.dumps(payload)) <= RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES


# ── Calendar: the whole record; 500 RSVPs held out at their cap ───────────


class _Rsvp:
    def __init__(self, n: int, *, over: bool = False) -> None:
        self.occurrence_at = "2027-04-01T10:00:00.000000+00:00"
        self.status = "waitlist_requested"
        self.updated_at = "2027-03-01T10:00:00.000000+00:00"
        self.user_id = f"{n:032d}"
        size = len(orjson.dumps(self._entry()))
        # Every entry at exactly its cap — or one byte past it.
        self.user_id += "u" * (RSVP_RECORD_MAX_BYTES - size + (1 if over else 0))

    def _entry(self) -> dict:
        return {
            "user_id": self.user_id,
            "occurrence_at": self.occurrence_at,
            "status": self.status,
            "updated_at": self.updated_at,
        }


class _CalendarRepo:
    def __init__(self, event: CalendarEvent) -> None:
        self._event = event

    async def list_events_sync_page(self, space_id, *, cursor, limit, since):
        return ([self._event] if cursor is None else []), None

    async def list_rsvps(self, event_id):
        return [_Rsvp(n) for n in range(MAX_RSVPS_PER_EVENT + 50)] + [
            _Rsvp(n, over=True) for n in range(5)
        ]


def _event_at_limit() -> CalendarEvent:
    rule = clean_rrule(
        ("FREQ=WEEKLY;BYDAY=" + ",".join(["MO"] * 300))[:MAX_RRULE_CHARS]
    )
    assert rule is not None and len(rule) == MAX_RRULE_CHARS
    event = CalendarEvent(
        id=ID,
        calendar_id=SPACE,
        summary=ADVERSARIAL * 50,
        start=datetime(2027, 4, 1, 10, tzinfo=UTC),
        end=datetime(2027, 4, 1, 11, tzinfo=UTC),
        created_by="u",
        description="",
        attendees=tuple(str(n) for n in range(MAX_EVENT_ATTENDEES)),
        rrule=rule,
        capacity=2**53,
        rsvp_enabled=True,
        cover_url="api/media/" + "c" * 60 + ".webp",
        location="\x01" * LOCATION_MAX_CHARS,  # 6 bytes each on the wire
        tz="America/Argentina/ComodRivadavia",
        announce_in_feed=False,
    )
    description = _fill(
        lambda text: calendar_record(replace(event, description=text)),
        SPACE_CALENDAR_ITEM_MAX_BYTES,
    )
    return replace(event, description=description)


async def test_a_calendar_event_at_its_limit_crosses_with_500_rsvps():
    """RSVPs are added long after the event was saved, by other people:
    held out at their hard cap (count and bytes per entry), so none can
    push a saved event out of reach. Every other field is measured."""
    event = _event_at_limit()
    assert not calendar_event_too_large(event)
    assert calendar_event_too_large(replace(event, location=event.location + "y"))
    exporter = CalendarExporter(_CalendarRepo(event))  # type: ignore[arg-type]
    [records] = [batch async for batch in exporter.iter_batches(SPACE)]
    [record] = records
    assert {k: v for k, v in record.items() if not k.startswith("rsvps")} == (
        calendar_record(event)
    )
    lists = {k for k, v in record.items() if isinstance(v, (list, tuple, dict))}
    assert lists == {"attendees", "rsvps"}  # attendees: measured
    assert len(record["rsvps"]) == MAX_RSVPS_PER_EVENT
    assert all(
        len(orjson.dumps(r)) == RSVP_RECORD_MAX_BYTES for r in record["rsvps"]
    )  # the oversize ones were left out
    assert record["rsvps_partial"]
    await _assert_record_crosses("calendar", _with_files(record, 1))  # the cover
    # Live: the real publisher's payload, inside a release.
    svc = SpaceCalendarService(None)  # type: ignore[arg-type]
    fed = _Federation()
    svc._federation = fed
    with release_scope("q" * 32, "u" * 32):
        await svc._publish_federation_event_saved(
            space_id=SPACE,
            event=event,
            evt_type=FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
            actor_user_id="u" * 32,
            previous_cover=event.cover_url,
        )
    [(_type, payload, _kw)] = fed.sent
    assert (
        len(orjson.dumps(_with_files(payload, 1))) <= RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES
    )


# ── Config: the whole config, carried twice, the relay pictures ───────────


async def test_a_config_at_its_limit_fits_with_the_pictures_and_the_gfs_list():
    """Built by the real ``SpaceConfigOutbound``: the event carries the
    config twice (the flat legacy shape and ``space_meta``), which is why
    the limit is half of what the reserve leaves."""
    key = generate_space_keypair()
    space = Space(
        id=SPACE,
        name=(ADVERSARIAL * 30)[:SPACE_NAME_MAX_CHARS],
        owner_instance_id="h" * 32,
        owner_username="anna",
        identity_public_key=key.public_key.hex(),
        config_sequence=2**53,
        roster_sequence=2**53,
        config_hlc=f"{2**53}-{2**31}",
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
        description="",
        about_markdown=(ADVERSARIAL * 2000)[:8000],
        emoji="🏡" * MAX_EMOJI_CODEPOINTS,
        tz="America/Argentina/ComodRivadavia",
        retention_days=2**31,
    )
    description = _fill(
        lambda text: space_metadata_fields(replace(space, description=text)),
        SPACE_CONFIG_ITEM_MAX_BYTES,
    )
    space = replace(space, description=description)
    require_config_fits(space)
    outbound = SpaceConfigOutbound(
        bus=None,  # type: ignore[arg-type]
        federation_service=SimpleNamespace(own_instance_id="x" * 32),  # type: ignore[arg-type]
        space_repo=None,  # type: ignore[arg-type]
    )
    payload = await outbound._build_payload(
        space,
        "updated",
        seed=key.private_key,
        own="x" * 32,
        cover=False,
        icon=False,
        cover_max_bytes=SPACE_COVER_BOOTSTRAP_MAX_BYTES,
        icon_max_bytes=SPACE_ICON_BOOTSTRAP_MAX_BYTES,
    )
    as_json = orjson.dumps(payload).decode()
    assert as_json.count(orjson.dumps(description).decode()) == 2
    # The relay variant's pictures at their caps, the owner's GFS list at
    # its cap, a cert and a statement with hybrid-size signatures.
    meta = payload["space_meta"]
    meta["cover_webp_base64"] = base64.b64encode(
        os.urandom(SPACE_COVER_BOOTSTRAP_MAX_BYTES)
    ).decode()
    meta["icon_webp_base64"] = base64.b64encode(
        os.urandom(SPACE_ICON_BOOTSTRAP_MAX_BYTES)
    ).decode()
    meta["gfs_servers"] = {
        "servers": ["g" * (MAX_GFS_SERVERS_JSON_BYTES - 6)],
        "issued_at": 2**63 - 1,
        "owner_pk": "k" * 64,
        "gfs_servers_sig_suite": HYBRID,
        "gfs_servers_sig": "s" * 4600,
    }
    meta["authority_cert"] = {"cert": "c" * 5000}
    meta["owner_statement"] = {"statement": "o" * 5000}
    lists = {
        k
        for part in (payload, meta)
        for k, v in part.items()
        if isinstance(v, (list, tuple, dict)) and k != "space_meta"
    }
    assert lists <= {
        "features",
        "retention_exempt_types",
        "authority_cert",
        "owner_statement",
        "gfs_servers",
        "local_area",
        "place_pin",
        "games",
    }, lists
    assert len(orjson.dumps(payload)) <= RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES


# ── One oversize record never makes a stream repeat for ever ──────────────


async def test_a_record_over_the_bound_is_left_out_and_the_stream_stays_whole(caplog):
    """A record saved before the limit can be larger than a chunk to a
    household behind a GFS carries. It is left out with a WARNING before it
    takes a chunk index — the stream stays clean, so the requester's
    watermark moves on and the same stream is not sent again on every
    sync."""
    small = [{"id": f"p{n}", "content": "x" * 3000} for n in range(4)]
    big = {"id": "p-big", "content": "x" * (RELAY_SYNC_RECORD_MAX_BYTES + 1)}
    with caplog.at_level(logging.WARNING):
        payloads = await _chunk_events(
            "pages",
            [*small[:2], big, *small[2:]],
            max_record_bytes=RELAY_SYNC_RECORD_MAX_BYTES,
        )
    assert "p-big" in caplog.text and "left out" in caplog.text
    assert "x" * 100 not in caplog.text  # its id and size, never its text
    sent_ids: list[str] = []
    indexes: list[int] = []
    for payload in payloads:
        envelope = orjson.loads(payload["chunk"])
        _nonce, ciphertext = envelope["encrypted_payload"].split(":")
        padded = ciphertext + "=" * (-len(ciphertext) % 4)
        body = orjson.loads(base64.urlsafe_b64decode(padded)[:-16])
        sent_ids += [r["id"] for r in body["records"]]
        indexes.append(body["chunk_index"])
    assert sent_ids == ["p0", "p1", "p2", "p3"]
    assert indexes == list(range(len(payloads)))  # no gap for the requester
    # Without a limit (a paired requester) the same record is streamed.
    everything = await _chunk_events("pages", [big])
    assert len(everything) == 1


def test_the_limits_are_derived_from_the_bound_and_stay_generous():
    assert RELAY_SYNC_RECORD_MAX_BYTES == RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES
    assert SPACE_PAGE_ITEM_MAX_BYTES == 475_038
    assert SPACE_CALENDAR_ITEM_MAX_BYTES == 383_402
    assert SPACE_CONFIG_ITEM_MAX_BYTES == 178_807
    # A roster part is far below all of it (it must fit a JSON envelope).
    assert ROSTER_SNAPSHOT_PART_BYTES < 100 * 1024
    assert SPACE_PAGE_ITEM_MAX_BYTES > 460 * 1024  # the demo's 450 KiB page fits
    # The SPA's own caps sit far below: a 300-character description and an
    # 8000-character about text.
    assert SPACE_CONFIG_ITEM_MAX_BYTES > 8 * (300 + 8000) * 2


@pytest.mark.parametrize("over", [1, 1000])
def test_one_byte_over_any_limit_is_too_large(over):
    page, sides = _page_at_limit()
    assert page_too_large(replace(page, content=page.content + "x" * over), sides)
