"""Tests for socialhome.services.notification_links — and the guard that
every path a notification can open is a route the SPA has.

The route table is read from ``client/src/router.ts`` itself (and the
space section / settings-section unions from their modules), so a
renamed or removed SPA route fails here instead of leaving bells that
open a blank page.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from socialhome.domain.task import Task, TaskStatus
from socialhome.services import notification_links as links

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "client" / "src"
SOURCES = (
    ROOT / "socialhome" / "services" / "notification_service.py",
    ROOT / "socialhome" / "services" / "notification_links.py",
    ROOT / "socialhome" / "infrastructure" / "calendar_reminder_scheduler.py",
)


def _task(*, list_id: str = "l1", task_id: str = "t1") -> Task:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    return Task(
        id=task_id,
        list_id=list_id,
        title="Bins",
        status=TaskStatus.TODO,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
    )


# ── The builders ──────────────────────────────────────────────────────


def test_household_post_link():
    assert links.household_post_link("p1") == "/post/p1"
    assert links.household_post_link("p1", "c1") == "/post/p1?comment=c1"


def test_space_post_link():
    assert links.space_post_link("s1", "p1") == "/spaces/s1?post=p1"
    assert links.space_post_link("s1", "p1", "c1") == "/spaces/s1?post=p1&comment=c1"


def test_task_link_household_and_space():
    assert links.task_link(_task()) == "/organize?list=l1&task=t1"
    assert links.task_link(_task(), "s1") == "/spaces/s1/tasks?list=l1&task=t1"


def test_bazaar_listing_link():
    assert links.bazaar_listing_link("L1") == "/bazaar?listing=L1"


def test_app_link_picks_the_app_out_on_the_apps_page():
    """A "new app" row opens the Apps page with the app's entry picked
    out — not ``/apps/{id}``, which launches the app."""
    assert links.app_link("chess") == "/apps?app=chess"
    assert spa_route_for(links.app_link("chess")) == "/apps"


def test_space_links():
    assert links.space_calendar_link("s1") == "/spaces/s1/calendar"
    assert links.space_games_link("s1") == "/spaces/s1/games"
    assert links.space_about_link("s1") == "/spaces/s1/about"
    assert links.space_members_link("s1") == "/spaces/s1/settings/members"
    assert links.space_moderation_link("s1") == "/spaces/s1/moderation"
    assert links.HOUSEHOLD_CALENDAR_LINK == "/calendar"


def test_feed_links_are_the_pages_a_feed_visit_reads():
    """A grouped new-posts row (D5) opens the feed, and the feed's visit
    (``markSeen('/spaces/s1')``) matches it."""
    assert links.space_feed_link("s1") == "/spaces/s1"
    assert links.link_target(links.space_feed_link("s1")) == "/spaces/s1"
    assert links.HOUSEHOLD_FEED_LINK == "/feed"


def test_link_target_drops_only_the_picked_out_part():
    """Viewing a page reads rows that pick a part of it out (a comment, a
    moment reply), never rows for another page under the same path."""
    assert links.link_target("/post/p1?comment=c1") == "/post/p1"
    assert links.link_target("/post/p1") == "/post/p1"
    assert links.link_target("/momentum/m1?reply=r1") == "/momentum/m1"
    assert links.link_target("/spaces/s1?post=p1&comment=c1") == "/spaces/s1?post=p1"
    # The space's feed is not one of its posts.
    assert links.link_target("/spaces/s1?post=p1") != links.link_target("/spaces/s1")
    assert links.link_target("/bazaar?listing=L1") == "/bazaar?listing=L1"


def test_query_values_are_encoded():
    """An id never breaks out of its query parameter."""
    assert links.household_post_link("p1", "a&b=c") == "/post/p1?comment=a%26b%3Dc"


# ── Every link is an SPA route ────────────────────────────────────────


def _spa_routes() -> list[str]:
    text = (CLIENT / "router.ts").read_text(encoding="utf-8")
    return re.findall(r"^\s*'(/[^']*)'\s*:", text, re.MULTILINE)


def _ts_union(path: Path, name: str) -> set[str]:
    text = path.read_text(encoding="utf-8")
    body = re.search(rf"export type {name} =(.*?)\n\n", text, re.DOTALL)
    assert body, f"{name} not found in {path}"
    return set(re.findall(r"'([a-z_-]+)'", body.group(1)))


SPACE_SECTIONS = _ts_union(
    CLIENT / "features" / "spaces" / "spaceTabs.ts", "SpaceSection"
)
SETTINGS_SECTIONS = _ts_union(
    CLIENT / "features" / "spaces" / "spaceSettingsSections.ts", "SettingsSection"
)


def _match(route: str, path: str) -> dict[str, str] | None:
    """preact-iso's matcher: ``:name`` takes one segment, ``:name?`` an
    optional one. Returns the params, or ``None`` when no match."""
    want = [s for s in route.split("/") if s]
    got = [s for s in path.split("/") if s]
    params: dict[str, str] = {}
    for i, seg in enumerate(want):
        if seg.startswith(":"):
            name = seg[1:].rstrip("?")
            if i >= len(got):
                if seg.endswith("?"):
                    continue
                return None
            params[name] = got[i]
        elif i >= len(got) or got[i] != seg:
            return None
    return params if len(got) <= len(want) else None


def spa_route_for(link: str) -> str | None:
    """The first SPA route ``link`` opens (the router's first-match rule),
    or ``None``. A space section must be one the space page knows."""
    path = re.split(r"[?#]", link, maxsplit=1)[0] or "/"
    for route in _spa_routes():
        params = _match(route, path)
        if params is None:
            continue
        if route == "/spaces/:id/:section?" and params.get("section", "feed") not in (
            SPACE_SECTIONS
        ):
            return None
        if (
            route == "/spaces/:id/settings/:section?"
            and params.get("section", "basics") not in SETTINGS_SECTIONS
        ):
            return None
        return route
    return None


def _source_links() -> set[str]:
    """Every literal SPA path in the notification sources, ids filled in."""
    found: set[str] = set()
    for src in SOURCES:
        text = src.read_text(encoding="utf-8")
        for lit in re.findall(r'f?"(/[^"\s]*)"', text):
            found.add(re.sub(r"\{[^}]*\}", "x1", lit))
    return found


def _builder_links() -> set[str]:
    return {
        links.household_post_link("p1"),
        links.household_post_link("p1", "c1"),
        links.space_post_link("s1", "p1"),
        links.space_post_link("s1", "p1", "c1"),
        links.task_link(_task()),
        links.task_link(_task(), "s1"),
        links.bazaar_listing_link("L1"),
        links.app_link("chess"),
        links.space_calendar_link("s1"),
        links.space_games_link("s1"),
        links.space_about_link("s1"),
        links.space_members_link("s1"),
        links.HOUSEHOLD_CALENDAR_LINK,
    }


def test_the_route_table_parses():
    routes = _spa_routes()
    assert "/post/:id" in routes
    assert "/spaces/:id/:section?" in routes
    assert {"feed", "tasks", "calendar", "moderation"} <= SPACE_SECTIONS
    assert "members" in SETTINGS_SECTIONS


@pytest.mark.parametrize("link", sorted(_source_links() | _builder_links()))
def test_every_notification_link_is_an_spa_route(link):
    assert spa_route_for(link) is not None, f"{link} opens no SPA route"


@pytest.mark.parametrize(
    "link",
    ["/tasks/l1", "/bazaar/L1", "/spaces/s1/nowhere", "/spaces/s1/settings/x"],
)
def test_dead_links_are_caught(link):
    """The guard itself: the old dead shapes match no route."""
    assert spa_route_for(link) is None
