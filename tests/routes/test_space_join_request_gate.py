"""Who may ask to join a space (``POST /api/spaces/{id}/join-requests``).

Regression: the route took a request from any local user for any space id
— a stranger filed a pending request on a **private** space in ``request``
mode (a private space is joined by invite only, never by a cold request),
and the answers told a stranger the join mode of a space they cannot see
(``INVITE_ONLY`` vs ``201``) and whether an id exists (``404``).

The rule: a request is for a space discoverable to the caller — the same
set whose landing page they may open (``SpaceService.read_space``): a
``public`` / ``global`` space, or a ``household`` space this household
hosts. Anything else — a private space, another household's household
space, an unknown id, an age-gated space below a protected minor's age —
answers the read gate's ``403 "Not a space member."``, and nothing is
filed.
"""

import pytest

from socialhome.app_keys import notification_delivery_key

from .test_space_read_gate import (
    _GUEST,
    _create_space,
    _forbidden,
    _leave,
    seeded,  # noqa: F401 — the shared fixture
)


async def _ask(client, sid: str, headers: dict):
    return await client.post(
        f"/api/spaces/{sid}/join-requests", json={"message": "hi"}, headers=headers
    )


async def _filed(client, sid: str) -> int:
    rows = await client._db.fetchall(
        "SELECT 1 FROM space_join_requests WHERE space_id=?", (sid,)
    )
    return len(rows)


@pytest.mark.parametrize("join_mode", ["request", "open", "invite_only"])
async def test_a_stranger_cannot_ask_to_join_a_private_space(seeded, join_mode):  # noqa: F811
    """Whatever its join mode, a private space answers the same 403 — no
    request filed, no seat, and no hint of the join mode."""
    client, hdr = seeded
    sid = await _create_space(client, hdr, "private", join_mode)
    r = await _ask(client, sid, hdr["guest"])
    await _forbidden(r)
    assert await _filed(client, sid) == 0
    r = await client.get(f"/api/spaces/{sid}/feed", headers=hdr["guest"])
    await _forbidden(r)


async def test_an_unknown_id_answers_the_same_403(seeded):  # noqa: F811
    client, hdr = seeded
    await _forbidden(await _ask(client, "0" * 32, hdr["guest"]))


async def test_another_households_household_space_is_403(seeded):  # noqa: F811
    client, hdr = seeded
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode)"
        " VALUES('sp-their-home', 'Their home', 'inst-elsewhere', 'x', ?,"
        " 'household', 'open')",
        ("ab" * 32,),
    )
    await _forbidden(await _ask(client, "sp-their-home", hdr["guest"]))
    assert await _filed(client, "sp-their-home") == 0


async def test_a_public_space_in_request_mode_files_a_request(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    r = await _ask(client, sid, hdr["guest"])
    assert r.status == 201, await r.text()
    assert (await r.json())["joined"] is False
    assert await _filed(client, sid) == 1


async def test_a_public_invite_only_space_says_so(seeded):  # noqa: F811
    """A public space publishes its join mode in the directory already, so
    saying "invite only" leaks nothing."""
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "invite_only")
    r = await _ask(client, sid, hdr["guest"])
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "INVITE_ONLY"


async def test_a_household_space_hosted_here_seats_who_left(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "household", "open")
    await _leave(client, hdr, sid)
    r = await _ask(client, sid, hdr["guest"])
    assert r.status == 201, await r.text()
    assert (await r.json())["joined"] is True


async def test_a_protected_minor_below_the_age_gate_gets_the_same_403(seeded):  # noqa: F811
    """§CP.F1: the directories hide an age-gated space from a protected
    minor below its gate — asking to join it is the outsider's 403 too."""
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    await client._db.enqueue("UPDATE spaces SET min_age=18 WHERE id=?", (sid,))
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1, declared_age=13 WHERE user_id=?",
        (_GUEST[1],),
    )
    await _forbidden(await _ask(client, sid, hdr["guest"]))
    assert await _filed(client, sid) == 0


