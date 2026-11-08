"""Wire shapes of space media on demand (v_83)."""

from __future__ import annotations

import pytest

from socialhome.domain.gfs_space_media_on_demand import (
    MODE_CACHE,
    SPACE_MEDIA_REQUEST_FRAME,
    InvalidOnDemandWire,
    MediaRequestFrame,
    WantedRequest,
    serve_signing_body,
    valid_ticket,
)
from socialhome.space_media_hint import SpaceMediaLocator, seal_space_media_hint

BLOB = "c" * 64


def _hint() -> dict:
    return seal_space_media_hint(
        media_key=b"k" * 32,
        scope="space",
        scope_id="sp-1",
        blob_id=BLOB,
        epoch=1,
        locator=SpaceMediaLocator(item_id="i", origin="o", filename="f.webp"),
    )


def test_wanted_round_trip() -> None:
    raw = {"scope": "space", "scope_id": "sp-1", "hint": _hint()}
    req = WantedRequest.from_wire(raw)
    assert req.scope == "space" and req.scope_id == "sp-1"
    assert WantedRequest.from_wire(req.to_wire()) == req


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"scope": "galaxy", "scope_id": "x", "hint": {}},
        {"scope": "space", "scope_id": "a|b", "hint": {}},
        {"scope": "space", "scope_id": "sp-1", "hint": {"suite": "nope"}},
        {"scope": "space", "scope_id": "sp-1"},
    ],
)
def test_wanted_refused(raw) -> None:
    with pytest.raises(InvalidOnDemandWire):
        WantedRequest.from_wire(raw)


def test_wanted_carries_no_reader_field() -> None:
    raw = {"scope": "space", "scope_id": "sp-1", "hint": _hint(), "from": "me"}
    assert "from" not in WantedRequest.from_wire(raw).to_wire()


def test_frame_round_trip() -> None:
    req = WantedRequest.from_wire(
        {"scope": "space", "scope_id": "sp-1", "hint": _hint()}
    )
    frame = MediaRequestFrame(
        ticket="node-a." + "0" * 32,
        scope="space",
        scope_id="sp-1",
        blob_id=BLOB,
        mode=MODE_CACHE,
        hint=req.hint,
    )
    push = frame.to_push()
    assert push["type"] == SPACE_MEDIA_REQUEST_FRAME
    assert MediaRequestFrame.from_frame(push) == frame


@pytest.mark.parametrize(
    "patch",
    [
        {"type": "relay"},
        {"ticket": "../x"},
        {"blob_id": "zz"},
        {"mode": "forever"},
    ],
)
def test_frame_refused(patch) -> None:
    req = WantedRequest.from_wire(
        {"scope": "space", "scope_id": "sp-1", "hint": _hint()}
    )
    push = MediaRequestFrame(
        ticket="0" * 32,
        scope="space",
        scope_id="sp-1",
        blob_id=BLOB,
        mode=MODE_CACHE,
        hint=req.hint,
    ).to_push()
    push.update(patch)
    with pytest.raises(InvalidOnDemandWire):
        MediaRequestFrame.from_frame(push)


def test_ticket_shapes() -> None:
    assert valid_ticket("0" * 32)
    assert valid_ticket("alloc-1." + "a" * 32)
    assert not valid_ticket("a" * 31)
    assert not valid_ticket("x/y." + "a" * 32)
    assert not valid_ticket(None)


def test_serve_signing_body() -> None:
    # The operation is signed: a serve signature is never a decline's.
    assert serve_signing_body("i", "t", 5, "serve") == {
        "instance_id": "i",
        "media_ticket": "t",
        "op": "serve",
        "ts": 5,
    }
    assert serve_signing_body("i", "t", 5, "decline") != serve_signing_body(
        "i", "t", 5, "serve"
    )
