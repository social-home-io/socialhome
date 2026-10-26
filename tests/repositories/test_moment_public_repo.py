"""SqliteMomentPublicFollowRepo — the author profile columns (§Momentum-public)."""

from __future__ import annotations

import dataclasses

from socialhome.domain.moment_public import MomentPublicKey, MomentPublicShare
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicKeyRepo,
    SqliteMomentPublicShareRepo,
)


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


async def test_set_keywrap_keeps_the_key_on_that_gfs_only_and_survives_a_refollow(
    db,
):
    """v_68: the author household's key-wrap key lands on every follow of
    them through that GFS; a re-follow (upsert) keeps it."""
    repo = await _seed(db)
    await repo.set_keywrap(followed_user_id="u-noor", gfs_id="g1", keywrap_pk="ab" * 32)
    rows = await repo.followers_of("u-noor")
    by_key = {(r.follower_user_id, r.gfs_id): r for r in rows}
    assert by_key[("u1", "g1")].followed_keywrap_pk == "ab" * 32
    assert by_key[("u2", "g1")].followed_keywrap_pk == "ab" * 32
    assert by_key[("u1", "g2")].followed_keywrap_pk is None
    await repo.upsert(
        follower_user_id="u1",
        followed_user_id="u-noor",
        gfs_id="g1",
        followed_instance_pk="cc" * 32,
        followed_username="noor",
        followed_display_name="Noor",
    )
    got = await repo.get(follower_user_id="u1", followed_user_id="u-noor", gfs_id="g1")
    assert got is not None and got.followed_keywrap_pk == "ab" * 32


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


# ── Moment content keys (v_62) ────────────────────────────────────────────


def _key(
    author: str, gen: int, *, own: bool, step: int = 0, sealed: str = "s"
) -> MomentPublicKey:
    return MomentPublicKey(
        author_user_id=author, key_gen=gen, key_step=step, is_own=own, key_sealed=sealed
    )


async def test_key_save_get_latest_and_first_write_wins(db):
    repo = SqliteMomentPublicKeyRepo(db)
    assert await repo.latest("alice", own=True) is None
    assert await repo.save(_key("alice", 10, own=True, sealed="k10"))
    assert await repo.save(_key("alice", 20, own=True, sealed="k20"))
    # The same generation is never replaced by a save.
    assert not await repo.save(_key("alice", 20, own=True, sealed="evil"))
    latest = await repo.latest("alice", own=True)
    assert (latest.key_gen, latest.key_sealed, latest.is_own) == (20, "k20", True)
    got = await repo.get("alice", 10, own=True)
    assert got is not None and got.key_sealed == "k10"
    assert await repo.get("alice", 30, own=True) is None


async def test_advance_moves_a_step_forward_never_back(db):
    repo = SqliteMomentPublicKeyRepo(db)
    await repo.save(_key("alice", 10, own=False, step=2, sealed="s2"))
    assert await repo.advance(_key("alice", 10, own=False, step=5, sealed="s5"))
    assert not await repo.advance(_key("alice", 10, own=False, step=3, sealed="s3"))
    assert not await repo.advance(_key("alice", 99, own=False, step=9, sealed="x"))
    got = await repo.get("alice", 10, own=False)
    assert (got.key_step, got.key_sealed) == (5, "s5")


async def test_own_and_delivered_keys_never_mix(db):
    """A key delivered for an author id can never become a local author's
    own key — even under the same id and a newer generation."""
    repo = SqliteMomentPublicKeyRepo(db)
    await repo.save(_key("alice", 10, own=True, sealed="mine"))
    assert await repo.save(_key("alice", 99, own=False, sealed="theirs"))
    own = await repo.latest("alice", own=True)
    assert (own.key_gen, own.key_sealed) == (10, "mine")
    assert (await repo.latest("alice", own=False)).key_sealed == "theirs"
    assert await repo.get("alice", 99, own=True) is None


async def test_prune_keeps_the_newest_generations_of_one_author(db):
    repo = SqliteMomentPublicKeyRepo(db)
    for gen in (1, 2, 3, 4):
        await repo.save(_key("alice", gen, own=False))
    await repo.save(_key("bob", 1, own=False))
    await repo.save(_key("alice", 1, own=True))
    assert await repo.prune("alice", own=False, keep=2) == 2
    assert await repo.get("alice", 2, own=False) is None
    assert await repo.get("alice", 3, own=False) is not None
    assert await repo.get("alice", 4, own=False) is not None
    assert await repo.get("bob", 1, own=False) is not None
    assert await repo.get("alice", 1, own=True) is not None


async def test_delete_all_drops_one_authors_delivered_keys(db):
    repo = SqliteMomentPublicKeyRepo(db)
    await repo.save(_key("alice", 1, own=False))
    await repo.save(_key("alice", 2, own=False))
    await repo.save(_key("alice", 1, own=True))
    assert await repo.delete_all("alice", own=False) == 2
    assert await repo.latest("alice", own=False) is None
    assert await repo.latest("alice", own=True) is not None


async def test_any_through_tells_whether_a_server_is_followed_through(db):
    repo = await _seed(db)
    assert await repo.any_through("g1") is True
    assert await repo.any_through("g9") is False


async def test_mark_rotate_due_flags_only_the_newest_own_generation(db):
    repo = SqliteMomentPublicKeyRepo(db)
    assert not await repo.mark_rotate_due("alice")
    await repo.save(_key("alice", 10, own=True))
    await repo.save(_key("alice", 20, own=True))
    await repo.save(_key("alice", 30, own=False))
    assert await repo.mark_rotate_due("alice")
    assert (await repo.latest("alice", own=True)).rotate_due
    assert not (await repo.get("alice", 10, own=True)).rotate_due
    assert not (await repo.latest("alice", own=False)).rotate_due
    # A new generation starts unmarked.
    await repo.save(_key("alice", 40, own=True))
    assert not (await repo.latest("alice", own=True)).rotate_due


# ── Share links ────────────────────────────────────────────────────


async def _moment_row(db, moment_id: str = "m-1") -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u-share', 'sh', 'Sh', 'active')"
    )
    await db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at) VALUES(?, 'u-share', 'x', 'i',"
        " '2026-10-21T08:00:00+00:00', '2099-10-22T08:00:00+00:00')",
        (moment_id,),
    )


async def test_share_material_is_kept_once_and_goes_with_its_moment(db):
    await _moment_row(db)
    repo = SqliteMomentPublicShareRepo(db)
    share = MomentPublicShare(
        moment_id="m-1",
        author_user_id="u-share",
        item_key_sealed="sealed",
        media_by_gfs='{"g1": {"blob_id": "b"}}',
    )
    assert await repo.save(share)
    assert not await repo.save(dataclasses.replace(share, item_key_sealed="other"))
    got = await repo.get("m-1")
    assert (got.item_key_sealed, got.media_by_gfs, got.share_blob_id) == (
        "sealed",
        '{"g1": {"blob_id": "b"}}',
        None,
    )
    assert await repo.set_media("m-1", '{"g1": {}, "g3": {}}')
    assert (await repo.get("m-1")).media_by_gfs == '{"g1": {}, "g3": {}}'
    assert not await repo.set_media("m-x", "{}")
    assert await repo.set_share_copy("m-1", blob_id="c" * 64, gfs_id="g1")
    got = await repo.get("m-1")
    assert (got.share_blob_id, got.share_gfs_id) == ("c" * 64, "g1")
    assert not await repo.set_share_copy("m-x", blob_id="c" * 64, gfs_id="g1")
    await db.enqueue("DELETE FROM moments WHERE id='m-1'")
    assert await repo.get("m-1") is None
