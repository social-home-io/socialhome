"""The size rule for space text measures what actually has to fit.

A household behind a GFS gets a large item as one relay frame, live and —
when that frame is lost — as the item's §25.6 sync record. The text is not
all that rides in either: file descriptors, RSVPs and open conflict sides
sit next to it. Each text limit is the bound less the HARD CAP of what can
ride with that kind of item; these tests build each worst case with the
real builders (``PagesExporter``, ``CalendarExporter``, ``canonical_extras``,
``ChunkBuilder``, the raw body suite) and check it still fits — so a list
that grows later can never make a saved item undeliverable.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime

import orjson

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.federation import FederationEventType
from socialhome.domain.file_hashes import FILES_FIELD, FILES_SUITE_FIELD
from socialhome.domain.relay_media_frame import (
    RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES,
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES,
    RELAY_SYNC_RECORD_MAX_BYTES,
    SPACE_CALENDAR_TEXT_MAX_BYTES,
    SPACE_CONFIG_TEXT_MAX_BYTES,
    SPACE_PAGE_STATE_MAX_BYTES,
)
from socialhome.domain.space_media_ref import (
    MAX_PAGE_DESCRIPTOR_ENTRIES,
    MEDIA_REFS_FIELD,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.relay_event_body import (
    EVENT_BODY_SUITE_SYNC_CHUNK_RAW,
    pack_event_body,
    unpack_event_body,
)
from socialhome.federation.sync.space.exporter import ChunkBuilder, serialise_chunk
from socialhome.federation.sync.space.exporters import PagesExporter
from socialhome.federation.sync.space.exporters.calendar import (
    MAX_RSVPS_PER_EVENT,
    CalendarExporter,
)
from socialhome.repositories.page_repo import new_page
from socialhome.services.page_conflict_service import (
    MAX_CONFLICT_SIDES,
    PageConflictSide,
    canonical_extras,
    page_state_bytes,
)
from socialhome.services.space_service import ROSTER_SNAPSHOT_PART_BYTES

SPACE = "s" * 32
CHUNK = FederationEventType.SPACE_SYNC_CHUNK
#: What a hybrid signature set adds to a chunk envelope's signatures.
HYBRID_SIGNATURE_BYTES = 4500


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


def _builder() -> ChunkBuilder:
    return ChunkBuilder(
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_Crypto(),  # type: ignore[arg-type]
    )


async def _chunk_events(resource: str, records: list[dict], **kw) -> list[dict]:
    """The ``SPACE_SYNC_CHUNK`` payloads the provider sends for *records*."""
    index = iter(range(10_000))
    out = []
    async for envelope in _builder().build_chunks(
        exporter=_Exporter(resource, records),  # type: ignore[arg-type]
        space_id=SPACE,
        sync_id="y" * 32,
        sig_suite="ed25519",
        next_index=lambda: next(index),
        **kw,
    ):
        out.append({"sync_id": "y" * 32, "chunk": serialise_chunk(envelope).decode()})
    return out


async def _assert_record_crosses(resource: str, record: dict) -> None:
    """*record* is within the record bound, and its chunk — built by the
    real builder, laid out raw — fits one frame with room for a hybrid
    signature set."""
    assert len(orjson.dumps(record)) <= RELAY_SYNC_RECORD_MAX_BYTES
    [payload] = await _chunk_events(
        resource, [record], max_record_bytes=RELAY_SYNC_RECORD_MAX_BYTES
    )
    suite, raw = pack_event_body(CHUNK, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    assert len(raw) + HYBRID_SIGNATURE_BYTES <= RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload


async def test_a_real_chunk_is_laid_out_raw_never_silently_as_json():
    """What ``ChunkBuilder`` + ``encrypt_chunk`` produce must take the raw
    suite: a silent fall-back to JSON would cost the third the bound is
    built on."""
    [payload] = await _chunk_events("pages", [{"id": "p", "content": "x" * 300_000}])
    as_json = orjson.dumps(payload)
    suite, raw = pack_event_body(CHUNK, as_json.decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    assert len(raw) < len(as_json) * 0.76


# ── Pages: the text, every open side, 64 file descriptors ─────────────────


def _descriptors() -> tuple[dict, dict]:
    """The most file descriptors a page record carries, and their hashes."""
    names = [f"sp-{i:02d}-{'f' * 40}.webp" for i in range(MAX_PAGE_DESCRIPTOR_ENTRIES)]
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


def _worst_page(space_id: str = SPACE):
    """A page whose state is exactly the limit: the body and one open side
    that is a whole other version (a side's text is not capped on its own),
    plus two more small sides — the most a conflict holds."""
    half = (SPACE_PAGE_STATE_MAX_BYTES - 5 * 2 - 4 * 302 - 6) // 2
    page = replace(
        new_page(
            title="T" * 200, content="c" * half, created_by="u" * 32, space_id=space_id
        ),
        cover_image_url="api/media/" + "c" * 60 + ".webp",
        last_editor_user_id="u" * 32,
        seq=2**53,
    )
    sides = [
        PageConflictSide(
            hash="a" * 64,
            title="T" * 200,
            content="s" * (half - 2 * 300 - 800),
            by="u" * 32,
            at=datetime.now(UTC).isoformat(timespec="microseconds"),
            cover_image_url="api/media/" + "d" * 60 + ".webp",
            base_seq=2**53,
        ),
        *[
            PageConflictSide(
                hash=f"{n}" * 64,
                title="T" * 200,
                content="t" * 100,
                by="u" * 32,
                at=datetime.now(UTC).isoformat(timespec="microseconds"),
                base_seq=2**53,
            )
            for n in range(MAX_CONFLICT_SIDES - 1)
        ],
    ]
    assert len(sides) == MAX_CONFLICT_SIDES
    state = page_state_bytes(page.title, page.content, sides)
    assert SPACE_PAGE_STATE_MAX_BYTES - 2048 < state <= SPACE_PAGE_STATE_MAX_BYTES
    return page, sides


class _PageRepo:
    def __init__(self, page, sides) -> None:
        self._page, self._sides = page, sides

    async def list(self, *, space_id, since_seq=None):
        return [self._page]

    async def get_draft_base(self, page_id, *, space_id):
        return None

    async def list_conflict_sides(self, page_id, *, space_id):
        return self._sides


async def test_a_page_at_its_state_limit_crosses_with_every_list_at_its_cap():
    page, sides = _worst_page()
    refs, hashes = _descriptors()
    [record] = await PagesExporter(_PageRepo(page, sides)).list_records(SPACE)  # type: ignore[arg-type]
    assert len(record["conflict"]) == MAX_CONFLICT_SIDES
    # What the provider adds for a household behind a GFS.
    record = {
        **record,
        MEDIA_REFS_FIELD: refs,
        FILES_FIELD: hashes,
        FILES_SUITE_FIELD: "sha256-b64url-v1",
    }
    await _assert_record_crosses("pages", record)
    # The live event: the host's version with the same sides and files.
    event = {
        "space_id": SPACE,
        "page_id": page.id,
        "id": page.id,
        "title": page.title,
        "content": page.content,
        "created_by": page.created_by,
        "actor_user_id": page.last_editor_user_id,
        **canonical_extras(page, sides),
        MEDIA_REFS_FIELD: refs,
        FILES_FIELD: hashes,
        FILES_SUITE_FIELD: "sha256-b64url-v1",
    }
    assert len(orjson.dumps(event)) <= RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES


# ── Calendar: the description, 500 RSVPs ──────────────────────────────────


class _Rsvp:
    def __init__(self, n: int) -> None:
        self.user_id = f"{n:032d}"
        self.occurrence_at = "2027-04-01T10:00:00.000000+00:00"
        self.status = "waitlist_requested"
        self.updated_at = "2027-03-01T10:00:00.000000+00:00"


class _CalendarRepo:
    def __init__(self, event: CalendarEvent, rsvps: int) -> None:
        self._event, self._rsvps = event, rsvps

    async def list_events_sync_page(self, space_id, *, cursor, limit, since):
        return ([self._event] if cursor is None else []), None

    async def list_rsvps(self, event_id):
        return [_Rsvp(n) for n in range(self._rsvps)]


async def test_a_calendar_event_at_its_text_limit_crosses_with_500_rsvps():
    """RSVPs are added long after the event was saved: the reserve is their
    hard cap, so no RSVP can push a saved event out of reach."""
    summary = "S" * 500
    description = "d" * (SPACE_CALENDAR_TEXT_MAX_BYTES - len(summary) - 4)
    event = CalendarEvent(
        id="e" * 32 + "@2027-04-01T10:00:00+00:00",
        calendar_id=SPACE,
        summary=summary,
        start=datetime(2027, 4, 1, 10, tzinfo=UTC),
        end=datetime(2027, 4, 1, 11, tzinfo=UTC),
        created_by="u" * 32,
        description=description,
        rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=500",
        capacity=10**6,
        rsvp_enabled=True,
    )
    exporter = CalendarExporter(_CalendarRepo(event, MAX_RSVPS_PER_EVENT + 50))  # type: ignore[arg-type]
    [records] = [batch async for batch in exporter.iter_batches(SPACE)]
    [record] = records
    assert len(record["rsvps"]) == MAX_RSVPS_PER_EVENT and record["rsvps_partial"]
    refs, hashes = _descriptors()
    one = next(iter(refs))
    record = {
        **record,
        "cover_url": "api/media/" + "c" * 60 + ".webp",
        "location": "L" * 500,
        MEDIA_REFS_FIELD: {one: refs[one]},
        FILES_FIELD: {one: hashes[one]},
    }
    await _assert_record_crosses("calendar", record)


# ── Config: the description, the relay variant's cover and icon ───────────


def test_a_config_change_at_its_text_limit_fits_with_the_relay_pictures():
    description = "d" * (SPACE_CONFIG_TEXT_MAX_BYTES // 2 - 2)
    about = "a" * (SPACE_CONFIG_TEXT_MAX_BYTES - len(description) - 4)
    payload = {
        "space_id": SPACE,
        "sequence": 2**53,
        "name": "N" * 200,
        "description": description,
        "about_markdown": about,
        "emoji": "🏡",
        # The relay variant's pictures (64 KiB + 16 KiB), base64'ed.
        "cover_webp_base64": "c" * (64 * 1024 * 4 // 3 + 4),
        "icon_webp_base64": "i" * (16 * 1024 * 4 // 3 + 4),
        "features": {f"feature_{n}": "moderated" for n in range(120)},
        "authority_sig": "s" * 128,
        "authority_cert": {"cert": "x" * 2048},
    }
    assert len(orjson.dumps(payload)) <= RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES


# ── One oversize record never makes a stream repeat for ever ──────────────


async def test_a_record_over_the_bound_is_left_out_and_the_stream_stays_whole(caplog):
    """A record saved before the space had a household behind a GFS can be
    larger than a chunk to it carries. It is left out with a WARNING before
    it takes a chunk index — the stream stays clean, so the requester's
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
    assert SPACE_PAGE_STATE_MAX_BYTES == 475_038
    assert SPACE_CALENDAR_TEXT_MAX_BYTES == 384_926
    assert SPACE_CONFIG_TEXT_MAX_BYTES == 384_926
    # A roster part is far below all of it (it must fit a JSON envelope).
    assert ROSTER_SNAPSHOT_PART_BYTES < 100 * 1024
    assert SPACE_PAGE_STATE_MAX_BYTES > 450 * 1024  # the demo's page fits