async def test_a_member_asking_again_is_told_already_member(seeded):  # noqa: F811
    """A member asking again is told so — they know the space."""
    client, hdr = seeded
    sid = await _create_space(client, hdr, "private", "request")
    r = await _ask(client, sid, hdr["owner"])
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "ALREADY_MEMBER"


# ── Answering a request: "Needs you" (D12) ────────────────────────────


async def _needs_you(client, headers: dict) -> list[dict]:
    # The rows are written by the delivery worker, after the request returns.
    await client.app[notification_delivery_key].drain()
    r = await client.get("/api/notifications/needs-you", headers=headers)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["count"] == len(body["items"])
    return body["items"]


async def test_a_request_is_pinned_for_the_admin_until_answered(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    rid = (await (await _ask(client, sid, hdr["guest"])).json())["request_id"]
    [item] = await _needs_you(client, hdr["owner"])
    assert item["type"] == "space_join_requested"
    assert item["object_key"] == f"join_request:{rid}"
    assert item["space_id"] == sid
    assert await _needs_you(client, hdr["guest"]) == []

    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{rid}/deny", headers=hdr["owner"]
    )
    assert r.status == 200, await r.text()
    assert await _needs_you(client, hdr["owner"]) == []
    # The timeline row says who handled it.
    rows = await (await client.get("/api/notifications", headers=hdr["owner"])).json()
    [row] = [n for n in rows if n["object_key"] == f"join_request:{rid}"]
    assert row["params"]["handled"]["outcome"] == "declined"
    assert row["read_at"] is not None


async def test_a_second_answer_hears_who_got_there_first(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    rid = (await (await _ask(client, sid, hdr["guest"])).json())["request_id"]
    url = f"/api/spaces/{sid}/join-requests/{rid}"
    assert (await client.post(f"{url}/approve", headers=hdr["owner"])).status == 200
    r = await client.post(f"{url}/deny", headers=hdr["owner"])
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "JOIN_REQUEST_HANDLED"
    assert body["error"]["params"]["status"] == "approved"


async def test_nobody_but_an_admin_declines_a_request(seeded):  # noqa: F811
    """A request id used to be enough to decline (and federate the refusal
    of) anyone's request."""
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    rid = (await (await _ask(client, sid, hdr["guest"])).json())["request_id"]
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{rid}/deny", headers=hdr["follower"]
    )
    assert r.status == 403
    # Through another space's URL the request does not exist.
    r = await client.post(
        f"/api/spaces/sp-glob/join-requests/{rid}/deny", headers=hdr["owner"]
    )
    assert r.status == 404
    row = await client._db.fetchone(
        "SELECT status FROM space_join_requests WHERE id=?", (rid,)
    )
    assert row["status"] == "pending"


# ── One tap only when nothing needs a closer look ─────────────────────
#
# The "Needs you" list approves with ``{"via": "inline"}``. The server
# works out the request's flags itself (never from the client) and refuses
# a one-tap approval of a flagged request with ``409 REVIEW_REQUIRED``; the
# review page (no ``via``, or ``"review"``) still decides it.

#: Per flag: the statement and whether it keys on the space (else the request).
_FLAG_SQL = {
    "edge": ("UPDATE space_join_requests SET area_verdict='edge' WHERE id=?", False),
    "admin": (
        "UPDATE space_join_requests SET requested_role='admin' WHERE id=?",
        False,
    ),
    "unpaired": (
        (
            "UPDATE space_join_requests"
            " SET remote_applicant_instance_id='inst-stranger' WHERE id=?"
        ),
        False,
    ),
    "age": ("UPDATE spaces SET min_age=16 WHERE id=?", True),
}


async def _flag(client, flag: str, sid: str, rid: str) -> None:
    sql, on_space = _FLAG_SQL[flag]
    await client._db.enqueue(sql, (sid if on_space else rid,))


async def _plain(client, hdr: dict) -> tuple[str, str]:
    """A pending request of the guest with nothing unusual about it."""
    sid = await _create_space(client, hdr, "public", "request")
    rid = (await (await _ask(client, sid, hdr["guest"])).json())["request_id"]
    return sid, rid


