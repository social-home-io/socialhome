"""Release-blocker protocol tests: ``MODERATED`` holds on a host-only space
(§4.3, the local moderation queue).

Marked ``@pytest.mark.security``.

The access matrix of ``test_space_content_access.py`` (ADMIN_ONLY on every
receiver) gets its MODERATED rows here, for a space hosted by this
household (federated moderation, v_43, has its own suite in
``test_space_moderation_federated.py``):

* a member's create → 202 queued, and NO row in the content table;
* a member's edit of their own item → proceeds;
* a member's edit / delete of somebody else's item → 202 queued, the row
  unchanged;
* a moderator (content authority) → proceeds;
* a space with a remote member household → queued the same way (v_43
  federated moderation), never silently applied.

Every case runs against the REAL application over a real SQLite database
and compares a snapshot of every content table.
"""

from __future__ import annotations

import pytest

from tests.routes import conftest as routes_conftest
from tests.routes import test_space_content_access as access
from tests.routes.test_space_content_access import (
    SID,
    _seed_content,
    _send,
    _set_access,
    _snapshot,
    _writes,
)

#: The REST suite's real app (standalone, a token-holding admin) and its
#: seeded host-only space (owner, admin, moderator, member).
client = routes_conftest.client
space = access.space

pytestmark = pytest.mark.security

FEATURES = ("posts", "pages", "tasks", "stickies", "calendar")
#: Position-only writes are LAYOUT: never queued.
_LAYOUT = ({"position": 3}, {"position_x": 40})


def _cases(ids: dict, feature: str, kind: str):
    """The feature's writes of one kind: ``create`` / ``edit`` / ``delete``."""
    for feat, method, path, body in _writes(ids):
        if feat != feature or "resolve-conflict" in path:
            continue
        if body in _LAYOUT or path.endswith("/reorder"):
            continue
        is_create = method == "post" and not path.endswith("/archive")
        is_delete = method == "delete" or path.endswith("/archive")
        got = "create" if is_create else "delete" if is_delete else "edit"
        if got == kind:
            yield method, path, body


@pytest.mark.parametrize("feature", FEATURES)
async def test_a_members_create_queues_and_writes_no_row(client, space, feature):
    ids = await _seed_content(client, {"mem": space["owner"]})
    await _set_access(client, feature, "moderated")
    for method, path, body in _cases(ids, feature, "create"):
        before = await _snapshot(client)
        r = await _send(client, method, path, body, space["mem"])
        assert r.status == 202, (path, await r.text())
        assert (await r.json())["queued"] is True
        assert await _snapshot(client) == before, path


@pytest.mark.parametrize("feature", ["pages", "tasks", "stickies", "calendar"])
async def test_a_members_edit_and_delete_of_others_items_queue(client, space, feature):
    ids = await _seed_content(client, {"mem": space["owner"]})
    await _set_access(client, feature, "moderated")
    for kind in ("edit", "delete"):
        for method, path, body in _cases(ids, feature, kind):
            before = await _snapshot(client)
            r = await _send(client, method, path, body, space["mem"])
            assert r.status == 202, (kind, path, await r.text())
            assert await _snapshot(client) == before, path


@pytest.mark.parametrize("feature", FEATURES)
async def test_a_members_edit_of_their_own_item_proceeds(client, space, feature):
    ids = await _seed_content(client, space)
    await _set_access(client, feature, "moderated")
    for method, path, body in _cases(ids, feature, "edit"):
        before = await _snapshot(client)
        r = await _send(client, method, path, body, space["mem"])
        assert r.status in (200, 201), (path, await r.text())
        assert await _snapshot(client) != before, path


@pytest.mark.parametrize("feature", FEATURES)
async def test_a_moderator_proceeds(client, space, feature):
    ids = await _seed_content(client, {"mem": space["owner"]})
    await _set_access(client, feature, "moderated")
    for kind in ("create", "edit"):
        for method, path, body in _cases(ids, feature, kind):
            r = await _send(client, method, path, body, space["mod"])
            assert r.status in (200, 201), (kind, path, await r.text())
    r = await client.get(f"/api/spaces/{SID}/moderation", headers=space["mod"])
    assert await r.json() == []


@pytest.mark.parametrize("feature", FEATURES)
async def test_a_space_with_remote_households_still_queues_never_writes(
    client, space, feature
):
    """v_43: a host with remote member households holds a member's item
    like any other (202 queued) — the queue federates now, and nothing
    reaches the content table before review."""
    ids = await _seed_content(client, {"mem": space["owner"]})
    await _set_access(client, feature, "moderated")
    await client._db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?, 'inst-remote')",
        (SID,),
    )
    # Somebody else's post is content authority's to change, never queued.
    kinds = ("create",) if feature == "posts" else ("create", "edit", "delete")
    for kind in kinds:
        for method, path, body in _cases(ids, feature, kind):
            before = await _snapshot(client)
            r = await _send(client, method, path, body, space["mem"])
            assert r.status == 202, (kind, path, await r.text())
            assert await _snapshot(client) == before, path
