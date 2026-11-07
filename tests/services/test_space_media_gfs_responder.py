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
    MemberWant,
)
from socialhome.domain.space_media_ref import space_page_media_name
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
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
    def __init__(self, conns, *, listed=None):
        self.conns = {c.id: c for c in conns}
        #: space → the connections it is listed on (published).
        self.listed = {SPACE: {"g1", "g2"}} if listed is None else listed

    async def get(self, gfs_id):
        return self.conns.get(gfs_id)

    async def list_publications_for_space(self, space_id):
        return [
            SimpleNamespace(space_id=space_id, gfs_connection_id=g, status="active")
            for g in self.listed.get(space_id, ())
        ]


class _Gfs:
    def __init__(self, status=200):
        self.status = status
        self.serves: list[tuple[str, str, bytes | None, str | None]] = []
        self.reasons: list[str] = []

    def known_instance_ids(self, conn):
        return frozenset({"inst-" + conn.id})

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


class _Channels:
    """This household's channel grant: seated on GFS g1 only."""

    def __init__(self, gfs_ids=("inst-g1",)):
        self.gfs_ids = gfs_ids

    async def current_grant(self, space_id):
        if space_id != SPACE:
            return None
        return SimpleNamespace(channel_id=CHANNEL, gfs_ids=self.gfs_ids)


def _responder(
    tmp_path, *, gfs=None, spaces=None, refs=None, uploader=None, gfs_repo=None
):
    r = SpaceMediaGfsResponder(
        gfs_repo=gfs_repo or _GfsRepo([_Conn("g1")]),
        gfs_service=gfs or _Gfs(),
        space_repo=spaces or _Spaces(),
        space_crypto=_Crypto(),
        media_refs=refs or _MediaRefs(),
        uploader=uploader or _Uploader(),
        media_dir=tmp_path,
    )
    r.attach_federation(SimpleNamespace(own_instance_id=OWN))
    r.attach_channels(_Channels())
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


async def test_bytes_sent_count_whatever_the_gfs_answers(tmp_path, monkeypatch):
    """#1317 review 3: the budget is charged BEFORE the bytes go — a GFS
    answering 409 / 422 / 429 after reading the body gets no more uploads
    than the budget; only a send that never left (no connection) is
    refunded."""
    data = _file(tmp_path, "a.webp", 70_000)
    blob, bid = _blob(data)
    monkeypatch.setattr(mod, "HOLDER_BYTES_PER_HOUR_PER_GFS", 2 * len(blob) + 1)
    for status in (409, 422, 429):
        gfs = _Gfs(status=status)
        r = _responder(tmp_path, gfs=gfs)
        for _ in range(3):
            await _ask(r, _frame(bid))
        assert [x[2] is not None for x in gfs.serves] == [True, True, False]
    # Nothing left this household (the connection failed): refunded.
    gfs = _Gfs(status=None)
    r = _responder(tmp_path, gfs=gfs)
    for _ in range(3):
        await _ask(r, _frame(bid))
    assert [x[2] is not None for x in gfs.serves] == [True, True, True]


class _CountingRefs(_MediaRefs):
    def __init__(self):
        super().__init__()
        self.asked = 0

    async def confined_to_space(self, basename, space_id):
        self.asked += 1
        return await super().confined_to_space(basename, space_id)


async def test_a_locator_naming_another_address_reseals_once(tmp_path):
    """Review N4: a key holder crafting hints that name a real file at the
    wrong address costs the holder one reseal, not one per request."""
    _file(tmp_path, "a.webp")
    refs = _CountingRefs()
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, refs=refs)
    await _ask(r, _frame("c" * 64))
    asked = refs.asked
    await _ask(r, _frame("c" * 64))
    assert gfs.reasons == ["bad_hint", "bad_hint"]
    assert refs.asked == asked


