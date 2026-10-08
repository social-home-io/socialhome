"""Tests for socialhome.services.space_page_service (space pages, §4.4.4)."""

from __future__ import annotations

import pytest

from socialhome.domain.errors import InvalidMediaRefError
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PageCreated, PageDeleted, PageUpdated
from socialhome.domain.space import (
    AccessAdminOnlyError,
    ContentAction,
    ContentQueuedForReview,
    ModerationStaleError,
    ModerationStatus,
    ModerationTargetGoneError,
    SpacePermissionError,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.domain.page_version import version_hash
from socialhome.services.page_conflict_service import (
    NoActiveConflictError,
    PageConflictService,
)
from socialhome.services.space_moderation_service import SpaceModerationService
from socialhome.services.space_page_service import (
    PageModerationHandler,
    PageStaleError,
    SpacePageService,
)

_ROLES = (
    ("u-owner", "owner"),
    ("u-admin", "admin"),
    ("u-mod", "moderator"),
    ("u-member", "member"),
    ("u-sub", "subscriber"),
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pages.db", batch_timeout_ms=10)
    await db.startup()
    for sid in ("sp-a", "sp-b"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, ?, 'inst', 'o', ?)",
            (sid, sid, "ab" * 32),
        )
    for uid, role in _ROLES:
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            ("sp-a", uid, role),
        )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        ("sp-b", "u-member", "member"),
    )
    bus = EventBus()
    events: list = []
    for et in (PageCreated, PageUpdated, PageDeleted):
        bus.subscribe(et, events.append)

    class E:
        pass

    e = E()
    e.db = db
    e.pages = SqlitePageRepo(db)
    e.events = events
    e.svc = SpacePageService(
        e.pages,
        space_repo=SqliteSpaceRepo(db),
        bus=bus,
        conflict_service=PageConflictService(e.pages, bus=bus),
    )
    yield e
    await db.shutdown()


async def _admin_only(env):
    await env.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id='sp-a'")


# ── CRUD + events ────────────────────────────────────────────────────────


async def test_create_update_delete_publish_with_the_actor(env):
    page = await env.svc.create(
        "sp-a", actor_user_id="u-member", title=" Notes ", content="body"
    )
    assert (page.title, page.created_by, page.space_id) == ("Notes", "u-member", "sp-a")
    assert [p.id for p in await env.svc.list("sp-a")] == [page.id]
    updated = await env.svc.update(
        "sp-a", page.id, actor_user_id="u-mod", content="body 2"
    )
    assert updated.content == "body 2"
    assert updated.last_editor_user_id == "u-mod"
    versions = await env.svc.versions("sp-a", page.id)
    assert [v.content for v in versions] == ["body"]
    await env.svc.delete("sp-a", page.id, actor_user_id="u-admin")
    assert await env.pages.get(page.id) is None
    assert [(type(e).__name__, e.actor_user_id) for e in env.events] == [
        ("PageCreated", "u-member"),
        ("PageUpdated", "u-mod"),
        ("PageDeleted", "u-admin"),
    ]
    assert all(e.space_id == "sp-a" for e in env.events)


async def test_create_and_update_need_a_title(env):
    with pytest.raises(ValueError):
        await env.svc.create("sp-a", actor_user_id="u-member", title="  ", content="")
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    with pytest.raises(ValueError):
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", title=" ")


async def test_titles_are_capped_at_200_characters(env):
    """The service backstop (a queued page replays through it too)."""
    with pytest.raises(ValueError):
        await env.svc.create("sp-a", actor_user_id="u-member", title="x" * 201)
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="y" * 200)
    with pytest.raises(ValueError):
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", title="z" * 201)


async def test_a_stale_update_reports_the_current_page(env):
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    with pytest.raises(PageStaleError) as info:
        await env.svc.update(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            content="mine",
            base_updated_at="1999-01-01T00:00:00+00:00",
        )
    assert info.value.current.id == page.id
    assert (await env.pages.get(page.id)).content == ""


async def test_signed_media_urls_are_stored_canonical(env):
    page = await env.svc.create(
        "sp-a",
        actor_user_id="u-member",
        title="T",
        content="![x](/api/media/a.webp?exp=1&sig=abc)",
    )
    assert page.content == "![x](/api/media/a.webp)"
    up = await env.svc.update(
        "sp-a",
        page.id,
        actor_user_id="u-member",
        cover_image_url="/api/media/c.webp?exp=1&sig=abc",
    )
    assert up.cover_image_url == "/api/media/c.webp"


# ── Scope (§24.11, #785) ─────────────────────────────────────────────────


