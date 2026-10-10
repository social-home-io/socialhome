"""§27.9 release blocker: a household's signed, identity-bound
``/gfs/subscribe`` (subscribe or unsubscribe) never reaches a GFS that did
not seat the subscription — that request alone would tell the operator this
household follows the space. Likewise the mirror's per-space detail fetch
(``GET /gfs/spaces/{id}``) only goes to a GFS whose whole directory lists the
space: probing the others would disclose the interest (and our address).

Real ``GfsConnectionService`` (real signing, real request bodies) over a
recording HTTP session, real SQLite repos. Both assertions fail against the
pre-change code, which fanned the unsubscribe out to every active GFS and
re-subscribed every followed space on every reconnecting GFS.
"""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_space_seat import GfsSpaceSeat
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.gfs_space_seat_repo import SqliteGfsSpaceSeatRepo
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_space_mirror_service import GfsSpaceMirrorService

pytestmark = pytest.mark.security

LISTING_GFS = "https://listing.gfs.example"
OTHER_GFS = "https://other.gfs.example"
ROTATED_PIN = "bb" * 32


class _Resp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        raw = json.dumps(body or {}).encode()
        self.content_length = len(raw)
        self._raw = raw
        self._body = body or {}
        self.content = self

    async def read(self, n: int = -1) -> bytes:
        out, self._raw = (self._raw, b"") if n < 0 else (self._raw[:n], self._raw[n:])
        return out

    async def json(self):
        return self._body

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Session:
    """Records every request. The listing GFS's directory lists the legacy
    space and ``sp-new`` (with a seatable detail body); the other GFS lists
    nothing of ours."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    def get(self, url, **_kw):
        self.requests.append(("GET", url, {}))
        if url == f"{LISTING_GFS}/gfs/spaces":
            return _Resp(
                200, {"spaces": [{"space_id": "sp-legacy"}, {"space_id": "sp-new"}]}
            )
        if url == f"{LISTING_GFS}/gfs/spaces/sp-new":
            return _Resp(
                200,
                {
                    "space_id": "sp-new",
                    "owning_instance": "remote-host",
                    "name": "New",
                    "status": "active",
                    "identity_public_key": "aa" * 32,
                    "allow_subscribers": True,
                },
            )
        if url == f"{LISTING_GFS}/gfs/spaces/sp-mirrored":
            # The owner rotated: the listing GFS re-pinned to a new key.
            return _Resp(
                200,
                {
                    "space_id": "sp-mirrored",
                    "identity_public_key": ROTATED_PIN,
                    "authority_rotation_seq": 1,
                },
            )
        if url == f"{OTHER_GFS}/gfs/spaces":
            return _Resp(200, {"spaces": [{"space_id": "sp-unrelated"}]})
        return _Resp(404)

    def post(self, url, *, json=None, **_kw):
        self.requests.append(("POST", url, json or {}))
        return _Resp(200, {"status": "ok"})


def _conn(gfs_id: str, url: str) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=gfs_id,
        public_key="pk",
        inbox_url=url,
        status="active",
        paired_at="2025-01-01T00:00:00+00:00",
    )


@pytest.fixture
async def household(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('local','u-local','Local')"
    )
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    conns = SqliteGfsConnectionRepo(db)
    seats = SqliteGfsSpaceSeatRepo(db)
    public = SqlitePublicSpaceRepo(db)
    await conns.save(_conn("listing", LISTING_GFS))
    await conns.save(_conn("other", OTHER_GFS))
    for space_id, seated_by in (("sp-mirrored", "listing"), ("sp-legacy", None)):
        await spaces.save(
            Space(
                id=space_id,
                name="Followed",
                owner_instance_id="remote-host",
                owner_username="them",
                identity_public_key="",
                config_sequence=0,
                space_type=SpaceType.GLOBAL,
                join_mode=JoinMode.OPEN,
                features=SpaceFeatures(allow_subscribers=True),
            )
        )
        await spaces.save_member(
            SpaceMember(
                space_id=space_id,
                user_id="u-local",
                role=SpaceRole.SUBSCRIBER,
                joined_at="2025-01-01T00:00:00+00:00",
            )
        )
        await public.upsert(
            PublicSpaceListing(
                space_id=space_id,
                instance_id="remote-host",
                name="Followed",
                description=None,
                emoji=None,
                lat=None,
                lon=None,
                radius_km=None,
                member_count=1,
            )
        )
        if seated_by is not None:
            await spaces.set_mirror_provenance(
                space_id, gfs_id=seated_by, rotation_seq=0
            )
            # What ``take_seat`` recorded when the follower subscribed.
            await seats.record(
                GfsSpaceSeat(
                    space_id=space_id,
                    gfs_instance_id=f"inst-{seated_by}",
                    gfs_connection_id=seated_by,
                    gfs_public_key="pk",
                    gfs_inbox_url=LISTING_GFS,
                )
            )
    session = _Session()
    gfs = GfsConnectionService(conns, http_client=session)
    gfs.attach_publish_context(
        space_repo=spaces, own_instance_id=iid, own_signing_key=kp.private_key
    )
    mirror = GfsSpaceMirrorService(
        space_repo=spaces,
        gfs_connection_repo=conns,
        gfs_connection_service=gfs,
        seat_repo=seats,
        public_space_repo=public,
    )
    mirror.attach_session(session)
    try:
        yield mirror, session, iid, conns, spaces
    finally:
        await db.shutdown()


def _leaks_to_other(session: _Session, iid: str) -> list[tuple[str, str, dict]]:
    """Requests to the non-seating GFS that carry our identity or a followed
    space id."""
    return [
        (m, url, body)
        for m, url, body in session.requests
        if url.startswith(OTHER_GFS)
        and any(
            marker in url or marker in json.dumps(body)
            for marker in (iid, "sp-mirrored", "sp-legacy", "sp-new")
        )
    ]


async def test_unsubscribe_never_reaches_a_gfs_that_did_not_seat_it(household):
    mirror, session, iid, _conns, _spaces = household
    await mirror.unsubscribe("sp-mirrored")
    await mirror.unsubscribe("sp-legacy")
    await mirror.wait_idle()  # a legacy mirror's lookup runs in the background
    assert _leaks_to_other(session, iid) == []
    unsubscribed = [
        body["space_id"]
        for m, url, body in session.requests
        if m == "POST" and url == f"{LISTING_GFS}/gfs/subscribe"
    ]
    assert sorted(unsubscribed) == ["sp-legacy", "sp-mirrored"]


async def test_reconnect_never_subscribes_on_a_gfs_that_did_not_seat_it(household):
    mirror, session, iid, _conns, _spaces = household
    assert await mirror.resubscribe_all("other") == 0
    assert _leaks_to_other(session, iid) == []
    assert not any(m == "POST" for m, _url, _b in session.requests)
    # The seating GFS still gets its seats back.
    assert await mirror.resubscribe_all("listing") == 2


async def test_mirror_detail_fetch_never_probes_a_gfs_that_does_not_list_it(
    household,
):
    mirror, session, iid, _conns, _spaces = household
    # Listed nowhere: only whole-directory reads, no per-space request at all.
    assert await mirror.ensure_mirror("sp-nowhere") is None
    assert not any("sp-nowhere" in url for _m, url, _b in session.requests)
    # Listed on one GFS: the detail comes from there, never from the other.
    got = await mirror.ensure_mirror("sp-new")
    assert got is not None and got[1] == "listing"
    assert _leaks_to_other(session, iid) == []
    assert ("GET", f"{LISTING_GFS}/gfs/spaces/sp-new", {}) in session.requests


async def test_a_re_paired_server_keeps_the_seat_and_nobody_else_learns_it(
    household,
):
    """H1: a disconnect + re-pair mints a new local connection id for the
    same server. The seat is re-taken there and torn down there — and the
    other server never hears of it."""
    mirror, session, iid, conns, _spaces = household
    await conns.delete("listing")
    await conns.save(
        GfsConnection(
            id="listing-again",
            gfs_instance_id="inst-listing",
            display_name="listing",
            public_key="pk",
            inbox_url=LISTING_GFS,
            status="active",
            paired_at="2025-02-01T00:00:00+00:00",
        )
    )
    assert await mirror.resubscribe_all("other") == 0
    assert await mirror.resubscribe_all("listing-again") == 2
    await mirror.unsubscribe("sp-mirrored")
    assert _leaks_to_other(session, iid) == []
    posts = [
        (body["action"], body["space_id"])
        for m, url, body in session.requests
        if m == "POST" and url == f"{LISTING_GFS}/gfs/subscribe"
    ]
    assert ("unsubscribe", "sp-mirrored") in posts
    assert ("subscribe", "sp-mirrored") in posts


async def _re_pair_listing(conns, *, public_key: str) -> None:
    await conns.delete("listing")
    await conns.save(
        GfsConnection(
            id="listing-again",
            gfs_instance_id="inst-listing",
            display_name="listing",
            public_key=public_key,
            inbox_url=LISTING_GFS,
            status="active",
            paired_at="2025-02-01T00:00:00+00:00",
        )
    )


async def test_a_same_key_re_pair_keeps_healing_the_follower_pin(household):
    """The v_44 pin heal trusts only the GFS that seated the mirror. A
    re-pair of that same server under the SAME key carries the anchor over
    (the reconnect self-heal moves it before re-taking the seat), so a
    rotation still heals."""
    mirror, _session, _iid, conns, spaces = household
    await _re_pair_listing(conns, public_key="pk")
    await mirror.resubscribe_all("listing-again")
    assert await mirror.refresh_authority_pins("listing-again") == 1
    assert (await spaces.get("sp-mirrored")).identity_public_key == ROTATED_PIN
    assert await spaces.get_mirror_provenance("sp-mirrored") == ("listing-again", 1)


async def test_a_re_pair_under_another_key_inherits_no_pin_trust(household):
    """Same server id, DIFFERENT pinned key: nothing proves it is the server
    that seated the mirror, so its listing never re-pins the space — not on
    reconnect, not after the seat was re-taken over it."""
    mirror, _session, _iid, conns, spaces = household
    await _re_pair_listing(conns, public_key="a-different-key")
    await mirror.resubscribe_all("listing-again")
    assert await mirror.refresh_authority_pins("listing-again") == 0
    assert await mirror.refresh_authority_pins("listing-again") == 0
    assert (await spaces.get("sp-mirrored")).identity_public_key == ""
    assert await spaces.get_mirror_provenance("sp-mirrored") == ("listing", 0)
