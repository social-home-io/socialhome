"""§27.9 release blocker: a member-published item reaches the connection
server as ciphertext under a generic type — never content, never the real
item type.

Trusted mode lets the GFS learn WHICH household published (owner decision,
``docs/principles.md``); it must still learn nothing about WHAT. This drives
a real GFS app end to end and checks every place the server could see or
keep the item: the request it receives, its logs, the queued row and the
frame it fans out. It also pins that the wire shape has no room for a
plaintext side channel: an extra field or a non-generic type is refused.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_FRAME_KEYS,
    PLAINTEXT_CERT_KEYS,
    MEMBER_PUBLISH_REQUEST_KEYS,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishRequest,
)
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.services.gfs_member_publish_service import (
    ITEM_SIZE_BUCKETS,
    GfsMemberPublishService,
)
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

pytestmark = pytest.mark.security

SPACE_ID = "sp-blind"
SECRET_CONTENT = "Grandma's secret dumpling recipe"
REAL_TYPE = "space_post_created"
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
CONTENT_KEY = AESGCM.generate_key(bit_length=256)


def _ciphertext() -> str:
    """What a member household puts in ``payload``: the real type and the
    content, sealed under the space content key."""
    inner = json.dumps({"type": REAL_TYPE, "content": SECRET_CONTENT}).encode()
    nonce = os.urandom(12)
    return b64url_encode(nonce + AESGCM(CONTENT_KEY).encrypt(nonce, inner, None))


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


def _request(publisher: _Household) -> dict:
    req = MemberPublishRequest(
        instance_id=publisher.instance_id,
        gfs_instance_id="gfs-node-a",
        ts=datetime.now(timezone.utc).isoformat(),
        signature="",
        target=SPACE_ID,
        epoch=1,
        writer_cert=sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=SPACE_ID,
            epoch=1,
            instance_pk=publisher.pk,
            scope="write",
        ),
        payload=_ciphertext(),
    )
    sig = sign_ed25519(publisher.seed, req.signing_bytes())
    return replace(req, signature=b64url_encode(sig)).to_wire()


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
        tc.publisher = _Household()
        tc.subscriber = _Household()
        for h in (tc.publisher, tc.subscriber):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=h.instance_id,
                    display_name="H",
                    public_key=h.pk.hex(),
                    inbox_url="http://h.home/wh",
                    status="active",
                )
            )
        await fed.upsert_space(
            GlobalSpace(
                space_id=SPACE_ID,
                owning_instance=tc.publisher.instance_id,
                allow_subscribers=True,
                status="active",
                identity_public_key=SPACE_PK.hex(),
            )
        )
        await fed.add_subscriber(
            space_id=SPACE_ID, instance_id=tc.subscriber.instance_id
        )
        await fed.mark_relay_seen(tc.subscriber.instance_id, at=int(time.time()))
        yield tc


def _assert_blind(text: str, where: str) -> None:
    assert SECRET_CONTENT not in text, f"{where} carries the content"
    assert REAL_TYPE not in text, f"{where} carries the real item type"


async def test_the_gfs_never_sees_content_or_the_real_item_type(gfs, caplog):
    body = _request(gfs.publisher)
    # What the GFS receives.
    assert set(body) == MEMBER_PUBLISH_REQUEST_KEYS
    assert body["event_type"] == SPACE_ITEM_EVENT_TYPE
    _assert_blind(json.dumps(body), "the request")

    with caplog.at_level(logging.DEBUG):
        resp = await gfs.post("/gfs/member-publish", json=body)
        assert resp.status == 200
        await gfs.app_[gfs_member_publish_key].wait_idle()
    _assert_blind(caplog.text, "the GFS logs")

    # What the GFS keeps for the offline subscriber, and later fans out.
    queued = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.subscriber.instance_id, now=0
    )
    assert len(queued) == 1
    frame = queued[0].sealed
    assert set(frame) == MEMBER_PUBLISH_FRAME_KEYS
    assert frame["event_type"] == SPACE_ITEM_EVENT_TYPE
    _assert_blind(json.dumps(frame), "the fan-out frame")
    assert "from_instance" not in frame


@pytest.mark.parametrize(
    "smuggle",
    [
        {"content": SECRET_CONTENT},
        {"item_type": REAL_TYPE},
        {"event_type": REAL_TYPE},
        {"payload": {"type": REAL_TYPE, "content": SECRET_CONTENT}},
    ],
)
async def test_the_wire_shape_has_no_room_for_a_plaintext_side_channel(gfs, smuggle):
    body = {**_request(gfs.publisher), **smuggle}
    resp = await gfs.post("/gfs/member-publish", json=body)
    assert resp.status == 400
    assert (
        await gfs.app_[gfs_envelope_queue_repo_key].list_for(
            gfs.subscriber.instance_id, now=0
        )
        == []
    )


async def test_the_user_binding_never_reaches_the_gfs(gfs, caplog):
    """U1: the household's real publish path (the v2 cert with its user
    binding) hands the connection server the v1 cert fields only — in the
    request, the fan-out frame and the queued row."""
    publisher = gfs.publisher
    cert = bind_writer_users(
        sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=SPACE_ID,
            epoch=1,
            instance_pk=publisher.pk,
            scope="write",
        ),
        space_seed=SPACE_SEED,
        user_ids=["alice-user-id", "bob-user-id"],
    )
    svc = GfsMemberPublishService(
        gfs=_HouseholdGfs(gfs.session),  # type: ignore[arg-type]
        conn_repo=None,  # type: ignore[arg-type]
        space_repo=_TrustedSpaces(),  # type: ignore[arg-type]
        space_crypto=_OneKey(),  # type: ignore[arg-type]
        writer_certs=_FixedCert(cert),  # type: ignore[arg-type]
        own_instance_id=publisher.instance_id,
        own_identity_seed=publisher.seed,
    )
    sent: list[dict] = []
    real_post = gfs.session.post

    def _spy(url, *a, json=None, **kw):
        sent.append(json)
        return real_post(url, *a, json=json, **kw)

    gfs.session.post = _spy  # type: ignore[method-assign]
    conn = GfsConnection(
        id="c",
        gfs_instance_id="gfs-node-a",
        display_name="g",
        public_key="00" * 32,
        inbox_url=str(gfs.make_url("")).rstrip("/"),
        status="active",
        paired_at="",
    )
    with caplog.at_level(logging.DEBUG):
        accepted = await svc.publish_post(
            SPACE_ID, {"post_id": "p", "space_id": SPACE_ID}, [conn]
        )
        await gfs.app_[gfs_member_publish_key].wait_idle()
    assert [c.id for c in accepted] == ["c"]
    queued = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.subscriber.instance_id, now=0
    )
    for where, blob in (
        ("the request", json.dumps(sent)),
        ("the fan-out frame / queued row", json.dumps([q.sealed for q in queued])),
        ("the GFS logs", caplog.text),
    ):
        for leak in ("alice-user-id", "bob-user-id", "writer_user_ids", "users_sig"):
            assert leak not in blob, f"{where} carries {leak!r}"
    assert set(queued[0].sealed["writer_cert"]) == PLAINTEXT_CERT_KEYS


class _HouseholdGfs:
    def __init__(self, session) -> None:
        self.session = session

    def client(self):
        return self.session

    def publish_client(self):
        return self.session


class _TrustedSpaces:
    """A space repo holding the one (trusted-mode) public space."""

    async def get(self, space_id):
        return SimpleNamespace(
            id=space_id, features=SimpleNamespace(gfs_publish_mode="trusted")
        )


class _OneKey:
    key = AESGCM.generate_key(bit_length=256)

    async def get_current_epoch(self, space_id):
        return 1

    async def encrypt(self, space_id, plaintext: bytes):
        nonce = os.urandom(12)
        return 1, b64url_encode(
            nonce + AESGCM(self.key).encrypt(nonce, plaintext, None)
        )


class _FixedCert:
    def __init__(self, cert) -> None:
        self.cert = cert

    async def own_cert(self, space_id, epoch):
        return self.cert

    async def own_writer_key(self, space_id, epoch):
        # Trusted mode: no writer group key held (v_50 strict is its own test).
        return None


ITEM_TYPES = (
    "post",
    "post_edit",
    "post_delete",
    "comment",
    "comment_edit",
    "comment_delete",
    "reaction_add",
    "reaction_remove",
)


@pytest.mark.parametrize("item_type", ITEM_TYPES)
async def test_no_member_item_type_ever_reaches_the_gfs_in_plaintext(
    gfs, caplog, item_type
):
    """PR 3: every item type rides the same generic ``space_item`` — the
    request, the fan-out frame, the queued row and the GFS logs carry
    neither the real type nor any of the item's fields (target ids, emoji,
    content, the signed stamps)."""
    publisher = gfs.publisher
    cert = bind_writer_users(
        sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=SPACE_ID,
            epoch=1,
            instance_pk=publisher.pk,
            scope="write",
        ),
        space_seed=SPACE_SEED,
        user_ids=["alice-user-id"],
    )
    svc = GfsMemberPublishService(
        gfs=_HouseholdGfs(gfs.session),  # type: ignore[arg-type]
        conn_repo=None,  # type: ignore[arg-type]
        space_repo=_TrustedSpaces(),  # type: ignore[arg-type]
        space_crypto=_OneKey(),  # type: ignore[arg-type]
        writer_certs=_FixedCert(cert),  # type: ignore[arg-type]
        own_instance_id=publisher.instance_id,
        own_identity_seed=publisher.seed,
    )
    secrets = {
        "item_target": "target-id-1f3a",
        "post_id": "post-id-9b2c",
        "content": SECRET_CONTENT,
        "emoji": "🦄",
        "ts": "2026-10-03T12:34:56.789012+00:00",
        "edited_at": "2026-10-03T12:34:57.123456+00:00",
        "parent_id": "parent-id-77aa",
    }
    sent: list[dict] = []
    real_post = gfs.session.post

    def _spy(url, *a, json=None, **kw):
        sent.append(json)
        return real_post(url, *a, json=json, **kw)

    gfs.session.post = _spy  # type: ignore[method-assign]
    conn = GfsConnection(
        id="c",
        gfs_instance_id="gfs-node-a",
        display_name="g",
        public_key="00" * 32,
        inbox_url=str(gfs.make_url("")).rstrip("/"),
        status="active",
        paired_at="",
    )
    with caplog.at_level(logging.DEBUG):
        accepted = await svc.publish_item(
            SPACE_ID, item_type, {"item_type": item_type, **secrets}, [conn]
        )
        await gfs.app_[gfs_member_publish_key].wait_idle()
    assert [c.id for c in accepted] == ["c"]
    queued = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.subscriber.instance_id, now=0
    )
    assert len(queued) == 1
    assert set(queued[0].sealed) == MEMBER_PUBLISH_FRAME_KEYS
    assert queued[0].sealed["event_type"] == SPACE_ITEM_EVENT_TYPE
    # Size padding: every small item — a reaction or a post alike — is one
    # 1 KiB bucket inside the AEAD (12-byte nonce + 16-byte tag around it),
    # so the ciphertext length doesn't tell the type either.
    assert len(queued[0].sealed["payload"]) == len(
        b64url_encode(bytes(12 + ITEM_SIZE_BUCKETS[0] + 16))
    )
    for where, blob in (
        ("the request", json.dumps(sent, ensure_ascii=False)),
        (
            "the fan-out frame / queued row",
            json.dumps([q.sealed for q in queued], ensure_ascii=False),
        ),
        ("the GFS logs", caplog.text),
    ):
        assert f'"{item_type}"' not in blob, f"{where} carries the item type"
        assert "item_type" not in blob, f"{where} carries an item_type field"
        for leak in secrets.values():
            assert leak not in blob, f"{where} carries {leak!r}"
