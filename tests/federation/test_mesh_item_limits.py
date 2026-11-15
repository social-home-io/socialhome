"""Tripwire: the mesh carries the largest space item, live and by sync.

Owner decision 2026-11-14: "Size should not matter if GFS or federation. In
the end members are equal." One limit per kind of space item holds on every
path (``domain/relay_media_frame.py``): a live event's payload up to
:data:`RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES` and a §25.6 record up to
:data:`RELAY_SYNC_RECORD_MAX_BYTES`. These tests build both at the exact
limit with the real builders — ``ChunkBuilder`` for the chunk,
``SpaceRoutedHandler.send_routed`` for the route's seal, relayed hop by hop
over the longest route (8 households), ``FederationService._seal_envelope``
for every hop's envelope — with hybrid-sized signatures everywhere (the
chunk's, the route origin's, each hop's), and check:

* every hop's envelope fits one hop (the HTTPS inbox body and the data
  channel message, 1 MiB each) and the target gets the identical payload;
* no size between "fits as JSON" and the limit falls through: every record
  size up to the limit either fits as JSON or is sealed raw and fits;
* the host hop's leg toward a household behind a GFS carries both in one
  binary relay frame (``space-routed-raw-v1``).
"""

from __future__ import annotations

import os

import orjson
import pytest

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.domain.media_chunking import ADVERTISED_MAX_MESSAGE_BYTES
from socialhome.domain.relay_media_frame import (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES,
    RELAY_MEDIA_MAX_BODY_BYTES,
    RELAY_SYNC_RECORD_MAX_BYTES,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.relay_event_body import (
    EVENT_BODY_SUITE_FIELD,
    EVENT_BODY_SUITE_SYNC_CHUNK_RAW,
)
from socialhome.federation.routed_envelope import _HOP_ENVELOPE_ALLOWANCE_BYTES
from socialhome.federation.sync.space.exporter import ChunkBuilder, serialise_chunk
from socialhome.hardening import DEFAULT_JSON_MAX_BYTES
from tests.federation.test_federation_service_relay_large_events import (
    _MLDSA65_SIG_CHARS,
    SPACE,
    _world,
)
from tests.federation.test_routed_envelope import (
    _build_identity_chain,
    _mint_target_eph,
    _Node,
    _routed_sends,
    _settle,
)

CHUNK = FederationEventType.SPACE_SYNC_CHUNK
PAGE = FederationEventType.SPACE_PAGE_UPDATED
ROUTED = FederationEventType.SPACE_ROUTED
#: One hop: the HTTPS inbox body cap and the data channel message size.
HOP_MAX = min(DEFAULT_JSON_MAX_BYTES, ADVERTISED_MAX_MESSAGE_BYTES)
#: The longest route (``routed_envelope._MAX_ROUTED_PATH_LEN``).
LONGEST_PATH = 8
#: A hybrid (Ed25519 + ML-DSA-65) origin signature on the route.
HYBRID_ORIGIN_SIG = "s" * (86 + _MLDSA65_SIG_CHARS)


class _ChunkCrypto:
    """``encrypt_chunk`` in its real wire form and size:
    ``b64url(nonce):b64url(ciphertext + tag)``."""

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 7, (
            b64url_encode(os.urandom(12))
            + ":"
            + b64url_encode(os.urandom(len(plaintext) + 16))
        )


class _OneRecord:
    resource = "pages"

    def __init__(self, record: dict) -> None:
        self._record = record

    async def list_records(self, space_id):
        return [self._record]


async def _sync_chunk(record_bytes: int) -> dict:
    """The ``SPACE_SYNC_CHUNK`` payload the provider sends for one record of
    exactly *record_bytes* JSON — built by the real ``ChunkBuilder``, its
    signature set made hybrid-sized."""
    record = {"id": "page-at-the-limit", "content": ""}
    record["content"] = "x" * (record_bytes - len(orjson.dumps(record)))
    assert len(orjson.dumps(record)) == record_bytes
    index = iter(range(10))
    [envelope] = [
        e
        async for e in ChunkBuilder(
            encoder=FederationEncoder(generate_identity_keypair().private_key),
            crypto=_ChunkCrypto(),  # type: ignore[arg-type]
        ).build_chunks(
            exporter=_OneRecord(record),  # type: ignore[arg-type]
            space_id=SPACE,
            sync_id="y" * 32,
            sig_suite="ed25519",
            next_index=lambda: next(index),
            max_record_bytes=RELAY_SYNC_RECORD_MAX_BYTES,
        )
    ]
    envelope["signatures"]["mldsa65"] = "p" * _MLDSA65_SIG_CHARS
    return {"sync_id": "y" * 32, "chunk": serialise_chunk(envelope).decode()}


def _live_page() -> dict:
    """A live page event's payload of exactly the live bound."""
    page = {"space_id": SPACE, "page_id": "page-at-the-limit", "content": ""}
    page["content"] = "x" * (
        RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - len(orjson.dumps(page))
    )
    assert len(orjson.dumps(page)) == RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES
    return page


def _hop_envelope_bytes(routed_payload: dict) -> int:
    """The real envelope one hop sends for *routed_payload*, every signature
    hybrid-sized: the route origin's and the hop's own."""
    w = _world()
    row = w.host.repo._instances[w.member.id]
    payload = {
        **routed_payload,
        "sealed": {
            **routed_payload["sealed"],
            "origin_sig_suite": "ed25519+mldsa65",
            "origin_sig": HYBRID_ORIGIN_SIG,
        },
    }
    envelope = w.host.svc._seal_envelope(
        row, event_type=ROUTED, payload=payload, space_id=SPACE
    )
    envelope["sig_suite"] = "ed25519+mldsa65"
    envelope["signatures"]["mldsa65"] = "p" * _MLDSA65_SIG_CHARS
    return len(orjson.dumps(envelope))


async def _route(
    nodes: list[_Node], event_type: FederationEventType, payload: dict
) -> None:
    await nodes[0].handler.send_routed(
        path=[n.instance_id for n in nodes],
        target_eph_pk_b64=_mint_target_eph(nodes[-1]),
        inner_event_type=event_type,
        inner_payload=payload,
        raw_body_ok=True,
    )
    await _settle()


def test_the_hop_allowance_covers_a_hybrid_signed_header():
    """``send_routed`` decides JSON or raw on the encoded payload plus this
    allowance; it must cover the largest header a hop adds."""
    small = {"route_id": "r", "sealed": {"ciphertext": "x"}}
    w = _world()
    row = w.host.repo._instances[w.member.id]
    envelope = w.host.svc._seal_envelope(
        row, event_type=ROUTED, payload=small, space_id=SPACE
    )
    envelope["sig_suite"] = "ed25519+mldsa65"
    envelope["signatures"]["mldsa65"] = "p" * _MLDSA65_SIG_CHARS
    header = len(orjson.dumps(envelope)) - len(envelope["encrypted_payload"])
    # ... and a route origin signature that turned hybrid (Ed25519 today).
    assert header + len(HYBRID_ORIGIN_SIG) <= _HOP_ENVELOPE_ALLOWANCE_BYTES


@pytest.mark.parametrize("item", ["live-event", "sync-chunk"])
async def test_the_largest_item_crosses_the_longest_mesh_route(item):
    nodes = _build_identity_chain(LONGEST_PATH)
    if item == "live-event":
        event_type, payload = PAGE, _live_page()
    else:
        event_type, payload = CHUNK, await _sync_chunk(RELAY_SYNC_RECORD_MAX_BYTES)
    await _route(nodes, event_type, payload)
    hops = [s["payload"] for n in nodes[:-1] for s in _routed_sends(n)]
    assert len(hops) == LONGEST_PATH - 1
    for hop in hops:
        assert _hop_envelope_bytes(hop) <= HOP_MAX
    if item == "sync-chunk":
        suite = hops[0]["sealed"][EVENT_BODY_SUITE_FIELD]
        assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    [got] = nodes[-1].dispatched
    assert got.event_type is event_type and got.payload == payload


async def test_no_sync_record_size_falls_between_json_and_raw():
    """Every record up to the limit reaches the target: as JSON while that
    fits a hop, raw from the first size where it would not — no size in
    between is sent in a form that does not fit."""
    sizes = [*range(400_000, RELAY_SYNC_RECORD_MAX_BYTES, 256)]
    sizes.append(RELAY_SYNC_RECORD_MAX_BYTES)
    forms = []
    for size in sizes:
        nodes = _build_identity_chain(LONGEST_PATH)
        payload = await _sync_chunk(size)
        await _route(nodes, CHUNK, payload)
        first = _routed_sends(nodes[0])[0]["payload"]
        assert _hop_envelope_bytes(first) <= HOP_MAX, size
        assert [e.payload for e in nodes[-1].dispatched] == [payload], size
        forms.append(EVENT_BODY_SUITE_FIELD in first["sealed"])
    # Both forms occur, and once raw, raw for every larger record.
    assert forms[0] is False and forms[-1] is True
    assert forms == sorted(forms)


@pytest.mark.parametrize("mailbox", [False, True], ids=["relay-seat", "mailbox"])
@pytest.mark.parametrize("item", ["live-event", "sync-chunk"])
async def test_the_largest_item_crosses_a_host_hop_to_a_household_behind_a_gfs(
    mailbox, item
):
    """The v_89 host hop ``[member, host, seat]``: the route's seal from the
    real ``send_routed``, then the host's leg to the seat over the GFS relay
    as one binary frame (the wrapper's sealed inner raw)."""
    origin, host, target = _build_identity_chain(3)
    if item == "live-event":
        event_type, payload = PAGE, _live_page()
    else:
        event_type, payload = CHUNK, await _sync_chunk(RELAY_SYNC_RECORD_MAX_BYTES)
    await _route([origin, host, target], event_type, payload)
    [leg] = [s["payload"] for s in _routed_sends(host)]
    leg = {
        **leg,
        "sealed": {
            **leg["sealed"],
            "origin_sig_suite": "ed25519+mldsa65",
            "origin_sig": HYBRID_ORIGIN_SIG,
        },
    }
    w = _world(mailbox=mailbox)
    got: list = []

    async def _record(event) -> None:
        got.append(event)

    w.member.svc._event_registry._handlers[ROUTED] = [_record]
    result = await w.send(w.host, w.member, ROUTED, leg)
    assert result.ok and result.via == "gfs_relay"
    assert len(w.gfs.seen[-1][2]) <= RELAY_MEDIA_MAX_BODY_BYTES
    assert await w.gfs.deliver() == [{"status": "ok"}]
    [event] = got
    assert event.payload == leg
