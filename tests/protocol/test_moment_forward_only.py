"""Release-blocker protocol tests: a forward-only household (v_96).

Marked ``@pytest.mark.security``.

A household keeps a moment only if someone here will see it
(``docs/protocol/momentum.md`` § Storage): a person whose ``max_hops``
covers the hop, or a person who follows the author and has not blocked
them (a protected account only at hop 1). Otherwise it forwards the moment
within its reach and keeps nothing. Against the real application registry
and SQLite, a household where nobody looks that far:

* forwards a create within reach — hops 4–5 only toward who looks — and
  keeps no moment row, reaction route, hashtag or media file (a) — unless a
  person here follows the author: then the row is kept and shown to that
  follower only (b);
* passes reactions from behind it back toward the origin on return tokens
  alone (c), and a reply like a create (d);
* passes a delete on, keeping only the content-free tombstone that stops a
  replay (e);
* relays a create once whichever neighbour hands it over, and after a
  restart never relays a replay older than the 48 h relay age (f);
* passes totals on while its seen-map remembers the create (g);
* never fetches a mesh moment's media — only the reference rides (media).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import aiohttp
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    moment_reaction_counts_key,
    moment_repo_key,
    moment_service_key,
    startup_announcer_key,
)
from socialhome.config import Config
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.federation_service import FederationService
from socialhome.federation.moment_reaction_route import (
    RETURN_TOKEN_SUITE_AESGCM_256,
    origin_keywrap_fields,
    seal_reaction,
)
from socialhome.moment_counts import sign_counts
from socialhome.moment_origin import REACH_FIELD, sign_moment_origin

pytestmark = pytest.mark.security

FET = FederationEventType
ORIGIN_KEY = generate_identity_keypair()
ORIGIN = derive_instance_id(ORIGIN_KEY.public_key)
ORIGIN_KW = generate_x25519_keypair()
MIA = "u-mia"  # the author, homed on ORIGIN
UP = "peer-up"  # the neighbour that hands moments to us
UP2 = "peer-up2"  # a second neighbour holding the same moments
DOWN = "peer-down"  # a neighbour whose people look 5 hops
FAR = "peer-far"  # a neighbour that said nothing about how far it looks
EXPIRES = (datetime.now(UTC) + timedelta(days=5)).isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "forward.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, pk: bytes) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            pk.hex(),
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            OURS,
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    announcer = app.get(startup_announcer_key)
    assert announcer is not None, "the startup announcer is not wired"
    await announcer.wait()
    db = app[db_key]
    # Nobody here looks past hop 1: a forward-only household for hop ≥ 2.
    for username, user_id in (("anna", "u-anna"), ("bea", "u-bea")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, preferences_json)"
            " VALUES(?,?,?,?)",
            (username, user_id, username, '{"moments": {"max_hops": 1}}'),
        )
    await _seed_peer(db, ORIGIN, ORIGIN_KEY.public_key)
    for peer in (UP, UP2, DOWN, FAR):
        await _seed_peer(db, peer, generate_identity_keypair().public_key)
    app[federation_service_key].set_peer_moment_look_hops(DOWN, 5)
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?, ?, 'mia', 'Mia')",
        (MIA, ORIGIN),
    )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    fetched: list[str] = []

    async def _no_fetch(_self, method, url, *_a, **_kw):
        fetched.append(str(url))
        raise AssertionError(f"unexpected HTTP fetch: {method} {url}")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _no_fetch)
    return app, db, sent, fetched, Path(tmp_dir) / "media"


async def _deliver(app, event_type, payload, *, from_instance=UP) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )


def _signed(
    event_type,
    moment_id: str,
    *,
    hop: int,
    reach: int | None = None,
    occurred_at: datetime | None = None,
    **fields,
) -> dict:
    """A create (or delete) by Mia on ORIGIN, as *hop* claims, carrying a
    complete return-token chain (one token per relay before us) and the
    origin's bound key-wrap key."""
    payload: dict = {
        "moment_id": moment_id,
        "author_user_id": MIA,
        "origin_instance_id": ORIGIN,
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        "hop_count": 1,
    }
    if event_type is FET.MOMENT_CREATED:
        payload.update(
            content="far away #sunset",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            expires_at=EXPIRES,
        )
        payload.update(fields)
    if reach is not None and reach != 3:
        payload[REACH_FIELD] = reach
    signed = sign_moment_origin(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        event_type=event_type,
        payload=payload,
    )
    out = {**signed, "hop_count": hop}
    if event_type is FET.MOMENT_CREATED:
        out.update(
            origin_keywrap_fields(
                ORIGIN_KW.public_key,
                b64url_encode(
                    sign_ed25519(ORIGIN_KEY.private_key, ORIGIN_KW.public_key)
                ),
            )
        )
        out["rt"] = [f"tok-{i}" for i in range(hop - 1)]
        out["rt_suite"] = RETURN_TOKEN_SUITE_AESGCM_256
    return out


