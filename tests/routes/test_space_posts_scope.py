"""Space post scope (§24.11): ``PATCH`` / ``DELETE
/api/spaces/{id}/posts/{post_id}`` act only on a post of the PATH space. A
post id of another space is 404 and its row is untouched — even for the
owner of both spaces, and for a moderator of the path space who is a plain
member where the post lives."""

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def _seed_space(client, sid: str) -> None:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, ?, 'inst', 'admin', ?)",
        (sid, sid, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'owner')",
        (sid, client._uid),
    )


async def _add_user(client, username: str, user_id: str) -> dict:
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, 0)",
        (username, user_id, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return {"Authorization": f"Bearer {token}"}


async def _post_row(client, post_id: str) -> tuple:
    row = await client._db.fetchone(
        "SELECT space_id, content, edited_at, deleted FROM space_posts WHERE id=?",
        (post_id,),
    )
    return tuple(row)


async def _two_spaces_with_a_post_in_b(client) -> str:
    await _seed_space(client, "sp-a")
    await _seed_space(client, "sp-b")
    r = await client.post(
        "/api/spaces/sp-b/posts",
        json={"type": "text", "content": "b's post"},
        headers=_auth(client._tok),
    )
    assert r.status in (200, 201), await r.text()
    return (await r.json())["id"]


async def test_another_spaces_post_is_404_and_untouched_for_the_owner(client):
    owner = _auth(client._tok)
    pid = await _two_spaces_with_a_post_in_b(client)
    before = await _post_row(client, pid)

    r = await client.patch(
        f"/api/spaces/sp-a/posts/{pid}", json={"content": "hijacked"}, headers=owner
    )
    assert r.status == 404, await r.text()
    r = await client.delete(f"/api/spaces/sp-a/posts/{pid}", headers=owner)
    assert r.status == 404, await r.text()
    assert await _post_row(client, pid) == before

    # Under its own space the same calls work.
    r = await client.patch(
        f"/api/spaces/sp-b/posts/{pid}", json={"content": "edited"}, headers=owner
    )
    assert r.status == 200, await r.text()
    assert (await _post_row(client, pid))[1] == "edited"
    r = await client.delete(f"/api/spaces/sp-b/posts/{pid}", headers=owner)
    assert r.status == 204
    assert (await _post_row(client, pid))[3]


async def test_a_moderator_elsewhere_gets_404_not_403(client):
    """A moderator of sp-a who is a plain member of sp-b: the sp-a path
    never reveals that the sp-b post exists (404, not the 403 the post's
    own space would answer)."""
    pid = await _two_spaces_with_a_post_in_b(client)
    mod = await _add_user(client, "mo", "uid-mo")
    for sid, role in (("sp-a", "moderator"), ("sp-b", "member")):
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (sid, "uid-mo", role),
        )
    before = await _post_row(client, pid)
    r = await client.patch(
        f"/api/spaces/sp-a/posts/{pid}", json={"content": "x"}, headers=mod
    )
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/posts/{pid}", headers=mod)
    assert r.status == 404
    assert await _post_row(client, pid) == before
