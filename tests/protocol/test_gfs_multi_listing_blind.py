"""§27.9 release blocker (v_57): a household reaches EVERY GFS the space's
owner listed — including servers it never connected to — and a server it
never connected to learns nothing about who it is.

Since v_96 the household's anonymous space traffic is the opaque channel's
(:mod:`socialhome.services.gfs_channel_service`: ``publish-anon``, seats,
notices); it resolves its targets with
:meth:`GfsConnectionService.server_targets` and sends over
:meth:`GfsConnectionService.anon_session` — the very path the removed
space-keyed member publish used. Two real GFS apps; the household's REAL
connection service; registered on GFS A only, the owner's signed list
names A and B. Checked:

* nothing that reaches B — over any session — carries the household's
  instance id, identity key, a household signature, a cookie, or a writer
  cert; B gets only its proof of possession and the anonymous request, over
  a listing session, and keeps no ``client_instances`` row for us;
* targets come only from the owner's list: a list that leaves B out sends
  nothing to B; a listed URL B cannot prove possession at gets nothing; a
  listed server on a private address we never joined is dropped before any
  request (SSRF);
* the space-keyed household senders are gone (no ``/gfs/publish``, no
  ``/gfs/member-publish-anon`` from the household).

Dropped with the space-keyed member publish (v_96): "a ciphertext sent
anonymously never goes identified on a retry" and "a delisted server gets
no retry" (that sender and its retry queue no longer exist; the channel
service re-resolves the list on every retry, ``_retry_listing``), and "the
host relay reaches an unregistered GFS identity-free" (the host relay is
gone; its removal is pinned below).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import time
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_channel import CHANNEL_PUBLISH_ANON_ROUTE
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_space_epoch_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.event_bus import EventBus
from socialhome.outbound_fetch import OutboundFetchRefused
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_member_publish_service import GfsMemberPublishService
from socialhome.services.gfs_server_selection_service import (
    GfsServerSelectionService,
)
from socialhome.services.gfs_server_targets import listing_target_id
from socialhome.writer_key import issue_writer_key_grant

pytestmark = pytest.mark.security

SPACE_ID = "sp-multi"
EPOCH = 4
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
USERS = ("alice-user-id",)


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)

    def leaks(self) -> tuple[str, ...]:
        return (self.instance_id, self.pk.hex(), b64url_encode(self.pk))


def _recorder(seen: list):
    """What a GFS saw arrive: method, path, headers (the body is recorded on
    the sending side — reading it here would consume the stream)."""

    @web.middleware
    async def record(request: web.Request, handler):
        seen.append(
            {
                "method": request.method,
                "path": request.path,
                "query": request.query_string,
                "headers": dict(request.headers),
            }
        )
        return await handler(request)

    return record


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _gfs(tmp_dir, gid: str, seen: list) -> TestClient:
    # The server's base_url is the address households reach it at, so its
    # proof of possession (bound to that URL) verifies for the listed URL.
    port = _free_port()
    cfg = GfsConfig(
        host="127.0.0.1",
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        data_dir=str(tmp_dir / gid),
        instance_id=gid,
        cluster_enabled=False,
        cluster_node_id=gid,
        cluster_peers=(),
    )
    (tmp_dir / gid).mkdir()
    app = create_gfs_app(cfg)
    app.middlewares.append(_recorder(seen))
    tc = TestClient(TestServer(app, host="127.0.0.1", port=port))
    await tc.start_server()
    return tc


class _Session:
    """Wraps a session; records every request it makes."""

    def __init__(self, session: aiohttp.ClientSession, log: list, name: str):
        self._s = session
        self._log = log
        self._name = name

    def post(self, url, *, json=None, **kw):
        self._log.append((self._name, "POST", str(url), json))
        return self._s.post(url, json=json, **kw)

    def get(self, url, *a, **kw):
        self._log.append((self._name, "GET", str(url), None))
        return self._s.get(url, *a, **kw)

    async def close(self):
        return None


async def _info(tc: TestClient) -> dict:
    resp = await tc.get("/gfs/info")
    return await resp.json()


async def _prepare_gfs(tc: TestClient, owner: _Household, follower: _Household):
    """The space listed there with the owner's (trusted-mode) epoch notice
    applied — epoch confirmed, writer key pinned — and a follower seated."""
    fed = tc.app[gfs_fed_repo_key]
    for h in (owner, follower):
        await fed.upsert_instance(
            ClientInstance(
                instance_id=h.instance_id,
                display_name="H",
                public_key=h.pk.hex(),
                status="active",
            )
        )
    await fed.upsert_space(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=owner.instance_id,
            allow_subscribers=True,
            status="active",
            identity_public_key=SPACE_PK.hex(),
        )
    )
    await fed.add_subscriber(space_id=SPACE_ID, instance_id=follower.instance_id)
    await fed.mark_relay_seen(follower.instance_id, at=int(time.time()))
    epochs = tc.app[gfs_space_epoch_repo_key]
    await epochs.confirm(SPACE_ID, EPOCH, now=int(time.time()))
    wkc = issue_writer_key_grant(
        space_seed=SPACE_SEED, space_id=SPACE_ID, epoch=EPOCH
    ).writer_key_cert
    await epochs.pin_writer_key(SPACE_ID, EPOCH, wkc.writer_pk, replace=True)


@pytest.fixture
async def world(tmp_dir):
    seen_a: list = []
    seen_b: list = []
    a = await _gfs(tmp_dir, "gfs-a", seen_a)
    b = await _gfs(tmp_dir, "gfs-b", seen_b)
    me, owner, follower = _Household(), _Household(), _Household()
    for tc in (a, b):
        await _prepare_gfs(tc, owner, follower)
    # Registered on A only.
    await a.app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=me.instance_id,
            display_name="me",
            public_key=me.pk.hex(),
            status="active",
        )
    )
    info_a, info_b = await _info(a), await _info(b)
    url_a = str(a.make_url("")).rstrip("/")
    url_b = str(b.make_url("")).rstrip("/")
    db = AsyncDatabase(tmp_dir / "household.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteGfsConnectionRepo(db)
    conn_a = GfsConnection(
        id="conn-a",
        gfs_instance_id="gfs-a",
        display_name="A",
        public_key=info_a["public_key"],
        inbox_url=url_a,
        status="active",
        paired_at="",
    )
    await repo.save(conn_a)
    sent: list = []
    # Cookie-carrying shared session (a sticky cookie for every host), the
    # cookie-less publish session, and the listing session factory.
    identified_raw = aiohttp.ClientSession()
    identified_raw.cookie_jar.update_cookies(
        {"sticky": "household-session"}, response_url=a.make_url("/")
    )
    anon_raw = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    listing_opened: list[str] = []

    async def _listing(url: str):
        listing_opened.append(url)
        raw = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        wrapped = _Session(raw, sent, "listing")

        async def _close():
            await raw.close()

        wrapped.close = _close  # type: ignore[method-assign]
        return wrapped

    gfs = GfsConnectionService(
        repo,
        http_client=_Session(identified_raw, sent, "identified"),  # type: ignore[arg-type]
        publish_client=_Session(anon_raw, sent, "anon"),  # type: ignore[arg-type]
        listing_sessions=_listing,
    )
    servers = {
        "list": (
            GfsServer(
                gfs_instance_id="gfs-a",
                inbox_url=url_a,
                public_key=info_a["public_key"],
                gfs_key_suite="ed25519",
                display_name="A",
            ),
            GfsServer(
                gfs_instance_id="gfs-b",
                inbox_url=url_b,
                public_key=info_b["public_key"],
                gfs_key_suite="ed25519",
                display_name="B",
            ),
        )
    }

    async def _member_servers(space_id):
        return servers["list"]

    gfs.attach_member_servers(_member_servers)
    seen_a.clear()
    seen_b.clear()
    yield {
        "a": a,
        "b": b,
        "seen_a": seen_a,
        "seen_b": seen_b,
        "me": me,
        "owner": owner,
        "follower": follower,
        "gfs": gfs,
        "servers": servers,
        "sent": sent,
        "listing_opened": listing_opened,
        "url_b": url_b,
        "b_target": listing_target_id(servers["list"][1]),
        "db": db,
        "repo": repo,
    }
    await identified_raw.close()
    await anon_raw.close()
    await db.shutdown()
    await a.close()
    await b.close()


_PUBLISH_BODY = {"channel_id": "0" * 32, "epoch": EPOCH, "payload": "Y3Q"}


async def _send_anon(world) -> list:
    """What the channel service does for an item
    (``GfsChannelService._anon_targets`` / ``_post_anon``): resolve the
    owner's list, keep a listing target only once its signed block proves
    ``private_channels`` (after a proof of possession of the listed key),
    then one anonymous request per target over :meth:`anon_session`."""
    gfs = world["gfs"]
    targets = [
        t
        for t in await gfs.server_targets(SPACE_ID)
        if t.registered or await gfs.private_channels_supported(t)
    ]
    for conn in targets:
        try:
            async with gfs.anon_session(conn) as client:
                if client is None:
                    continue
                async with client.post(
                    f"{conn.inbox_url}{CHANNEL_PUBLISH_ANON_ROUTE}",
                    json=_PUBLISH_BODY,
                    allow_redirects=False,
                ):
                    pass
        except OutboundFetchRefused:
            continue
    return targets


async def test_nothing_reaching_an_unregistered_gfs_names_the_household(world, caplog):
    with caplog.at_level(logging.DEBUG):
        targets = await _send_anon(world)
    assert sorted(c.id for c in targets) == sorted(["conn-a", world["b_target"]])
    b_requests = world["seen_b"]
    assert b_requests, "B received nothing"
    # B offers proof of possession: it had to prove the listed key at the
    # listed URL (a bare nonce — nothing of ours) before we sent anything.
    assert {(r["method"], r["path"]) for r in b_requests} == {
        ("GET", "/gfs/info"),
        ("POST", "/gfs/proof"),
        ("POST", CHANNEL_PUBLISH_ANON_ROUTE),
    }
    url_b = world["url_b"]
    to_b = [
        {"url": url, "body": body}
        for _n, _m, url, body in world["sent"]
        if url.startswith(url_b)
    ]
    for r in [*b_requests, *to_b]:
        assert "Cookie" not in r.get("headers", {})
        blob = json.dumps(r)
        for leak in (
            *world["me"].leaks(),
            *USERS,
            "writer_cert",
            "instance_pk",
            "writer_user_ids",
            '"signature"',
            "owning_instance",
        ):
            assert leak not in blob, f"a request to B carries {leak!r}"
    # Every request to B rode a listing session — never the household's
    # identified or shared publish session.
    assert {name for name, _m, url, _b in world["sent"] if url.startswith(url_b)} == {
        "listing"
    }
    # B never learned the household as a client.
    assert (
        await world["b"].app[gfs_fed_repo_key].get_instance(world["me"].instance_id)
        is None
    )
    for leak in world["me"].leaks():
        assert leak not in caplog.text


async def test_targets_come_only_from_the_owners_list(world):
    # The owner's list names only A: B is never contacted.
    world["servers"]["list"] = world["servers"]["list"][:1]
    targets = await _send_anon(world)
    assert [c.id for c in targets] == ["conn-a"]
    assert world["seen_b"] == []
    assert world["listing_opened"] == []


async def test_a_listed_server_that_cannot_prove_the_listed_url_gets_nothing(world):
    """B offers proof of possession, but the owner's entry names it at an
    address its proof is not bound to (``localhost`` instead of its base
    URL — what a relay or an impostor replaying B's descriptor looks like):
    nothing is sent there."""
    a_entry, b_entry = world["servers"]["list"]
    port = world["url_b"].rsplit(":", 1)[1]
    world["servers"]["list"] = (
        a_entry,
        dataclasses.replace(b_entry, inbox_url=f"http://localhost:{port}"),
    )
    targets = await _send_anon(world)
    assert [c.id for c in targets] == ["conn-a"]
    assert {(r["method"], r["path"]) for r in world["seen_b"]} == {
        ("GET", "/gfs/info"),
        ("POST", "/gfs/proof"),
    }


async def test_a_listed_private_address_we_never_joined_is_dropped(world):
    """The real read side of the owner's signed list: B on a loopback
    address, a server we hold no connection to — dropped before any
    request (an owner's list must never aim us at our own network)."""
    db, repo = world["db"], world["repo"]
    spaces = SqliteSpaceRepo(db)
    await spaces.save(
        Space(
            id=SPACE_ID,
            name="Multi",
            owner_instance_id=world["owner"].instance_id,
            owner_username="o",
            identity_public_key=SPACE_PK.hex(),
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    await spaces.set_gfs_servers(
        SPACE_ID,
        sign_gfs_server_list(
            space_id=SPACE_ID,
            servers=world["servers"]["list"],
            issued_at=int(datetime.now(UTC).timestamp() * 1000),
            owner_seed=world["owner"].seed,
        ),
    )
    selection = GfsServerSelectionService(
        space_repo=spaces, gfs_repo=repo, bus=EventBus()
    )
    world["gfs"].attach_member_servers(selection.member_servers)
    targets = await _send_anon(world)
    assert [t.id for t in targets] == ["conn-a"]
    assert world["seen_b"] == []
    assert world["listing_opened"] == []


def test_the_space_keyed_household_senders_are_gone():
    """The host relay (``POST /gfs/publish``) and the space-keyed member
    publish (``/gfs/member-publish-anon``, ``/gfs/spaces/{id}/epoch``) have
    no household sender any more."""
    assert not hasattr(GfsConnectionService, "publish_space_event")
    for name in ("_post_anon_item", "_post_notice", "_anon_targets", "_retry_send"):
        assert not hasattr(GfsMemberPublishService, name), name
