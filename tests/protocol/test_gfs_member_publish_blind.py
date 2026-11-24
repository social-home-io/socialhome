"""§27.9 release blocker: a member-published item reaches the connection
server as ciphertext under a generic type — never content, never the real
item type.

The publish is anonymous (one mode since v_90, ``docs/principles.md``): the
GFS learns neither WHO published nor WHAT. This drives
a real GFS app end to end and checks every place the server could see or
keep the item: the request it receives, its logs, the queued row and the
frame it fans out. It also pins that the wire shape has no room for a
plaintext side channel: an extra field or a non-generic type is refused.

Since v_96 the household no longer sends this request (its items go into a
space's opaque channel); these checks pin the GFS side of the legacy route
until the GFS drops it. The household-driven checks — every item type, the
writer cert's user binding, an author profile — moved onto the channel in
``test_gfs_global_channel_blind``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_ANON_FRAME_KEYS,
    MEMBER_PUBLISH_ANON_REQUEST_KEYS,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishAnonRequest,
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
from socialhome.writer_key import (
    derive_writer_seed,
    issue_writer_key_grant,
    sign_with_writer_key,
)

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
    """An anonymous publish: nothing in it names *publisher*."""
    req = MemberPublishAnonRequest(
        gfs_instance_id="gfs-node-a",
        ts=datetime.now(UTC).isoformat(),
        nonce=b64url_encode(os.urandom(16)),
        target=SPACE_ID,
        epoch=1,
        payload=_ciphertext(),
        writer_sig="",
        writer_sig_suite="ed25519",
    )
    seed = derive_writer_seed(SPACE_SEED, SPACE_ID, 1)
    return replace(
        req, writer_sig=sign_with_writer_key(seed, req.signing_bytes())
    ).to_wire()


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
        # The owner confirmed epoch 1 and pinned its writer key.
        epochs = app[gfs_space_epoch_repo_key]
        await epochs.confirm(SPACE_ID, 1, now=int(time.time()))
        await epochs.pin_writer_key(
            SPACE_ID,
            1,
            issue_writer_key_grant(
                space_seed=SPACE_SEED, space_id=SPACE_ID, epoch=1
            ).writer_key_cert.writer_pk,
            replace=True,
        )
        yield tc


def _assert_blind(text: str, where: str) -> None:
    assert SECRET_CONTENT not in text, f"{where} carries the content"
    assert REAL_TYPE not in text, f"{where} carries the real item type"


async def test_the_gfs_never_sees_content_or_the_real_item_type(gfs, caplog):
    body = _request(gfs.publisher)
    # What the GFS receives.
    assert set(body) == MEMBER_PUBLISH_ANON_REQUEST_KEYS
    assert gfs.publisher.instance_id not in json.dumps(body)
    assert body["event_type"] == SPACE_ITEM_EVENT_TYPE
    _assert_blind(json.dumps(body), "the request")

    with caplog.at_level(logging.DEBUG):
        resp = await gfs.post("/gfs/member-publish-anon", json=body)
        assert resp.status == 200
        await gfs.app_[gfs_member_publish_key].wait_idle()
    _assert_blind(caplog.text, "the GFS logs")

    # What the GFS keeps for the offline subscriber, and later fans out.
    queued = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.subscriber.instance_id, now=0
    )
    assert len(queued) == 1
    frame = queued[0].sealed
    assert set(frame) == MEMBER_PUBLISH_ANON_FRAME_KEYS
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
    resp = await gfs.post("/gfs/member-publish-anon", json=body)
    assert resp.status == 400
    assert (
        await gfs.app_[gfs_envelope_queue_repo_key].list_for(
            gfs.subscriber.instance_id, now=0
        )
        == []
    )
