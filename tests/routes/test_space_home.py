"""The space home's reads (redesign PR 9).

* ``GET /api/spaces/{id}`` carries ``attention`` — what waits for the
  caller: items held for review and reports (owner / admin / moderator),
  join requests and open proposals with ``voted_by_me`` (owner / admin).
  ``null`` for a plain member and a follower; never on the seatless
  landing page.
* ``GET /api/spaces/{id}/summary`` — counts per section for More's rows,
  for any seat (a follower's included); an outsider and an unknown id get
  the same 403 (#1002 seat gate); a section that is off is ``null``.
"""

import pytest

from .conftest import _auth
from .test_space_read_gate import _add_user, _forbidden, _seat

_OWNER = ("olive", "uid-olive")
_ADMIN = ("adam", "uid-adam")
_MODERATOR = ("mo", "uid-mo")
_MEMBER = ("mia", "uid-mia")
_FOLLOWER = ("fern", "uid-fern")
_GUEST = ("guest", "uid-guest")


@pytest.fixture
async def seeded(client):
    """A private space olive hosts here (created through the API), with an
    admin, a moderator, a member and a follower; one post held for review,
    one join request and one open proposal (olive proposed it)."""
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "admin": await _add_user(client, *_ADMIN),
        "moderator": await _add_user(client, *_MODERATOR),
        "member": await _add_user(client, *_MEMBER),
        "follower": await _add_user(client, *_FOLLOWER),
        "guest": await _add_user(client, *_GUEST),
    }
    r = await client.post(
        "/api/spaces",
        json={"name": "Neighbourhood Garden", "space_type": "public"},
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await _seat(client, sid, _ADMIN[1], "admin")
    await _seat(client, sid, _MODERATOR[1], "moderator")
    await _seat(client, sid, _MEMBER[1], "member")
    await _seat(client, sid, _FOLLOWER[1], "subscriber")
    await client._db.enqueue(
        "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
        " submitted_by, payload_json, expires_at)"
        " VALUES('mq-1', ?, 'posts', 'create', ?, '{}', '2999-01-01')",
        (sid, _MEMBER[1]),
    )
    await client._db.enqueue(
        "INSERT INTO space_join_requests(id, space_id, user_id, expires_at)"
        " VALUES('jr-1', ?, ?, '2999-01-01')",
        (sid, _GUEST[1]),
    )
    # Two admins (olive + adam): a tier change needs both, so it stays open.
    r = await client.post(
        f"/api/spaces/{sid}/proposals",
        json={"action": "set_public_tier", "space_type": "private"},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    return client, hdr, sid


async def _attention(client, headers, sid):
    r = await client.get(f"/api/spaces/{sid}", headers=headers)
    assert r.status == 200, await r.text()
    body = await r.json()
    assert "attention" in body
    return body["attention"]


async def test_the_owner_sees_everything_waiting(seeded):
    client, hdr, sid = seeded
    a = await _attention(client, hdr["owner"], sid)
    assert a["pending_review"] == 1
    assert a["reports"] == 0
    assert a["join_requests"] == 1
    (p,) = a["proposals"]
    assert p["action"] == "set_public_tier"
    assert (p["approvals"], p["required"]) == (1, 2)
    # olive proposed it: she is not asked to vote again (bug 9).
    assert p["voted_by_me"] is True


async def test_another_admin_is_asked_to_vote(seeded):
    client, hdr, sid = seeded
    a = await _attention(client, hdr["admin"], sid)
    assert a["join_requests"] == 1
    (p,) = a["proposals"]
    assert p["voted_by_me"] is False


async def test_a_moderator_sees_review_but_no_join_requests_or_proposals(seeded):
    client, hdr, sid = seeded
    a = await _attention(client, hdr["moderator"], sid)
    assert a == {
        "pending_review": 1,
        "reports": 0,
        "join_requests": 0,
        "proposals": [],
    }


@pytest.mark.parametrize("who", ["member", "follower"])
async def test_nothing_waits_for_a_member_or_follower(seeded, who):
    client, hdr, sid = seeded
    assert await _attention(client, hdr[who], sid) is None


async def test_the_seatless_landing_page_has_no_attention(seeded):
    """The public space's landing page (no seat) shows directory fields
    only — no ``attention`` key at all."""
    client, hdr, sid = seeded
    r = await client.get(f"/api/spaces/{sid}", headers=hdr["guest"])
    assert r.status == 200, await r.text()
    assert "attention" not in await r.json()


# ── /summary ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["owner", "member", "follower"])
async def test_every_seat_reads_the_summary(seeded, who):
    client, hdr, sid = seeded
    r = await client.get(f"/api/spaces/{sid}/summary", headers=hdr[who])
    assert r.status == 200, await r.text()
    body = await r.json()
    assert set(body) == {"tasks", "pages", "gallery", "stickies", "bazaar", "calendar"}
    assert body["tasks"] == {"open": 0, "mine": 0}
    assert body["calendar"]["upcoming"] == 0


async def test_the_summary_counts_and_skips_sections_that_are_off(seeded):
    client, hdr, sid = seeded
    await client._db.enqueue(
        "INSERT INTO stickies(id, space_id, author, content) VALUES('st-1', ?, ?, 'x')",
        (sid, _OWNER[1]),
    )
    r = await client.patch(
        f"/api/spaces/{sid}", json={"features": {"bazaar": False}}, headers=hdr["owner"]
    )
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/spaces/{sid}/summary", headers=hdr["member"])
    body = await r.json()
    assert body["stickies"] == {"count": 1}
    assert body["bazaar"] is None


async def test_no_seat_and_an_unknown_id_get_the_same_403(seeded):
    client, hdr, sid = seeded
    await _forbidden(
        await client.get(f"/api/spaces/{sid}/summary", headers=hdr["guest"])
    )
    await _forbidden(
        await client.get("/api/spaces/sp-nope/summary", headers=hdr["guest"])
    )


async def test_a_household_admin_without_a_seat_is_refused(seeded):
    """The operator passes the seat gate, but the summary is the seat's
    own read — no admin bypass (owner decision 12)."""
    client, _, sid = seeded
    r = await client.get(f"/api/spaces/{sid}/summary", headers=_auth(client._tok))
    await _forbidden(r)
