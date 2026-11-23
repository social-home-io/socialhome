"""Release-blocker protocol tests: a follower writes on every path (v_96, P4).

Marked ``@pytest.mark.security``.

A follower is a member with the read-only ``subscriber`` role on every
path. Its writes are judged on each path by the same rule, never by a
follower-only route:

* **Comments and reactions** pass only while the RECEIVER's pinned switch
  (``allow_subscriber_comment`` / ``allow_subscriber_react``) is on.
* **Every other family** — posts and their edits / deletes, and every other
  space write — is refused, whatever the switches say.
* **The author's own comment delete always passes** (owner decision F6,
  2026-11-23), with every switch off and with no writer cert at all: it is
  decided on authorship (the stored comment's author), not on the switch —
  and it never reaches anybody else's comment.

Paths: the federation pipeline and ``SPACE_ROUTED`` (the same post-decrypt
gates, :meth:`FederationService.post_decrypt_gate_steps`, then the real
handlers), the §25.6 sync stream, and the channel (the writer-cert check
every channel / GFS item passes, :meth:`SpaceWriterCertService.check_item_cert`).
All over the real app and SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.app_keys import federation_service_key, space_sync_receiver_key
from socialhome.crypto import generate_identity_keypair, generate_space_keypair
from socialhome.domain.federation import (
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_item import _REQUIRED_SCOPE as REQUIRED_SCOPE_TABLE
from socialhome.domain.space_item import (
    ITEM_TYPE_AUTHOR_PROFILE,
    ITEM_TYPE_COMMENT,
    ITEM_TYPE_COMMENT_DELETE,
    ITEM_TYPE_COMMENT_EDIT,
    ITEM_TYPE_REACTION_ADD,
    ITEM_TYPE_REACTION_REMOVE,
    required_scope,
)
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import WRITER_SCOPE_COMMENT, sign_writer_cert

from .test_space_content_authorship import SP, env  # noqa: F401

pytestmark = pytest.mark.security

FET = FederationEventType
FOLLOWER = "house-follower"  # holds ONLY a follower seat (u-f) in SP
FAN = "u-f"
OWN = "cmt-f"  # u-f's own comment on post-a

#: The writes a follower seat may make at all, each under its switch —
#: and the own-comment delete, under none.
_FOLLOWER_WRITES = {
    FET.SPACE_COMMENT_CREATED,
    FET.SPACE_COMMENT_UPDATED,
    FET.SPACE_COMMENT_DELETED,
    FET.SPACE_POST_REACTION,
}


async def _follower(db, *, comment: bool, react: bool) -> None:
    await db.enqueue(
        "INSERT OR IGNORE INTO space_remote_members(space_id, instance_id,"
        " user_id, role) VALUES(?, ?, ?, 'subscriber')",
        (SP, FOLLOWER, FAN),
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO space_post_comments(id, post_id, author, type,"
        " content) VALUES(?, 'post-a', ?, 'text', 'mine')",
        (OWN, FAN),
    )
    await _switches(db, comment=comment, react=react)


async def _switches(db, *, comment: bool, react: bool) -> None:
    await db.enqueue(
        "UPDATE spaces SET allow_subscriber_comment=?, allow_subscriber_react=?"
        " WHERE id=?",
        (int(comment), int(react), SP),
    )


def _event(event_type, payload, *, sender=FOLLOWER) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}-{sender}-{datetime.now(UTC).timestamp()}",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload={"space_id": SP, **payload},
        space_id=SP,
    )


async def _gated(app, event: FederationEvent) -> bool:
    """The post-decrypt gates of the pipeline and of ``SPACE_ROUTED`` (one
    list, :meth:`FederationService.post_decrypt_gate_steps`), then — when
    they pass — the real handlers. Returns whether the gates passed."""
    fed = app[federation_service_key]
    ctx = InboundContext(
        envelope={"space_id": SP, "from_instance": event.from_instance},
        event=event,
    )
    if not await run_post_decrypt_gates(
        ctx, steps=fed.post_decrypt_gate_steps(include_ban_check=True)
    ):
        return False
    for handler in fed._event_registry.handlers_for(event.event_type):
        await handler(event)
    return True


async def _comment(db, cid: str) -> tuple | None:
    row = await db.fetchone(
        "SELECT author, deleted, content FROM space_post_comments WHERE id=?", (cid,)
    )
    return tuple(row) if row is not None else None


# ── Federation and SPACE_ROUTED ──────────────────────────────────────────


@pytest.mark.parametrize(
    "event_type",
    sorted(SPACE_WRITE_EVENT_TYPES - _FOLLOWER_WRITES, key=lambda t: t.value),
)
async def test_a_follower_writes_no_other_family_with_every_switch_on(
    env,  # noqa: F811
    event_type,
):
    app, db = env
    await _follower(db, comment=True, react=True)
    payload = {"author": FAN, "user_id": FAN, "id": "x-1", "post_id": "post-a"}
    assert not await _gated(app, _event(event_type, payload)), event_type


async def test_a_follower_comment_follows_its_switch(env):  # noqa: F811
    app, db = env
    await _follower(db, comment=False, react=True)
    comment = {
        "post_id": "post-a",
        "comment_id": "c-new",
        "author": FAN,
        "type": "text",
        "content": "x",
    }
    assert not await _gated(app, _event(FET.SPACE_COMMENT_CREATED, comment))
    assert await _comment(db, "c-new") is None
    await _switches(db, comment=True, react=False)
    assert await _gated(app, _event(FET.SPACE_COMMENT_CREATED, comment))
    assert await _comment(db, "c-new") == (FAN, 0, "x")


async def test_a_follower_reaction_follows_its_switch(env):  # noqa: F811
    app, db = env
    await _follower(db, comment=True, react=False)
    reaction = {
        "post_id": "post-a",
        "user_id": FAN,
        "emoji": "🎉",
        "action": "add",
        "at": datetime.now(UTC).isoformat(),
    }
    assert not await _gated(app, _event(FET.SPACE_POST_REACTION, reaction))
    await _switches(db, comment=False, react=True)
    assert await _gated(app, _event(FET.SPACE_POST_REACTION, reaction))
    row = await db.fetchone("SELECT reactions FROM space_posts WHERE id='post-a'", ())
    assert FAN in row["reactions"]


async def test_a_follower_edits_its_comment_only_under_the_switch(env):  # noqa: F811
    app, db = env
    await _follower(db, comment=False, react=True)
    edit = {"id": OWN, "comment_id": OWN, "author": FAN, "content": "rewritten"}
    assert not await _gated(app, _event(FET.SPACE_COMMENT_UPDATED, edit))
    assert await _comment(db, OWN) == (FAN, 0, "mine")


async def test_a_follower_deletes_its_own_comment_with_every_switch_off(env):  # noqa: F811
    """Owner decision F6: the author's own delete, no switch and no cert —
    the household's signed envelope and its live follower seat prove the
    author; the handler deletes only a comment that author wrote."""
    app, db = env
    await _follower(db, comment=False, react=False)
    delete = {"id": OWN, "comment_id": OWN, "post_id": "post-a", "author": FAN}
    assert await _gated(app, _event(FET.SPACE_COMMENT_DELETED, delete))
    assert await _comment(db, OWN) == (FAN, 1, None)


@pytest.mark.parametrize("named", [FAN, "u-a"])
async def test_a_follower_never_deletes_anybody_elses_comment(env, named):  # noqa: F811
    """Naming itself (the gate passes, the handler checks the stored author)
    or naming the real author (no seat of that user here) — either way u-a's
    comment stays, with every switch on."""
    app, db = env
    await _follower(db, comment=True, react=True)
    delete = {"id": "cmt-a", "comment_id": "cmt-a", "post_id": "post-a"}
    await _gated(app, _event(FET.SPACE_COMMENT_DELETED, {**delete, "author": named}))
    assert await _comment(db, "cmt-a") == ("u-a", 0, "c")


async def test_a_left_follower_deletes_nothing(env):  # noqa: F811
    """The proof is a LIVE seat: once the follower left, its household is
    refused like any other former member's."""
    app, db = env
    await _follower(db, comment=True, react=True)
    await db.enqueue(
        "UPDATE space_remote_members SET tombstoned=1 WHERE instance_id=?",
        (FOLLOWER,),
    )
    delete = {"id": OWN, "comment_id": OWN, "post_id": "post-a", "author": FAN}
    assert not await _gated(app, _event(FET.SPACE_COMMENT_DELETED, delete))
    assert await _comment(db, OWN) == (FAN, 0, "mine")


