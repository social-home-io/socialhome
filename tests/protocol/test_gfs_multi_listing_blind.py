"""§27.9 release blocker (v_57): a member publishes to EVERY GFS the space's
owner listed — including servers its own household never connected to —
and a server it never connected to learns nothing about who it is.

Drives two real GFS apps end to end with the household's REAL connection
service and member publisher. The household is registered on GFS A only; the
owner's signed list names A and B. Checked:

* nothing that reaches B — over any session — carries the household's
  instance id, identity key, a household signature, a cookie, or a writer
  cert; B never gets an identified route, and B keeps no ``client_instances``
  row for the household;
* once a ciphertext went out anonymously it never goes identified anywhere
  (not to A, not on a retry after the key is gone);
* targets come only from the owner's list: a list that leaves B out sends
  nothing to B, and a listed server on a private address we never joined is
  dropped before any request (SSRF);
* a server the owner delisted gets no retry.
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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_space_epoch_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_member_publish_service import GfsMemberPublishService
from socialhome.services.gfs_publish_retry import GfsPublish
from socialhome.services.gfs_server_selection_service import (
    GfsServerSelectionService,
)
from socialhome.services.gfs_server_targets import listing_target_id
from socialhome.writer_cert import bind_writer_users, sign_writer_cert
from socialhome.writer_key import derive_writer_seed, issue_writer_key_grant

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


class _Certs:
    def __init__(self, household: _Household) -> None:
        self.household = household
        self.key = True

    async def own_cert(self, space_id, epoch):
        cert = sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=epoch,
            instance_pk=self.household.pk,
            scope="write",
        )
        return bind_writer_users(cert, space_seed=SPACE_SEED, user_ids=USERS)

    async def current_own_cert_wire(self, space_id):
        return (await self.own_cert(space_id, EPOCH)).to_wire()

    async def own_writer_key(self, space_id, epoch):
        return derive_writer_seed(SPACE_SEED, space_id, epoch) if self.key else None

    async def writer_key_cert_wire(self, space_id, epoch):
        return None


class _Crypto:
    key = AESGCM.generate_key(bit_length=256)

    async def get_current_epoch(self, space_id):
        return EPOCH

    async def encrypt(self, space_id, plaintext: bytes):
        nonce = os.urandom(12)
        return EPOCH, b64url_encode(
            nonce + AESGCM(self.key).encrypt(nonce, plaintext, None)
        )


class _Spaces:
    """A TRUSTED, publicly readable global space; we are a plain member."""

    def __init__(self, owner: str) -> None:
        self.space = Space(
            id=SPACE_ID,
            name="Multi",
            owner_instance_id=owner,
            owner_username="o",
            identity_public_key=SPACE_PK.hex(),
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )

    async def get(self, space_id):
        return self.space if space_id == SPACE_ID else None

    async def get_space_seed(self, space_id):
        return None


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
    certs = _Certs(me)
    spaces = _Spaces(owner.instance_id)
    svc = GfsMemberPublishService(
        gfs=gfs,
        conn_repo=repo,
        space_repo=spaces,  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=me.instance_id,
        own_identity_seed=me.seed,
    )
    await svc.start()
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
        "svc": svc,
        "gfs": gfs,
        "certs": certs,
        "servers": servers,
        "sent": sent,
        "listing_opened": listing_opened,
        "url_b": url_b,
        "b_target": listing_target_id(servers["list"][1]),
        "db": db,
        "repo": repo,
    }
    await svc.stop()
    await identified_raw.close()
    await anon_raw.close()
    await db.shutdown()
    await a.close()
    await b.close()


def _inner(me: _Household, n: int = 1) -> dict:
    return {
        "post_id": f"p-{n}",
        "space_id": SPACE_ID,
        "origin_instance_id": me.instance_id,
        "author_pk": me.pk.hex(),
        "content": "hello",
    }


async def _publish(world, n: int = 1) -> list:
    svc = world["svc"]
    targets = await svc.plan_post(SPACE_ID, USERS[0])
    accepted = await svc.publish_post(SPACE_ID, _inner(world["me"], n), targets)
    for tc in (world["a"], world["b"]):
        await tc.app[gfs_member_publish_key].wait_idle()
    return accepted


async def test_nothing_reaching_an_unregistered_gfs_names_the_household(world, caplog):
    with caplog.at_level(logging.DEBUG):
        accepted = await _publish(world)
    assert sorted(c.id for c in accepted) == sorted(["conn-a", world["b_target"]])
    b_requests = world["seen_b"]
    assert b_requests, "B received nothing"
    paths = {(r["method"], r["path"]) for r in b_requests}
    # B offers proof of possession: it had to prove the listed key at the
    # listed URL (a bare nonce — nothing of ours) before we published.
    assert paths == {
        ("GET", "/gfs/info"),
        ("POST", "/gfs/proof"),
        ("POST", "/gfs/member-publish-anon"),
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
    # And B's follower still got the item.
    queued = (
        await world["b"]
        .app[gfs_envelope_queue_repo_key]
        .list_for(world["follower"].instance_id, now=0)
    )
    assert len(queued) == 1
    assert "writer_cert" not in queued[0].sealed


async def test_a_ciphertext_sent_anonymously_never_goes_identified(world):
    await _publish(world)
    anon_payloads = {
        body["payload"]
        for _n, method, url, body in world["sent"]
        if method == "POST" and url.endswith("/gfs/member-publish-anon")
    }
    # One ciphertext per payload suite tried (a server may refuse the
    # dictionary copy and take the next one), never more.
    assert 1 <= len(anon_payloads) <= 3
    # The key is gone (a rotation we have not caught up with): a retry of
    # the queued anonymous item is dropped — never downgraded to identified.
    world["certs"].key = False
    for payload in anon_payloads:
        item = GfsPublish(
            space_id=SPACE_ID,
            event_type="space_item_anon",
            payload={"epoch": EPOCH, "payload": payload},
        )
        for conn_id in ("conn-a", world["b_target"]):
            outcome = await world["svc"]._retry_send(conn_id, item)
            assert outcome.kind == "permanent"
    for _name, method, url, body in world["sent"]:
        if method == "POST" and url.endswith("/gfs/member-publish"):
            pytest.fail(f"identified publish sent: {url}")
        if body and isinstance(body, dict) and body.get("payload") in anon_payloads:
            assert url.endswith("/gfs/member-publish-anon")
    for r in (*world["seen_a"], *world["seen_b"]):
        assert r["path"] != "/gfs/member-publish"


async def test_targets_come_only_from_the_owners_list(world):
    # The owner's list names only A: B is never contacted.
    world["servers"]["list"] = world["servers"]["list"][:1]
    accepted = await _publish(world)
    assert [c.id for c in accepted] == ["conn-a"]
    assert world["seen_b"] == []
    assert world["listing_opened"] == []


async def test_a_listed_server_that_cannot_prove_the_listed_url_gets_nothing(world):
    """B offers proof of possession, but the owner's entry names it at an
    address its proof is not bound to (``localhost`` instead of its base
    URL — what a relay or an impostor replaying B's descriptor looks like):
    nothing is published there."""
    a_entry, b_entry = world["servers"]["list"]
    port = world["url_b"].rsplit(":", 1)[1]
    world["servers"]["list"] = (
        a_entry,
        dataclasses.replace(b_entry, inbox_url=f"http://localhost:{port}"),
    )
    accepted = await _publish(world)
    assert [c.id for c in accepted] == ["conn-a"]
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
    targets = await world["gfs"].server_targets(SPACE_ID)
    assert [t.id for t in targets] == ["conn-a"]
    await _publish(world)
    assert world["seen_b"] == []
    assert world["listing_opened"] == []


async def test_a_delisted_server_gets_no_retry(world):
    world["svc"]._writer_certs.key = True
    item = GfsPublish(
        space_id=SPACE_ID,
        event_type="space_item_anon",
        payload={"epoch": EPOCH, "payload": "Y3Q"},
    )
    world["servers"]["list"] = world["servers"]["list"][:1]
    outcome = await world["svc"]._retry_send(world["b_target"], item)
    assert outcome.kind == "permanent"
    assert world["seen_b"] == []


async def test_the_host_relay_reaches_an_unregistered_gfs_identity_free(world):
    """A delegated admin relaying while the owner is offline: B gets the
    ``{space_id, event_type, payload}`` body over its listing session."""
    gfs = world["gfs"]
    gfs.attach_publish_context(
        space_repo=None,
        own_instance_id=world["me"].instance_id,
        own_signing_key=world["me"].seed,
    )
    await gfs.publish_space_event(
        space_id=SPACE_ID, event_type="space_post_public", payload={"x": 1}
    )
    posts = [r for r in world["seen_b"] if r["path"] == "/gfs/publish"]
    assert len(posts) == 1
    assert "Cookie" not in posts[0]["headers"]
    [(name, body)] = [
        (n, b)
        for n, m, url, b in world["sent"]
        if m == "POST" and url == f"{world['url_b']}/gfs/publish"
    ]
    assert name == "listing"
    assert set(body) == {"space_id", "event_type", "payload"}
    for leak in world["me"].leaks():
        assert leak not in json.dumps(body)