# ── #1317 independent review ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "case", ["unlisted", "listed_elsewhere", "private_unlisted", "no_seat_here"]
)
async def test_only_a_space_listed_on_that_gfs_is_answered(tmp_path, case):
    """#1317 review 5: a household answers for a listed space only when it
    owns it AND it is listed on the asking GFS (a channel: it holds a seat
    there). Everything else is ``not_held``, before any crypto — so a GFS
    can't probe which spaces (private ones included) a household owns."""
    data = _file(tmp_path, "a.webp")
    crypto = _SpyCrypto()
    gfs = _Gfs()
    listed = {
        "unlisted": {},
        "listed_elsewhere": {SPACE: {"g9"}},
        "private_unlisted": {},
        "no_seat_here": None,
    }[case]
    r = SpaceMediaGfsResponder(
        gfs_repo=_GfsRepo([_Conn("g1")], listed=listed),
        gfs_service=gfs,
        space_repo=_Spaces(channel=CHANNEL),
        space_crypto=crypto,
        media_refs=_MediaRefs(),
        uploader=_Uploader(),
        media_dir=tmp_path,
    )
    r.attach_federation(SimpleNamespace(own_instance_id=OWN))
    r.attach_channels(_Channels(gfs_ids=("inst-g9",)))
    if case == "no_seat_here":
        _b, bid = _blob(data, scope="channel", scope_id=CHANNEL)
        frame = _frame(bid, scope="channel", scope_id=CHANNEL)
    else:
        _b, bid = _blob(data)
        frame = _frame(bid)
    await _ask(r, frame)
    assert gfs.reasons == ["not_held"]
    assert crypto.asked == 0


async def test_one_locator_reseals_once_whatever_blob_ids_it_is_asked_for(tmp_path):
    """#1317 review 4: one valid locator sent with many made-up blob ids
    costs the holder ONE reseal, not one per id."""
    _file(tmp_path, "a.webp")
    refs = _CountingRefs()
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, refs=refs)
    await _ask(r, _frame("c" * 64))
    asked = refs.asked
    for n in range(5):
        await _ask(r, _frame(f"{n}" * 64))
    assert gfs.reasons == ["bad_hint"] * 6
    assert refs.asked == asked


async def test_a_members_page_copy_is_served_for_a_channel(tmp_path):
    """v_83 × v_82 — a private space's seated member keeps a page's file
    under its page-scoped name (``sp-`` of space and the host's name); asked
    for it over the channel, it re-seals that copy byte-identical."""
    name = space_page_media_name(SPACE, "pagepic.webp")
    data = _file(tmp_path, name)
    blob, bid = _blob(
        data,
        origin=AUTHOR,
        filename="pagepic.webp",
        scope="channel",
        scope_id=CHANNEL,
    )
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs, spaces=_Spaces(channel=CHANNEL))
    await _ask(
        r,
        _frame(
            bid,
            origin=AUTHOR,
            filename="pagepic.webp",
            scope="channel",
            scope_id=CHANNEL,
        ),
    )
    assert gfs.serves[0][2] == blob


async def test_a_page_copy_the_live_page_no_longer_shows_is_declined(tmp_path):
    name = space_page_media_name(SPACE, "pagepic.webp")
    data = _file(tmp_path, name)
    _b, bid = _blob(
        data, origin=AUTHOR, filename="pagepic.webp", scope="channel", scope_id=CHANNEL
    )
    gfs = _Gfs()
    r = _responder(
        tmp_path,
        gfs=gfs,
        spaces=_Spaces(channel=CHANNEL),
        refs=_MediaRefs(gone={name}),
    )
    await _ask(
        r,
        _frame(
            bid,
            origin=AUTHOR,
            filename="pagepic.webp",
            scope="channel",
            scope_id=CHANNEL,
        ),
    )
    assert gfs.reasons == ["not_held"]