async def test_a_page_of_another_space_is_not_found(env):
    other = await env.svc.create("sp-b", actor_user_id="u-member", title="B")
    for call in (
        lambda: env.svc.get("sp-a", other.id),
        lambda: env.svc.versions("sp-a", other.id),
        lambda: env.svc.update("sp-a", other.id, actor_user_id="u-owner", title="X"),
        lambda: env.svc.delete("sp-a", other.id, actor_user_id="u-owner"),
    ):
        with pytest.raises(KeyError):
            await call()
    assert (await env.pages.get(other.id)).title == "B"


# ── Writer gate ──────────────────────────────────────────────────────────


async def test_require_writer_refuses_subscribers_strangers_and_archives(env):
    await env.svc.require_writer("sp-a", "u-member")
    for uid in ("u-sub", "u-nobody"):
        with pytest.raises(SpacePermissionError):
            await env.svc.require_writer("sp-a", uid)
    await env.db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-a'")
    with pytest.raises(SpacePermissionError):
        await env.svc.require_writer("sp-a", "u-owner")
    with pytest.raises(KeyError):
        await env.svc.require_writer("sp-404", "u-owner")


# ── ADMIN_ONLY (§4.3) ────────────────────────────────────────────────────


@pytest.mark.parametrize("actor", ["u-member", "u-mod"])
@pytest.mark.parametrize("op", ["create", "update", "delete", "resolve"])
async def test_admin_only_pages_refuse_members_and_moderators(env, actor, op):
    page = await env.svc.create("sp-a", actor_user_id=actor, title="T", content="c")
    await _admin_only(env)
    env.events.clear()
    calls = {
        "create": lambda: env.svc.create("sp-a", actor_user_id=actor, title="New"),
        "update": lambda: env.svc.update(
            "sp-a", page.id, actor_user_id=actor, content="changed"
        ),
        "delete": lambda: env.svc.delete("sp-a", page.id, actor_user_id=actor),
        "resolve": lambda: env.svc.resolve_conflict(
            "sp-a", page.id, actor_user_id=actor, resolution="mine"
        ),
    }
    with pytest.raises(AccessAdminOnlyError):
        await calls[op]()
    held = await env.pages.get(page.id)
    assert held is not None and held.content == "c"
    assert [p.id for p in await env.svc.list("sp-a")] == [page.id]
    assert env.events == []


@pytest.mark.parametrize("actor", ["u-owner", "u-admin"])
async def test_admin_only_pages_let_admins_write(env, actor):
    await _admin_only(env)
    page = await env.svc.create("sp-a", actor_user_id=actor, title="T")
    await env.svc.update("sp-a", page.id, actor_user_id=actor, content="x")
    await env.svc.delete("sp-a", page.id, actor_user_id=actor)
    assert await env.pages.get(page.id) is None


async def test_admin_only_pages_are_still_readable(env):
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    await _admin_only(env)
    assert (await env.svc.get("sp-a", page.id)).id == page.id
    assert await env.svc.versions("sp-a", page.id) == []


# ── Conflict resolution ──────────────────────────────────────────────────


async def test_resolve_conflict_needs_an_open_conflict(env):
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    with pytest.raises(NoActiveConflictError):
        await env.svc.resolve_conflict(
            "sp-a", page.id, actor_user_id="u-member", resolution="mine"
        )


async def test_resolve_conflict_rejects_an_unknown_resolution(env):
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    with pytest.raises(ValueError):
        await env.svc.resolve_conflict(
            "sp-a", page.id, actor_user_id="u-member", resolution="yolo"
        )


# ── MODERATED pages (§4.3 review queue) ──────────────────────────────────


async def _moderated(env):

    await env.db.enqueue(
        "UPDATE spaces SET pages_access='moderated', feature_pages=1 WHERE id='sp-a'"
    )
    mod = SpaceModerationService(
        SqliteSpaceRepo(env.db),
        user_repo=SqliteUserRepo(env.db),
        own_instance_id="inst",
    )
    env.svc.attach_moderation(mod)
    handler = PageModerationHandler(env.svc)
    for action in ContentAction:
        if action is not ContentAction.LAYOUT:
            mod.register("pages", action, handler)
    return mod


async def test_moderated_member_create_queues_then_approve_keeps_author(env):

    mod = await _moderated(env)
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.create("sp-a", actor_user_id="u-member", title="T", content="b")
    assert await env.svc.list("sp-a") == []
    assert env.events == []
    item = exc.value.item
    await mod.approve("sp-a", item.id, actor_user_id="u-mod")
    [page] = await env.svc.list("sp-a")
    assert (page.id, page.created_by, page.content) == (
        item.payload["target_id"],
        "u-member",
        "b",
    )
    assert [type(e) for e in env.events] == [PageCreated]


