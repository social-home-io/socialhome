"""Unit tests for :class:`SpaceItemDispatch` (v_97 event-shaped items).

The authorization equivalence with the legacy event over real repos is in
``tests/protocol/test_space_item_equivalence.py``; these pin the item-only
checks and the dedupe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation import space_item_dispatch as mod
from socialhome.federation.space_item_dispatch import (
    ITEM_MSG_ID_PREFIX,
    SpaceItemDispatch,
    parse_event_item,
)
from socialhome.services.space_item_author import build_signed_event_item
from socialhome.services.space_writer_cert_service import WRITER_CERT_FIELD

KP = generate_identity_keypair()
ORIGIN = derive_instance_id(KP.public_key)
SPACE = Space(
    id="sp-1",
    name="S",
    owner_instance_id="host",
    owner_username="anna",
    identity_public_key="00" * 32,
    config_sequence=0,
    features=SpaceFeatures(),
    space_type=SpaceType.GLOBAL,
    join_mode=JoinMode.INVITE_ONLY,
)
CERT = {
    "cert_suite": "ed25519",
    "space_id": "sp-1",
    "epoch": 1,
    "instance_pk": "x",
    "scope": "write",
    "issued_at": 1,
    "cert_sig": "s",
}


class _Sink:
    def __init__(self, verdict: bool = True) -> None:
        self.events: list = []
        self.verdict = verdict

    async def dispatch_space_item(self, event) -> bool:
        self.events.append(event)
        return self.verdict


class _Certs:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    async def check_item(self, space, raw, **kw) -> bool:
        self.calls.append(kw)
        return self.ok


def _inner(item_type="space_task_created", item_id="ab" * 16, **over) -> dict:
    kw = {
        "item_type": item_type,
        "item_id": item_id,
        "space_id": "sp-1",
        "origin_instance_id": ORIGIN,
        "identity_seed": KP.private_key,
        "identity_pk": KP.public_key,
        "ts": datetime.now(UTC).isoformat(),
        "body": {"space_id": "sp-1", "task_id": "t-1"},
    }
    kw.update(over)
    inner = build_signed_event_item(**kw)
    inner[WRITER_CERT_FIELD] = CERT
    return inner


def _dispatch(sink=None, certs=None, own="us") -> SpaceItemDispatch:
    return SpaceItemDispatch(
        federation=sink or _Sink(),
        writer_certs=certs or _Certs(),  # type: ignore[arg-type]
        own_instance_id=own,
    )


async def _apply(d, inner, item_type="space_task_created") -> bool:
    return await d.apply(SPACE, epoch=1, item_type=item_type, inner=inner)


async def test_a_valid_item_becomes_its_event_from_the_origin():
    sink, certs = _Sink(), _Certs()
    d = _dispatch(sink, certs)
    assert await _apply(d, _inner())
    (event,) = sink.events
    assert event.event_type is FederationEventType.SPACE_TASK_CREATED
    assert event.from_instance == ORIGIN
    assert event.to_instance == "us"
    assert event.space_id == "sp-1"
    assert event.epoch == 1
    assert event.payload == {"space_id": "sp-1", "task_id": "t-1"}
    assert event.msg_id == ITEM_MSG_ID_PREFIX + "ab" * 16
    assert certs.calls[0]["required_scope"] == "write"
    assert certs.calls[0]["author_pk"] == KP.public_key


async def test_a_copy_runs_the_handler_once():
    sink = _Sink()
    d = _dispatch(sink)
    inner = _inner()
    assert await _apply(d, inner)
    assert not await _apply(d, inner)
    assert len(sink.events) == 1


async def test_a_refused_item_is_still_remembered():
    sink = _Sink(verdict=False)
    d = _dispatch(sink)
    inner = _inner()
    assert not await _apply(d, inner)
    assert not await _apply(d, inner)
    assert len(sink.events) == 1


async def test_the_seen_set_is_bounded(monkeypatch):
    monkeypatch.setattr(mod, "SEEN_ITEMS_MAX", 2)
    sink = _Sink()
    d = _dispatch(sink)
    for n in range(3):
        await _apply(d, _inner(item_id=f"{n:032x}"))
    assert await _apply(d, _inner(item_id=f"{0:032x}"))  # evicted → runs again


async def test_our_own_echo_is_dropped():
    sink = _Sink()
    assert not await _apply(_dispatch(sink, own=ORIGIN), _inner())
    assert sink.events == []


@pytest.mark.parametrize(
    "item_type", ["space_member_banned", "post", "nope", "space_media_blob"]
)
async def test_only_person_item_types_are_taken(item_type):
    sink = _Sink()
    inner = _inner(item_type=item_type)
    assert not await _apply(_dispatch(sink), inner, item_type)
    assert sink.events == []


async def test_the_outer_type_must_be_the_signed_one():
    sink = _Sink()
    inner = _inner(item_type="space_task_deleted")
    assert not await _apply(_dispatch(sink), inner, "space_task_created")


async def test_a_tampered_item_is_refused():
    sink = _Sink()
    inner = dict(_inner(), body={"space_id": "sp-1", "task_id": "t-2"})
    assert not await _apply(_dispatch(sink), inner)
    assert sink.events == []


async def test_an_unknown_author_suite_is_refused():
    inner = dict(_inner(), author_sig_suite="rsa")
    assert not await _apply(_dispatch(), inner)


async def test_an_item_for_another_space_is_refused():
    assert not await _apply(_dispatch(), _inner(space_id="sp-2"))
    other_body = _inner(body={"space_id": "sp-2", "task_id": "t"})
    assert not await _apply(_dispatch(), other_body)


async def test_a_body_without_a_space_id_is_taken():
    sink = _Sink()
    assert await _apply(_dispatch(sink), _inner(body={"task_id": "t"}))


async def test_a_future_stamp_is_refused():
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert not await _apply(_dispatch(), _inner(ts=future))
    assert not await _apply(_dispatch(), _inner(ts="yesterday"))


async def test_no_cert_or_a_refused_cert_is_refused():
    sink = _Sink()
    bare = _inner()
    del bare[WRITER_CERT_FIELD]
    assert not await _apply(_dispatch(sink), bare)
    assert not await _apply(_dispatch(sink, _Certs(ok=False)), _inner())
    assert sink.events == []


async def test_a_comment_needs_comment_scope():
    certs = _Certs()
    await _apply(
        _dispatch(certs=certs),
        _inner(item_type="space_comment_created"),
        "space_comment_created",
    )
    assert certs.calls[0]["required_scope"] == "comment"


def test_parse_event_item_takes_only_v2_types():
    inner = {"x": 1}
    raw = orjson.dumps({"item_type": "space_task_created", "inner": inner, "_pad": ""})
    assert parse_event_item(raw) == ("space_task_created", inner)
    assert parse_event_item(orjson.dumps({"item_type": "post", "inner": {}})) is None
    assert parse_event_item(orjson.dumps({"item_type": "space_task_created"})) is None
    assert parse_event_item(b"[1]") is None
    assert parse_event_item(b"not json") is None
