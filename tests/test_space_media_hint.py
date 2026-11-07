"""The sealed "where is this file" hint a reader sends with a media wanted."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from socialhome import space_media_hint as mod
from socialhome.space_media_hint import (
    HINT_SUITE_AESGCM256,
    MAX_HINT_CHARS,
    InvalidSpaceMediaHint,
    SpaceMediaLocator,
    UnsupportedSpaceMediaHintSuite,
    open_space_media_hint,
    parse_hint_wire,
    seal_space_media_hint,
)

KEY = b"k" * 32
BLOB = "a" * 64
LOC = SpaceMediaLocator(item_id="post-1", origin="inst-a", filename="p.webp")


def _seal(**over):
    args = {
        "media_key": KEY,
        "scope": "space",
        "scope_id": "sp-1",
        "blob_id": BLOB,
        "epoch": 3,
        "locator": LOC,
    }
    args.update(over)
    return seal_space_media_hint(**args)


def test_round_trip() -> None:
    wire = _seal()
    assert wire["suite"] == HINT_SUITE_AESGCM256
    assert wire["epoch"] == 3
    hint = parse_hint_wire(wire)
    got = open_space_media_hint(
        hint, media_key=KEY, scope="space", scope_id="sp-1", blob_id=BLOB
    )
    assert (got.item_id, got.origin, got.filename) == ("post-1", "inst-a", "p.webp")
    # It carries when it was sealed: fresh now, refused by a holder later.
    assert got.fresh()
    assert not got.fresh(got.issued_at + mod.HINT_MAX_AGE_S + 1)


def test_names_nothing_in_the_clear() -> None:
    wire = _seal()
    flat = repr(wire)
    for secret in ("post-1", "inst-a", "p.webp", "sp-1"):
        assert secret not in flat
    assert len(wire["ct"]) <= MAX_HINT_CHARS


def test_two_seals_differ() -> None:
    # A random nonce: two readers asking for one file send unlinkable hints.
    assert _seal()["ct"] != _seal()["ct"]


@pytest.mark.parametrize(
    "field,value",
    [("scope_id", "sp-2"), ("blob_id", "b" * 64), ("scope", "channel")],
)
def test_bound_to_scope_and_blob(field: str, value: str) -> None:
    hint = parse_hint_wire(_seal())
    args = {"media_key": KEY, "scope": "space", "scope_id": "sp-1", "blob_id": BLOB}
    args[field] = value
    with pytest.raises(InvalidTag):
        open_space_media_hint(hint, **args)


def test_wrong_key() -> None:
    hint = parse_hint_wire(_seal())
    with pytest.raises(InvalidTag):
        open_space_media_hint(
            hint, media_key=b"x" * 32, scope="space", scope_id="sp-1", blob_id=BLOB
        )


def test_unknown_suite_refused() -> None:
    wire = _seal()
    wire["suite"] = "rot13"
    with pytest.raises(UnsupportedSpaceMediaHintSuite):
        parse_hint_wire(wire)


@pytest.mark.parametrize(
    "wire",
    [
        None,
        [],
        {"suite": HINT_SUITE_AESGCM256, "epoch": -1, "ct": "AAAA"},
        {"suite": HINT_SUITE_AESGCM256, "epoch": True, "ct": "AAAA"},
        {"suite": HINT_SUITE_AESGCM256, "epoch": 1, "ct": "!!"},
        {"suite": HINT_SUITE_AESGCM256, "epoch": 1, "ct": "A" * (MAX_HINT_CHARS + 4)},
        {"suite": HINT_SUITE_AESGCM256, "epoch": 1},
    ],
)
def test_malformed_wire(wire) -> None:
    with pytest.raises(InvalidSpaceMediaHint):
        parse_hint_wire(wire)


def test_garbage_plaintext_refused() -> None:
    # A hint that decrypts but names no usable locator (a forging key
    # holder) is refused, never half-used.
    hint = parse_hint_wire(_seal())
    bad = mod._seal_raw(
        KEY, b'{"i": "", "o": 1}', scope="space", scope_id="sp-1", blob_id=BLOB, epoch=3
    )
    with pytest.raises(InvalidSpaceMediaHint):
        open_space_media_hint(
            parse_hint_wire(bad),
            media_key=KEY,
            scope="space",
            scope_id="sp-1",
            blob_id=BLOB,
        )
    assert hint.epoch == 3
