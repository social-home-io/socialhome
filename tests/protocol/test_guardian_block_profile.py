"""§CP.F2: a guardian block keeps a child's picture and bio from the blocked
person's household — the name still goes.

The block never leaves the child's household, so the sender is the only
place it can hold. Against the real application (registry, SQLite,
routes), with only the transport recorded:

* **Outbound** — every ``USER_UPDATED`` / ``USERS_SYNC`` entry for the
  protected account to a household where someone its guardian blocked is
  homed carries the name only: ``bio`` and ``picture_hash`` ``null``, no
  picture bytes, and no trace of the old picture hash. Every other
  household gets the full profile.
* **On block** — that household gets one name-only update at once, so a
  picture or bio it holds from before the block is cleared.
* **On lift** (block removed, protection off) — the full profile goes
  back, picture bytes included, together with the current status and
  online state the block kept away.
* The admin re-publish of a hidden-then-visible member follows the rule.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from socialhome.app_keys import (
    db_key,
    event_bus_key,
    online_status_service_key,
    user_service_key,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEventType

from .test_guardian_block_status import (  # noqa: F401 — the shared fixture
    ANN_HOME,
    REX,
    REX_HOME,
    _block,
    _h,
    env,
)

pytestmark = pytest.mark.security

FET = FederationEventType
KID = "u-kid"
BIO = "Loves dinosaurs, goes to Maple Street School"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color="green").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def kid(env):  # noqa: F811 — pytest fixture injection
    """The protected account has a bio and a picture before any block."""
    resp = await env.patch("/api/me", json={"bio": BIO}, headers=_h("kid"))
    assert resp.status == 200, await resp.text()
    user = await env._app[user_service_key].set_picture(KID, _png())
    assert user.picture_hash
    env._picture_hash = user.picture_hash
    return env


def _profile_sends(tc, peer: str) -> list[dict]:
    return [
        p
        for i, t, p in tc._sent
        if t == FET.USER_UPDATED and i == peer and p.get("user_id") == KID
    ]


def _assert_name_only(tc, payload: dict) -> None:
    assert payload["display_name"] == "Kid"
    assert payload["bio"] is None
    assert payload["picture_hash"] is None
    assert "picture_webp_base64" not in payload
    wire = json.dumps(payload, ensure_ascii=False)
    assert tc._picture_hash not in wire
    assert "dinosaurs" not in wire


def _assert_full(tc, payload: dict, *, with_bytes: bool) -> None:
    assert payload["bio"] == BIO
    assert payload["picture_hash"] == tc._picture_hash
    assert ("picture_webp_base64" in payload) is with_bytes


async def _unblock(tc, blocked: str) -> None:
    await tc._cp.unblock_user_for_minor(
        minor_user_id=tc._ids["kid"],
        blocked_user_id=blocked,
        guardian_user_id=tc._ids["admin"],
    )


async def test_both_households_get_the_full_profile_without_a_block(kid):
    for peer in (REX_HOME, ANN_HOME):
        _assert_full(kid, _profile_sends(kid, peer)[-1], with_bytes=True)


async def test_block_sends_one_name_only_update_to_the_blocked_household(kid):
    kid._sent.clear()
    await _block(kid, REX)
    sends = _profile_sends(kid, REX_HOME)
    assert len(sends) == 1
    _assert_name_only(kid, sends[0])
    assert _profile_sends(kid, ANN_HOME) == []


async def test_later_edits_reach_the_blocked_household_as_name_only(kid):
    await _block(kid, REX)
    kid._sent.clear()
    resp = await kid.patch(
        "/api/me",
        json={"display_name": "Kiddo", "bio": BIO + "!"},
        headers=_h("kid"),
    )
    assert resp.status == 200, await resp.text()
    await kid._app[user_service_key].set_picture(KID, _png())
    for payload in _profile_sends(kid, REX_HOME):
        assert payload["bio"] is None and payload["picture_hash"] is None
        assert "picture_webp_base64" not in payload
    assert _profile_sends(kid, REX_HOME)[0]["display_name"] == "Kiddo"
    ann = _profile_sends(kid, ANN_HOME)
    assert ann[0]["bio"] == BIO + "!"
    assert "picture_webp_base64" in ann[-1]


@pytest.mark.parametrize("peer", [REX_HOME, ANN_HOME])
async def test_pairing_roster_carries_the_name_only_to_the_blocked_household(kid, peer):
    await _block(kid, REX)
    kid._sent.clear()
    await kid._app[event_bus_key].publish(PairingConfirmed(instance_id=peer))
    syncs = [p for i, t, p in kid._sent if t == FET.USERS_SYNC and i == peer]
    entry = next(u for u in syncs[-1]["users"] if u["user_id"] == KID)
    if peer == REX_HOME:
        _assert_name_only(kid, entry)
        assert entry["status"] is None
    else:
        _assert_full(kid, entry, with_bytes=True)


async def test_unblock_sends_the_full_profile_and_status_back(kid):
    online = kid._app[online_status_service_key]
    await online.user_session_opened(KID, 101)
    await _block(kid, REX)
    kid._sent.clear()
    await _unblock(kid, REX)
    sends = _profile_sends(kid, REX_HOME)
    assert len(sends) == 1
    _assert_full(kid, sends[0], with_bytes=True)
    statuses = [
        p for i, t, p in kid._sent if t == FET.USER_STATUS_UPDATED and i == REX_HOME
    ]
    assert statuses and statuses[-1]["text"] == "At school"
    assert (REX_HOME, FET.USER_ONLINE, {"user_id": KID}) in kid._sent
    # Nothing was kept from the other household, so nothing goes there.
    assert not [p for i, _t, p in kid._sent if i == ANN_HOME]


async def test_protection_off_sends_the_full_profile_back(kid):
    await _block(kid, REX)
    kid._sent.clear()
    await kid._cp.disable_protection(
        minor_username="kid", actor_user_id=kid._ids["admin"]
    )
    sends = _profile_sends(kid, REX_HOME)
    assert len(sends) == 1
    _assert_full(kid, sends[0], with_bytes=True)
    statuses = [
        p for i, t, p in kid._sent if t == FET.USER_STATUS_UPDATED and i == REX_HOME
    ]
    assert statuses[-1]["text"] == "At school"


async def test_protection_back_on_sends_the_name_only_again(kid):
    await _block(kid, REX)
    await kid._cp.disable_protection(
        minor_username="kid", actor_user_id=kid._ids["admin"]
    )
    kid._sent.clear()
    await kid._cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=kid._ids["admin"]
    )
    sends = _profile_sends(kid, REX_HOME)
    assert len(sends) == 1
    _assert_name_only(kid, sends[0])


async def test_unblock_keeps_the_household_covered_while_another_block_stands(kid):
    """A second blocked person homed on the same household keeps it covered."""
    await kid._app[db_key].enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        ("u-rex2", REX_HOME, "rex2", "Rex Two"),
    )
    await _block(kid, REX)
    await _block(kid, "u-rex2")
    kid._sent.clear()
    await _unblock(kid, REX)
    assert not [p for i, _t, p in kid._sent if i == REX_HOME]


async def test_visible_again_republish_is_name_only_to_the_blocked_household(kid):
    await _block(kid, REX)
    for visible in (False, True):
        resp = await kid.patch(
            f"/api/pairing/connections/{REX_HOME}/visible-users",
            json={"updates": [{"user_id": KID, "visible": visible}]},
            headers=_h("admin"),
        )
        assert resp.status == 200, await resp.text()
    _assert_name_only(kid, _profile_sends(kid, REX_HOME)[-1])


async def test_visible_again_republish_carries_the_picture_bytes_otherwise(kid):
    for visible in (False, True):
        resp = await kid.patch(
            f"/api/pairing/connections/{ANN_HOME}/visible-users",
            json={"updates": [{"user_id": KID, "visible": visible}]},
            headers=_h("admin"),
        )
        assert resp.status == 200, await resp.text()
    _assert_full(kid, _profile_sends(kid, ANN_HOME)[-1], with_bytes=True)


async def test_the_picture_is_not_served_to_anyone_without_a_local_session(kid):
    """A peer household has no session here, so it can't fetch the bytes by
    URL either — the picture only ever travels inside a gated payload."""
    await _block(kid, REX)
    resp = await kid.get(f"/api/users/{KID}/picture?v={kid._picture_hash}")
    assert resp.status == 401