# ── §25.6 sync stream ────────────────────────────────────────────────────


async def _sync(app, resource: str, records: list[dict]) -> None:
    await app[space_sync_receiver_key]._dispatch(
        resource, SP, records, provider=FOLLOWER
    )


async def test_a_follower_sync_stream_writes_no_post(env):  # noqa: F811
    app, db = env
    await _follower(db, comment=True, react=True)
    await _sync(
        app,
        "posts",
        [{"id": "p-sync-f", "author": FAN, "type": "text", "content": "x"}],
    )
    assert (
        await db.fetchone("SELECT 1 FROM space_posts WHERE id='p-sync-f'", ()) is None
    )


async def test_a_follower_sync_stream_deletes_its_own_comment(env):  # noqa: F811
    app, db = env
    await _follower(db, comment=False, react=False)
    await _sync(
        app, "comments_deleted", [{"id": OWN, "post_id": "post-a", "author": FAN}]
    )
    assert await _comment(db, OWN) == (FAN, 1, None)
    await _sync(
        app, "comments_deleted", [{"id": "cmt-a", "post_id": "post-a", "author": FAN}]
    )
    assert await _comment(db, "cmt-a") == ("u-a", 0, "c")


# ── The channel: the writer-cert check every relayed item passes ─────────


_SPACE_KP = generate_space_keypair()
_AUTHOR_KP = generate_identity_keypair()