async def test_a_member_never_serves_its_own_file_a_hostile_page_names(db, tmp_path):
    """#1317 review 1: on a MEMBER household a page body naming one of our
    own raw files (an orphan, a deleted post's picture) makes it neither
    confined nor live — asked with a hint that names us as the sealer, we
    decline ``not_held`` and send nothing. Real repository, member DB."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES('u1', 'u1', 'U1')"
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'the-host', 'u1', 'k')",
        (SPACE,),
    )
    await db.enqueue(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg', ?, 'P', '![x](api/media/orphan.webp)', 'u1')",
        (SPACE,),
    )
    data = _file(tmp_path, "orphan.webp")
    _b, bid = _blob(data, filename="orphan.webp", scope="channel", scope_id=CHANNEL)
    gfs = _Gfs()
    r = _responder(
        tmp_path,
        gfs=gfs,
        spaces=_Spaces(channel=CHANNEL),
        refs=SqliteMediaReferenceRepo(db),
    )
    await _ask(
        r, _frame(bid, filename="orphan.webp", scope="channel", scope_id=CHANNEL)
    )
    assert [x[2] for x in gfs.serves] == [None]
    assert gfs.reasons == ["not_held"]


# ── Member fallback (a member household serves a want it pulled) ─────────

WRITER_SEED = bytes(range(1, 33))


class _MemberGfs(_Gfs):
    """Records member serves apart from the owner's household-signed ones."""

    def __init__(self, status=200):
        super().__init__(status)
        self.member: list[dict] = []

    async def serve_space_media_as_member(
        self,
        conn,
        ticket,
        blob,
        *,
        space_id,
        epoch,
        writer_seed,
        claim,
        upload_auth=None,
        decline_reason="not_held",
    ):
        self.member.append(
            {
                "claim": claim,
                "conn": conn.id,
                "ticket": ticket,
                "blob": blob,
                "space": space_id,
                "epoch": epoch,
                "seed": writer_seed,
                "auth": upload_auth,
                "reason": None if blob is not None else decline_reason,
            }
        )
        return self.status


class _AnonUploader(_Uploader):
    def __init__(self, *, authorized=True):
        super().__init__(authorized=authorized)
        self.anonymous: list = []

    async def authorize(self, claim, space_id):  # pragma: no cover - must not run
        raise AssertionError("a member serve never uses the identified path")

    async def authorize_anonymous(self, claim):
        self.anonymous.append(claim)
        if not self.authorized:
            return None
        return SpaceMediaUploadAuth(
            claim=claim, kind=AUTH_AUTHORITY, sig="c2ln", sig_suite="ed25519"
        )


def _want(bid, *hints, mode=MODE_STREAM, ticket=None):
    """A pulled want naming *bid*, with the readers' hints (default: one
    naming AUTHOR's ``a.webp``)."""
    if not hints:
        hints = (_hint(bid),)
    return MemberWant(
        ticket=ticket or os.urandom(16).hex(),
        blob_id=bid,
        mode=mode,
        hints=tuple(parse_hint_wire(h) for h in hints),
        claim="c" * 32,
    )


def _hint(bid, *, origin=AUTHOR, filename="a.webp", key=KEY, item=ITEM):
    return seal_space_media_hint(
        media_key=key,
        scope="space",
        scope_id=SPACE,
        blob_id=bid,
        epoch=4,
        locator=SpaceMediaLocator(item_id=item, origin=origin, filename=filename),
    )


def _member(tmp_path, *, gfs=None, refs=None, uploader=None, owner="the-host"):
    return _responder(
        tmp_path,
        gfs=gfs or _MemberGfs(),
        spaces=_Spaces(owner=owner),
        refs=refs,
        uploader=uploader or _AnonUploader(),
    )


async def _answer(r, want, *, conn=None):
    return await r.answer_member_want(
        want, conn or _Conn("g1"), space_id=SPACE, epoch=7, writer_seed=WRITER_SEED
    )


