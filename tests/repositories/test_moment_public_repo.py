"""SqliteMomentPublicFollowRepo — the author profile columns (§Momentum-public)."""

from __future__ import annotations

from socialhome.repositories.moment_public_repo import SqliteMomentPublicFollowRepo


async def _seed(db) -> SqliteMomentPublicFollowRepo:
    for uid in ("u1", "u2"):
        await db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state)"
            " VALUES(?, ?, ?, 'active')",
            (uid, uid, uid),
        )
    for gfs in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', 'aa',"
            " 'https://g.example', 'active', datetime('now'))",
            (gfs, gfs),
        )
    repo = SqliteMomentPublicFollowRepo(db)
    for follower, followed, gfs in (
        ("u1", "u-noor", "g1"),
        ("u2", "u-noor", "g1"),
        ("u1", "u-noor", "g2"),
        ("u1", "u-sam", "g1"),
    ):
        await repo.upsert(
            follower_user_id=follower,
            followed_user_id=followed,
            gfs_id=gfs,
            followed_instance_pk="bb" * 32,
            followed_username=followed,
            followed_display_name=followed.upper(),
        )
    return repo


async def test_upsert_round_trips_bio_and_digest(db):
    repo = await _seed(db)
    got = await repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-noor",
        gfs_id="g1",
        followed_instance_pk="bb" * 32,
        followed_username="noor",
        followed_display_name="Noor",
        followed_bio="Beekeeper.",
        followed_picture_digest="0123456789abcdef",
    )
    assert (got.followed_bio, got.followed_picture_digest) == (
        "Beekeeper.",
        "0123456789abcdef",
    )


async def test_set_profile_updates_every_follow_on_that_gfs_only(db):
    repo = await _seed(db)
    await repo.set_profile(
        followed_user_id="u-noor",
        gfs_id="g1",
        display_name="Noor Haddad",
        bio="Beekeeper.",
        picture_digest="0123456789abcdef",
    )
    rows = await repo.followers_of("u-noor")
    by_key = {(r.follower_user_id, r.gfs_id): r for r in rows}
    for key in (("u1", "g1"), ("u2", "g1")):
        assert by_key[key].followed_display_name == "Noor Haddad"
        assert by_key[key].followed_bio == "Beekeeper."
    # Another GFS's follow is not touched — it verifies its own envelopes.
    assert by_key[("u1", "g2")].followed_display_name == "U-NOOR"
    assert by_key[("u1", "g2")].followed_bio is None


async def test_list_for_followed_reads_many_authors_at_once(db):
    repo = await _seed(db)
    rows = await repo.list_for_followed(["u-noor", "u-sam"])
    assert {(r.follower_user_id, r.followed_user_id, r.gfs_id) for r in rows} == {
        ("u1", "u-noor", "g1"),
        ("u2", "u-noor", "g1"),
        ("u1", "u-noor", "g2"),
        ("u1", "u-sam", "g1"),
    }
    assert await repo.list_for_followed([]) == []
