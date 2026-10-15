"""§CP.F2: a protected account's status never crosses a guardian block.

A status ("🏫 at school until 3") says where a child is and what they are
doing — the same kind of thing as a moment, which a guardian block already
keeps off a blocked person's household. Against the real application
(registry, SQLite, routes):

* **Outbound** — the account's ``USER_STATUS_UPDATED`` never reaches a
  household where someone its guardian blocked is homed (the block never
  leaves this household, so that receiver can't filter it); every other
  household gets the real status.
* **On block** — a status set before the block is cleared on the blocked
  person's household at once, with one ordinary ``status_cleared``.
* **Local** — ``GET /api/presence`` (status, whereabouts) leaves out
  everyone a guardian block separates from the viewer, either way, and
  ``GET /api/users`` reads their status as unset; others are unaffected.
* **Inbound** — a blocked remote person's status never reaches the
  protected account on ``GET /api/friends``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    event_bus_key,
    federation_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType
REX_HOME = "peer-rex"  # Rex — blocked for kid — lives here.
ANN_HOME = "peer-ann"  # nobody blocked lives here.
REX = "u-rex"
LOCALS = ("admin", "kid", "bob", "carol")
CLEARED = {"user_id": "u-kid", "status_cleared": True}


def _h(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "gbs.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    """admin (guardian) · kid (protected) · bob, carol (adults) · rex on a
    paired household, ann's household paired too. kid has a status set
    *before* bob and rex are blocked for kid."""
    app = create_app(_config(tmp_dir))
    tc = await aiohttp_client(app)
    db = app[db_key]
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
            "INSERT INTO presence(username, entity_id, state) VALUES(?,?,?)",
            (name, f"person.{name}", "home"),
        )
    for peer in (REX_HOME, ANN_HOME):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                peer,
                peer,
                "00" * 32,
                "k1",
                "k2",
                f"https://{peer}/wh",
                f"wh-{peer}",
                "confirmed",
                "manual",
            ),
        )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (REX, REX_HOME, "rex", "Rex"),
    )

    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record(self, *, to_instance_id, event_type, payload, space_id=None):
        sent.append((to_instance_id, event_type, payload))

    # The wire itself is the protocol tests' business elsewhere; here only
    # what this household hands it for each peer matters.
    monkeypatch.setattr(type(app[federation_service_key]), "send_event", _record)

    resp = await tc.patch(
        "/api/me",
        json={"status_emoji": "🏫", "status_text": "At school"},
        headers=_h("kid"),
    )
    assert resp.status == 200, await resp.text()

    cp = app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=ids["admin"]
    )
    await cp.add_guardian(
        minor_user_id=ids["kid"],
        guardian_user_id=ids["admin"],
        actor_user_id=ids["admin"],
    )
    tc._sent = sent
    tc._cp = cp
    tc._ids = ids
    tc._app = app
    return tc


async def _block(env, blocked: str) -> None:
    await env._cp.block_user_for_minor(
        minor_user_id=env._ids["kid"],
        blocked_user_id=blocked,
        guardian_user_id=env._ids["admin"],
    )


def _status_sends(env) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for peer, event_type, payload in env._sent:
        if event_type == FET.USER_STATUS_UPDATED and payload.get("user_id") == "u-kid":
            out.setdefault(peer, []).append(payload)
    return out


async def test_status_set_before_protection_reached_both_households(env):
    """Baseline: with no block, both households get the real status."""
    sends = _status_sends(env)
    for peer in (REX_HOME, ANN_HOME):
        assert sends[peer][-1]["text"] == "At school"


async def test_block_clears_the_status_on_the_blocked_household_only(env):
    env._sent.clear()
    await _block(env, REX)
    assert _status_sends(env) == {REX_HOME: [CLEARED]}


async def test_later_status_sends_the_blocked_household_nothing(env):
    """Not even a clear: a real sender never repeats one, so a stream of
    clears would time-stamp each change the account makes."""
    await _block(env, REX)
    env._sent.clear()
    resp = await env.patch(
        "/api/me",
        json={"status_emoji": "🛝", "status_text": "At the park"},
        headers=_h("kid"),
    )
    assert resp.status == 200, await resp.text()
    sends = _status_sends(env)
    assert REX_HOME not in sends
    assert sends[ANN_HOME][-1]["text"] == "At the park"


async def test_status_flows_again_once_protection_is_off(env):
    await _block(env, REX)
    await env._cp.disable_protection(
        minor_username="kid", actor_user_id=env._ids["admin"]
    )
    env._sent.clear()
    await env.patch(
        "/api/me",
        json={"status_emoji": "🛝", "status_text": "At the park"},
        headers=_h("kid"),
    )
    assert _status_sends(env)[REX_HOME][-1]["text"] == "At the park"


async def test_turning_protection_back_on_clears_it_again(env):
    """A block kept from an earlier protection counts again at once."""
    await _block(env, REX)
    await env._cp.disable_protection(
        minor_username="kid", actor_user_id=env._ids["admin"]
    )
    env._sent.clear()
    await env._cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=env._ids["admin"]
    )
    assert _status_sends(env) == {REX_HOME: [CLEARED]}


async def _presence_ids(env, viewer: str) -> set[str]:
    resp = await env.get("/api/presence", headers=_h(viewer))
    assert resp.status == 200, await resp.text()
    return {row["user_id"] for row in await resp.json()}


@pytest.mark.parametrize(
    ("viewer", "missing"),
    [("kid", "u-bob"), ("bob", "u-kid")],
)
async def test_presence_leaves_out_the_other_side_of_a_block(env, viewer, missing):
    await _block(env, env._ids["bob"])
    assert missing not in await _presence_ids(env, viewer)


async def test_presence_is_unchanged_for_everyone_else(env):
    await _block(env, env._ids["bob"])
    assert {"u-kid", "u-bob"} <= await _presence_ids(env, "carol")


async def test_user_directory_reads_the_status_as_unset_across_a_block(env):
    resp = await env.patch(
        "/api/me",
        json={"status_emoji": "🍕", "status_text": "Lunch"},
        headers=_h("bob"),
    )
    assert resp.status == 200, await resp.text()
    await _block(env, env._ids["bob"])
    for viewer, seen in (("kid", None), ("carol", "Lunch")):
        resp = await env.get("/api/users", headers=_h(viewer))
        rows = {r["user_id"]: r for r in await resp.json()}
        assert rows["u-bob"]["status"]["text"] == seen
    for viewer, seen in (("bob", None), ("carol", "At school")):
        resp = await env.get("/api/users", headers=_h(viewer))
        rows = {r["user_id"]: r for r in await resp.json()}
        # The directory itself keeps everyone (mentions, household chat).
        assert "u-kid" in rows
        assert rows["u-kid"]["status"]["text"] == seen


async def test_blocked_remote_persons_status_never_reaches_the_account(env):
    await _block(env, REX)
    handlers = env._app[federation_service_key]._event_registry.handlers_for(
        FET.USER_STATUS_UPDATED
    )
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="env-rex-status",
                event_type=FET.USER_STATUS_UPDATED,
                from_instance=REX_HOME,
                to_instance="self",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={"user_id": REX, "emoji": "🎮", "text": "Gaming"},
            )
        )
    for viewer, visible in (("kid", False), ("carol", True)):
        resp = await env.get("/api/friends", headers=_h(viewer))
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        members = [m for h in body["households"] for m in h["members"]]
        assert any(m["user_id"] == REX for m in members) is visible
        assert ("Gaming" in str(body)) is visible


@pytest.mark.parametrize(
    ("peer", "shipped"), [(REX_HOME, None), (ANN_HOME, "At school")]
)
async def test_users_sync_carries_no_status_to_the_blocked_household(
    env, peer, shipped
):
    """The roster snapshot sent at pair-confirm follows the same rule."""
    await _block(env, REX)
    env._sent.clear()
    await env._app[event_bus_key].publish(PairingConfirmed(instance_id=peer))
    syncs = [p for i, t, p in env._sent if t == FET.USERS_SYNC and i == peer]
    assert syncs
    kid = next(u for u in syncs[-1]["users"] if u["user_id"] == "u-kid")
    assert (kid["status"] or {}).get("text") == shipped