async def test_a_member_serves_a_file_it_got_from_the_author_byte_identical(
    tmp_path,
):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    blob, bid = _blob(data, origin=AUTHOR)
    gfs = _MemberGfs()
    r = _member(tmp_path, gfs=gfs)
    assert await _answer(r, _want(bid)) == "served"
    assert gfs.serves == []  # never the household-signed owner path
    assert len(gfs.member) == 1
    m = gfs.member[0]
    assert (m["blob"], m["space"], m["epoch"], m["seed"]) == (
        blob,
        SPACE,
        7,
        WRITER_SEED,
    )
    assert m["auth"] is None
    assert m["claim"] == "c" * 32  # the pull's claim secret goes back


async def test_a_member_also_finds_a_gfs_delivered_copy(tmp_path):
    data = _file(tmp_path, gfs_space_media_name(SPACE, AUTHOR, ITEM, "a.webp"))
    blob, bid = _blob(data, origin=AUTHOR)
    gfs = _MemberGfs()
    assert await _answer(_member(tmp_path, gfs=gfs), _want(bid)) == "served"
    assert gfs.member[0]["blob"] == blob


async def test_a_member_tries_every_hint(tmp_path):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    blob, bid = _blob(data, origin=AUTHOR)
    foreign = derive_space_media_key(bytes(32))
    gfs = _MemberGfs()
    want = _want(bid, _hint(bid, key=foreign), _hint(bid))
    assert await _answer(_member(tmp_path, gfs=gfs), want) == "served"
    assert gfs.member[0]["blob"] == blob


async def test_a_cache_want_reuploads_only_under_an_anonymous_authorization(
    tmp_path,
):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    _b, bid = _blob(data, origin=AUTHOR)
    gfs, up = _MemberGfs(), _AnonUploader()
    r = _member(tmp_path, gfs=gfs, uploader=up)
    assert await _answer(r, _want(bid, mode=MODE_CACHE)) == "served"
    assert await _answer(r, _want(bid, mode=MODE_CACHE)) == "served"
    # Once per (GFS, blob) — the owner path's coalescing window, shared.
    assert [m["auth"] is not None for m in gfs.member] == [True, False]
    assert len(up.anonymous) == 1
    assert up.anonymous[0].blob_id == bid
    assert up.remembered == [("g1", bid)]


async def test_no_anonymous_authorization_streams_only(tmp_path):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    blob, bid = _blob(data, origin=AUTHOR)
    gfs = _MemberGfs()
    r = _member(tmp_path, gfs=gfs, uploader=_AnonUploader(authorized=False))
    assert await _answer(r, _want(bid, mode=MODE_CACHE)) == "served"
    assert gfs.member[0]["blob"] == blob and gfs.member[0]["auth"] is None


@pytest.mark.parametrize("case", ["foreign", "gone"])
async def test_a_member_never_serves_an_unconfined_or_deleted_file(tmp_path, case):
    name = space_direct_media_name(SPACE, AUTHOR, "a.webp")
    data = _file(tmp_path, name)
    _b, bid = _blob(data, origin=AUTHOR)
    refs = _MediaRefs(foreign={name}) if case == "foreign" else _MediaRefs(gone={name})
    gfs = _MemberGfs()
    assert await _answer(_member(tmp_path, gfs=gfs, refs=refs), _want(bid)) == (
        "declined"
    )
    assert [(m["blob"], m["reason"]) for m in gfs.member] == [(None, "not_held")]


async def test_a_member_never_serves_a_reserved_name(tmp_path):
    """Its own sealed name counts only when it is no reserved name (a DM /
    moment file is never a space's)."""
    reserved = "dm-" + "ab" * 16 + ".webp"
    data = _file(tmp_path, reserved)
    _b, bid = _blob(data, origin=OWN, filename=reserved)
    gfs = _MemberGfs()
    want = _want(bid, _hint(bid, origin=OWN, filename=reserved))
    assert await _answer(_member(tmp_path, gfs=gfs), want) == "declined"
    assert gfs.member[0]["blob"] is None


