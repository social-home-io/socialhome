"""§27.9 release blocker: a household's signed, identity-bound
``/gfs/subscribe`` (subscribe or unsubscribe) never reaches a GFS that did
not seat the subscription — that request alone would tell the operator this
household follows the space.

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
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_space_mirror_service import GfsSpaceMirrorService

pytestmark = pytest.mark.security

LISTING_GFS = "https://listing.gfs.example"
OTHER_GFS = "https://other.gfs.example"


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
    """Records every request. The listing GFS's directory lists only the
    legacy space; the other GFS lists nothing of ours."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    def get(self, url, **_kw):
        self.requests.append(("GET", url, {}))
        if url == f"{LISTING_GFS}/gfs/spaces":
            return _Resp(200, {"spaces": [{"space_id": "sp-legacy"}]})
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
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    conns = SqliteGfsConnectionRepo(db)
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
    session = _Session()
    gfs = GfsConnectionService(conns, http_client=session)
    gfs.attach_publish_context(
        space_repo=spaces, own_instance_id=iid, own_signing_key=kp.private_key
    )
    mirror = GfsSpaceMirrorService(
        space_repo=spaces,
        gfs_connection_repo=conns,
        gfs_connection_service=gfs,
        public_space_repo=public,
    )
    mirror.attach_session(session)
    try:
        yield mirror, session, iid
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
            for marker in (iid, "sp-mirrored", "sp-legacy")
        )
    ]


async def test_unsubscribe_never_reaches_a_gfs_that_did_not_seat_it(household):
    mirror, session, iid = household
    await mirror.unsubscribe("sp-mirrored")
    await mirror.unsubscribe("sp-legacy")
    assert _leaks_to_other(session, iid) == []
    unsubscribed = [
        body["space_id"]
        for m, url, body in session.requests
        if m == "POST" and url == f"{LISTING_GFS}/gfs/subscribe"
    ]
    assert sorted(unsubscribed) == ["sp-legacy", "sp-mirrored"]


async def test_reconnect_never_subscribes_on_a_gfs_that_did_not_seat_it(household):
    mirror, session, iid = household
    assert await mirror.resubscribe_all("other") == 0
    assert _leaks_to_other(session, iid) == []
    assert not any(m == "POST" for m, _url, _b in session.requests)
    # The seating GFS still gets its seats back.
    assert await mirror.resubscribe_all("listing") == 2
