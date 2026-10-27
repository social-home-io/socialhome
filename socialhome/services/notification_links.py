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
"""

from __future__ import annotations

from urllib.parse import urlencode

from ..domain.task import Task

#: The household calendar page (personal calendars).
HOUSEHOLD_CALENDAR_LINK = "/calendar"


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


def space_about_link(space_id: str) -> str:
    """The space's landing page — what a non-member can see of it."""
    return f"/spaces/{space_id}/about"


def space_members_link(space_id: str) -> str:
    """The space's settings, Members section (invites and requests)."""
    return f"/spaces/{space_id}/settings/members"