async def test_a_member_never_serves_a_raw_page_file(db, tmp_path):
    """#1317 member-side page rule, on the member path: a page body naming
    one of our own raw files makes it neither confined nor live — only a
    ``sp-`` copy of a page's file counts on a member."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES('u1', 'u1', 'U1')"
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'the-host', 'u1', 'k')",
        (SPACE,),
    )
    await db.enqueue(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg', ?, 'P', '![x](api/media/orphan.webp)', 'u1')",
        (SPACE,),
    )
    data = _file(tmp_path, "orphan.webp")
    _b, bid = _blob(data, origin=OWN, filename="orphan.webp")
    gfs = _MemberGfs()
    r = _member(tmp_path, gfs=gfs, refs=SqliteMediaReferenceRepo(db))
    want = _want(bid, _hint(bid, origin=OWN, filename="orphan.webp"))
    assert await _answer(r, want) == "declined"
    assert [m["blob"] for m in gfs.member] == [None]


async def test_a_members_page_copy_is_served(tmp_path):
    name = space_page_media_name(SPACE, "pagepic.webp")
    data = _file(tmp_path, name)
    blob, bid = _blob(data, origin=AUTHOR, filename="pagepic.webp")
    gfs = _MemberGfs()
    want = _want(bid, _hint(bid, origin=AUTHOR, filename="pagepic.webp"))
    assert await _answer(_member(tmp_path, gfs=gfs), want) == "served"
    assert gfs.member[0]["blob"] == blob


@pytest.mark.parametrize("case", ["owner", "unknown_space"])
async def test_the_owner_or_a_stranger_never_answers_as_a_member(tmp_path, case):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    _b, bid = _blob(data, origin=AUTHOR)
    gfs = _MemberGfs()
    r = _member(tmp_path, gfs=gfs, owner=OWN if case == "owner" else "the-host")
    want = _want(bid)
    if case == "unknown_space":
        out = await r.answer_member_want(
            want, _Conn("g1"), space_id="sp-other", epoch=7, writer_seed=WRITER_SEED
        )
    else:
        out = await _answer(r, want)
    assert out == "declined"
    assert [m["blob"] for m in gfs.member] == [None]


async def test_a_member_over_its_budget_declines_busy(tmp_path, monkeypatch):
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"), 70_000)
    blob, bid = _blob(data, origin=AUTHOR)
    monkeypatch.setattr(mod, "HOLDER_BYTES_PER_HOUR_PER_GFS", len(blob) + 1)
    gfs = _MemberGfs()
    r = _member(tmp_path, gfs=gfs)
    assert await _answer(r, _want(bid)) == "served"
    assert await _answer(r, _want(bid)) == "declined"
    assert gfs.member[1]["reason"] == "busy"


async def test_a_member_past_the_in_flight_cap_declines_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MAX_SERVES_PER_GFS", 0)
    data = _file(tmp_path, space_direct_media_name(SPACE, AUTHOR, "a.webp"))
    _b, bid = _blob(data, origin=AUTHOR)
    gfs = _MemberGfs()
    assert await _answer(_member(tmp_path, gfs=gfs), _want(bid)) == "declined"
    assert gfs.member[0]["reason"] == "busy"


async def test_the_owner_over_its_budget_declines_busy(tmp_path, monkeypatch):
    """Busy (not "not held"): the GFS may then ask the space's members."""
    data = _file(tmp_path, "a.webp", 70_000)
    blob, bid = _blob(data)
    monkeypatch.setattr(mod, "HOLDER_BYTES_PER_HOUR_PER_GFS", len(blob) + 1)
    gfs = _Gfs()
    r = _responder(tmp_path, gfs=gfs)
    await _ask(r, _frame(bid))
    await _ask(r, _frame(bid))
    assert gfs.reasons == ["busy"]
