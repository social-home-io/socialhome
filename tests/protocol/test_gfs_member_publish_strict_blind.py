"""§27.9 release blocker (v_50): in a STRICT space nothing identifying
reaches the connection server for a member publish, and the request is
unlinkable to the publishing household.

Drives a real GFS app end to end with the household's real publisher: the
owner's notice puts the space in strict mode and pins the epoch's writer
group key; two different member households — both holding that key — publish.
Every place the server could see or keep the item is checked: the requests it
receives (over every session), its logs, the queued rows and the fan-out
frames. None may carry a household id, a household key, a household
signature, the writer cert or the users it binds — and the two households'
requests must be indistinguishable apart from per-request randomness.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import b64url_encode, derive_instance_id, ed25519_public_key
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_ANON_FRAME_KEYS,
    MEMBER_PUBLISH_ANON_REQUEST_KEYS,
)
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_space_epoch_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.services.gfs_member_publish_service import GfsMemberPublishService
from socialhome.writer_cert import bind_writer_users, sign_writer_cert
from socialhome.writer_key import derive_writer_seed, issue_writer_key_grant

pytestmark = pytest.mark.security

SPACE_ID = "sp-strict-blind"
EPOCH = 4
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
USERS = ("alice-user-id", "bob-user-id")


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)

    def leaks(self) -> tuple[str, ...]:
        return (self.instance_id, self.pk.hex(), b64url_encode(self.pk))


class _Session:
    """Records every request a household session makes."""

    def __init__(self, session, log: list) -> None:
        self._session = session
        self._log = log

    def post(self, url, *, json=None, **kw):
        self._log.append(("POST", str(url), json))
        return self._session.post(url, json=json, **kw)

    def get(self, url, *a, **kw):
        self._log.append(("GET", str(url), None))
        return self._session.get(url, *a, **kw)


class _Gfs:
    def __init__(self, identified, anon) -> None:
        self._identified = identified
        self._anon = anon
        self.subscribed: list = []

    def client(self):
        return self._identified

    def publish_client(self):
        return self._anon

    def addressee_key_for(self, conn):
        return None

    def forget_addressee_key(self, conn):
        self.forgotten = [*getattr(self, "forgotten", []), conn.id]

    async def refresh_if_stale(self, conn):
        return conn

    async def member_publish_trusted_supported(self, conn):
        return True

    async def member_publish_strict_supported(self, conn):
        return True

    async def subscribe_to_gfs_space(self, space_id, gfs_id):
        self.subscribed.append((space_id, gfs_id))


class _Certs:
    def __init__(self, household: _Household) -> None:
        self.household = household

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
        return derive_writer_seed(SPACE_SEED, space_id, epoch)

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
    async def get(self, space_id):
        return SimpleNamespace(
            id=space_id,
            space_type=SimpleNamespace(value="public"),
            features=SimpleNamespace(gfs_publish_mode="strict", allow_subscribers=True),
        )


@pytest.fixture
async def gfs(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-node-a",
        cluster_enabled=False,
        cluster_node_id="gfs-node-a",
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        fed = app[gfs_fed_repo_key]
        tc.app_ = app
        tc.owner, tc.writer_a, tc.writer_b, tc.follower = (
            _Household(),
            _Household(),
            _Household(),
            _Household(),
        )
        for h in (tc.owner, tc.writer_a, tc.writer_b, tc.follower):
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
                owning_instance=tc.owner.instance_id,
                allow_subscribers=True,
                status="active",
                identity_public_key=SPACE_PK.hex(),
            )
        )
        await fed.add_subscriber(space_id=SPACE_ID, instance_id=tc.follower.instance_id)
        await fed.mark_relay_seen(tc.follower.instance_id, at=int(time.time()))
        # What the owner's v_50 notice establishes.
        epochs = app[gfs_space_epoch_repo_key]
        now = int(time.time())
        await epochs.confirm(SPACE_ID, EPOCH, now=now)
        await epochs.set_publish_mode(SPACE_ID, "strict", at=now)
        wkc = issue_writer_key_grant(
            space_seed=SPACE_SEED, space_id=SPACE_ID, epoch=EPOCH
        ).writer_key_cert
        await epochs.pin_writer_key(SPACE_ID, EPOCH, wkc.writer_pk, replace=True)
        yield tc


def _publisher(tc, household: _Household, log: list) -> GfsMemberPublishService:
    gfs = _Gfs(_Session(tc.session, log), _Session(tc.session, log))
    return GfsMemberPublishService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=None,  # type: ignore[arg-type]
        space_repo=_Spaces(),  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=_Certs(household),  # type: ignore[arg-type]
        own_instance_id=household.instance_id,
        own_identity_seed=household.seed,
    )


def _conn(tc) -> GfsConnection:
    return GfsConnection(
        id="c",
        gfs_instance_id="gfs-node-a",
        display_name="g",
        public_key="00" * 32,
        inbox_url=str(tc.make_url("")).rstrip("/"),
        status="active",
        paired_at="",
    )


async def _publish(tc, household, log) -> None:
    svc = _publisher(tc, household, log)
    inner = {
        "post_id": "p-" + household.instance_id[:6],
        "space_id": SPACE_ID,
        "origin_instance_id": household.instance_id,
        "author_pk": household.pk.hex(),
    }
    accepted = await svc.publish_post(SPACE_ID, inner, [_conn(tc)])
    assert [c.id for c in accepted] == ["c"]


async def test_nothing_identifying_reaches_the_gfs_for_a_strict_publish(gfs, caplog):
    log: list = []
    with caplog.at_level(logging.DEBUG):
        await _publish(gfs, gfs.writer_a, log)
        await gfs.app_[gfs_member_publish_key].wait_idle()
    queued = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.follower.instance_id, now=0
    )
    assert len(queued) == 1
    frame = queued[0].sealed
    assert set(frame) == MEMBER_PUBLISH_ANON_FRAME_KEYS
    [(_m, url, body)] = log
    assert url.endswith("/gfs/member-publish-anon")
    assert set(body) == MEMBER_PUBLISH_ANON_REQUEST_KEYS
    for where, blob in (
        ("the request", json.dumps(log)),
        ("the fan-out frame / queued row", json.dumps(frame)),
        ("the GFS logs", caplog.text),
    ):
        for leak in (
            *gfs.writer_a.leaks(),
            *USERS,
            "writer_cert",
            "instance_pk",
            "writer_user_ids",
            '"signature"',
        ):
            assert leak not in blob, f"{where} carries {leak!r}"


async def test_two_households_publish_indistinguishable_requests(gfs):
    log_a: list = []
    log_b: list = []
    await _publish(gfs, gfs.writer_a, log_a)
    await _publish(gfs, gfs.writer_b, log_b)
    [(_ma, url_a, a)] = log_a
    [(_mb, url_b, b)] = log_b
    assert url_a == url_b
    assert set(a) == set(b)
    # Everything but the per-request randomness is identical — nothing in
    # the request depends on WHICH household sent it.
    per_request = {"ts", "nonce", "payload", "writer_sig"}
    assert {k: a[k] for k in set(a) - per_request} == {
        k: b[k] for k in set(b) - per_request
    }
    # The ciphertext is the same size bucket too.
    assert len(a["payload"]) == len(b["payload"])
    for leak in gfs.writer_a.leaks():
        assert leak not in json.dumps(b)
    for leak in gfs.writer_b.leaks():
        assert leak not in json.dumps(a)


async def test_an_identified_publish_into_a_strict_space_is_refused(gfs):
    """An older (v_49) or misconfigured household's identified publish must
    not be relayed into a strict space."""
    from_trusted = _publisher(gfs, gfs.writer_a, [])
    data = {
        "epoch": EPOCH,
        "writer_cert": (await from_trusted._writer_certs.own_cert(SPACE_ID, EPOCH))
        .v1()
        .to_wire(),
        "payload": "Y3Q",
    }
    body = from_trusted._signed_item_body(_conn(gfs), SPACE_ID, data)
    resp = await gfs.post("/gfs/member-publish", json=body)
    assert resp.status == 403
    await gfs.app_[gfs_member_publish_key].wait_idle()
    assert (
        await gfs.app_[gfs_envelope_queue_repo_key].list_for(
            gfs.follower.instance_id, now=0
        )
        == []
    )
