"""§27.9 release blocker (v_50; the one mode since v_90): nothing
identifying reaches the connection server for a member publish, and the
request is unlinkable to the publishing household.

Drives a real GFS app end to end with the household's real publisher: the
owner's notice pins the epoch's writer group key; two different member
households — both holding that key — publish.
Every place the server could see or keep the item is checked: the requests it
receives (over every session), its logs, the queued rows and the fan-out
frames. None may carry a household id, a household key, a household
signature, the writer cert or the users it binds — and the two households'
requests must be indistinguishable apart from per-request randomness.

Since v_96 the household's items go into a space's opaque channel; the two
household-driven checks (nothing identifying reaches the GFS, two
households' requests are indistinguishable) now run on the channel in
``test_gfs_global_channel_blind``. What stays here is the GFS side (the
identified route is gone) until the GFS drops the legacy routes.
"""

from __future__ import annotations

import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_space_epoch_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.writer_key import issue_writer_key_grant

pytestmark = pytest.mark.security

SPACE_ID = "sp-strict-blind"
EPOCH = 4
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


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
        # What the owner's notice establishes.
        epochs = app[gfs_space_epoch_repo_key]
        now = int(time.time())
        await epochs.confirm(SPACE_ID, EPOCH, now=now)
        wkc = issue_writer_key_grant(
            space_seed=SPACE_SEED, space_id=SPACE_ID, epoch=EPOCH
        ).writer_key_cert
        await epochs.pin_writer_key(SPACE_ID, EPOCH, wkc.writer_pk, replace=True)
        yield tc


async def test_the_identified_publish_route_is_gone(gfs):
    """v_90 — one mode: an older household's identified publish (household
    id, signature, plaintext writer cert) has no route, so nothing of it is
    relayed."""
    resp = await gfs.post(
        "/gfs/member-publish",
        json={
            "instance_id": gfs.writer_a.instance_id,
            "target": SPACE_ID,
            "writer_cert": {"cert_suite": "ed25519"},
            "payload": "Y3Q",
        },
    )
    assert resp.status in (404, 405)
    await gfs.app_[gfs_member_publish_key].wait_idle()
    assert (
        await gfs.app_[gfs_envelope_queue_repo_key].list_for(
            gfs.follower.instance_id, now=0
        )
        == []
    )
