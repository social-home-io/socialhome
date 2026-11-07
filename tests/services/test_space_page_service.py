"""Tests for socialhome.services.space_page_service (space pages, §4.4.4)."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import InvalidMediaRefError
from socialhome.domain.events import PageCreated, PageDeleted, PageUpdated
from socialhome.domain.page_version import version_hash
from socialhome.domain.space import (
    AccessAdminOnlyError,
    ContentAction,
    ContentQueuedForReview,
    ModerationStaleError,
    ModerationStatus,
    ModerationTargetGoneError,
    SpacePermissionError,
)
from socialhome.domain.space_media_ref import MAX_PAGE_FILES, space_page_media_name
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.media_ref_guard import MediaRefGuard
from socialhome.services.page_conflict_service import (
    NoActiveConflictError,
    PageConflictService,
    PageMode,
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


async def test_a_reviewed_pages_cover_and_pictures_are_released(env):
    """Regression (same gap as a reviewed calendar cover): the release
    re-checks the page's files for the submitter after the approve has
    claimed the item, and the held item — the only row naming them — no
    longer counted as theirs."""
    mod = await _moderated(env)
    env.svc.attach_media_guard(MediaRefGuard(SqliteMediaReferenceRepo(env.db)))
    # A local submitter: one of this household's people (a ``users`` row).
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
        ("mia", "u-member", "Mia"),
    )
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.create(
            "sp-a",
            actor_user_id="u-member",
            title="T",
            content="![p](api/media/in-page.webp)",
            cover_image_url="api/media/page-cover.webp",
        )
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    [page] = await env.svc.list("sp-a")
    assert page.cover_image_url == "api/media/page-cover.webp"
    assert "api/media/in-page.webp" in page.content


async def test_an_edit_rechecks_only_the_files_the_page_does_not_show(env):
    """An edit repeats the pictures the page shows already (a body patch
    carries the whole body): those are the page's own, re-published to the
    same space — only a file the edit adds is checked for its writer. A
    remote member's reviewed edit keeping another member's picture is
    released (a held item naming it is someone else's)."""
    mod = await _moderated(env)
    guard = MediaRefGuard(SqliteMediaReferenceRepo(env.db))
    page = await env.svc.create(
        "sp-a",
        actor_user_id="u-owner",
        title="T",
        content="![k](api/media/kept.webp)",
        cover_image_url="api/media/cover.webp",
    )
    env.svc.attach_media_guard(guard)
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
        ("mia", "u-member", "Mia"),
    )
    # Another submitter's held item names the page's picture too.
    await env.db.enqueue(
        "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
        " submitted_by, payload_json, submitted_at, expires_at, status)"
        " VALUES('other', 'sp-a', 'posts', 'create', 'u-x',"
        " '{\"media_url\": \"api/media/kept.webp\"}', datetime('now'),"
        " datetime('now', '+1 day'), 'pending')"
    )
    with pytest.raises(ContentQueuedForReview) as exc:
        await env.svc.update(
            "sp-a",
            page.id,
            actor_user_id="u-member",
            content="![k](api/media/kept.webp) ![n](api/media/new.webp)",
            cover_image_url="api/media/cover.webp",
        )
    await mod.approve("sp-a", exc.value.item.id, actor_user_id="u-mod")
    got = await env.svc.get("sp-a", page.id)
    assert "api/media/new.webp" in got.content
    # A file another row shows that the page does not is still refused.
    await env.db.enqueue(
        "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
        " submitted_by, payload_json, submitted_at, expires_at, status)"
        " VALUES('other2', 'sp-a', 'posts', 'create', 'u-x',"
        " '{\"media_url\": \"api/media/theirs.webp\"}', datetime('now'),"
        " datetime('now', '+1 day'), 'pending')"
    )
    with pytest.raises(InvalidMediaRefError):
        await env.svc.update(
            "sp-a",
            page.id,
            actor_user_id="u-owner",
            content="![t](api/media/theirs.webp)",
        )


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


# ── v_82: a page's files on a member household ──────────────────────────


def _as_member(env, monkeypatch) -> None:
    async def _mode(space_id):
        return PageMode.MEMBER, "host-x"

    monkeypatch.setattr(PageConflictService, "mode", lambda self, sid: _mode(sid))


async def _page_by(env, author: str, content: str, cover: str | None = None):
    """A space page row as a host version would store it (written by
    *author*), straight into the repo."""
    page = await env.svc.create("sp-a", actor_user_id="u-owner", title="T", content="x")
    await env.db.enqueue(
        "UPDATE space_pages SET content=?, cover_image_url=?, created_by=?,"
        " last_editor_user_id=? WHERE id=?",
        (content, cover, author, author, page.id),
    )
    return await env.svc.get("sp-a", page.id)


