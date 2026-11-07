"""Wire shapes of space media on demand (v_83)."""

from __future__ import annotations

import pytest

from socialhome.canonical_json import canonical_bytes
from socialhome.domain.gfs_space_media_on_demand import (
    DECLINE_BUSY,
    DECLINE_REASONS,
    MAX_MEMBER_WANT_HINTS,
    MEMBER_PULL_CAPABILITY_KEY,
    MEMBER_PULL_PREFIX,
    MEMBER_SERVE_PREFIX,
    MEMBER_WRITER_EPOCH_HEADER,
    MEMBER_WRITER_SIG_HEADER,
    MEMBER_WRITER_SIG_SUITE_HEADER,
    MODE_CACHE,
    MODE_STREAM,
    SPACE_MEDIA_REQUEST_FRAME,
    InvalidOnDemandWire,
    MediaRequestFrame,
    MemberPullRequest,
    MemberWant,
    WantedRequest,
    member_serve_signing_bytes,
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


# ── Member fallback (listed spaces, writer-key holders) ──────────────────


def _pull(**over) -> dict:
    raw = {
        "scope": "space",
        "scope_id": "sp-1",
        "epoch": 7,
        "gfs_instance_id": "gfs-1",
        "gfs_key": "ab" * 32,
        "ts": 1_700_000_000,
        "nonce": "0f" * 16,
        "sig": "c2ln",
        "sig_suite": "ed25519",
    }
    raw.update(over)
    return {k: v for k, v in raw.items() if v is not None}


def test_member_pull_round_trip_and_signing_bytes() -> None:
    req = MemberPullRequest.from_wire(_pull())
    assert MemberPullRequest.from_wire(req.to_wire()) == req
    assert req.signing_bytes() == MEMBER_PULL_PREFIX + canonical_bytes(
        {
            "scope": "space",
            "scope_id": "sp-1",
            "epoch": 7,
            "gfs_instance_id": "gfs-1",
            "gfs_key": "ab" * 32,
            "ts": 1_700_000_000,
            "nonce": "0f" * 16,
        }
    )
    # gfs_key is optional and then not signed.
    bare = MemberPullRequest.from_wire(_pull(gfs_key=None))
    assert bare.gfs_key is None
    assert b"gfs_key" not in bare.signing_bytes()
    assert "gfs_key" not in bare.to_wire()


def test_member_pull_names_no_household() -> None:
    req = MemberPullRequest.from_wire(_pull())
    assert set(req.to_wire()) == {
        "scope",
        "scope_id",
        "epoch",
        "gfs_instance_id",
        "gfs_key",
        "ts",
        "nonce",
        "sig",
        "sig_suite",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"scope": "channel", "scope_id": "1e" * 16},
        {"scope": "galaxy"},
        {"scope_id": "a|b"},
        {"epoch": -1},
        {"epoch": True},
        {"epoch": "7"},
        {"gfs_instance_id": ""},
        {"gfs_instance_id": "x" * 200},
        {"gfs_key": "zz"},
        {"ts": "1700000000"},
        {"ts": False},
        {"nonce": "short"},
        {"nonce": "G" * 32},
        {"sig": ""},
        {"sig_suite": "ed25519+mldsa65"},
        {"instance_id": "me"},
        {"writer_cert": {}},
    ],
)
def test_member_pull_refused(patch) -> None:
    with pytest.raises(InvalidOnDemandWire):
        MemberPullRequest.from_wire(_pull(**patch))


def test_member_pull_refuses_non_objects() -> None:
    with pytest.raises(InvalidOnDemandWire):
        MemberPullRequest.from_wire([])


def test_member_want_round_trip() -> None:
    want = MemberWant.from_wire(
        {
            "ticket": "node-a." + "1" * 32,
            "blob_id": BLOB,
            "mode": MODE_STREAM,
            "hints": [_hint(), _hint()],
            "claim": "c" * 32,
        }
    )
    assert len(want.hints) == 2
    assert MemberWant.from_wire(want.to_wire()) == want
    assert set(want.to_wire()) == {"ticket", "blob_id", "mode", "hints", "claim"}


@pytest.mark.parametrize(
    "patch",
    [
        {"ticket": "../x"},
        {"blob_id": "zz"},
        {"mode": "forever"},
        {"hints": []},
        {"hints": "x"},
        {"hints": [{"suite": "nope"}]},
        {"hints": [None]},
        {"hints": [_hint() for _ in range(MAX_MEMBER_WANT_HINTS + 1)]},
        {"claim": None},
        {"claim": "C" * 32},
        {"claim": "c" * 31},
    ],
)
def test_member_want_refused(patch) -> None:
    raw = {
        "ticket": "1" * 32,
        "blob_id": BLOB,
        "mode": MODE_CACHE,
        "hints": [_hint()],
        "claim": "c" * 32,
    }
    raw.update(patch)
    with pytest.raises(InvalidOnDemandWire):
        MemberWant.from_wire(raw)


def test_member_want_refuses_non_objects() -> None:
    with pytest.raises(InvalidOnDemandWire):
        MemberWant.from_wire(None)


def test_member_serve_signing_bytes_bind_op_scope_epoch_and_claim() -> None:
    c = "c" * 32
    base = member_serve_signing_bytes("t", "serve", "space", "sp-1", 7, 5, c)
    assert base == MEMBER_SERVE_PREFIX + canonical_bytes(
        {
            "media_ticket": "t",
            "claim": c,
            "op": "serve",
            "scope": "space",
            "scope_id": "sp-1",
            "epoch": 7,
            "ts": 5,
        }
    )
    assert base != member_serve_signing_bytes("t", "decline", "space", "sp-1", 7, 5, c)
    assert base != member_serve_signing_bytes("t", "serve", "space", "sp-2", 7, 5, c)
    assert base != member_serve_signing_bytes("t", "serve", "space", "sp-1", 8, 5, c)
    assert base != member_serve_signing_bytes(
        "t", "serve", "space", "sp-1", 7, 5, "d" * 32
    )
    # Never a pull's bytes.
    assert not base.startswith(MEMBER_PULL_PREFIX)


def test_member_constants() -> None:
    assert MEMBER_PULL_CAPABILITY_KEY == "member_pull"
    assert DECLINE_BUSY == "busy"
    assert DECLINE_BUSY in DECLINE_REASONS
    assert {
        MEMBER_WRITER_EPOCH_HEADER,
        MEMBER_WRITER_SIG_HEADER,
        MEMBER_WRITER_SIG_SUITE_HEADER,
    } == {"X-SH-Writer-Epoch", "X-SH-Writer-Sig", "X-SH-Writer-Sig-Suite"}
