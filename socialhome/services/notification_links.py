"""SPA paths a notification opens — one place, so every bell, push and
HA event names a page that exists and shows the exact thing.

Each path is relative to the SPA's ``<base href>`` (the SPA's
``appHref`` / the service worker's ``registration.scope`` add the
ingress prefix). Every shape here must match a route in
``client/src/router.ts``; ``tests/services/test_notification_links.py``
checks that against the route table itself.

Deep links into a page ride the query string, the same way a moment
reply does (``/momentum/{id}?reply={reply_id}``):

* ``/post/{id}?comment={cid}`` — a household feed post, the comment
  picked out.
* ``/spaces/{id}?post={pid}&comment={cid}`` — a space post in the
  space's Feed section.
* ``…?list={list_id}&task={task_id}`` — the task's list, the task
  picked out (household: Organize; space: the Tasks section).
* ``/bazaar?listing={post_id}`` — the listing opened.
* ``/apps?app={app_id}`` — the app's entry on the Apps page.
* ``/apps/{app_id}/about`` — the app's detail page (a request for it).
* ``/apps/{app_id}/tables/{session_id}`` — a group game's "Who's in"
  page (an invite to it, someone joining it).
* ``/apps/{app_id}?session={session_id}`` — the app opened on one game.

A grouped "new posts" row (D5) opens the feed itself: ``/feed`` for the
household, ``/spaces/{id}`` for a space.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from ..domain.task import Task

#: The household calendar page (personal calendars).
HOUSEHOLD_CALENDAR_LINK = "/calendar"

#: The household feed — where a grouped "new posts" row leads (D5).
HOUSEHOLD_FEED_LINK = "/feed"

#: Query keys that pick a part *inside* the page out (a comment, a moment
#: reply) — the page itself, and so what a visit reads, is the same.
_PICK_OUT_PARAMS: frozenset[str] = frozenset({"comment", "reply"})


def _with_query(path: str, **params: str | None) -> str:
    """``path`` plus the set ``params`` as a query string (in order)."""
    query = urlencode({k: v for k, v in params.items() if v})
    return f"{path}?{query}" if query else path


def household_post_link(post_id: str, comment_id: str | None = None) -> str:
    """A household feed post — its permalink page, a comment picked out."""
    return _with_query(f"/post/{post_id}", comment=comment_id)


def space_post_link(space_id: str, post_id: str, comment_id: str | None = None) -> str:
    """A space post, shown in the space's Feed — a comment picked out."""
    return _with_query(f"/spaces/{space_id}", post=post_id, comment=comment_id)


def space_feed_link(space_id: str) -> str:
    """A space's Feed itself — where a grouped "new posts" row leads (D5);
    the SPA reads it as seen once the feed list renders."""
    return f"/spaces/{space_id}"


def task_link(task: Task, space_id: str | None = None) -> str:
    """The task's list with the task picked out: Organize for a
    household task, the space's Tasks section for a space task."""
    path = f"/spaces/{space_id}/tasks" if space_id else "/organize"
    return _with_query(path, list=task.list_id, task=task.id)


def bazaar_listing_link(listing_post_id: str) -> str:
    """The Bazaar with the listing opened."""
    return _with_query("/bazaar", listing=listing_post_id)


def space_calendar_link(space_id: str) -> str:
    """The space's Calendar section."""
    return f"/spaces/{space_id}/calendar"


def space_games_link(space_id: str) -> str:
    """The space's Games section (group play, v_77)."""
    return f"/spaces/{space_id}/games"


def space_about_link(space_id: str) -> str:
    """The space's landing page — what a non-member can see of it."""
    return f"/spaces/{space_id}/about"


def space_moderation_link(space_id: str) -> str:
    """The space's review queue (join requests, posts, reports)."""
    return f"/spaces/{space_id}/moderation"


def space_members_link(space_id: str) -> str:
    """The space's settings, Members section (invites and requests)."""
    return f"/spaces/{space_id}/settings/members"


def app_link(app_id: str) -> str:
    """The Apps page with the app's entry picked out."""
    return _with_query("/apps", app=app_id)


def app_about_link(app_id: str) -> str:
    """The app's detail page — what it can do, and where it is added."""
    return f"/apps/{app_id}/about"


def app_table_link(app_id: str, session_id: str) -> str:
    """A group game's page before it starts — who's in, Join, Start
    (v_76)."""
    return f"/apps/{app_id}/tables/{session_id}"


def app_game_link(app_id: str, session_id: str) -> str:
    """The app opened on one game (a group game that started)."""
    return _with_query(f"/apps/{app_id}", session=session_id)


def app_draw_link(app_id: str, session_id: str) -> str:
    """Social Home's own gift-draw result sheet (v_78) — never the app."""
    return f"/apps/{app_id}/draw/{session_id}"


def link_target(link: str) -> str:
    """The page ``link`` opens, without the part it picks out.

    ``/post/p?comment=c`` and ``/post/p`` are the same page, so viewing
    the post reads both; ``/spaces/s?post=p`` is not the space's feed
    (``/spaces/s``) — that post is a page of its own. Clearing a
    notification by visit compares these targets.
    """
    parts = urlsplit(link)
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k not in _PICK_OUT_PARAMS]
    return f"{parts.path}?{urlencode(kept)}" if kept else parts.path
