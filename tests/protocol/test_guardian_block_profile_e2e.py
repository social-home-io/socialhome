"""Release-blocker protocol test: a guardian block takes a child's picture
and bio off the blocked person's household, and a lifted block brings them
back.

Two real households, each a full app over its own SQLite file. On **A** a
protected child has a picture and a bio; **B** is home to Rex. B first
holds the child's full profile (picture bytes included). A guardian on A
blocks Rex: A's real outbound sends B the name only, and B's real inbound
handler drops the bio, the picture hash *and* the cached picture bytes —
Rex can't open the picture by its URL on B any more. Lifting the block
sends the full profile back and B serves the picture again. Only the
transport is stubbed: each envelope's payload is handed from A's
``send_event`` to B's registered handlers.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import pytest
from PIL import Image

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    federation_service_key,
    profile_picture_repo_key,
    user_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import FederationEvent, FederationEventType

from .test_users_sync_status_e2e import _add_member, _config, _identity, _pin

pytestmark = pytest.mark.security

BIO = "Loves dinosaurs"
REX_AUTH = {"Authorization": "Bearer rex-tok"}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color="purple").save(buf, format="PNG")
    return buf.getvalue()


async def _token(app, username: str, user_id: str) -> None:
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", user_id, "web", sha256_token_hash(f"{username}-tok")),
    )
    await db.enqueue(
        "INSERT INTO platform_users(username, display_name) VALUES(?, ?)",
        (username, username.title()),
    )
    await db.enqueue(
        "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
        (f"t-{username}", username, sha256_token_hash(f"{username}-tok")),
    )


@pytest.fixture
async def world(aiohttp_client, tmp_path, monkeypatch):
    """A (child + guardian) and B (Rex) paired; B holds the child's full
    profile before any block."""
    a = create_app(_config(tmp_path, "a"))
    b = create_app(_config(tmp_path, "b"))
    await aiohttp_client(a)
    client_b = await aiohttp_client(b)
    a_id, a_pk = await _identity(a)
    b_id, b_pk = await _identity(b)
    await _pin(a, b_id, b_pk)
    await _pin(b, a_id, a_pk)

    admin = await _add_member(a, a_pk, "admin")
    await a[db_key].enqueue("UPDATE users SET is_admin=1 WHERE user_id=?", (admin,))
    kid = await _add_member(a, a_pk, "kid")
    await a[db_key].enqueue("UPDATE users SET bio=? WHERE user_id=?", (BIO, kid))
    rex = await _add_member(b, b_pk, "rex")
    await _token(b, "rex", rex)
    # A knows Rex as homed on B (the pairing roster put him there).
    await a[db_key].enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (rex, b_id, "rex", "Rex"),
    )

    b_registry = b[federation_service_key]._event_registry

    async def _deliver(self, *, to_instance_id, event_type, payload, **_kw):
        if to_instance_id != b_id:
            return
        for handler in b_registry.handlers_for(event_type):
            await handler(
                FederationEvent(
                    msg_id=uuid.uuid4().hex,
                    event_type=event_type,
                    from_instance=a_id,
                    to_instance=b_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    payload=payload,
                )
            )

    # Transport stub (the service is slotted, so patch the class).
    monkeypatch.setattr(type(a[federation_service_key]), "send_event", _deliver)

    # The picture upload fans USER_UPDATED (bio + hash + bytes) to B.
    await a[user_service_key].set_picture(kid, _png())

    cp = a[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=admin
    )
    await cp.add_guardian(
        minor_user_id=kid, guardian_user_id=admin, actor_user_id=admin
    )
    return a, b, client_b, cp, admin, kid, rex, a_id, b_id


async def _b_holds(b, kid: str) -> tuple[str | None, str | None]:
    row = await b[db_key].fetchone(
        "SELECT bio, picture_hash FROM remote_users WHERE user_id=?", (kid,)
    )
    return row["bio"], row["picture_hash"]


async def _picture_status(client_b, kid: str) -> int:
    resp = await client_b.get(f"/api/users/{kid}/picture", headers=REX_AUTH)
    return resp.status


async def test_b_holds_the_full_profile_before_the_block(world):
    _a, b, client_b, _cp, _admin, kid, _rex, *_ = world
    bio, picture_hash = await _b_holds(b, kid)
    assert bio == BIO and picture_hash
    assert await _picture_status(client_b, kid) == 200


async def test_block_takes_picture_and_bio_off_the_blocked_household(world):
    _a, b, client_b, cp, admin, kid, rex, *_ = world
    await cp.block_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    assert await _b_holds(b, kid) == (None, None)
    # The cached bytes went too — not reachable by URL on B any more.
    assert await _picture_status(client_b, kid) == 404
    row = await b[db_key].fetchone(
        "SELECT display_name FROM remote_users WHERE user_id=?", (kid,)
    )
    assert row["display_name"] == "Kid"


async def test_lifting_the_block_brings_picture_and_bio_back(world):
    _a, b, client_b, cp, admin, kid, rex, *_ = world
    await cp.block_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    await cp.unblock_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    bio, picture_hash = await _b_holds(b, kid)
    assert bio == BIO and picture_hash
    assert await _picture_status(client_b, kid) == 200


async def test_protection_off_brings_picture_and_bio_back(world):
    _a, b, client_b, cp, admin, kid, rex, *_ = world
    await cp.block_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    await cp.disable_protection(minor_username="kid", actor_user_id=admin)
    bio, picture_hash = await _b_holds(b, kid)
    assert bio == BIO and picture_hash
    assert await _picture_status(client_b, kid) == 200


async def test_a_block_placed_after_a_re_pair_still_takes_them_off(world):
    """After an unpair A has no row for Rex, so the block places him nowhere
    and covers no household. Once B's roster names him again (the re-pair),
    the block starts covering B — and B gets the name only right then."""
    a, b, client_b, cp, admin, kid, rex, a_id, b_id = world
    await a[db_key].enqueue("DELETE FROM remote_users WHERE user_id=?", (rex,))
    await cp.block_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    # Nothing could be sent yet: B still holds everything.
    assert (await _b_holds(b, kid))[0] == BIO
    for handler in a[federation_service_key]._event_registry.handlers_for(
        FederationEventType.USERS_SYNC
    ):
        await handler(
            FederationEvent(
                msg_id=uuid.uuid4().hex,
                event_type=FederationEventType.USERS_SYNC,
                from_instance=b_id,
                to_instance=a_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={
                    "users": [
                        {"user_id": rex, "username": "rex", "display_name": "Rex"}
                    ]
                },
            )
        )
    assert await _b_holds(b, kid) == (None, None)
    assert await _picture_status(client_b, kid) == 404


async def test_an_older_receiver_keeps_only_a_grey_placeholder(world, monkeypatch):
    """A receiver built before this rule keeps its cached bytes on a ``null``
    hash. The block's placeholder overwrites them first, so what it can
    still serve by URL is plain grey — never the child's picture."""
    _a, b, client_b, cp, admin, kid, rex, *_ = world
    before = await client_b.get(f"/api/users/{kid}/picture", headers=REX_AUTH)
    child_bytes = await before.read()

    async def _keep(self, user_id):  # the older receiver: no clear on null
        return None

    monkeypatch.setattr(type(b[profile_picture_repo_key]), "clear_user_picture", _keep)
    await cp.block_user_for_minor(
        minor_user_id=kid, blocked_user_id=rex, guardian_user_id=admin
    )
    assert await _b_holds(b, kid) == (None, None)
    resp = await client_b.get(f"/api/users/{kid}/picture", headers=REX_AUTH)
    assert resp.status == 200
    grey = Image.open(io.BytesIO(await resp.read())).convert("RGB")
    assert await resp.read() != child_bytes
    assert set(grey.get_flattened_data()) == {(204, 204, 204)}