async def test_a_member_shows_a_pages_files_as_its_page_scoped_copies(
    env, monkeypatch, tmp_path
):
    """A page keeps its host's names byte for byte; a member shows each as
    its page-scoped copy — never the shared file of that name (another
    space's, a household file). The host shows its names as they are."""
    page = await _page_by(
        env,
        "u-remote",
        "![a](api/media/a.webp) ![b](/api/media/b.webp)",
        "api/media/c.webp",
    )
    assert await env.svc.media_view(page) == page  # the host
    _as_member(env, monkeypatch)
    env.svc._media_dir = tmp_path
    (tmp_path / "a.webp").write_bytes(b"a household file")  # never shown
    shown = await env.svc.media_view(page)
    sp = {n: space_page_media_name("sp-a", n) for n in ("a.webp", "b.webp", "c.webp")}
    assert shown.content == (
        f"![a](api/media/{sp['a.webp']}) ![b](/api/media/{sp['b.webp']})"
    )
    assert shown.cover_image_url == f"api/media/{sp['c.webp']}"


@pytest.mark.parametrize(
    "link",
    [
        "./api/media/dm-x.webp",
        "x/api/media/f.webp",
        "%2Fapi/media/f.webp",
        "https://evil.example/api/media/f.webp",
        "api/media/dm-x.webp",
        "/api/media/gm-x.webp",
    ],
)
async def test_a_member_never_signs_a_shared_file_inside_a_page(
    env, monkeypatch, tmp_path, link
):
    """Review finding 1 (HIGH): the signer, the guard and the page rename
    share ONE pattern, so every link the signer would sign in a page is one
    the page maps to its page-scoped copy; a reserved name is stripped. A
    path inside another URL or path is never signed at all. No signed URL to
    the shared store ever leaves a member household's page."""
    from socialhome.media_signer import MediaUrlSigner, sign_media_urls_in_markdown

    page = await _page_by(env, "u-remote", f"![x]({link})")
    _as_member(env, monkeypatch)
    env.svc._media_dir = tmp_path
    for name in ("dm-x.webp", "gm-x.webp", "f.webp"):
        (tmp_path / name).write_bytes(b"shared")
    shown = await env.svc.media_view(page)
    signed = sign_media_urls_in_markdown(shown.content, MediaUrlSigner(b"k" * 32))
    for name in ("dm-x.webp", "gm-x.webp", "f.webp"):
        assert f"{name}?exp=" not in signed, signed


async def test_an_old_page_keeps_its_own_households_pictures(
    env, monkeypatch, tmp_path
):
    """Review finding 3: a page from before v_82 (or our own draft) written
    by one of our people shows the files we uploaded ourselves under their
    own names while no page-scoped copy exists. A host-written page naming
    the very same file of ours never resolves it, nor does a received
    (``sd-``) name."""
    _as_member(env, monkeypatch)
    env.svc._media_dir = tmp_path
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('o', 'u-owner', 'O')"
    )
    (tmp_path / "mine.webp").write_bytes(b"ours")
    sd = "sd-" + "1" * 40 + ".webp"
    (tmp_path / sd).write_bytes(b"received")
    ours = await _page_by(
        env, "u-owner", f"![m](api/media/mine.webp) ![s](api/media/{sd})"
    )
    shown = await env.svc.media_view(ours)
    assert shown.content == (
        f"![m](api/media/mine.webp) ![s](api/media/{space_page_media_name('sp-a', sd)})"
    )
    theirs = await _page_by(env, "u-remote", "![m](api/media/mine.webp)")
    shown = await env.svc.media_view(theirs)
    assert shown.content == (
        f"![m](api/media/{space_page_media_name('sp-a', 'mine.webp')})"
    )
    # Once the host's copy arrived, the page-scoped copy wins.
    (tmp_path / space_page_media_name("sp-a", "mine.webp")).write_bytes(b"host")
    shown = await env.svc.media_view(ours)
    assert "api/media/mine.webp" not in shown.content


async def test_what_a_member_edits_carries_the_hosts_names(env, monkeypatch):
    """A member's editor sends back what it was shown: the page-scoped names
    map back to the names the page stores, so the proposal carries the
    host's names (and a file it adds keeps its own)."""
    page = await env.svc.create(
        "sp-a",
        actor_user_id="u-owner",
        title="T",
        content="![a](api/media/a.webp)",
        cover_image_url="api/media/c.webp",
    )
    _as_member(env, monkeypatch)
    sp_a = space_page_media_name("sp-a", "a.webp")
    sp_c = space_page_media_name("sp-a", "c.webp")
    content, cover = await env.svc._as_stored(
        "sp-a",
        page.id,
        f"![a](api/media/{sp_a}?sig=1) ![n](api/media/n.webp)",
        f"api/media/{sp_c}",
    )
    assert content == "![a](api/media/a.webp?sig=1) ![n](api/media/n.webp)"
    assert cover == "api/media/c.webp"
    # Another space's page-scoped name maps to nothing (and the guard
    # refuses a write naming it).
    other = space_page_media_name("sp-b", "a.webp")
    content, _ = await env.svc._as_stored(
        "sp-a", page.id, f"![x](api/media/{other})", None
    )
    assert content == f"![x](api/media/{other})"