async def test_moderated_own_edit_proceeds_others_queues(env):

    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", content="v2")
    assert (await env.svc.get("sp-a", page.id)).content == "v1"
    described = await mod.describe(exc.value.item, with_current=True)
    assert described["snapshot"] == {"content": "v1"}
    assert described["preview"] == {"content": "v2"}
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    got = await env.svc.get("sp-a", page.id)
    assert (got.content, got.last_editor_user_id) == ("v2", "u-member")


async def test_moderated_page_edit_stale_then_force(env):

    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", content="mine")
    await env.svc.update("sp-a", page.id, actor_user_id="u-owner", content="theirs")
    with pytest.raises(ModerationStaleError) as stale:
        await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    assert stale.value.current == {"content": "theirs"}
    assert stale.value.proposed == {"content": "mine"}
    assert stale.value.base == {"content": "v1"}
    assert (
        await mod.get_item("sp-a", exc.value.item.id)
    ).status is ModerationStatus.PENDING
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod", force=True)
    assert (await env.svc.get("sp-a", page.id)).content == "mine"


async def test_moderated_page_delete_queues_and_gone_edit_410(env):

    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    with pytest.raises(ContentQueuedForReview) as d:
        await env.svc.delete("sp-a", page.id, actor_user_id="u-member")
    with pytest.raises(ContentQueuedForReview) as e:
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", title="New")
    assert (await mod.describe(d.value.item))["preview"]["content"] == "v1"
    await mod.approve("sp-a", d.value.item.id, actor_user_id="u-mod")
    assert await env.svc.list("sp-a") == []
    with pytest.raises(ModerationTargetGoneError):
        await mod.approve("sp-a", e.value.item.id, actor_user_id="u-mod")
    assert (
        await mod.get_item("sp-a", e.value.item.id)
    ).status is ModerationStatus.EXPIRED


async def _open_conflict(env, page, *bodies: str) -> list[str]:
    """Seed an open conflict on ``page``: one side per body, the page
    showing the first. Returns the side hashes."""
    for i, body in enumerate(bodies):
        await env.pages.insert_snapshot(
            page_id=page.id,
            space_id="sp-a",
            body=body,
            title=page.title,
            author_user_id=f"u-side-{i}",
            side="mine" if i == 0 else "theirs",
            conflict=True,
        )
    await env.db.enqueue(
        "UPDATE space_pages SET content=? WHERE id=?", (bodies[0], page.id)
    )
    return [version_hash(page.title, b) for b in bodies]


async def test_moderated_resolve_conflict_queues_and_gone_conflict_410(env):

    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    # Nothing to resolve: refused at submit, never queued.
    with pytest.raises(NoActiveConflictError):
        await env.svc.resolve_conflict(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            resolution="merged_content",
            merged_content="merged",
        )
    sides = await _open_conflict(env, page, "one", "two")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.resolve_conflict(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            resolution="side",
            side=sides[1],
            sides=sides,
        )
    item = exc.value.item
    assert item.payload["op"] == "resolve_conflict"
    assert item.payload["side"] == sides[1]
    assert sorted(item.payload["sides"]) == sorted(sides)
    assert PageModerationHandler(env.svc).preview(item)["content"] == "two"
    # Resolved by somebody else meanwhile → nothing to resolve any more.
    await env.pages.clear_conflict_flag(page.id, space_id="sp-a")
    with pytest.raises(ModerationTargetGoneError):
        await mod.approve("sp-a", item.id, actor_user_id="u-mod")


async def test_moderated_resolution_is_released_by_side(env):
    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    sides = await _open_conflict(env, page, "one", "two")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.resolve_conflict(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            resolution="side",
            side=sides[1],
            sides=sides,
        )
    env.events.clear()
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    assert (await env.svc.get("sp-a", page.id)).content == "two"
    (event,) = env.events
    assert isinstance(event, PageUpdated)
    # Sequenced on this (host) household: every side retired.
    assert event.canonical["conflict"] == []
    assert await env.svc.conflict_sides("sp-a", page.id) == []


async def test_moderated_resolution_with_changed_sides_is_stale_even_forced(env):
    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    sides = await _open_conflict(env, page, "one", "two")
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.resolve_conflict(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            resolution="side",
            side=sides[1],
            sides=sides,
        )
    # A third household's version joined the conflict meanwhile.
    await env.pages.insert_snapshot(
        page_id=page.id,
        space_id="sp-a",
        body="three",
        title="T",
        author_user_id="u-x",
        side="theirs",
        conflict=True,
    )
    for force in (False, True):
        with pytest.raises(ModerationStaleError):
            await mod.approve(
                "sp-a", exc.value.item.id, actor_user_id="u-mod", force=force
            )


