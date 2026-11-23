"""A space's member + follower counts on the GFS: declared by the owning
household inside its signed listing publish, stored with the publish's ``ts``
(last writer wins), served on the directory, the viewer JSON and the public
pages — and never the GFS's own relay-seat ``subscriber_count``.

An older household (no counts) shows NO number, never "0 members".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.space_listing_counts import MAX_LISTING_COUNT
from socialhome.global_server.app_keys import gfs_cluster_key, gfs_fed_repo_key
from socialhome.global_server.cluster import _space_to_wire, _wire_to_space
from socialhome.global_server.domain import GlobalSpace
from socialhome.global_server.public import _render_landing, _render_space_page

from . import test_space_publish_routes as _routes

_register_owner = _routes._register_owner
_sign_publish_body = _routes._sign_publish_body
_gfs_key = _routes._gfs_key
#: The GFS test client fixture, shared with the publish-route tests.
gfs_client = _routes.gfs_client

_SPACE = "sp-garden"
_BASE = {
    "space_id": _SPACE,
    "owning_instance": "owner.home",
    "name": "Garden Club",
    "description": "Tomatoes",
    "about_markdown": "",
    "cover_url": "",
    "min_age": 0,
    "category": "general",
    "accent_color": "#D2542A",
    "icon_url": "",
    "primary_color": "#D2542A",
}


def _ts(*, seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def _publish(client, seed: bytes, extra: dict | None = None, **kw):
    body = _sign_publish_body({**_BASE, **(extra or {})}, seed=seed, **kw)
    return await client.post(f"/gfs/spaces/{_SPACE}/publish", json=body)


async def _listed(client) -> dict:
    items = (await (await client.get("/gfs/spaces")).json())["spaces"]
    return next(sp for sp in items if sp["space_id"] == _SPACE)


async def _stored(client) -> GlobalSpace:
    return await client.server.app[gfs_fed_repo_key].get_space(_SPACE)


# ── publish: signed, stored, served ─────────────────────────────────────


@pytest.mark.security
async def test_signed_counts_are_stored_and_served(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    ts = _ts()
    resp = await _publish(
        gfs_client, seed, {"member_count": 7, "follower_count": 31}, ts=ts
    )
    assert resp.status == 200
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == (7, 31)
    assert stored.counts_ts == datetime.fromisoformat(ts).astimezone(UTC).isoformat(
        timespec="microseconds"
    )
    for item in (
        await _listed(gfs_client),
        await (await gfs_client.get(f"/gfs/spaces/{_SPACE}")).json(),
    ):
        assert item["member_count"] == 7
        assert item["follower_count"] == 31
        # The relay-seat bookkeeping and the LWW stamp stay inside the GFS.
        assert "subscriber_count" not in item
        assert "counts_ts" not in item


@pytest.mark.security
async def test_a_tampered_count_breaks_the_signature(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    body = _sign_publish_body(
        {**_BASE, "member_count": 5, "follower_count": 1}, seed=seed, ts=_ts()
    )
    body["member_count"] = 500
    resp = await gfs_client.post(f"/gfs/spaces/{_SPACE}/publish", json=body)
    assert resp.status == 403
    # A count bolted onto a body signed without one is refused just the same.
    plain = _sign_publish_body(dict(_BASE), seed=seed, ts=_ts())
    plain["follower_count"] = 3
    resp = await gfs_client.post(f"/gfs/spaces/{_SPACE}/publish", json=plain)
    assert resp.status == 403
    assert await _stored(gfs_client) is None


async def test_an_older_household_without_counts_shows_no_number(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    # No ``ts`` either — the oldest shipped body shape still verifies.
    assert (await _publish(gfs_client, seed)).status == 200
    listed = await _listed(gfs_client)
    assert listed["member_count"] is None
    assert listed["follower_count"] is None
    stored = await _stored(gfs_client)
    assert stored.counts_ts is None
    page = await (await gfs_client.get(f"/spaces/{_SPACE}")).text()
    assert "member" not in page and "follower" not in page
    landing = await (await gfs_client.get("/")).text()
    assert "Garden Club" in landing
    assert "0 members" not in landing


@pytest.mark.parametrize("bad", [-1, MAX_LISTING_COUNT + 1, True, "3", 2.5, [1]])
async def test_an_invalid_count_is_stored_as_no_number(gfs_client, bad, caplog):
    seed, _ = await _register_owner(gfs_client.server.app)
    resp = await _publish(
        gfs_client, seed, {"member_count": bad, "follower_count": bad}, ts=_ts()
    )
    assert resp.status == 200
    listed = await _listed(gfs_client)
    assert (listed["member_count"], listed["follower_count"]) == (None, None)
    assert "invalid member_count" in caplog.text
    assert "invalid follower_count" in caplog.text


async def test_the_largest_sane_count_is_kept(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    resp = await _publish(
        gfs_client,
        seed,
        {"member_count": MAX_LISTING_COUNT, "follower_count": 0},
        ts=_ts(),
    )
    assert resp.status == 200
    listed = await _listed(gfs_client)
    assert (listed["member_count"], listed["follower_count"]) == (
        MAX_LISTING_COUNT,
        0,
    )


# ── last writer wins ────────────────────────────────────────────────────


async def test_an_older_publish_does_not_overwrite_newer_counts(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    newer = await _publish(
        gfs_client, seed, {"member_count": 9, "follower_count": 4}, ts=_ts()
    )
    assert newer.status == 200
    # A delayed publish signed a minute earlier arrives late.
    older = await _publish(
        gfs_client,
        seed,
        {"member_count": 2, "follower_count": 1, "name": "Garden Club 2"},
        ts=_ts(seconds=-60),
    )
    assert older.status == 200
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == (9, 4)
    # The rest of the listing is not part of this rule.
    assert stored.name == "Garden Club 2"


async def test_a_newer_publish_replaces_the_counts(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    await _publish(
        gfs_client,
        seed,
        {"member_count": 2, "follower_count": 1},
        ts=_ts(seconds=-60),
    )
    await _publish(gfs_client, seed, {"member_count": 3, "follower_count": 0}, ts=_ts())
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == (3, 0)


async def test_a_publish_without_counts_keeps_them(gfs_client):
    """REGRESSION (review I2): a publish that declares no counts — one sent
    before the household learned this server takes them, or a replayable
    no-``ts`` body — never wipes the counts the owner last declared."""
    seed, _ = await _register_owner(gfs_client.server.app)
    await _publish(
        gfs_client,
        seed,
        {"member_count": 2, "follower_count": 1},
        ts=_ts(seconds=-60),
    )
    before = await _stored(gfs_client)
    assert (await _publish(gfs_client, seed, ts=_ts())).status == 200
    assert (await _publish(gfs_client, seed)).status == 200
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count, stored.counts_ts) == (
        2,
        1,
        before.counts_ts,
    )


async def test_counts_in_a_body_without_ts_are_not_taken(gfs_client):
    """A no-``ts`` body is replayable forever: counts in it never land."""
    seed, _ = await _register_owner(gfs_client.server.app)
    assert (
        await _publish(gfs_client, seed, {"member_count": 9, "follower_count": 9})
    ).status == 200
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == (None, None)


async def test_a_pin_only_withdrawn_publish_keeps_the_counts(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    await _publish(gfs_client, seed, {"member_count": 6, "follower_count": 2}, ts=_ts())
    before = await _stored(gfs_client)
    resp = await _publish(
        gfs_client,
        seed,
        {"withdrawn": True, "gfs_key": await _gfs_key(gfs_client), "name": ""},
        ts=_ts(),
    )
    assert resp.status == 200
    after = await _stored(gfs_client)
    assert after.withdrawn is True
    assert (after.member_count, after.follower_count, after.counts_ts) == (
        6,
        2,
        before.counts_ts,
    )


# ── relay seats are not the owner's to declare ──────────────────────────


async def test_a_publish_racing_a_subscribe_keeps_the_seat_recount(gfs_client):
    """``publish_space`` reads the row, then upserts: a subscribe that lands
    between the two must not be undone by the stale ``subscriber_count``."""
    app = gfs_client.server.app
    repo = app[gfs_fed_repo_key]
    seed, _ = await _register_owner(app)
    await _publish(gfs_client, seed, {"allow_subscribers": True}, ts=_ts())
    stale = await _stored(gfs_client)
    await _register_owner(app, instance_id="reader.home")
    await repo.add_subscriber(space_id=_SPACE, instance_id="reader.home")
    # The publish's upsert, built from the row it read before the subscribe.
    await repo.upsert_space(stale)
    assert (await _stored(gfs_client)).subscriber_count == 1


async def test_a_new_row_still_takes_the_given_subscriber_count(gfs_client):
    repo = gfs_client.server.app[gfs_fed_repo_key]
    await _register_owner(gfs_client.server.app)
    await repo.upsert_space(
        GlobalSpace(space_id=_SPACE, owning_instance="owner.home", subscriber_count=4)
    )
    assert (await _stored(gfs_client)).subscriber_count == 4


# ── /gfs/info ───────────────────────────────────────────────────────────


async def test_gfs_info_advertises_listing_counts_in_the_signed_block(gfs_client):
    body = await (await gfs_client.get("/gfs/info")).json()
    assert body["capabilities"]["listing_counts"] is True
    assert body["capabilities_sig"]


# ── viewer JSON ─────────────────────────────────────────────────────────


async def test_viewer_json_serves_the_declared_counts(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    await _publish(
        gfs_client, seed, {"member_count": 1, "follower_count": 12}, ts=_ts()
    )
    space = (await (await gfs_client.get(f"/gfs/pages/spaces/{_SPACE}")).json())[
        "space"
    ]
    assert (space["member_count"], space["follower_count"]) == (1, 12)
    assert "subscriber_count" not in space


async def test_viewer_json_serves_null_when_nothing_was_declared(gfs_client):
    app = gfs_client.server.app
    seed, _ = await _register_owner(app)
    await _publish(gfs_client, seed)
    # Relay seats exist, but they are not a member count.
    await app[gfs_fed_repo_key].upsert_space(
        GlobalSpace(
            space_id="other",
            owning_instance="owner.home",
            status="active",
            subscriber_count=9,
        )
    )
    for sid in (_SPACE, "other"):
        space = (await (await gfs_client.get(f"/gfs/pages/spaces/{sid}")).json())[
            "space"
        ]
        assert (space["member_count"], space["follower_count"]) == (None, None)


# ── HTML ────────────────────────────────────────────────────────────────


async def test_space_page_and_landing_show_members_and_followers(gfs_client):
    seed, _ = await _register_owner(gfs_client.server.app)
    await _publish(gfs_client, seed, {"member_count": 3, "follower_count": 1}, ts=_ts())
    page = await (await gfs_client.get(f"/spaces/{_SPACE}")).text()
    assert "3 members · 1 follower" in page
    landing = await (await gfs_client.get("/")).text()
    assert "3 members · 1 follower" in landing
    assert "posts/week" in landing


def _space_html(**counts) -> str:
    html, _css = _render_space_page(
        space={
            "space_id": "s",
            "name": "S",
            "description": "",
            "about_markdown": "",
            "cover_url": None,
            "icon_url": None,
            "accent_color": "#123456",
            "primary_color": "#123456",
            "category": "general",
            **counts,
        },
        server_name="GFS",
        base_url="https://gfs.test",
    )
    return html


def _landing_html(**counts) -> str:
    html, _css = _render_landing(
        server_name="GFS",
        landing_markdown="",
        header_image_url="",
        token="t",
        pair_code="c",
        pair_qr_data_uri="",
        spaces=[
            {
                "space_id": "s",
                "name": "S",
                "description": "",
                "accent_color": "#123456",
                "posts_per_week": 2.0,
                "category": "general",
                **counts,
            }
        ],
        search="",
        category="",
        listed_categories={"general"},
        base_url="https://gfs.test",
    )
    return html


@pytest.mark.parametrize(
    ("counts", "shown", "absent"),
    [
        ({"member_count": 1, "follower_count": 1}, "1 member · 1 follower", "members"),
        ({"member_count": 2, "follower_count": 0}, "2 members · 0 followers", None),
        ({"member_count": 4, "follower_count": None}, "4 members", "follower"),
        ({"member_count": None, "follower_count": 5}, "5 followers", "member"),
        ({"member_count": None, "follower_count": None}, None, "member"),
    ],
)
def test_count_wording(counts, shown, absent):
    for html in (_space_html(**counts), _landing_html(**counts)):
        if shown is not None:
            assert shown in html
        if absent is not None:
            assert absent not in html


def test_count_wording_never_says_zero_members_for_no_number():
    for html in (
        _space_html(member_count=None, follower_count=None),
        _landing_html(member_count=None, follower_count=None),
    ):
        assert "0 members" not in html
        assert "members" not in html
        assert "followers" not in html


# ── cluster gossip ──────────────────────────────────────────────────────


def test_cluster_wire_round_trips_the_counts():
    space = GlobalSpace(
        space_id="s",
        owning_instance="o",
        member_count=8,
        follower_count=0,
        counts_ts="2026-11-23T10:00:00.000000+00:00",
    )
    wire = _space_to_wire(space)
    assert wire["member_count"] == 8
    assert wire["follower_count"] == 0
    assert wire["counts_ts"] == "2026-11-23T10:00:00.000000+00:00"
    assert _wire_to_space(wire) == space
    # A peer's values are re-validated like any wire count.
    bad = _wire_to_space(
        {**wire, "member_count": -3, "follower_count": "9", "counts_ts": 5}
    )
    assert (bad.member_count, bad.follower_count, bad.counts_ts) == (None, None, None)


async def _seed_counts(app, **kw) -> None:
    await _register_owner(app)
    await app[gfs_fed_repo_key].upsert_space(
        GlobalSpace(
            space_id=_SPACE,
            owning_instance="owner.home",
            status="active",
            subscriber_count=2,
            member_count=5,
            follower_count=3,
            counts_ts="2026-11-23T10:00:00.000000+00:00",
            **kw,
        )
    )


async def test_apply_sync_space_never_adopts_a_peer_seat_count(gfs_client):
    app = gfs_client.server.app
    await _seed_counts(app)
    wire = _space_to_wire(await _stored(gfs_client))
    await app[gfs_cluster_key].apply_sync_space(
        "upsert", {**wire, "subscriber_count": 99}
    )
    assert (await _stored(gfs_client)).subscriber_count == 2


async def test_apply_sync_space_from_an_older_node_keeps_the_counts(gfs_client):
    app = gfs_client.server.app
    await _seed_counts(app)
    wire = _space_to_wire(await _stored(gfs_client))
    for key in ("member_count", "follower_count", "counts_ts"):
        wire.pop(key)
    await app[gfs_cluster_key].apply_sync_space("upsert", wire)
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == (5, 3)
    assert stored.counts_ts == "2026-11-23T10:00:00.000000+00:00"


@pytest.mark.parametrize(
    ("peer_ts", "expected"),
    [
        ("2026-11-23T10:05:00.000000+00:00", (11, 6)),  # newer wins
        ("2026-11-23T09:55:00.000000+00:00", (5, 3)),  # older loses
        # REGRESSION (review M1): a node that never got counts gossips a
        # ``null`` stamp — it must not clear ours…
        (None, (5, 3)),
        # …nor may a far-future stamp pin its counts against every later
        # owner publish.
        ("2099-01-01T00:00:00.000000+00:00", (5, 3)),
    ],
)
async def test_apply_sync_space_counts_are_last_writer_wins(
    gfs_client, peer_ts, expected
):
    app = gfs_client.server.app
    await _seed_counts(app)
    wire = _space_to_wire(await _stored(gfs_client))
    await app[gfs_cluster_key].apply_sync_space(
        "upsert",
        {**wire, "member_count": 11, "follower_count": 6, "counts_ts": peer_ts},
    )
    stored = await _stored(gfs_client)
    assert (stored.member_count, stored.follower_count) == expected


def test_a_peer_stamp_is_normalised_to_utc():
    wire = _space_to_wire(GlobalSpace(space_id="s", owning_instance="o"))
    for raw, stored in (
        ("2026-11-23T12:00:00+02:00", "2026-11-23T10:00:00.000000+00:00"),
        ("2026-11-23T10:00:00", None),  # naive: unordered, so no stamp
        ("yesterday", None),
    ):
        assert _wire_to_space({**wire, "counts_ts": raw}).counts_ts == stored
