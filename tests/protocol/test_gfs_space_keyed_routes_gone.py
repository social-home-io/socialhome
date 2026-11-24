"""§27.9 release blocker (v_96, P7): a GFS keeps no space-id-keyed path.

Before v_96 a GFS fanned a listed space's content out to identity-bound
follower seats keyed by the plaintext space id (``POST /gfs/subscribe``,
``POST /gfs/publish``, the anonymous member publish and its epoch notice,
the follower key pull and the owner's subscriber roster read), and kept its
media under a ``space`` scope. Since v_96 a space's content reaches a GFS
only through its opaque channel (``/gfs/channels/*`` — blindness pinned by
``test_gfs_global_channel_blind`` / ``test_gfs_private_channel_blind``).

This file is the removal proof for the GFS side, standing in for the
retired GFS-side checks of ``test_gfs_member_publish_blind``,
``test_gfs_member_publish_strict_blind`` and the space-keyed fan-out checks
of ``test_gfs_payload_minimization``:

* every retired route answers ``404`` on a real GFS app, whatever the body
  (a signed-looking one included) — nothing is relayed, seated or stored;
* the GFS package names no follow graph by space id (``space_subscribers``),
  no space audience, no ``new_subscriber`` push and no space-keyed relay
  event type — a module scan, so a re-introduced path fails here;
* the media cache takes no ``space`` scope (the codec refuses it), and the
  signed ``/gfs/info`` block advertises none of the retired capabilities.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.domain.gfs_space_media import (
    MEDIA_SCOPES,
    InvalidSpaceMediaAuth,
    valid_scope_id,
)
from socialhome.global_server.app_keys import gfs_db_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app

pytestmark = pytest.mark.security

_GFS_DIR = Path(__file__).resolve().parents[2] / "socialhome" / "global_server"

#: Every route of the space-keyed relay, with a body shaped like the one it
#: used to take.
RETIRED_ROUTES: tuple[tuple[str, dict], ...] = (
    (
        "/gfs/publish",
        {"space_id": "sp-1", "event_type": "space_post_public", "payload": {}},
    ),
    (
        "/gfs/subscribe",
        {"instance_id": "i", "space_id": "sp-1", "ts": "t", "signature": "s"},
    ),
    ("/gfs/member-publish-anon", {"target": "sp-1", "payload": "x"}),
    ("/gfs/member-publish", {"space_id": "sp-1", "payload": "x"}),
    ("/gfs/spaces/sp-1/epoch", {"epoch": 1, "authority_sig": "x"}),
    ("/gfs/spaces/sp-1/key_request", {"instance_id": "i", "ts": "t"}),
)

#: What the signed capability block no longer names.
RETIRED_CAPABILITIES: tuple[str, ...] = (
    "anonymous_publish",
    "member_publish_strict",
    "targeted_publish",
    "space_key_pull",
    "space_item_followers",
    "payload_zlib1",
    "payload_zlib1_dict1",
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
        tc.app_ = app
        yield tc


@pytest.mark.parametrize(("route", "body"), RETIRED_ROUTES)
async def test_every_space_keyed_route_is_gone(gfs, route, body):
    resp = await gfs.post(route, json=body)
    assert resp.status in (404, 405)
    assert (await gfs.get(route)).status in (404, 405)


async def test_the_subscriber_roster_read_is_gone(gfs):
    resp = await gfs.get(
        "/gfs/spaces/sp-1/subscribers",
        params={"ts": "1", "authority_sig": "x", "authority_sig_suite": "ed25519"},
    )
    assert resp.status == 404


async def test_the_gfs_database_keeps_no_follow_graph(gfs):
    """GFS migration 0033 dropped ``space_subscribers``."""
    db = gfs.app_[gfs_db_key]
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("space_subscribers",),
    )
    assert rows == []


async def test_the_signed_block_names_no_retired_capability(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    caps = body["capabilities"]
    for name in RETIRED_CAPABILITIES:
        assert name not in caps
        assert name not in body
    assert caps["subscriber_seats"] is True
    assert caps["private_channels"] is True
    media = caps.get("space_media")
    if media is not None:
        assert media["scopes"] == ["channel"]
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        caps,
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


def test_the_media_codec_takes_no_space_scope():
    assert MEDIA_SCOPES == frozenset({"channel"})
    with pytest.raises(InvalidSpaceMediaAuth):
        valid_scope_id("space", "sp-1")
    assert valid_scope_id("channel", os.urandom(16).hex())


#: Names only the space-keyed relay had. A GFS module naming one is a path
#: that came back.
_FORBIDDEN = re.compile(
    r"space_subscribers"
    r"|AUDIENCE_SPACE\b"
    r"|new_subscriber"
    r"|space_post_public"
    r"|space_subscriber_key_handoff"
    r"|space_author_profiles_bundle"
    r"|member-publish-anon"
    r"|/gfs/subscribe\b"
    r"|/gfs/publish\b"
    r"|/gfs/spaces/[^/\"]*/key_request"
)


def test_no_gfs_module_names_the_space_keyed_relay():
    offenders: list[str] = []
    for path in sorted(_GFS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _FORBIDDEN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(_GFS_DIR)}:{line}: {match.group()}")
    assert offenders == []


def test_the_scan_sees_a_space_keyed_name():
    assert _FORBIDDEN.search('app.router.add_view("/gfs/subscribe", View)')
    assert _FORBIDDEN.search("SELECT * FROM space_subscribers")
    assert not _FORBIDDEN.search("/gfs/channels/subscribe")
    assert not _FORBIDDEN.search("/gfs/moments/publish")
    assert _FORBIDDEN.search('"/gfs/spaces/{space_id}/key_request"')
    assert not _FORBIDDEN.search("/gfs/moments/users/{user_id}/key_request")