async def _flagged(client, hdr: dict, flag: str) -> tuple[str, str]:
    """A pending request of the guest carrying exactly *flag*."""
    sid, rid = await _plain(client, hdr)
    await _flag(client, flag, sid, rid)
    return sid, rid


async def _approve(client, hdr: dict, sid: str, rid: str, **kw):
    return await client.post(
        f"/api/spaces/{sid}/join-requests/{rid}/approve", headers=hdr["owner"], **kw
    )


async def _status(client, rid: str) -> str:
    row = await client._db.fetchone(
        "SELECT status FROM space_join_requests WHERE id=?", (rid,)
    )
    return row["status"]


@pytest.mark.parametrize("flag", ["edge", "admin", "unpaired", "age"])
async def test_a_one_tap_approval_of_a_flagged_request_is_refused(seeded, flag):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, flag)
    r = await _approve(client, hdr, sid, rid, json={"via": "inline"})
    assert r.status == 409, await r.text()
    error = (await r.json())["error"]
    assert error["code"] == "REVIEW_REQUIRED"
    assert error["params"]["flags"] == flag
    # Nothing changed: the request still waits for the review page.
    assert await _status(client, rid) == "pending"


async def test_every_flag_is_named(seeded):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, "edge")
    for flag in ("admin", "age"):
        await _flag(client, flag, sid, rid)
    r = await _approve(client, hdr, sid, rid, json={"via": "inline"})
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["params"]["flags"] == "admin,age,edge"


async def test_the_flags_come_from_the_server_never_the_client(seeded):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, "edge")
    r = await _approve(client, hdr, sid, rid, json={"via": "inline", "flags": []})
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["params"]["flags"] == "edge"
    # A plain request is one tap, whatever flags the client claims.
    sid, rid = await _plain(client, hdr)
    r = await _approve(client, hdr, sid, rid, json={"via": "inline", "flags": ["edge"]})
    assert r.status == 200, await r.text()
    assert await _status(client, rid) == "approved"


async def test_a_plain_request_is_approved_with_one_tap(seeded):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _plain(client, hdr)
    r = await _approve(client, hdr, sid, rid, json={"via": "inline"})
    assert r.status == 200, await r.text()
    assert (await r.json())["status"] == "approved"
    assert await _needs_you(client, hdr["owner"]) == []


@pytest.mark.parametrize("body", [None, {}, {"via": "review"}])
async def test_the_review_page_approves_a_flagged_request(seeded, body):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, "edge")
    kw = {} if body is None else {"json": body}
    r = await _approve(client, hdr, sid, rid, **kw)
    assert r.status == 200, await r.text()
    assert await _status(client, rid) == "approved"


async def test_a_flagged_request_is_still_declined_with_one_tap(seeded):  # noqa: F811
    """Declining is the safe direction: the list offers it inline for any
    single request, flagged or not."""
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, "edge")
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{rid}/deny",
        json={"via": "inline"},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    assert await _status(client, rid) == "denied"


@pytest.mark.parametrize("via", ["sideways", 1, None])
async def test_an_unknown_via_is_refused(seeded, via):  # noqa: F811
    client, hdr = seeded
    sid, rid = await _plain(client, hdr)
    r = await _approve(client, hdr, sid, rid, json={"via": via})
    assert r.status == 422, await r.text()
    assert await _status(client, rid) == "pending"
    r = await _approve(client, hdr, sid, rid, json=["inline"])
    assert r.status == 422, await r.text()
    assert await _status(client, rid) == "pending"


async def test_an_answered_request_still_hears_who_won_before_the_flags(seeded):  # noqa: F811
    """The #1218 race answer comes first: a flagged request someone already
    approved answers ``JOIN_REQUEST_HANDLED``, not ``REVIEW_REQUIRED``."""
    client, hdr = seeded
    sid, rid = await _flagged(client, hdr, "edge")
    assert (await _approve(client, hdr, sid, rid)).status == 200
    r = await _approve(client, hdr, sid, rid, json={"via": "inline"})
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "JOIN_REQUEST_HANDLED"
