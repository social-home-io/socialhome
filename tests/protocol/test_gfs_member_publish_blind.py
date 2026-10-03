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

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_FRAME_KEYS,
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
from socialhome.writer_cert import sign_writer_cert

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