async def test_a_moderated_edit_approved_while_conflicted_still_lands(env):
    """v_48: a conflict never blocks an edit — not a released one either."""
    mod = await _moderated(env)
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="v1"
    )
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update("sp-a", page.id, actor_user_id="u-member", content="v2")
    await _open_conflict(env, page, "v1", "two")
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    assert (await env.svc.get("sp-a", page.id)).content == "v2"
    assert len(await env.svc.conflict_sides("sp-a", page.id)) == 2


def test_validate_carries_the_resolution_sides():
    handler = PageModerationHandler.__new__(PageModerationHandler)
    side = version_hash("T", "two")
    out = handler.validate(
        None,  # type: ignore[arg-type]
        {
            "entity": "page",
            "target_id": "pg",
            "op": "resolve_conflict",
            "resolution": "side",
            "side": side,
            "sides": [side],
            "extra": "dropped",
        },
    )
    assert out["side"] == side and out["sides"] == [side]
    assert "extra" not in out
    for bad in (
        {"resolution": "side", "side": "nope"},
        {"resolution": "side", "side": side, "sides": ["nope"]},
        {"resolution": "bogus"},
    ):
        with pytest.raises(ValueError):
            handler.validate(
                None,  # type: ignore[arg-type]
                {"entity": "page", "target_id": "pg", "op": "resolve_conflict", **bad},
            )


async def test_an_update_while_a_conflict_is_open_is_allowed(env):
    page = await env.svc.create(
        "sp-a", actor_user_id="u-member", title="T", content="v1"
    )
    await _open_conflict(env, page, "one", "two")
    updated = await env.svc.update(
        "sp-a", page.id, actor_user_id="u-member", content="v2"
    )
    assert updated.content == "v2"
    assert len(await env.svc.conflict_sides("sp-a", page.id)) == 2
    assert await env.svc.pages_in_conflict("sp-a") == {page.id}
    assert await env.svc.pages_in_conflict("sp-b") == set()


async def test_the_host_sequences_its_own_edits(env):
    page = await env.svc.create(
        "sp-a", actor_user_id="u-member", title="T", content="v1"
    )
    assert page.seq == 1
    await env.svc.update("sp-a", page.id, actor_user_id="u-member", content="v2")
    await env.svc.update("sp-a", page.id, actor_user_id="u-member", content="v3")
    updates = [e for e in env.events if isinstance(e, PageUpdated)]
    assert [e.canonical["seq"] for e in updates] == [2, 3]
    assert not any(e.proposal for e in updates)
    assert (await env.svc.get("sp-a", page.id)).seq == 3


class _MemberFed:
    def __init__(self, version: int) -> None:
        self.version = version

    async def peer_supports(self, instance_id, *, min_version):
        return self.version >= min_version


@pytest.mark.parametrize(("version", "proposal"), [(48, True), (47, False)])
async def test_a_member_household_drafts_or_stays_last_write_wins(
    env, version, proposal
):
    """Under a v_48 host a member's edit is a draft (proposed, never
    broadcast); under an older host it is last write wins, as before."""
    page = await env.svc.create(
        "sp-a", actor_user_id="u-member", title="T", content="v1"
    )
    env.svc._conflicts.attach_federation(
        _MemberFed(version), own_instance_id="me", space_repo=SqliteSpaceRepo(env.db)
    )
    env.events.clear()
    updated = await env.svc.update(
        "sp-a", page.id, actor_user_id="u-member", content="v2"
    )
    (event,) = env.events
    assert event.proposal is proposal and event.canonical is None
    assert updated.content == "v2"
    assert updated.pending_base_seq == (1 if proposal else None)
    created = await env.svc.create(
        "sp-a", actor_user_id="u-member", title="N", content="x"
    )
    assert env.events[-1].proposal is proposal
    assert created.pending_base_seq == (0 if proposal else None)


async def test_moderated_moderator_writes_directly(env):
    await _moderated(env)
    page = await env.svc.create("sp-a", actor_user_id="u-mod", title="T", content="x")
    assert (await env.svc.get("sp-a", page.id)).created_by == "u-mod"


# ── Local media only (docs/principles.md "No third-party fetches") ───────


async def test_page_cover_must_be_local_media(env):
    """Receivers drop a remote cover and every reader's browser would fetch
    it — create and edit refuse it (422); clearing stays allowed."""
    with pytest.raises(InvalidMediaRefError):
        await env.svc.create(
            "sp-a",
            actor_user_id="u-member",
            title="T",
            cover_image_url="https://cdn.example/c.jpg",
        )
    page = await env.svc.create("sp-a", actor_user_id="u-member", title="T")
    with pytest.raises(InvalidMediaRefError) as ei:
        await env.svc.update(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            cover_image_url="//evil.example/c.jpg",
        )
    assert ei.value.detail.startswith("cover_image_url ")
    cleared = await env.svc.update(
        "sp-a", page.id, actor_user_id="u-member", cover_image_url=None
    )
    assert cleared.cover_image_url is None