def _space(*, comment: bool, react: bool) -> Space:
    return Space(
        id="sp-chan",
        name="S",
        owner_instance_id="host.home",
        owner_username="h",
        identity_public_key=_SPACE_KP.public_key.hex(),
        config_sequence=0,
        features=SpaceFeatures(
            allow_subscribers=True,
            allow_subscriber_comment=comment,
            allow_subscriber_react=react,
        ),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )


#: What a follower's ``reader`` cert carries, per type: the switch it needs
#: (``None``: always — the own-comment delete), or absent: never.
_READER_NEEDS: dict[str, str | None] = {
    ITEM_TYPE_COMMENT: "comment",
    ITEM_TYPE_COMMENT_EDIT: "comment",
    ITEM_TYPE_COMMENT_DELETE: None,
    ITEM_TYPE_REACTION_ADD: "react",
    ITEM_TYPE_REACTION_REMOVE: "react",
    ITEM_TYPE_AUTHOR_PROFILE: "either",
}


@pytest.mark.parametrize("item_type", sorted(REQUIRED_SCOPE_TABLE))
@pytest.mark.parametrize("comment", [False, True])
@pytest.mark.parametrize("react", [False, True])
def test_a_reader_cert_on_the_channel_carries_only_its_switches(
    item_type, comment, react
):
    space = _space(comment=comment, react=react)
    cert = sign_writer_cert(
        space_seed=_SPACE_KP.private_key,
        space_id=space.id,
        epoch=3,
        instance_pk=_AUTHOR_KP.public_key,
        scope=WRITER_SCOPE_COMMENT,
        reader=True,
    ).to_wire()
    accepted = SpaceWriterCertService.check_item_cert(
        space,
        cert,
        epoch=3,
        author_pk=_AUTHOR_KP.public_key,
        required_scope=required_scope(item_type),
        item_type=item_type,
    )
    if item_type not in _READER_NEEDS:
        expected = False  # posts and their edits / deletes: never
    else:
        need = _READER_NEEDS[item_type]
        expected = {
            None: True,
            "comment": comment,
            "react": react,
            "either": comment or react,
        }[need]
    assert accepted is expected, (item_type, comment, react)