async def test_a_page_shows_at_most_its_file_cap(env):
    """Review finding 5: every household ships and describes a whole
    version, so a page version shows at most MAX_PAGE_FILES files — enforced
    on create, edit and resolution with a clear error."""
    many = " ".join(f"![p](api/media/p{i}.webp)" for i in range(MAX_PAGE_FILES))
    with pytest.raises(ValueError, match="at most"):
        await env.svc.create(
            "sp-a",
            actor_user_id="u-owner",
            title="T",
            content=many,
            cover_image_url="api/media/cover.webp",
        )
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content=many
    )
    with pytest.raises(ValueError, match="at most"):
        await env.svc.update(
            "sp-a", page.id, actor_user_id="u-owner", cover_image_url="api/media/c.webp"
        )
    # The same file twice counts once.
    await env.svc.update(
        "sp-a",
        page.id,
        actor_user_id="u-owner",
        content=many + " ![p](api/media/p0.webp)",
    )


async def test_a_file_toggled_off_and_on_again_is_not_shipped_twice(env):
    """Review finding 5: what the host already shipped is everything the
    page's history showed, not only the replaced version — a picture
    removed and put back is never re-queued for every member."""
    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="![a](api/media/a.webp)"
    )
    await env.svc.update("sp-a", page.id, actor_user_id="u-owner", content="gone")
    await env.svc.update(
        "sp-a", page.id, actor_user_id="u-owner", content="![a](api/media/a.webp) back"
    )
    last = [e for e in env.events if isinstance(e, PageUpdated)][-1]
    assert "api/media/a.webp" in last.shown_before


def test_a_queued_page_item_over_the_file_cap_is_refused():
    """A queued create / edit / resolution (local or from another
    household) over MAX_PAGE_FILES is refused like any invalid payload."""
    handler = PageModerationHandler.__new__(PageModerationHandler)
    many = " ".join(f"![p](api/media/p{i}.webp)" for i in range(MAX_PAGE_FILES + 1))
    for payload in (
        {"entity": "page", "target_id": "pg", "title": "T", "content": many},
        {"entity": "page", "target_id": "pg", "patch": {"content": many}},
        {
            "entity": "page",
            "target_id": "pg",
            "op": "resolve_conflict",
            "resolution": "merged_content",
            "merged_content": many,
        },
    ):
        with pytest.raises(ValueError, match="at most"):
            handler.validate(None, payload)  # type: ignore[arg-type]


async def test_an_edit_adding_files_the_host_could_never_get_is_refused(env):
    """Review finding 7: a household seated from an invite link without an
    upload authorization can't hand the host a page's new files — the edit
    is refused with PAGE_FILES_NOT_SHAREABLE (the SPA translates it) instead
    of landing with pictures broken for everyone. Words alone still save."""
    from socialhome.domain.errors import PageFilesNotShareableError

    page = await env.svc.create(
        "sp-a", actor_user_id="u-owner", title="T", content="![k](api/media/k.webp)"
    )

    async def _never(space_id):
        return False

    env.svc.attach_files_reach_host(_never)
    with pytest.raises(PageFilesNotShareableError) as err:
        await env.svc.update(
            "sp-a",
            page.id,
            actor_user_id="u-owner",
            content="![k](api/media/k.webp) ![n](api/media/n.webp)",
        )
    assert err.value.code == "PAGE_FILES_NOT_SHAREABLE"
    with pytest.raises(PageFilesNotShareableError):
        await env.svc.create(
            "sp-a",
            actor_user_id="u-owner",
            title="T2",
            content="![n](api/media/n.webp)",
        )
    # Keeping the page's own picture, or words only: fine.
    await env.svc.update(
        "sp-a", page.id, actor_user_id="u-owner", content="words ![k](api/media/k.webp)"
    )


async def test_a_sides_and_a_versions_cover_are_shown_page_scoped(env, monkeypatch):
    """Review finding 8: conflict-side covers and the history drawer's
    versions show the same page-scoped names the page does, so a diff never
    marks an unchanged picture as changed — and never resolves a shared
    file."""
    _as_member(env, monkeypatch)
    sp = space_page_media_name("sp-a", "c.webp")
    assert (
        await env.svc.media_view_cover("sp-a", "api/media/c.webp") == f"api/media/{sp}"
    )
    assert await env.svc.media_view_cover("sp-a", "api/media/dm-x.webp") is None
    assert await env.svc.media_view_cover("sp-a", None) is None
    body = await env.svc.media_view_text("sp-a", "![c](api/media/c.webp)")
    assert body == f"![c](api/media/{sp})"
