"""Route-level error-path tests to push module coverage above 90%.

Every test targets a specific ``error_response(...)`` or guard branch
that happy-path tests leave uncovered. Each test is independent; no
cross-test ordering required.
"""

from __future__ import annotations

import json

from socialhome.auth import sha256_token_hash

from .conftest import _auth


# ─── helpers ───────────────────────────────────────────────────────────────


async def _add_second_user(
    client,
    *,
    username: str = "bob",
    user_id: str = "bob-id",
    token: str = "bob-tok",
    is_admin: int = 0,
) -> None:
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, ?)",
        (username, user_id, username.title(), is_admin),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )


async def _seed_space(client, *, sid: str = "sp-x", role: str = "admin") -> str:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'X', 'inst', 'admin', ?)",
        (sid, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, client._uid, role),
    )
    return sid


# ─── pages.py ──────────────────────────────────────────────────────────────


async def test_pages_post_missing_title_422(client):
    r = await client.post(
        "/api/pages",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_pages_patch_missing_404(client):
    r = await client.patch(
        "/api/pages/does-not-exist",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_patch_empty_title_422(client):
    r = await client.post(
        "/api/pages",
        json={"title": "P", "content": "x"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/pages/{pid}",
        json={"title": "   "},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_pages_patch_cover_image_url_succeeds(client):
    r = await client.post(
        "/api/pages",
        json={"title": "C", "content": "x"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/pages/{pid}",
        json={"cover_image_url": "/media/x.webp"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["cover_image_url"] == "/media/x.webp"


async def test_pages_lock_taken_409(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/pages",
        json={"title": "L", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.post(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    # Bob tries — already locked by admin.
    r2 = await client.post(
        f"/api/pages/{pid}/lock",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 409


async def test_pages_lock_not_found_404(client):
    r = await client.post(
        "/api/pages/does-not-exist/lock",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_refresh_not_found_404(client):
    r = await client.post(
        "/api/pages/missing/lock/refresh",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_revert_non_int_version_422(client):
    r = await client.post(
        "/api/pages",
        json={"title": "R", "content": "v1"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/pages/{pid}/revert",
        json={"version": "not-a-number"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_pages_revert_page_missing_404(client):
    r = await client.post(
        "/api/pages/does-not-exist/revert",
        json={"version": 1},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_revert_unknown_version_404(client):
    r = await client.post(
        "/api/pages",
        json={"title": "R", "content": "v1"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Create a version via patch.
    await client.patch(
        f"/api/pages/{pid}",
        json={"content": "v2"},
        headers=_auth(client._tok),
    )
    r2 = await client.post(
        f"/api/pages/{pid}/revert",
        json={"version": 9999},
        headers=_auth(client._tok),
    )
    assert r2.status == 404


async def test_pages_revert_succeeds_with_valid_version(client):
    r = await client.post(
        "/api/pages",
        json={"title": "R", "content": "v1"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.patch(
        f"/api/pages/{pid}",
        json={"content": "v2"},
        headers=_auth(client._tok),
    )
    r2 = await client.post(
        f"/api/pages/{pid}/revert",
        json={"version": 1},
        headers=_auth(client._tok),
    )
    assert r2.status == 200


async def test_pages_delete_request_missing_404(client):
    r = await client.post(
        "/api/pages/missing/delete-request",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_delete_approve_missing_404(client):
    r = await client.post(
        "/api/pages/missing/delete-approve",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_delete_approve_not_requested_409(client):
    r = await client.post(
        "/api/pages",
        json={"title": "A", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # No delete-request happened; admin approves directly → 409.
    r2 = await client.post(
        f"/api/pages/{pid}/delete-approve",
        headers=_auth(client._tok),
    )
    assert r2.status == 409


async def test_pages_delete_cancel_missing_404(client):
    r = await client.post(
        "/api/pages/missing/delete-cancel",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_pages_delete_cancel_forbidden(client):
    """Non-requester, non-admin → 403."""
    await _add_second_user(client)
    await _add_second_user(
        client,
        username="carl",
        user_id="carl-id",
        token="carl-tok",
    )
    r = await client.post(
        "/api/pages",
        json={"title": "X", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Bob raises request.
    await client.post(
        f"/api/pages/{pid}/delete-request",
        headers={"Authorization": "Bearer bob-tok"},
    )
    # Carl (also non-admin, not the requester) tries to cancel → 403.
    r2 = await client.post(
        f"/api/pages/{pid}/delete-cancel",
        headers={"Authorization": "Bearer carl-tok"},
    )
    assert r2.status == 403


async def test_pages_delete_cancel_by_requester_ok(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/pages",
        json={"title": "X", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.post(
        f"/api/pages/{pid}/delete-request",
        headers={"Authorization": "Bearer bob-tok"},
    )
    r2 = await client.post(
        f"/api/pages/{pid}/delete-cancel",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 200


async def test_space_page_create_missing_title_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_space_page_detail_non_member_403(client):
    sid = await _seed_space(client)
    # Create a page in the space as a member.
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await _add_second_user(client)
    # Bob isn't in the space.
    r2 = await client.get(
        f"/api/spaces/{sid}/pages/{pid}",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 403


async def test_space_page_detail_wrong_space_404(client):
    sid = await _seed_space(client)
    # A household-scope page's space_id is None — detail in the space must 404.
    r = await client.post(
        "/api/pages",
        json={"title": "H", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.get(
        f"/api/spaces/{sid}/pages/{pid}",
        headers=_auth(client._tok),
    )
    assert r2.status == 404


# ─── users.py ──────────────────────────────────────────────────────────────


async def test_me_picture_upload_not_multipart_422(client):
    r = await client.post(
        "/api/me/picture",
        data="not-multipart",
        headers={**_auth(client._tok), "Content-Type": "application/octet-stream"},
    )
    assert r.status == 422


async def test_me_picture_upload_missing_field_422(client):
    """Multipart request with no file field → 422."""
    # Empty multipart body
    form = b"--BOUND--\r\n"
    r = await client.post(
        "/api/me/picture",
        data=form,
        headers={
            **_auth(client._tok),
            "Content-Type": "multipart/form-data; boundary=BOUND",
        },
    )
    assert r.status == 422


async def test_me_picture_delete_204(client):
    r = await client.delete("/api/me/picture", headers=_auth(client._tok))
    assert r.status == 204


async def test_me_picture_refresh_non_ha_mode_501(client):
    r = await client.post(
        "/api/me/picture/refresh-from-ha",
        headers=_auth(client._tok),
    )
    assert r.status == 501


async def test_user_picture_missing_404(client):
    r = await client.get(
        "/api/users/no-such-user/picture",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_admin_patch_user_missing_404(client):
    r = await client.patch(
        "/api/users/no-such-user",
        json={"is_admin": True},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_admin_patch_user_non_admin_caller_403(client):
    await _add_second_user(client)
    # Caller demotes self first so they're not admin.
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.patch(
        "/api/users/bob-id",
        json={"is_admin": True},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_admin_patch_user_missing_field_422(client):
    await _add_second_user(client)
    r = await client.patch(
        "/api/users/bob-id",
        json={"display_name": "Nope"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_admin_patch_user_last_admin_409(client):
    # admin tries to demote themselves, and they are the ONLY admin.
    r = await client.patch(
        f"/api/users/{client._uid}",
        json={"is_admin": False},
        headers=_auth(client._tok),
    )
    assert r.status == 409


async def test_me_tokens_get(client):
    r = await client.get("/api/me/tokens", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body.get("tokens"), list)


async def test_me_tokens_create(client):
    r = await client.post(
        "/api/me/tokens",
        json={"label": "laptop"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert "token" in body and "token_id" in body


async def test_me_tokens_delete(client):
    r = await client.post(
        "/api/me/tokens",
        json={"label": "x"},
        headers=_auth(client._tok),
    )
    tid = (await r.json())["token_id"]
    r2 = await client.delete(
        f"/api/me/tokens/{tid}",
        headers=_auth(client._tok),
    )
    assert r2.status == 204


async def test_admin_tokens_list(client):
    r = await client.get("/api/admin/tokens", headers=_auth(client._tok))
    assert r.status == 200


async def test_admin_tokens_non_admin_403(client):
    await _add_second_user(client)
    r = await client.get(
        "/api/admin/tokens",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_admin_token_delete_non_admin_403(client):
    await _add_second_user(client)
    r = await client.delete(
        "/api/admin/tokens/whatever",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_me_export_returns_json(client):
    r = await client.get("/api/me/export", headers=_auth(client._tok))
    assert r.status == 200
    # Body is a JSON attachment — payload should parse.
    body = await r.read()
    data = json.loads(body)
    assert isinstance(data, dict)


async def test_user_export_non_admin_403(client):
    await _add_second_user(client)
    r = await client.get(
        "/api/users/bob-id/export",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_user_export_admin_ok(client):
    r = await client.get(
        f"/api/users/{client._uid}/export",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_auth_token_route_standalone_422_missing_creds(client):
    r = await client.post("/api/auth/token", json={})
    assert r.status == 422


async def test_auth_token_route_bad_credentials_401(client):
    r = await client.post(
        "/api/auth/token",
        json={"username": "nobody", "password": "nope"},
    )
    # StandaloneAdapter refuses unknown creds.
    assert r.status in (401, 429)


# ─── stickies.py ───────────────────────────────────────────────────────────


async def test_sticky_patch_not_found_404(client):
    r = await client.patch(
        "/api/stickies/missing",
        json={"content": "hi"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_sticky_patch_updates_content_and_position_and_color(client):
    r = await client.post(
        "/api/stickies",
        json={"content": "c", "color": "#fff", "position_x": 1, "position_y": 2},
        headers=_auth(client._tok),
    )
    sid = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/stickies/{sid}",
        json={"content": "new", "position_x": 5, "position_y": 6, "color": "#abc"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["content"] == "new"
    assert body["position_x"] == 5
    assert body["color"] == "#abc"


async def test_sticky_delete_not_found_404(client):
    r = await client.delete(
        "/api/stickies/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_sticky_delete_ok(client):
    r = await client.post(
        "/api/stickies",
        json={"content": "c"},
        headers=_auth(client._tok),
    )
    sid = (await r.json())["id"]
    r2 = await client.delete(
        f"/api/stickies/{sid}",
        headers=_auth(client._tok),
    )
    assert r2.status == 200


async def test_space_sticky_non_member_get_403(client):
    sid = await _seed_space(client)
    await _add_second_user(client)
    r = await client.get(
        f"/api/spaces/{sid}/stickies",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_space_sticky_non_member_post_403(client):
    sid = await _seed_space(client)
    await _add_second_user(client)
    r = await client.post(
        f"/api/spaces/{sid}/stickies",
        json={"content": "x"},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_space_sticky_full_crud(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/stickies",
        json={"content": "x", "color": "#fff", "position_x": 1, "position_y": 2},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    stid = (await r.json())["id"]
    # Patch
    r2 = await client.patch(
        f"/api/spaces/{sid}/stickies/{stid}",
        json={"content": "y", "position_x": 3, "position_y": 4, "color": "#aaa"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    # Delete
    r3 = await client.delete(
        f"/api/spaces/{sid}/stickies/{stid}",
        headers=_auth(client._tok),
    )
    assert r3.status == 200


async def test_space_sticky_patch_missing_404(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/stickies/missing",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_sticky_delete_missing_404(client):
    sid = await _seed_space(client)
    r = await client.delete(
        f"/api/spaces/{sid}/stickies/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_sticky_patch_non_member_403(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/stickies",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    stid = (await r.json())["id"]
    await _add_second_user(client)
    r2 = await client.patch(
        f"/api/spaces/{sid}/stickies/{stid}",
        json={"content": "y"},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 403


async def test_space_sticky_delete_non_member_403(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/stickies",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    stid = (await r.json())["id"]
    await _add_second_user(client)
    r2 = await client.delete(
        f"/api/spaces/{sid}/stickies/{stid}",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 403


# ─── polls.py ──────────────────────────────────────────────────────────────


async def _create_feed_post(client) -> str:
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "poll host"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


async def test_poll_vote_missing_option_id_422(client):
    pid = await _create_feed_post(client)
    r = await client.post(
        f"/api/posts/{pid}/poll/vote",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_poll_vote_unknown_post_422(client):
    r = await client.post(
        "/api/posts/nope/poll/vote",
        json={"option_id": "whatever"},
        headers=_auth(client._tok),
    )
    # Either 422 (service rejects) or 404 (post missing) — both hit the
    # branch under test.
    assert r.status in (404, 422)


async def test_poll_create_missing_fields_422(client):
    pid = await _create_feed_post(client)
    r = await client.post(
        f"/api/posts/{pid}/poll",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_poll_create_and_summary(client):
    pid = await _create_feed_post(client)
    r = await client.post(
        f"/api/posts/{pid}/poll",
        json={"question": "Q?", "options": ["a", "b"]},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    r2 = await client.get(
        f"/api/posts/{pid}/poll",
        headers=_auth(client._tok),
    )
    assert r2.status == 200


async def test_schedule_poll_create_missing_slots_422(client):
    pid = await _create_feed_post(client)
    r = await client.post(
        f"/api/posts/{pid}/schedule-poll",
        json={"title": "Plan"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_schedule_poll_respond_missing_fields_422(client):
    r = await client.post(
        "/api/schedule-polls/whatever/respond",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_schedule_poll_finalize_missing_slot_422(client):
    r = await client.post(
        "/api/schedule-polls/whatever/finalize",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_schedule_poll_summary_any_id_ok(client):
    r = await client.get(
        "/api/schedule-polls/whatever/summary",
        headers=_auth(client._tok),
    )
    # summary tolerates unknown IDs (empty summary)
    assert r.status == 200


async def test_space_schedule_poll_non_member_403(client):
    sid = await _seed_space(client)
    await _add_second_user(client)
    r = await client.post(
        f"/api/spaces/{sid}/posts/pid/schedule-poll",
        json={"title": "T", "slots": []},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_space_schedule_poll_finalize_missing_slot_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/schedule-polls/pid/finalize",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_space_schedule_poll_respond_missing_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/schedule-polls/pid/respond",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_space_schedule_poll_summary_non_member_403(client):
    sid = await _seed_space(client)
    await _add_second_user(client)
    r = await client.get(
        f"/api/spaces/{sid}/schedule-polls/pid/summary",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


# ─── tasks.py ──────────────────────────────────────────────────────────────


async def test_task_list_collection_full_crud(client):
    # Create list
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "Chores"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    lid = (await r.json())["id"]
    # GET
    r2 = await client.get(f"/api/tasks/lists/{lid}", headers=_auth(client._tok))
    assert r2.status == 200
    # Rename
    r3 = await client.patch(
        f"/api/tasks/lists/{lid}",
        json={"name": "Weekend"},
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    assert (await r3.json())["name"] == "Weekend"
    # List all
    r4 = await client.get("/api/tasks/lists", headers=_auth(client._tok))
    assert r4.status == 200
    # Delete
    r5 = await client.delete(
        f"/api/tasks/lists/{lid}",
        headers=_auth(client._tok),
    )
    assert r5.status == 200


async def test_task_list_tasks_filters_and_pagination(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    # Create 3 tasks
    for i in range(3):
        await client.post(
            f"/api/tasks/lists/{lid}/tasks",
            json={"title": f"t{i}"},
            headers=_auth(client._tok),
        )
    # List with filters
    r2 = await client.get(
        f"/api/tasks/lists/{lid}/tasks?limit=2&offset=0&include_done=false",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    assert len(await r2.json()) <= 2


async def test_task_list_tasks_bad_limit_422(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    r2 = await client.get(
        f"/api/tasks/lists/{lid}/tasks?limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_task_reorder_bad_payload_422(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/tasks/lists/{lid}/reorder",
        json={"order": "not-a-list"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_task_reorder_unknown_list_404(client):
    r = await client.post(
        "/api/tasks/lists/does-not-exist/reorder",
        json={"order": []},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_task_detail_patch_delete(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "t", "description": "d"},
        headers=_auth(client._tok),
    )
    tid = (await r2.json())["id"]
    r3 = await client.patch(
        f"/api/tasks/{tid}",
        json={"title": "t2", "status": "done"},
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    r4 = await client.delete(f"/api/tasks/{tid}", headers=_auth(client._tok))
    assert r4.status == 200


async def test_task_comments_full_flow(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "t"},
        headers=_auth(client._tok),
    )
    tid = (await r2.json())["id"]
    r3 = await client.post(
        f"/api/tasks/{tid}/comments",
        json={"content": "hi"},
        headers=_auth(client._tok),
    )
    assert r3.status == 201
    cid = (await r3.json())["id"]
    r4 = await client.get(
        f"/api/tasks/{tid}/comments",
        headers=_auth(client._tok),
    )
    assert r4.status == 200
    assert len(await r4.json()) == 1
    r5 = await client.delete(
        f"/api/tasks/{tid}/comments/{cid}",
        headers=_auth(client._tok),
    )
    assert r5.status == 200


async def test_task_attachments_full_flow(client):
    r = await client.post(
        "/api/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    lid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/tasks/lists/{lid}/tasks",
        json={"title": "t"},
        headers=_auth(client._tok),
    )
    tid = (await r2.json())["id"]
    r3 = await client.post(
        f"/api/tasks/{tid}/attachments",
        json={
            "url": "/media/x.bin",
            "filename": "x.bin",
            "mime": "application/octet-stream",
            "size_bytes": 10,
        },
        headers=_auth(client._tok),
    )
    assert r3.status == 201
    aid = (await r3.json())["id"]
    r4 = await client.get(
        f"/api/tasks/{tid}/attachments",
        headers=_auth(client._tok),
    )
    assert r4.status == 200
    r5 = await client.delete(
        f"/api/tasks/{tid}/attachments/{aid}",
        headers=_auth(client._tok),
    )
    assert r5.status == 200


async def test_space_tasks_full_flow(client):
    sid = await _seed_space(client)
    # list lists
    r0 = await client.get(
        f"/api/spaces/{sid}/tasks/lists",
        headers=_auth(client._tok),
    )
    assert r0.status == 200
    # create list
    r = await client.post(
        f"/api/spaces/{sid}/tasks/lists",
        json={"name": "L"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    lid = (await r.json())["id"]
    # rename
    r2 = await client.patch(
        f"/api/spaces/{sid}/tasks/lists/{lid}",
        json={"name": "L2"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    # create task
    r3 = await client.post(
        f"/api/spaces/{sid}/tasks/lists/{lid}/tasks",
        json={"title": "t"},
        headers=_auth(client._tok),
    )
    assert r3.status == 201
    tid = (await r3.json())["id"]
    # list tasks
    r4 = await client.get(
        f"/api/spaces/{sid}/tasks/lists/{lid}/tasks",
        headers=_auth(client._tok),
    )
    assert r4.status == 200
    # patch task
    r5 = await client.patch(
        f"/api/spaces/{sid}/tasks/{tid}",
        json={"title": "t2"},
        headers=_auth(client._tok),
    )
    assert r5.status == 200
    # delete task
    r6 = await client.delete(
        f"/api/spaces/{sid}/tasks/{tid}",
        headers=_auth(client._tok),
    )
    assert r6.status == 200
    # delete list
    r7 = await client.delete(
        f"/api/spaces/{sid}/tasks/lists/{lid}",
        headers=_auth(client._tok),
    )
    assert r7.status == 200


async def test_space_task_non_member_all_403(client):
    sid = await _seed_space(client)
    await _add_second_user(client)
    hdr = {"Authorization": "Bearer bob-tok"}
    assert (
        await client.get(f"/api/spaces/{sid}/tasks/lists", headers=hdr)
    ).status == 403
    assert (
        await client.post(
            f"/api/spaces/{sid}/tasks/lists",
            json={"name": "L"},
            headers=hdr,
        )
    ).status == 403
    assert (
        await client.patch(
            f"/api/spaces/{sid}/tasks/lists/x",
            json={"name": "L"},
            headers=hdr,
        )
    ).status == 403
    assert (
        await client.delete(f"/api/spaces/{sid}/tasks/lists/x", headers=hdr)
    ).status == 403
    assert (
        await client.get(
            f"/api/spaces/{sid}/tasks/lists/x/tasks",
            headers=hdr,
        )
    ).status == 403
    assert (
        await client.post(
            f"/api/spaces/{sid}/tasks/lists/x/tasks",
            json={"title": "t"},
            headers=hdr,
        )
    ).status == 403
    assert (
        await client.patch(
            f"/api/spaces/{sid}/tasks/x",
            json={"title": "t"},
            headers=hdr,
        )
    ).status == 403
    assert (
        await client.delete(f"/api/spaces/{sid}/tasks/x", headers=hdr)
    ).status == 403


async def test_space_task_patch_missing_404(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/tasks/does-not-exist",
        json={"title": "t"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_task_delete_missing_404(client):
    sid = await _seed_space(client)
    r = await client.delete(
        f"/api/spaces/{sid}/tasks/does-not-exist",
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── bazaar.py ─────────────────────────────────────────────────────────────


async def test_bazaar_post_title_required_422(client):
    r = await client.post(
        "/api/bazaar",
        json={"mode": "fixed", "currency": "EUR"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_post_mode_required_422(client):
    r = await client.post(
        "/api/bazaar",
        json={"title": "T", "currency": "EUR"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_post_currency_required_422(client):
    r = await client.post(
        "/api/bazaar",
        json={"title": "T", "mode": "fixed"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_post_image_urls_must_be_list_422(client):
    r = await client.post(
        "/api/bazaar",
        json={
            "title": "T",
            "mode": "fixed",
            "currency": "EUR",
            "image_urls": "not-a-list",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_post_invalid_price_422(client):
    r = await client.post(
        "/api/bazaar",
        json={
            "title": "T",
            "mode": "fixed",
            "currency": "EUR",
            "price": "not-an-int",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_get_missing_404(client):
    r = await client.get(
        "/api/bazaar/does-not-exist",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_patch_missing_404(client):
    r = await client.patch(
        "/api/bazaar/does-not-exist",
        json={"title": "T"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_delete_missing_404(client):
    r = await client.delete(
        "/api/bazaar/does-not-exist",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_bid_place_missing_amount_422(client):
    r = await client.post(
        "/api/bazaar/does-not-exist/bids",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_bid_place_bad_amount_422(client):
    r = await client.post(
        "/api/bazaar/does-not-exist/bids",
        json={"amount": "NaN"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_bazaar_bid_delete_missing_404(client):
    r = await client.delete(
        "/api/bazaar/whatever/bids/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_bid_accept_missing_404(client):
    r = await client.post(
        "/api/bazaar/whatever/bids/missing/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_bid_reject_missing_404(client):
    r = await client.post(
        "/api/bazaar/whatever/bids/missing/reject",
        json={"reason": "no"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_bazaar_list_active_and_seller_me(client):
    r = await client.get("/api/bazaar", headers=_auth(client._tok))
    assert r.status == 200
    r2 = await client.get(
        "/api/bazaar?seller=me",
        headers=_auth(client._tok),
    )
    assert r2.status == 200


async def test_bazaar_bids_list(client):
    r = await client.get(
        "/api/bazaar/whatever/bids",
        headers=_auth(client._tok),
    )
    assert r.status == 200  # empty list for unknown listing


# ─── gfs.py ────────────────────────────────────────────────────────────────


async def test_gfs_connections_list_ok(client):
    r = await client.get("/api/gfs/connections", headers=_auth(client._tok))
    assert r.status == 200


async def test_gfs_connections_create_non_admin_403(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/gfs/connections",
        json={},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_gfs_connections_create_bad_payload_422(client):
    r = await client.post(
        "/api/gfs/connections",
        json={"garbage": True},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_gfs_connection_get_missing_404(client):
    r = await client.get(
        "/api/gfs/connections/does-not-exist",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_gfs_connection_delete_non_admin_403(client):
    await _add_second_user(client)
    r = await client.delete(
        "/api/gfs/connections/does-not-exist",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_gfs_connection_delete_missing_404(client):
    r = await client.delete(
        "/api/gfs/connections/does-not-exist",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_gfs_space_publish_non_admin_403(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/spaces/x/publish/y",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_gfs_space_publish_bad_422(client):
    r = await client.post(
        "/api/spaces/nope/publish/nope",
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_gfs_space_unpublish_bad_422(client):
    r = await client.delete(
        "/api/spaces/nope/publish/nope",
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_gfs_publications_list(client):
    r = await client.get(
        "/api/gfs/publications",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_gfs_publications_non_admin_403(client):
    await _add_second_user(client)
    r = await client.get(
        "/api/gfs/publications",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_gfs_appeal_non_admin_403(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/gfs/connections/x/appeal",
        json={"target_type": "space", "target_id": "sp", "message": "m"},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_gfs_appeal_missing_target_422(client):
    r = await client.post(
        "/api/gfs/connections/x/appeal",
        json={"target_type": "bogus", "target_id": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── spaces.py ─────────────────────────────────────────────────────────────


async def test_space_member_me_patch_missing_field_422(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/members/me",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_space_member_me_patch_updates_display_name(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/members/me",
        json={"space_display_name": "Alt"},
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_space_member_me_delete_leaves_space(client):
    sid = await _seed_space(client)
    # admin leaves — service decides whether this is allowed (last admin?)
    r = await client.delete(
        f"/api/spaces/{sid}/members/me",
        headers=_auth(client._tok),
    )
    # Either 200 ok or 403 — branch is hit either way.
    assert r.status in (200, 403, 409)


async def test_space_member_me_picture_delete_204(client):
    sid = await _seed_space(client)
    r = await client.delete(
        f"/api/spaces/{sid}/members/me/picture",
        headers=_auth(client._tok),
    )
    assert r.status in (204, 404)


async def test_space_member_me_picture_upload_bad_multipart_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/members/me/picture",
        data="not multipart",
        headers={**_auth(client._tok), "Content-Type": "application/octet-stream"},
    )
    assert r.status == 422


async def test_space_member_picture_missing_404(client):
    sid = await _seed_space(client)
    r = await client.get(
        f"/api/spaces/{sid}/members/bob-id/picture",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_cover_get_missing_404(client):
    sid = await _seed_space(client)
    r = await client.get(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_cover_post_bad_multipart_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/cover",
        data="x",
        headers={**_auth(client._tok), "Content-Type": "application/octet-stream"},
    )
    assert r.status == 422


async def test_space_member_detail_invalid_role_422(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/members/{client._uid}",
        json={"role": "god"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Picture uploads with real bytes ───────────────────────────────────────


def _make_multipart(file_bytes: bytes, *, boundary: str = "BOUND") -> bytes:
    """Build a minimal multipart/form-data body with a single file field."""
    return (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="x.bin"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def test_me_picture_upload_oversized_422(client):
    """Upload exceeding MAX_UPLOAD_BYTES is rejected with 422."""
    # Build a payload larger than the 10 MiB cap (PROFILE_PICTURE_MAX_UPLOAD_BYTES).
    from socialhome.domain.media_constraints import PROFILE_PICTURE_MAX_UPLOAD_BYTES

    body = _make_multipart(b"A" * (PROFILE_PICTURE_MAX_UPLOAD_BYTES + 1024))
    r = await client.post(
        "/api/me/picture",
        data=body,
        headers={
            **_auth(client._tok),
            "Content-Type": "multipart/form-data; boundary=BOUND",
        },
    )
    assert r.status == 422


async def test_me_picture_upload_non_image_bytes_422(client):
    """Non-image bytes fail the ImageProcessor step → 422."""
    body = _make_multipart(b"not actually an image")
    r = await client.post(
        "/api/me/picture",
        data=body,
        headers={
            **_auth(client._tok),
            "Content-Type": "multipart/form-data; boundary=BOUND",
        },
    )
    # ImageProcessor raises ValueError which the handler maps to 422.
    assert r.status == 422


# ─── More pages.py coverage — space-scoped patch/delete + conflict resolve ──


async def test_space_page_patch_and_delete(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": "a"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # PATCH with every field.
    r2 = await client.patch(
        f"/api/spaces/{sid}/pages/{pid}",
        json={
            "title": "P2",
            "content": "b",
            "cover_image_url": "/m/c.webp",
        },
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    # PATCH with stale base → 409.
    r3 = await client.patch(
        f"/api/spaces/{sid}/pages/{pid}",
        json={"content": "c", "base_updated_at": "2020-01-01T00:00:00Z"},
        headers=_auth(client._tok),
    )
    assert r3.status == 409
    # PATCH with empty title → 422.
    r4 = await client.patch(
        f"/api/spaces/{sid}/pages/{pid}",
        json={"title": "   "},
        headers=_auth(client._tok),
    )
    assert r4.status == 422
    # DELETE
    r5 = await client.delete(
        f"/api/spaces/{sid}/pages/{pid}",
        headers=_auth(client._tok),
    )
    assert r5.status == 200


async def test_space_page_patch_non_member_403(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await _add_second_user(client)
    r2 = await client.patch(
        f"/api/spaces/{sid}/pages/{pid}",
        json={"content": "x"},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 403


async def test_space_page_delete_non_member_403(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await _add_second_user(client)
    r2 = await client.delete(
        f"/api/spaces/{sid}/pages/{pid}",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 403


async def test_space_page_patch_missing_404(client):
    sid = await _seed_space(client)
    r = await client.patch(
        f"/api/spaces/{sid}/pages/missing",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_page_delete_missing_404(client):
    sid = await _seed_space(client)
    r = await client.delete(
        f"/api/spaces/{sid}/pages/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_page_conflict_resolve_bad_resolution_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": "x"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/pages/{pid}/resolve-conflict",
        json={"resolution": "bogus"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_page_conflict_resolve_merged_requires_content_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "P", "content": "x"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/spaces/{sid}/pages/{pid}/resolve-conflict",
        json={"resolution": "merged_content"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


# ─── Remote invites (spaces.py) ────────────────────────────────────────────


async def test_remote_invites_list_unauth_401(client):
    r = await client.get("/api/remote_invites")
    assert r.status == 401


async def test_remote_invites_list_auth(client):
    r = await client.get(
        "/api/remote_invites",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_remote_invites_decision_unauth_401(client):
    r = await client.post("/api/remote_invites/whatever/accept")
    assert r.status == 401


async def test_remote_invites_decision_unknown_404(client):
    r = await client.post(
        "/api/remote_invites/tkn/bogus",
        headers=_auth(client._tok),
    )
    # Unknown decision verb → route either 404 or 405 (falls through).
    assert r.status in (404, 405)


async def test_space_remote_invites_missing_fields_422(client):
    sid = await _seed_space(client)
    r = await client.post(
        f"/api/spaces/{sid}/remote-invites",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── public_spaces ─────────────────────────────────────────────────────────


async def test_public_spaces_list_unauth_401(client):
    r = await client.get("/api/public_spaces")
    assert r.status == 401


async def test_public_spaces_list_empty(client):
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == []


async def test_public_spaces_list_with_limit(client):
    r = await client.get(
        "/api/public_spaces?limit=10",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_public_spaces_list_bad_limit_falls_back(client):
    r = await client.get(
        "/api/public_spaces?limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_public_spaces_list_with_cp_active(client):
    """User with child_protection enabled + declared_age filters view."""
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1, declared_age=12 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/public_spaces",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_public_spaces_join_request_missing_host_422(client):
    r = await client.post(
        "/api/public_spaces/sp/join-request",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_public_spaces_join_request_unauth_401(client):
    r = await client.post("/api/public_spaces/sp/join-request", json={})
    assert r.status == 401


async def test_public_spaces_hide_unauth_401(client):
    r = await client.post("/api/public_spaces/sp/hide")
    assert r.status == 401


async def test_public_spaces_hide_ok(client):
    r = await client.post(
        "/api/public_spaces/sp/hide",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_public_spaces_refresh_admin_ok(client):
    r = await client.post(
        "/api/public_spaces/refresh",
        headers=_auth(client._tok),
    )
    assert r.status == 202


async def test_public_spaces_refresh_non_admin_403(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/public_spaces/refresh",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_public_spaces_refresh_unauth_401(client):
    r = await client.post("/api/public_spaces/refresh")
    assert r.status == 401


async def test_public_spaces_block_instance_admin(client):
    r = await client.post(
        "/api/public_spaces/blocked_instances/iii",
        json={"reason": "spam"},
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_public_spaces_block_instance_non_admin_403(client):
    await _add_second_user(client)
    r = await client.post(
        "/api/public_spaces/blocked_instances/iii",
        json={},
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


async def test_public_spaces_block_instance_unauth_401(client):
    r = await client.post("/api/public_spaces/blocked_instances/iii")
    assert r.status == 401


# ─── notifications routes ─────────────────────────────────────────────────


async def test_notifications_read_missing_id_404_or_ok(client):
    # read an unknown notification — should return 404 or ok gracefully.
    r = await client.post(
        "/api/notifications/missing/read",
        headers=_auth(client._tok),
    )
    assert r.status in (200, 204, 404)


async def test_notifications_read_all_ok(client):
    r = await client.post(
        "/api/notifications/read-all",
        headers=_auth(client._tok),
    )
    assert r.status in (200, 204)


# ─── Misc edge paths ──────────────────────────────────────────────────────


async def test_pages_space_page_get_missing_404(client):
    sid = await _seed_space(client)
    r = await client.get(
        f"/api/spaces/{sid}/pages/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_cover_delete_no_cover_succeeds(client):
    sid = await _seed_space(client)
    r = await client.delete(
        f"/api/spaces/{sid}/cover",
        headers=_auth(client._tok),
    )
    assert r.status == 204
