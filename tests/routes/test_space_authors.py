"""``GET /api/spaces/{id}/authors`` and its picture route (v_60): the
authors a GFS follower knows from their published profiles."""

from __future__ import annotations

from socialhome.app_keys import space_author_profile_repo_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.space_author_profile import SpaceAuthorProfile

from .conftest import _auth

PIC = b"RIFF\x00\x00\x00\x00WEBPVP8 fake"


async def _space_with_profile(client) -> str:
    r = await client.post(
        "/api/spaces", json={"name": "Garden"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    await client.app[space_author_profile_repo_key].upsert_if_newer(
        SpaceAuthorProfile(
            space_id=sid,
            author_user_id="u-noor",
            author_pk="ab" * 32,
            display_name="Noor Haddad",
            bio="Beekeeper.",
            picture_hash="0123456789abcdef",
            profile_rev="r",
            ts="2026-10-20T08:00:00.000000+00:00",
        ),
        picture_webp=PIC,
    )
    return sid


async def test_a_reader_gets_the_authors_with_a_signed_picture(client):
    sid = await _space_with_profile(client)
    r = await client.get(f"/api/spaces/{sid}/authors", headers=_auth(client._tok))
    assert r.status == 200
    [row] = await r.json()
    assert row["user_id"] == "u-noor"
    assert (row["display_name"], row["bio"]) == ("Noor Haddad", "Beekeeper.")
    assert row["picture_url"].startswith(
        f"api/spaces/{sid}/authors/u-noor/picture?v=0123456789abcdef"
    )
    assert "sig=" in row["picture_url"]
    # The signed URL loads with no bearer (an ``<img src>``).
    pic = await client.get("/" + row["picture_url"])
    assert pic.status == 200
    assert await pic.read() == PIC
    assert pic.headers["Content-Type"] == "image/webp"


async def test_someone_who_cannot_read_the_space_gets_nothing(client):
    sid = await _space_with_profile(client)
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('eve','u-eve','Eve')"
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-eve", "u-eve", "t", sha256_token_hash("eve-tok")),
    )
    r = await client.get(f"/api/spaces/{sid}/authors", headers=_auth("eve-tok"))
    assert r.status in (403, 404)


async def test_no_picture_is_a_404(client):
    sid = await _space_with_profile(client)
    r = await client.get(
        f"/api/spaces/{sid}/authors/u-nobody/picture", headers=_auth(client._tok)
    )
    assert r.status == 404


async def test_across_a_guardian_block_the_name_only(client):
    sid = await _space_with_profile(client)
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?", (client._uid,)
    )
    await client._db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?,?,?)",
        (client._uid, "u-noor", client._uid),
    )
    r = await client.get(f"/api/spaces/{sid}/authors", headers=_auth(client._tok))
    [row] = await r.json()
    assert row["display_name"] == "Noor Haddad"
    assert (row["bio"], row["picture_url"]) == (None, None)
    pic = await client.get(
        f"/api/spaces/{sid}/authors/u-noor/picture", headers=_auth(client._tok)
    )
    assert pic.status == 404
