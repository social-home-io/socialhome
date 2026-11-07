"""The holder side of space media on demand (v_83): a GFS asks this
household for a blob a reader missed
(:mod:`socialhome.services.space_media_gfs_responder`)."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from socialhome import space_media_hint as hint_mod
from socialhome.domain.gfs_space_media import (
    AUTH_AUTHORITY,
    SpaceMediaUploadAuth,
    space_media_address,
)
from socialhome.domain.gfs_space_media_on_demand import (
    MODE_CACHE,
    MODE_STREAM,
    MediaRequestFrame,
)
from socialhome.services import space_media_gfs_responder as mod
from socialhome.services.inbound_media_store import (
    gfs_space_media_name,
    space_direct_media_name,
)
from socialhome.services.space_media_gfs_responder import SpaceMediaGfsResponder
from socialhome.services.space_media_gfs_uploader import seal_and_address_sync
from socialhome.space_media_hint import (
    SpaceMediaLocator,
    parse_hint_wire,
    seal_space_media_hint,
)
from socialhome.space_media_seal import derive_space_media_key

SPACE = "sp-1"
CHANNEL = "ab" * 16
OWN = "own-inst"
AUTHOR = "author-inst"
ITEM = "post-1"
KEY = derive_space_media_key(bytes(range(32)))


@dataclass
class _Conn:
    id: str
    inbox_url: str = "https://g1.example"
    status: str = "active"
    registered: bool = True


class _GfsRepo:
    def __init__(self, conns):
        self.conns = {c.id: c for c in conns}

    async def get(self, gfs_id):
        return self.conns.get(gfs_id)


class _Gfs:
    def __init__(self, status=200):
        self.status = status
        self.serves: list[tuple[str, str, bytes | None, str | None]] = []
        self.reasons: list[str] = []

    async def serve_space_media(
        self, conn, ticket, blob, *, upload_auth=None, decline_reason="not_held"
    ):
        self.serves.append((conn.id, ticket, blob, upload_auth))
        if blob is None:
            self.reasons.append(decline_reason)
        return self.status


class _Crypto:
    async def media_key(self, space_id, *, epoch=None):
        if space_id != SPACE or epoch != 4:
            return None
        return 4, KEY


class _Spaces:
    def __init__(self, *, channel=None, owner=OWN):
        self.channel = channel
        self.owner = owner

    async def get(self, space_id):
        if space_id != SPACE:
            return None
        return SimpleNamespace(id=space_id, owner_instance_id=self.owner)

    async def spaces_for_gfs_channel(self, channel_id):
        return [SPACE] if channel_id == self.channel else []


class _MediaRefs:
    def __init__(self, foreign=(), gone=()):
        self.foreign = set(foreign)
        #: Files only a deleted (or held) row shows.
        self.gone = set(gone)

    async def confined_to_space(self, basename, space_id):
        return space_id == SPACE and basename not in self.foreign

    async def live_in_space(self, basename, space_id):
        return space_id == SPACE and basename not in self.gone


class _Uploader:
    def __init__(self, *, authorized=True):
        self.authorized = authorized
        self.remembered: list[tuple[str, str]] = []
        self.claims: list = []

    async def authorize(self, claim, space_id):
        self.claims.append((claim, space_id))
        if not self.authorized:
            return None
        return SpaceMediaUploadAuth(
            claim=claim, kind=AUTH_AUTHORITY, sig="c2ln", sig_suite="ed25519"
        )

    async def remember_stored(self, conn, blob_id):
        self.remembered.append((conn.id, blob_id))


def _responder(tmp_path, *, gfs=None, spaces=None, refs=None, uploader=None):
    r = SpaceMediaGfsResponder(
        gfs_repo=_GfsRepo([_Conn("g1")]),
        gfs_service=gfs or _Gfs(),
        space_repo=spaces or _Spaces(),
        space_crypto=_Crypto(),
        media_refs=refs or _MediaRefs(),
        uploader=uploader or _Uploader(),
        media_dir=tmp_path,
    )
    r.attach_federation(SimpleNamespace(own_instance_id=OWN))
    return r


def _file(tmp_path: pathlib.Path, name: str, size: int = 5000) -> bytes:
    data = os.urandom(size)
    (tmp_path / name).write_bytes(data)
    return data


def _blob(data, *, origin=OWN, filename="a.webp", scope="space", scope_id=SPACE):
    return seal_and_address_sync(
        KEY, SPACE, 4, ITEM, origin, filename, data, scope, scope_id
    )


def _frame(
    blob_id,
    *,
    origin=OWN,
    filename="a.webp",
    mode=MODE_STREAM,
    scope="space",
    scope_id=SPACE,
    key=KEY,
    ticket=None,
) -> dict:
    hint = seal_space_media_hint(
        media_key=key,
        scope=scope,
        scope_id=scope_id,
        blob_id=blob_id,
        epoch=4,
        locator=SpaceMediaLocator(item_id=ITEM, origin=origin, filename=filename),
    )
    return MediaRequestFrame(
        ticket=ticket or os.urandom(16).hex(),
        scope=scope,
        scope_id=scope_id,
        blob_id=blob_id,
        mode=mode,
        hint=parse_hint_wire(hint),
    ).to_push()


async def _ask(r, frame) -> None:
    await r.handle(frame, gfs_id="g1")
    await r.drain()


async def test_its_own_file_is_streamed_byte_identical(tmp_path):
    data = _file(tmp_path, "a.webp")
    blob, bid = _blob(data)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await _ask(r, _frame(bid))
    assert gfs.serves == [("g1", gfs.serves[0][1], blob, None)]


async def test_a_cache_request_attaches_the_upload_authorization_once(tmp_path):
    data = _file(tmp_path, "a.webp")
    blob, bid = _blob(data)
    gfs, up = _Gfs(), _Uploader()
    r = _responder(tmp_path, gfs=gfs, uploader=up)
    await _ask(r, _frame(bid, mode=MODE_CACHE))
    auth = SpaceMediaUploadAuth.from_header(gfs.serves[0][3])
    assert (auth.claim.blob_id, auth.claim.size, auth.claim.scope) == (
        bid,
        len(blob),
        "space",
    )
    assert up.remembered == [("g1", bid)]
    # Coalesced at the holder: a second cache request for the same blob in
    # the window streams only — one re-upload per blob.
    await _ask(r, _frame(bid, mode=MODE_CACHE))
    assert gfs.serves[1][3] is None
    assert len(up.claims) == 1


async def test_a_file_another_household_delivered_is_found_under_its_name(tmp_path):
    """The host holds a member's picture under the name it received it
    as (``sd-`` direct, ``sg-`` via a GFS) and re-seals it byte-identical."""
    for name in (
        space_direct_media_name(SPACE, AUTHOR, "m.webp"),
        gfs_space_media_name(SPACE, AUTHOR, ITEM, "m.webp"),
    ):
        data = _file(tmp_path, name)
        blob, bid = _blob(data, origin=AUTHOR, filename="m.webp")
        gfs = _Gfs()
        r = _responder(tmp_path, gfs=gfs)
        await _ask(r, _frame(bid, origin=AUTHOR, filename="m.webp"))
        assert gfs.serves[0][2] == blob
        (tmp_path / name).unlink()


async def test_a_private_channel_blob_is_served_under_the_channel_scope(tmp_path):
    data = _file(tmp_path, "a.webp")
    blob, bid = _blob(data, scope="channel", scope_id=CHANNEL)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, spaces=_Spaces(channel=CHANNEL))
    await _ask(r, _frame(bid, scope="channel", scope_id=CHANNEL))
    assert gfs.serves[0][2] == blob
    assert space_media_address("channel", CHANNEL, blob) == bid


@pytest.mark.parametrize(
    "case",
    ["unreferenced", "reserved", "other_blob", "no_key", "not_member", "no_file"],
)
async def test_what_the_space_does_not_reference_is_declined(tmp_path, case):
    """Never a file the space doesn't reference (``confined_to_space``),
    never a DM / moment name, never bytes at another address."""
    data = _file(tmp_path, "a.webp")
    _blob_bytes, bid = _blob(data)
    refs, spaces = _MediaRefs(), _Spaces()
    frame = _frame(bid)
    if case == "unreferenced":
        refs = _MediaRefs(foreign={"a.webp"})
    elif case == "reserved":
        dm = "dm-" + "0" * 40 + ".webp"
        (tmp_path / dm).write_bytes(data)
        frame = _frame(_blob(data, filename=dm)[1], filename=dm)
    elif case == "other_blob":
        frame = _frame("c" * 64)
    elif case == "no_key":
        frame = _frame(bid, key=os.urandom(32))
    elif case == "not_member":
        frame = _frame(bid, scope_id="sp-other")
    elif case == "no_file":
        (tmp_path / "a.webp").unlink()
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, refs=refs, spaces=spaces)
    await _ask(r, frame)
    assert [(s[2], s[3]) for s in gfs.serves] in ([(None, None)], [])
    assert all(s[2] is None for s in gfs.serves)


async def test_the_holder_byte_budget_declines_past_it(tmp_path, monkeypatch):
    data = _file(tmp_path, "a.webp", 70_000)
    blob, bid = _blob(data)
    monkeypatch.setattr(mod, "HOLDER_BYTES_PER_HOUR_PER_GFS", len(blob) + 1)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await _ask(r, _frame(bid))
    await _ask(r, _frame(bid))
    assert gfs.serves[0][2] == blob
    assert gfs.serves[1][2] is None


async def test_requests_past_the_per_gfs_cap_are_dropped_before_any_work(
    tmp_path, monkeypatch
):
    """Review M5: a GFS flooding requests costs no task, no signature and no
    request past the cap — it asks the next holder in time."""
    monkeypatch.setattr(mod, "MAX_SERVES_PER_GFS", 0)
    data = _file(tmp_path, "a.webp")
    _b, bid = _blob(data)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await _ask(r, _frame(bid))
    assert gfs.serves == []
    assert r._tasks == set()


async def test_a_malformed_frame_or_unknown_gfs_does_nothing(tmp_path):
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await r.handle({"type": "space_media_request", "ticket": "x"}, gfs_id="g1")
    data = _file(tmp_path, "a.webp")
    await r.handle(_frame(_blob(data)[1]), gfs_id="unknown")
    await r.drain()
    assert gfs.serves == []


# ── Review regressions (adversarial round 1) ─────────────────────────────


class _SpyCrypto(_Crypto):
    def __init__(self):
        self.asked = 0

    async def media_key(self, space_id, *, epoch=None):
        self.asked += 1
        return await super().media_key(space_id, epoch=epoch)


@pytest.mark.parametrize("owner", ["someone-else", None])
async def test_only_the_owner_answers_for_a_listed_space(tmp_path, owner):
    """Review H2: a GFS replaying a reader's hint to other households must
    not learn who is a member — a member that doesn't own the space (and a
    non-member) declines the same way, before opening anything."""
    data = _file(tmp_path, "a.webp")
    _b, bid = _blob(data)
    gfs, crypto = _Gfs(), _SpyCrypto()
    spaces = _Spaces(owner=owner) if owner else _Spaces()
    r = SpaceMediaGfsResponder(
        gfs_repo=_GfsRepo([_Conn("g1")]),
        gfs_service=gfs,
        space_repo=spaces,
        space_crypto=crypto,
        media_refs=_MediaRefs(),
        uploader=_Uploader(),
        media_dir=tmp_path,
    )
    r.attach_federation(SimpleNamespace(own_instance_id=OWN))
    frame = _frame(bid) if owner else _frame(bid, scope_id="sp-other")
    await _ask(r, frame)
    assert [s[2] for s in gfs.serves] == [None]
    assert gfs.reasons == ["not_held"]
    assert crypto.asked == 0


async def test_a_stale_or_foreign_hint_is_a_bad_hint(tmp_path, monkeypatch):
    """A hint older than its window (a replay) or under another key is
    declined as ``bad_hint`` — the GFS then tries another reader's hint."""
    data = _file(tmp_path, "a.webp")
    _b, bid = _blob(data)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await _ask(r, _frame(bid, key=os.urandom(32)))
    monkeypatch.setattr(hint_mod, "HINT_MAX_AGE_S", -1)
    await _ask(r, _frame(bid))
    assert gfs.reasons == ["bad_hint", "bad_hint"]