def _relayed(sent, moment_id: str, et=FET.MOMENT_CREATED) -> list[tuple[str, int]]:
    return sorted(
        (to, p["hop_count"])
        for to, t, p in sent
        if t is et and p.get("moment_id") == moment_id
    )


async def _count(db, table: str, column: str, moment_id: str) -> int:
    row = await db.fetchone(
        f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
        (moment_id,),
    )
    return int(row[0])


async def _kept_nothing(db, media_dir: Path, moment_id: str) -> None:
    assert await _count(db, "moments", "id", moment_id) == 0
    assert await _count(db, "moment_hashtags", "moment_id", moment_id) == 0
    assert await _count(db, "moment_reactions", "moment_id", moment_id) == 0
    assert not media_dir.exists() or not any(media_dir.rglob("*.*"))


# ═══ a: nobody looks that far, nobody follows — forward only ══════════════


@pytest.mark.parametrize(
    ("hop", "reach", "targets"),
    [
        (2, 3, [(DOWN, 3), (FAR, 3), (UP2, 3)]),
        (2, 5, [(DOWN, 3), (FAR, 3), (UP2, 3)]),
        (3, 5, [(DOWN, 4)]),  # hop 4: only toward who looks that far
        (4, 5, [(DOWN, 5)]),
        (3, 3, []),  # at its reach: neither kept nor forwarded
    ],
)
async def test_a_forward_only_household_forwards_and_keeps_nothing(
    env, hop, reach, targets
):
    app, db, sent, fetched, media = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-fwd", hop=hop, reach=reach),
    )
    assert _relayed(sent, "m-fwd") == targets
    await _kept_nothing(db, media, "m-fwd")
    routes = await app[moment_repo_key].get_reaction_routes(["m-fwd"])
    assert routes.get("m-fwd", ()) == ()  # no reaction route either
    assert fetched == []
    # Nothing local announced it — no inbox row for anybody here.
    for viewer in ("u-anna", "u-bea"):
        assert await app[moment_service_key].list_inbox(viewer_user_id=viewer) == []


# ═══ b: a follower keeps it — and only the follower sees it ═══════════════


async def _follow_direct(db) -> None:
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id)"
        " VALUES('u-anna', ?)",
        (MIA,),
    )


async def _follow_via_gfs(db) -> None:
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1',"
        " 'GFS One', ?, 'https://gfs1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES('u-anna', ?, 'g1', ?, 'mia', 'Mia')",
        (MIA, "bb" * 32),
    )


@pytest.mark.parametrize("follow", [_follow_direct, _follow_via_gfs])
@pytest.mark.parametrize("hop", [2, 3, 5])
async def test_a_follower_keeps_the_authors_moment_and_alone_sees_it(env, hop, follow):
    app, db, sent, _fetched, _media = env
    await follow(db)
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-kept", hop=hop, reach=5),
    )
    assert await _count(db, "moments", "id", "m-kept") == 1
    moments = app[moment_service_key]
    assert [m.id for m in await moments.list_inbox(viewer_user_id="u-anna")] == [
        "m-kept"
    ]
    assert await moments.list_inbox(viewer_user_id="u-bea") == []
    # Still relayed within reach (hop 5 is the cap).
    assert bool(_relayed(sent, "m-kept")) is (hop < 5)


