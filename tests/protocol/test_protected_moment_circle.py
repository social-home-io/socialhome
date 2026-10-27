"""§CP.R, owner decision 14 (2026-10-25): a protected account sees moments
from its own household and its directly paired households only — the circle
its own ``no_relay`` moments reach (v_38).

A moment relayed from 2–3 hops away, or a stranger's that came through a
GFS, is one household-wide row an adult housemate may see; for the protected
account it must not exist on ANY path: the inbox, the archive (and its tag
filter), the detail read, a thread's replies and their count, trending tags,
the Saved list (a save made before protection), saving or reacting to it,
and the live ``moment.created`` / ``moment.reaction_changed`` WebSocket
frames. Read-time filter: no migration, the adult keeps seeing everything.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    event_bus_key,
    moment_repo_key,
    ws_manager_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.events import MomentCreated, MomentReactionTotalsUpdated
from socialhome.domain.moment import Moment

pytestmark = pytest.mark.security

#: (moment id, author, received_via, hop_count) — all tagged ``#fun``.
_ROWS = [
    ("m-hop1", "u-paired", "household", 1),
    ("m-hop2", "u-fof", "household", 2),
    ("m-hop3", "u-far", "household", 3),
    ("m-gfs", "u-stranger", "gfs", 1),
]
_FAR = {"m-hop2", "m-hop3", "m-gfs"}


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _moment(mid: str, author: str, via: str, hop: int, **kw) -> Moment:
    now = datetime.now(UTC)
    return Moment(
        id=mid,
        author_user_id=author,
        content="#fun",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=kw.get("parent"),
        origin_instance_id="inst-" + author,
        created_at=(now - timedelta(minutes=kw.get("age_min", 1))).isoformat(),
        expires_at=(now + timedelta(days=7)).isoformat(),
        hop_count=hop,
        received_via=via,
    )


@pytest.fixture
async def hh(aiohttp_client, tmp_dir):
    """A household with an admin (adult) and a protected kid; a moment the
    admin wrote, one from a paired household, one each from 2 and 3 hops
    away and one through a GFS; replies to the admin's moment from each;
    the kid saved every moment before protection was turned on."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    db = app[db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk = bytes.fromhex(row["identity_public_key"])
    ids = {name: derive_user_id(pk, name) for name in ("admin", "kid")}
    for name, uid in ids.items():
        await db.enqueue(
            "INSERT OR REPLACE INTO users(username, user_id, display_name, "
            "is_admin) VALUES(?,?,?,?)",
            (name, uid, name.title(), 1 if name == "admin" else 0),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
            "VALUES(?,?,?,?)",
            (f"t-{name}", uid, "web", sha256_token_hash(f"{name}-tok")),
        )
        # A password sign-in session: it survives protection, unlike a
        # personal API token.
        await db.enqueue(
            "INSERT INTO platform_users(username, display_name) VALUES(?, ?)",
            (name, name.title()),
        )
        await db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (f"t-{name}", name, sha256_token_hash(f"{name}-tok")),
        )
    repo = app[moment_repo_key]
    await repo.save(_moment("m-home", ids["admin"], "self", 1, age_min=0))
    for mid, author, via, hop in _ROWS:
        await repo.save(_moment(mid, author, via, hop))
        await repo.save(_moment(f"r-{mid}", f"{author}-r", via, hop, parent="m-home"))
    for mid in ["m-home", *(r[0] for r in _ROWS)]:
        if mid != "m-home":
            await db.enqueue(
                "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
                " VALUES(?, ?, ?)",
                (ids["kid"], mid, datetime.now(UTC).isoformat()),
            )
    cp = app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=ids["admin"]
    )
    tc._ids = ids
    return tc


async def _ids(resp) -> set[str]:
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    return {item["id"] for item in body}


async def test_the_inbox_and_archive_hold_the_circle_only(hh):
    for path in (
        "/api/moments",
        "/api/moments/archive",
        "/api/moments/archive?tag=fun",
    ):
        kid = await _ids(await hh.get(path, headers=_h("kid-tok")))
        adult = await _ids(await hh.get(path, headers=_h("admin-tok")))
        assert kid & _FAR == set(), path
        assert {"m-home", "m-hop1"} <= kid, path
        assert _FAR <= adult, path