async def test_a_deleted_items_file_is_never_sent_again(tmp_path):
    """Review M6: only a file a LIVE row of the space shows is sent."""
    data = _file(tmp_path, "a.webp")
    _b, bid = _blob(data)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, refs=_MediaRefs(gone={"a.webp"}))
    await _ask(r, _frame(bid))
    assert [s[2] for s in gfs.serves] == [None]
    assert gfs.reasons == ["not_held"]


async def test_one_gfs_cannot_spend_the_budget_of_another(tmp_path, monkeypatch):
    """Review M2: the byte budget is per GFS too."""
    data = _file(tmp_path, "a.webp", 70_000)
    blob, bid = _blob(data)
    monkeypatch.setattr(mod, "HOLDER_BYTES_PER_HOUR_PER_GFS", len(blob) + 1)
    gfs = _Gfs()
    r = SpaceMediaGfsResponder(
        gfs_repo=_GfsRepo([_Conn("g1"), _Conn("g2", "https://g2.example")]),
        gfs_service=gfs,
        space_repo=_Spaces(),
        space_crypto=_Crypto(),
        media_refs=_MediaRefs(),
        uploader=_Uploader(),
        media_dir=tmp_path,
    )
    r.attach_federation(SimpleNamespace(own_instance_id=OWN))
    for gfs_id in ("g1", "g1", "g2"):
        await r.handle(_frame(bid), gfs_id=gfs_id)
        await r.drain()
    assert [(s[0], s[2] is not None) for s in gfs.serves] == [
        ("g1", True),
        ("g1", False),
        ("g2", True),
    ]