async def test_a_follower_who_blocked_the_author_keeps_nothing(env):
    app, db, _sent, _fetched, media = env
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id)"
        " VALUES('u-anna', ?)",
        (MIA,),
    )
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES('u-anna', ?)",
        (MIA,),
    )
    await _deliver(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, "m-blk", hop=2))
    await _kept_nothing(db, media, "m-blk")


async def test_a_protected_follower_does_not_keep_a_far_moment(env):
    app, db, sent, _fetched, media = env
    await db.enqueue("UPDATE users SET child_protection_enabled=1")
    await db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id)"
        " VALUES('u-anna', ?)",
        (MIA,),
    )
    await _deliver(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, "m-kid", hop=2))
    await _kept_nothing(db, media, "m-kid")
    assert _relayed(sent, "m-kid")  # still forwarded


# ═══ c: reactions from behind a forward-only household reach the origin ═══


async def test_a_reaction_from_behind_passes_the_forward_only_household(env):
    app, db, sent, _fetched, _media = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-rx", hop=2, reach=5),
    )
    assert await _count(db, "moments", "id", "m-rx") == 0
    to_down = next(
        p
        for to, t, p in sent
        if t is FET.MOMENT_CREATED and to == DOWN and p["moment_id"] == "m-rx"
    )
    assert to_down["rt"][:1] == ["tok-0"] and len(to_down["rt"]) == 2
    sent.clear()
    sealed = seal_reaction(
        origin_keywrap_pub=ORIGIN_KW.public_key, inner={"kind": "moment_reaction"}
    )
    await _deliver(
        app,
        FET.MOMENT_REACTION_ROUTED,
        {
            "moment_id": "m-rx",
            "origin_instance_id": ORIGIN,
            "rt": to_down["rt"],
            "rt_suite": RETURN_TOKEN_SUITE_AESGCM_256,
            "sealed": sealed,
        },
        from_instance=DOWN,
    )
    assert [(to, t) for to, t, _p in sent] == [(UP, FET.MOMENT_REACTION_ROUTED)]
    forwarded = sent[0][2]
    assert forwarded["rt"] == ["tok-0"]  # our token taken off, the rest kept
    assert forwarded["sealed"] == sealed  # opaque to us
    assert await _count(db, "moment_reactions", "moment_id", "m-rx") == 0


# ═══ d: a reply travels like a create ═════════════════════════════════════


async def test_a_reply_passes_the_forward_only_household_like_a_create(env):
    app, db, sent, _fetched, media = env
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-reply", hop=2, parent_moment_id="m-root-far"),
    )
    assert _relayed(sent, "m-reply") == [(DOWN, 3), (FAR, 3), (UP2, 3)]
    assert all(
        p["parent_moment_id"] == "m-root-far"
        for _to, t, p in sent
        if t is FET.MOMENT_CREATED
    )
    await _kept_nothing(db, media, "m-reply")


# ═══ e: a delete passes; a replayed delete stops ══════════════════════════


async def test_a_delete_passes_through_and_a_replay_stops(env):
    app, db, sent, _fetched, _media = env
    await _deliver(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, "m-del", hop=2))
    sent.clear()
    await _deliver(app, FET.MOMENT_DELETED, _signed(FET.MOMENT_DELETED, "m-del", hop=2))
    assert _relayed(sent, "m-del", FET.MOMENT_DELETED) == [
        (DOWN, 3),
        (FAR, 3),
        (UP2, 3),
    ]
    row = await db.fetchone(
        "SELECT content, media_url, deleted_at FROM moments WHERE id='m-del'", ()
    )
    assert row is not None and row["deleted_at"] is not None
    assert row["content"] == "" and row["media_url"] is None  # content-free
    assert await _count(db, "moment_hashtags", "moment_id", "m-del") == 0
    sent.clear()
    for sender in (UP, UP2):
        await _deliver(
            app,
            FET.MOMENT_DELETED,
            _signed(FET.MOMENT_DELETED, "m-del", hop=2),
            from_instance=sender,
        )
    assert sent == []
    # …and a replayed create is refused by the tombstone.
    await _deliver(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, "m-del", hop=2))
    assert sent == []


