"""§CP.F2: across a guardian block, local surfaces show a name only.

Owner decision (2026-10-15): toward someone a guardian blocked, a protected
account shows by name only — picture and bio withheld. #951 did this for
other households (the block never leaves this one, so the sender gates).
This file covers the blocked person living in the *same* household, and —
like every guardian-block rule since #948 — the other way round: the
protected account sees the blocked person by name only too. Against the
real application (registry, SQLite, routes):

* ``GET /api/users`` — the counterpart's row keeps its name, but
  ``picture_hash`` / ``picture_url`` / ``bio`` read ``None``.
* ``GET /api/users/{id}/picture`` — 404 for the counterpart, as for an
  account with no picture.
* A space both sit in (they can share one after the block) — the member
  list is name-only, the per-space picture 404s and the space map drops
  their pin.
* ``GET /api/me/corner`` — the dashboard's whereabouts slice leaves the
  counterpart out, like ``GET /api/presence``.
* The household chat (it seats every local user) — the roster is
  name-only, without online state.
* ``GET /api/moments/follows`` — a follow made before the block names the
  account without its picture.
* ``GET /api/calendars/invitees`` — a blocked remote person reads by name
  only to the protected account.

Everyone else sees everything.
"""

from __future__ import annotations

import io
from types import MappingProxyType

import pytest
from aiohttp import FormData
from PIL import Image

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    federation_service_key,
    space_service_key,
    user_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config

pytestmark = pytest.mark.security

REX_HOME = "peer-rex"
REX = "u-rex"
LOCALS = ("admin", "kid", "bob", "carol")


def _h(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "gblp.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


def _png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return buf.getvalue()


async def _upload_picture(tc, name: str, color: str) -> None:
    form = FormData()
    form.add_field("file", _png(color), filename="p.png", content_type="image/png")
    resp = await tc.post("/api/me/picture", data=form, headers=_h(name))
    assert resp.status == 200, await resp.text()


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    """admin (guardian) · kid (protected) · bob (blocked for kid) · carol.

    Before the block: kid and bob each set a picture and a bio, and bob
    follows kid. A space
    carol owns seats kid and bob together after the block."""
    app = create_app(_config(tmp_dir))
    tc = await aiohttp_client(app)
    db = app[db_key]

    async def _no_wire(self, **_kw):
        return None

    # Only what this household serves its own members matters here.
    monkeypatch.setattr(type(app[federation_service_key]), "send_event", _no_wire)
    ids = {name: f"u-{name}" for name in LOCALS}
    for name, uid in ids.items():
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,?)",
            (name, uid, name.title(), 1 if name == "admin" else 0),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", uid, "web", sha256_token_hash(f"{name}-tok")),
        )
        await db.enqueue(
            "INSERT INTO platform_users(username, display_name) VALUES(?, ?)",
            (name, name.title()),
        )
        await db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (f"t-{name}", name, sha256_token_hash(f"{name}-tok")),
        )
        await db.enqueue(
            "INSERT INTO presence(username, entity_id, state, latitude, longitude)"
            " VALUES(?,?,?,?,?)",
            (name, f"person.{name}", "home", 52.5, 13.4),
        )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            REX_HOME,
            REX_HOME,
            "00" * 32,
            "k1",
            "k2",
            f"https://{REX_HOME}/wh",
            f"wh-{REX_HOME}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name, picture_hash, bio) VALUES(?,?,?,?,?,?)",
        (REX, REX_HOME, "rex", "Rex", "0123456789abcdef", "Rex's bio"),
    )
    for name, color, bio in (("kid", "red", "I like trains"), ("bob", "blue", "Bob")):
        await _upload_picture(tc, name, color)
        resp = await tc.patch("/api/me", json={"bio": bio}, headers=_h(name))
        assert resp.status == 200, await resp.text()

    await app[user_service_key].follow("bob", ids["kid"])

    cp = app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=ids["admin"]
    )
    await cp.add_guardian(
        minor_user_id=ids["kid"],
        guardian_user_id=ids["admin"],
        actor_user_id=ids["admin"],
    )
    for blocked in (ids["bob"], REX):
        await cp.block_user_for_minor(
            minor_user_id=ids["kid"],
            blocked_user_id=blocked,
            guardian_user_id=ids["admin"],
        )

    spaces = app[space_service_key]
    space = await spaces.create_space(owner_username="carol", name="Club")
    for name in ("kid", "bob"):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role,"
            " location_share_enabled) VALUES(?,?,?,1)",
            (space.id, ids[name], "member"),
        )
        await spaces.set_member_picture(
            space.id, ids[name], actor_user_id=ids[name], raw_bytes=_png("green")
        )
    await db.enqueue("UPDATE spaces SET feature_location=1 WHERE id=?", (space.id,))
    tc._ids = ids
    tc._space_id = space.id
    return tc


async def _rows(env, path: str, viewer: str) -> list[dict]:
    resp = await env.get(path, headers=_h(viewer))
    assert resp.status == 200, await resp.text()
    return await resp.json()


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["user_id"]: r for r in rows}