async def test_a_far_moment_is_an_unknown_id_for_the_protected_account(hh):
    for mid in _FAR:
        r = await hh.get(f"/api/moments/{mid}", headers=_h("kid-tok"))
        assert r.status == 404, mid
        r = await hh.put(
            f"/api/moments/{mid}/reaction", json={"emoji": "🔥"}, headers=_h("kid-tok")
        )
        assert r.status == 404, mid
        r = await hh.put(f"/api/moments/{mid}/save", headers=_h("kid-tok"))
        assert r.status == 404, mid
        r = await hh.get(f"/api/moments/{mid}", headers=_h("admin-tok"))
        assert r.status == 200, mid
    r = await hh.get("/api/moments/m-hop1", headers=_h("kid-tok"))
    assert r.status == 200


async def test_a_thread_shows_and_counts_circle_replies_only(hh):
    r = await hh.get("/api/moments/m-home", headers=_h("kid-tok"))
    assert r.status == 200, await r.text()
    body = await r.json()
    assert {x["id"] for x in body["replies"]} == {"r-m-hop1"}
    assert body["moment"]["reply_count"] == 1
    inbox = await (await hh.get("/api/moments", headers=_h("kid-tok"))).json()
    home = next(m for m in inbox if m["id"] == "m-home")
    assert home["reply_count"] == 1
    adult = await (await hh.get("/api/moments/m-home", headers=_h("admin-tok"))).json()
    assert len(adult["replies"]) == 4


async def test_trending_tags_count_circle_moments_only(hh):
    r = await hh.get("/api/moments/hashtags", headers=_h("kid-tok"))
    assert r.status == 200, await r.text()
    kid = {t["tag"]: t["count"] for t in (await r.json())["hashtags"]}
    r = await hh.get("/api/moments/hashtags", headers=_h("admin-tok"))
    adult = {t["tag"]: t["count"] for t in (await r.json())["hashtags"]}
    # Counts are top-level moments only, like the tag view lists them
    # (replies are never counted): m-home and m-hop1 for the kid; all five
    # roots for the adult.
    assert kid == {"fun": 2}
    assert adult == {"fun": 5}


async def test_saves_from_before_protection_stay_hidden(hh):
    r = await hh.get("/api/moments/archive?scope=saved", headers=_h("kid-tok"))
    assert await _ids(r) == {"m-hop1"}


async def _ws(hh, name: str):
    return await hh.ws_connect(f"/api/ws?token={name}-tok")


async def _moment_frames(ws, until: str) -> list[dict]:
    """Moment frames up to and including the one naming *until*."""
    out: list[dict] = []
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
        if not msg.data or msg.data == "pong":
            continue
        frame = json.loads(msg.data)
        if not str(frame.get("type", "")).startswith("moment."):
            continue
        out.append(frame)
        if frame.get("moment_id") == until:
            return out


def _created(mid: str, hop: int, via: str | None = None) -> MomentCreated:
    return MomentCreated(
        moment_id=mid,
        author_user_id="u-x",
        content="x",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-x",
        expires_at="2099-01-01T00:00:00+00:00",
        received_via=via,
        hop_count=hop,
    )


async def test_live_frames_for_a_far_moment_never_reach_the_protected_account(hh):
    """Far moments first, then a paired one as the end marker: the adult
    gets every frame, the protected account only the paired one."""
    kid_ws, adult_ws = await _ws(hh, "kid"), await _ws(hh, "admin")
    try:
        manager = hh.server.app[ws_manager_key]
        for _ in range(40):
            if manager.connection_count() >= 2:
                break
            await asyncio.sleep(0.05)
        bus = hh.server.app[event_bus_key]
        await bus.publish(_created("w-hop2", 2))
        await bus.publish(_created("w-hop3", 3))
        await bus.publish(_created("w-gfs", 1, "gfs"))
        await bus.publish(
            MomentReactionTotalsUpdated(
                moment_id="w-hop2", author_user_id="u-x", hop_count=2
            )
        )
        await bus.publish(_created("w-hop1", 1))
        kid = await _moment_frames(kid_ws, "w-hop1")
        adult = await _moment_frames(adult_ws, "w-hop1")
        assert [(f["type"], f["moment_id"]) for f in kid] == [
            ("moment.created", "w-hop1")
        ]
        assert [(f["type"], f["moment_id"]) for f in adult] == [
            ("moment.created", "w-hop2"),
            ("moment.created", "w-hop3"),
            ("moment.created", "w-gfs"),
            ("moment.reaction_changed", "w-hop2"),
            ("moment.created", "w-hop1"),
        ]
    finally:
        await kid_ws.close()
        await adult_ws.close()