# ═══ f: one relay per create; no old replay after a restart ═══════════════


async def test_a_duplicate_from_a_second_neighbour_is_not_relayed_again(env):
    app, _db, sent, _fetched, _media = env
    create = _signed(FET.MOMENT_CREATED, "m-dup", hop=2)
    await _deliver(app, FET.MOMENT_CREATED, create, from_instance=UP)
    first = _relayed(sent, "m-dup")
    assert first
    sent.clear()
    await _deliver(app, FET.MOMENT_CREATED, create, from_instance=UP2)
    await _deliver(app, FET.MOMENT_CREATED, create, from_instance=UP)
    assert sent == []


async def test_after_a_restart_a_replay_older_than_48h_is_not_relayed(env):
    app, db, sent, _fetched, media = env
    app[moment_reaction_counts_key]._outbound._relayed.clear()  # our restart
    old = datetime.now(UTC) - timedelta(hours=49)
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(FET.MOMENT_CREATED, "m-old", hop=2, occurred_at=old),
    )
    assert sent == []
    await _kept_nothing(db, media, "m-old")


# ═══ g: totals pass while the seen-map remembers the create ═══════════════


def _totals(rev_ms: int) -> dict:
    return sign_counts(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        moment_id="m-tot",
        author_user_id=MIA,
        origin_instance_id=ORIGIN,
        rev=rev_ms,
        counts={"🔥": 2},
        total=2,
        no_relay=False,
    )


async def test_totals_pass_a_forward_only_household_that_relayed_the_create(env):
    app, db, sent, _fetched, _media = env
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    # Never relayed the create: the totals go nowhere.
    await _deliver(app, FET.MOMENT_REACTIONS_COUNTED, _totals(now_ms))
    assert sent == []
    await _deliver(app, FET.MOMENT_CREATED, _signed(FET.MOMENT_CREATED, "m-tot", hop=2))
    sent.clear()
    await _deliver(app, FET.MOMENT_REACTIONS_COUNTED, _totals(now_ms + 1))
    assert _relayed(sent, "m-tot", FET.MOMENT_REACTIONS_COUNTED) == [
        (DOWN, 3),
        (FAR, 3),
        (UP2, 3),
    ]
    assert await _count(db, "moments", "id", "m-tot") == 0
    # The totals live on the row (``moments.reaction_summary``): none kept.


# ═══ media: only the reference rides; nothing is fetched ═══════════════════


@pytest.mark.parametrize("follows", [False, True])
async def test_a_mesh_moments_media_is_never_fetched(env, follows):
    app, db, sent, fetched, media = env
    if follows:
        await db.enqueue(
            "INSERT INTO user_follows(follower_user_id, followed_user_id)"
            " VALUES('u-anna', ?)",
            (MIA,),
        )
    await _deliver(
        app,
        FET.MOMENT_CREATED,
        _signed(
            FET.MOMENT_CREATED,
            "m-pic",
            hop=2,
            media_url="api/media/far.webp",
            media_type="image",
        ),
    )
    assert fetched == []
    assert not media.exists() or not any(media.rglob("*.*"))
    assert all(
        p["media_url"] == "api/media/far.webp"
        for _to, t, p in sent
        if t is FET.MOMENT_CREATED
    )
    row = await db.fetchone("SELECT media_url FROM moments WHERE id='m-pic'", ())
    if follows:
        assert row is not None and row["media_url"] == "api/media/far.webp"
    else:
        assert row is None