# (viewer, the counterpart they see) — both ways round.
PAIRS = [("bob", "u-kid"), ("kid", "u-bob")]


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_directory_shows_the_counterpart_by_name_only(env, viewer, other):
    row = _by_id(await _rows(env, "/api/users", viewer))[other]
    assert row["display_name"] == other[2:].title()
    assert row["picture_hash"] is None
    assert row["picture_url"] is None
    assert row["bio"] is None


async def test_directory_is_unchanged_for_everyone_else(env):
    rows = _by_id(await _rows(env, "/api/users", "carol"))
    assert rows["u-kid"]["bio"] == "I like trains"
    assert rows["u-kid"]["picture_url"]
    assert rows["u-bob"]["picture_hash"]


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_picture_is_404_for_the_counterpart(env, viewer, other):
    resp = await env.get(f"/api/users/{other}/picture", headers=_h(viewer))
    assert resp.status == 404
    resp = await env.get(f"/api/users/{other}/picture", headers=_h("carol"))
    assert resp.status == 200
    assert resp.content_type == "image/webp"


async def test_a_signed_url_minted_for_someone_else_still_loads(env):
    """A signed URL carries no caller — the picture view can't tell who
    holds it; the directory just never hands a counterpart one."""
    row = _by_id(await _rows(env, "/api/users", "carol"))["u-kid"]
    resp = await env.get("/" + row["picture_url"].lstrip("/"))
    assert resp.status == 200


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_shared_space_shows_the_counterpart_by_name_only(env, viewer, other):
    sid = env._space_id
    rows = _by_id(await _rows(env, f"/api/spaces/{sid}/members", viewer))
    assert rows[other]["picture_hash"] is None
    assert rows[other]["picture_url"] is None
    carol_rows = _by_id(await _rows(env, f"/api/spaces/{sid}/members", "carol"))
    assert carol_rows[other]["picture_url"]

    path = f"/api/spaces/{sid}/members/{other}/picture"
    assert (await env.get(path, headers=_h(viewer))).status == 404
    assert (await env.get(path, headers=_h("carol"))).status == 200


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_space_map_drops_the_counterparts_pin(env, viewer, other):
    path = f"/api/spaces/{env._space_id}/presence"
    resp = await env.get(path, headers=_h(viewer))
    assert resp.status == 200, await resp.text()
    assert other not in {e["user_id"] for e in (await resp.json())["entries"]}
    resp = await env.get(path, headers=_h("carol"))
    assert other in {e["user_id"] for e in (await resp.json())["entries"]}


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_dashboard_whereabouts_leave_the_counterpart_out(env, viewer, other):
    resp = await env.get("/api/me/corner", headers=_h(viewer))
    assert resp.status == 200, await resp.text()
    assert other not in {p["user_id"] for p in (await resp.json())["presence"]}
    resp = await env.get("/api/me/corner", headers=_h("carol"))
    assert other in {p["user_id"] for p in (await resp.json())["presence"]}


@pytest.mark.parametrize(("viewer", "other"), PAIRS)
async def test_household_chat_roster_shows_the_counterpart_by_name_only(
    env, viewer, other
):
    """The household chat seats every local user — the block can't take
    the counterpart out of it, so the roster names them, nothing more."""
    resp = await env.get("/api/household/chat", headers=_h(viewer))
    assert resp.status == 200, await resp.text()
    conv_id = (await resp.json())["conversation_id"]
    assert conv_id
    await env.get("/api/household/chat", headers=_h("carol"))
    path = f"/api/conversations/{conv_id}/members"
    roster = _by_id(await _rows(env, path, viewer))
    assert roster[other]["display_name"] == other[2:].title()
    assert roster[other]["picture_url"] is None
    assert roster[other]["is_online"] is False
    assert roster[other]["last_seen_at"] is None
    assert _by_id(await _rows(env, path, "carol"))[other]["picture_url"]


async def test_follows_list_names_the_account_without_its_picture(env):
    resp = await env.get("/api/moments/follows", headers=_h("bob"))
    assert resp.status == 200, await resp.text()
    follows = _by_id((await resp.json())["follows"])
    assert follows["u-kid"]["display_name"] == "Kid"
    assert follows["u-kid"]["picture_url"] is None


async def test_calendar_invitees_show_a_blocked_remote_person_by_name_only(env):
    def rex(body: dict) -> dict:
        members = [m for g in body["instances"] for m in g["members"]]
        return next(m for m in members if m["user_id"] == REX)

    kid_view = rex(await _rows(env, "/api/calendars/invitees", "kid"))
    assert kid_view["display_name"] == "Rex"
    assert kid_view["picture_url"] is None
    assert rex(await _rows(env, "/api/calendars/invitees", "carol"))["picture_url"]


async def test_lifting_the_block_restores_picture_and_bio(env):
    cp = env.app[child_protection_service_key]
    await cp.unblock_user_for_minor(
        minor_user_id=env._ids["kid"],
        blocked_user_id=env._ids["bob"],
        guardian_user_id=env._ids["admin"],
    )
    row = _by_id(await _rows(env, "/api/users", "bob"))["u-kid"]
    assert row["bio"] == "I like trains"
    assert row["picture_url"]
    resp = await env.get("/api/users/u-kid/picture", headers=_h("bob"))
    assert resp.status == 200
