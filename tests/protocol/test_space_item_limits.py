# ruff: noqa: F811 — ``mesh`` is the shared fixture, injected by name
"""Release-blocker protocol tests: one size limit per kind of space item.

Marked ``@pytest.mark.security``.

A page, a space calendar event and a space's config each have ONE limit,
the same in every space and on every path (owner decision 2026-11-14:
members are equal however their household is reached). The item is
measured WHOLE — its record as it travels, every field — so no field can
grow it past what one relay frame carries. Only what other people add
after it was saved (RSVPs) is held out, under its own hard cap.

* **Where a person saves**: 413 ``SPACE_ITEM_TOO_LARGE`` after the actor
  was authorized, before anything is stored (create and edit; a new space).
* **Where a household receives** (live, a refusal answer, the §25.6 sync):
  an item over the limit AND larger than the copy held is not stored, with
  a WARNING. A config change still applies its settings — only the text
  that grew past the limit is not taken.
* **An item stored before the limit** stays readable; a save that does not
  make it larger passes, everywhere.

Uses the four real applications of ``test_space_moderation_federated``.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.app_keys import federation_service_key, space_repo_key
from socialhome.crypto import generate_space_keypair
from socialhome.domain.calendar import MAX_EVENT_ATTENDEES
from socialhome.domain.emoji import MAX_EMOJI_CODEPOINTS
from socialhome.domain.federation import FederationEvent
from socialhome.domain.relay_media_frame import (
    SPACE_CALENDAR_ITEM_MAX_BYTES,
    SPACE_CONFIG_ITEM_MAX_BYTES,
    SPACE_PAGE_ITEM_MAX_BYTES,
)
from socialhome.domain.space import SPACE_NAME_MAX_CHARS, SpaceFeatureAccess
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.services.space_service import space_metadata_fields
from socialhome.utils.rrule import MAX_RRULE_CHARS

from .test_space_moderation_federated import (  # noqa: F401 — the shared fixture
    FET,
    SID,
    House,
    _event,
    _pages,
    _pump,
    _queue_rows,
    _receive,
    _sends,
    mesh,
)

pytestmark = pytest.mark.security


def _event_body(**extra) -> dict:
    start = datetime.now(UTC) + timedelta(days=3)
    return {
        "summary": "Picnic",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        **extra,
    }


async def _calendar_rows(house: House) -> list[tuple[str, int]]:
    rows = await house.db.fetchall(
        "SELECT summary, length(coalesce(description, '')) AS n"
        " FROM space_calendar_events ORDER BY summary",
        (),
    )
    return [(r["summary"], r["n"]) for r in rows]


async def _space_row(house: House, space_id: str = SID):
    return await house.db.fetchone(
        "SELECT name, emoji, description, about_markdown, calendar_access,"
        " config_sequence FROM spaces WHERE id=?",
        (space_id,),
    )


# ── Where a person saves ──────────────────────────────────────────────────


async def test_a_page_calendar_event_or_description_over_its_limit_is_413(mesh):
    """One limit per kind of item, the same in every space: over it answers
    413 ``SPACE_ITEM_TOO_LARGE`` with the item's limit in kB, and nothing is
    stored. This space has no household behind a GFS — the rule does not
    look."""
    a, h = mesh["A"], mesh["H"]
    big = secrets.token_hex(260 * 1024)  # 520 KiB of text
    host_pages = await _pages(h)
    for method, path, body, limit in (
        (
            "post",
            f"/api/spaces/{SID}/pages",
            {"title": "Big", "content": big},
            SPACE_PAGE_ITEM_MAX_BYTES,
        ),
        (
            "post",
            f"/api/spaces/{SID}/calendar/events",
            _event_body(description=big),
            SPACE_CALENDAR_ITEM_MAX_BYTES,
        ),
        (
            "patch",
            f"/api/spaces/{SID}",
            {"description": big},
            SPACE_CONFIG_ITEM_MAX_BYTES,
        ),
    ):
        r = await getattr(h.tc, method)(path, json=body, headers=h.headers)
        assert r.status == 413, path
        error = (await r.json())["error"]
        assert error["code"] == "SPACE_ITEM_TOO_LARGE", path
        assert error["params"] == {"max_kb": limit // 1000}, path
    assert await _pages(h) == host_pages
    assert await _calendar_rows(h) == []
    # A person without the right gets the 403 they always got.
    r = await a.tc.patch(
        f"/api/spaces/{SID}", json={"description": big}, headers=a.headers
    )
    assert r.status == 403
    # A's pages are reviewed: the queue's own (smaller) cap answers.
    queue_before = await _queue_rows(a)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": big},
        headers=a.headers,
    )
    assert r.status == 413
    assert await _queue_rows(a) == queue_before
    # Under the limit — 450 KiB, more than any JSON relay envelope: taken.
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Fits", "content": secrets.token_hex(225 * 1024)},
        headers=h.headers,
    )
    assert r.status in (200, 201)
    # The owner's description within the limit: taken.
    r = await h.tc.patch(
        f"/api/spaces/{SID}",
        json={"description": "d" * 100_000},
        headers=h.headers,
    )
    assert r.status == 200, (await r.text())[:300]
    assert (await _space_row(h))["description"] == "d" * 100_000


async def test_a_calendar_event_is_measured_whole(mesh):
    """Every field of the event counts — the recurrence rule, the attendee
    ids, the location — not a chosen few: a description that alone is
    under the limit is refused once the rest of the event rides with it."""
    h = mesh["H"]
    url = f"/api/spaces/{SID}/calendar/events"
    rule = "FREQ=WEEKLY;BYDAY=" + ",".join(["MO"] * 100)
    assert len(rule) <= MAX_RRULE_CHARS
    description = "d" * (SPACE_CALENDAR_ITEM_MAX_BYTES - 100)
    r = await h.tc.post(
        url,
        json=_event_body(description=description, rrule=rule, location="L" * 400),
        headers=h.headers,
    )
    assert r.status == 413, (await r.text())[:300]
    assert (await r.json())["error"]["params"] == {
        "max_kb": SPACE_CALENDAR_ITEM_MAX_BYTES // 1000
    }
    assert await _calendar_rows(h) == []
    # The same description with a short rule and no location: taken.
    r = await h.tc.post(
        url,
        json=_event_body(description="d" * (SPACE_CALENDAR_ITEM_MAX_BYTES - 1000)),
        headers=h.headers,
    )
    assert r.status == 201, (await r.text())[:300]
    event_id = (await r.json())["id"]
    # An edit that grows it past the limit through the rule alone: 413.
    r = await h.tc.patch(
        f"{url}/{event_id}",
        json={"rrule": rule, "location": "L" * 500},
        headers=h.headers,
    )
    assert r.status == 413, (await r.text())[:300]
    row = await h.db.fetchone("SELECT rrule FROM space_calendar_events", ())
    assert row["rrule"] is None


@pytest.mark.parametrize(
    "extra",
    [
        # The reviewer's repro: 200,000 empty ids (3 bytes each on the wire).
        {"attendees": [""] * 200_000},
        {"attendees": ["u-1", "  "]},
        {"attendees": [f"{n:032d}" for n in range(MAX_EVENT_ATTENDEES + 1)]},
        {"attendees": "u-1"},
        {"attendees": [7]},
        {"rrule": "FREQ=DAILY;" + "X" * 300_000},
        {"rrule": "FREQ=DAILY;" + "X" * MAX_RRULE_CHARS},
        {"rrule": "NOT A RULE"},
        {"rrule": "FREQ=HOURLY"},
        {"rrule": 5},
    ],
    ids=lambda e: next(iter(e)),
)
async def test_attendees_and_the_rule_are_validated_where_a_person_saves(mesh, extra):
    h = mesh["H"]
    url = f"/api/spaces/{SID}/calendar/events"
    r = await h.tc.post(url, json=_event_body(**extra), headers=h.headers)
    assert r.status == 422, (await r.text())[:300]
    assert await _calendar_rows(h) == []
    # And on an edit.
    r = await h.tc.post(url, json=_event_body(summary="Ok"), headers=h.headers)
    event_id = (await r.json())["id"]
    r = await h.tc.patch(f"{url}/{event_id}", json=extra, headers=h.headers)
    assert r.status == 422, (await r.text())[:300]


async def test_attendee_ids_are_kept_once_each(mesh):
    h = mesh["H"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json=_event_body(attendees=["u-x", "u-y", "u-x"], rrule="FREQ=WEEKLY"),
        headers=h.headers,
    )
    assert r.status == 201, (await r.text())[:300]
    body = await r.json()
    assert body["attendees"] == ["u-x", "u-y"]
    assert body["rrule"] == "FREQ=WEEKLY"


async def test_authorization_comes_before_the_size_check(mesh):
    """A plain member in a space whose calendar only admins write gets the
    403 they always got — never a 413 that tells them about the size first.
    The same for a page under ``admin_only``."""
    a = mesh["A"]
    await a.db.enqueue(
        "UPDATE spaces SET calendar_access='admin_only', pages_access='admin_only'"
        " WHERE id=?",
        (SID,),
    )
    big = "x" * 600_000
    r = await a.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json=_event_body(description=big),
        headers=a.headers,
    )
    assert r.status == 403, (await r.text())[:300]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": big},
        headers=a.headers,
    )
    assert r.status == 403, (await r.text())[:300]
    assert await _calendar_rows(a) == []


async def test_a_new_space_is_held_to_the_config_limit_and_its_caps(mesh):
    """Creating a space checks its config like an edit does: the whole
    config over the limit is 413, a name or an emoji over its own cap is
    422 — and nothing is created."""
    h = mesh["H"]
    count = (await h.db.fetchone("SELECT COUNT(*) AS n FROM spaces", ()))["n"]
    for body, status in (
        ({"name": "Big", "description": "d" * SPACE_CONFIG_ITEM_MAX_BYTES}, 413),
        ({"name": "N" * (SPACE_NAME_MAX_CHARS + 1)}, 422),
        ({"name": "Ok", "emoji": "🏡" * (MAX_EMOJI_CODEPOINTS + 1)}, 422),
    ):
        r = await h.tc.post("/api/spaces", json=body, headers=h.headers)
        assert r.status == status, (body.keys(), (await r.text())[:300])
    assert (await h.db.fetchone("SELECT COUNT(*) AS n FROM spaces", ()))["n"] == count
    # The same caps on an edit.
    for body in (
        {"name": "N" * (SPACE_NAME_MAX_CHARS + 1)},
        {"emoji": "🏡" * (MAX_EMOJI_CODEPOINTS + 1)},
    ):
        r = await h.tc.patch(f"/api/spaces/{SID}", json=body, headers=h.headers)
        assert r.status == 422, (await r.text())[:300]
    assert (await _space_row(h))["name"] == "Club"
    # Within every cap: created.
    r = await h.tc.post(
        "/api/spaces",
        json={
            "name": "N" * SPACE_NAME_MAX_CHARS,
            "emoji": "🏡",
            "description": "d" * 100_000,
        },
        headers=h.headers,
    )
    assert r.status in (200, 201), (await r.text())[:300]


# ── Where a household receives ────────────────────────────────────────────


async def test_an_over_limit_calendar_event_from_a_member_is_not_stored(mesh, caplog):
    """An honest household never sends a calendar event over the limit (its
    own save answers 413), so a receiver needs no answer for one: a live
    event over the limit and larger than the copy it holds is not stored,
    with a WARNING — whichever field makes it large. Within the limit, the
    same household's edit is taken."""
    h, c, d = mesh["H"], mesh["C"], mesh["D"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json=_event_body(description="Bring cake"),
        headers=h.headers,
    )
    assert r.status in (200, 201)
    ((_to, created),) = _sends(h, FET.SPACE_CALENDAR_EVENT_CREATED)[:1]
    await _pump(mesh)
    assert await _calendar_rows(c) == [("Picnic", 10)]
    big = "x" * SPACE_CALENDAR_ITEM_MAX_BYTES
    caplog.set_level(logging.WARNING)
    for house in (c, d):
        await _receive(
            house,
            _event(
                h,
                FET.SPACE_CALENDAR_EVENT_UPDATED,
                {**created, "description": big},
                house,
            ),
        )
        assert await _calendar_rows(house) == [("Picnic", 10)]
    assert "over the limit" in caplog.text
    # A field the old text sum skipped makes it large: the location.
    await _receive(
        c,
        _event(
            h,
            FET.SPACE_CALENDAR_EVENT_UPDATED,
            {
                **created,
                "description": "x" * (SPACE_CALENDAR_ITEM_MAX_BYTES - 200),
                "location": "L" * 400,
            },
            c,
        ),
    )
    assert await _calendar_rows(c) == [("Picnic", 10)]
    # Invalid lists are refused on the way in, as where a person saves.
    for bad in (
        {"attendees": [""] * 1000},
        {"rrule": "FREQ=DAILY;" + "X" * MAX_RRULE_CHARS},
    ):
        await _receive(
            c,
            _event(
                h,
                FET.SPACE_CALENDAR_EVENT_UPDATED,
                {**created, **bad, "summary": "Bad"},
                c,
            ),
        )
        assert await _calendar_rows(c) == [("Picnic", 10)]
    # Within the limit: applied.
    await _receive(
        c,
        _event(
            h,
            FET.SPACE_CALENDAR_EVENT_UPDATED,
            {**created, "description": "y" * 300_000},
            c,
        ),
    )
    assert await _calendar_rows(c) == [("Picnic", 300_000)]


async def test_a_calendar_event_stored_before_the_limit_keeps_arriving(mesh):
    """C holds an event saved before the limit, larger than it. An edit that
    does not make it larger is taken; one that grows it is not."""
    h, c = mesh["H"], mesh["C"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json=_event_body(description="small"),
        headers=h.headers,
    )
    assert r.status in (200, 201)
    ((_to, created),) = _sends(h, FET.SPACE_CALENDAR_EVENT_CREATED)[:1]
    await _pump(mesh)
    old = "o" * (SPACE_CALENDAR_ITEM_MAX_BYTES + 10_000)
    await c.db.enqueue("UPDATE space_calendar_events SET description=?", (old,))
    same_size = {**created, "description": old, "summary": "Picnix"}
    await _receive(c, _event(h, FET.SPACE_CALENDAR_EVENT_UPDATED, same_size, c))
    assert await _calendar_rows(c) == [("Picnix", len(old))]
    grown = {**created, "description": old + "o", "summary": "Picnix"}
    await _receive(c, _event(h, FET.SPACE_CALENDAR_EVENT_UPDATED, grown, c))
    assert await _calendar_rows(c) == [("Picnix", len(old))]


def _signed(key, c: House, meta: dict, sequence: int) -> dict:
    meta = {**meta, "config_sequence": sequence, "config_author_instance": c.iid}
    meta.update(
        sign_authority_event(
            event_type="space_config_changed",
            space_id=SID,
            payload=strip_authority_sig_fields(meta),
            space_seed=key.private_key,
        )
    )
    return {"space_id": SID, "sequence": sequence, "space_meta": meta}


async def test_a_config_change_applies_its_settings_without_an_over_limit_text(
    mesh, caplog
):
    """A receiver never drops a config change's settings for a size reason:
    an access level riding with a description over the limit is applied —
    otherwise a member household would keep enforcing a stale access level —
    and only the text that grew past the limit is not taken (the held text
    stays, WARNING). Owner push and delegated admin's signed edit alike."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    key = generate_space_keypair()
    await a.db.enqueue(
        "UPDATE spaces SET identity_public_key=?, description='held' WHERE id=?",
        (key.public_key.hex(), SID),
    )
    space = await a.app[space_repo_key].get(SID)
    big = "x" * SPACE_CONFIG_ITEM_MAX_BYTES
    caplog.set_level(logging.WARNING)
    # The owner's own push, over the limit, with a stricter calendar.
    meta = space_metadata_fields(
        replace(
            space,
            description=big,
            config_sequence=50,
            features=replace(
                space.features, calendar_access=SpaceFeatureAccess.ADMIN_ONLY
            ),
        )
    )
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_CONFIG_CHANGED,
            {"space_id": SID, "sequence": 50, "space_meta": meta},
            a,
        ),
    )
    row = await _space_row(a)
    assert (row["description"], row["calendar_access"], row["config_sequence"]) == (
        "held",
        "admin_only",
        50,
    )
    assert "over the limit" in caplog.text
    # A delegated admin's household, validly signed, over the limit: its
    # rename and its access level apply, its description does not.
    meta = space_metadata_fields(
        replace(
            space,
            name="Renamed",
            description=big,
            features=replace(space.features, pages_access=SpaceFeatureAccess.OPEN),
        )
    )
    await _receive(a, _event(c, FET.SPACE_CONFIG_CHANGED, _signed(key, c, meta, 60), a))
    row = await _space_row(a)
    assert (row["name"], row["description"], row["config_sequence"]) == (
        "Renamed",
        "held",
        60,
    )
    pages = await a.db.fetchone("SELECT pages_access FROM spaces WHERE id=?", (SID,))
    assert pages["pages_access"] == "open"
    # The same within the limit: the text is taken too.
    meta = space_metadata_fields(replace(space, description="short"))
    await _receive(a, _event(c, FET.SPACE_CONFIG_CHANGED, _signed(key, c, meta, 70), a))
    assert (await _space_row(a))["description"] == "short"


async def test_a_config_stored_before_the_limit_keeps_arriving(mesh):
    """A holds a description saved before the limit, larger than it: the
    owner's push that does not make the config larger is taken whole; one
    that grows it keeps the held text and applies the rest."""
    a, h = mesh["A"], mesh["H"]
    old = "o" * (SPACE_CONFIG_ITEM_MAX_BYTES + 10_000)
    await a.db.enqueue("UPDATE spaces SET description=? WHERE id=?", (old, SID))
    space = await a.app[space_repo_key].get(SID)
    meta = space_metadata_fields(replace(space, name="Clux", config_sequence=50))
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_CONFIG_CHANGED,
            {"space_id": SID, "sequence": 50, "space_meta": meta},
            a,
        ),
    )
    row = await _space_row(a)
    assert (row["name"], len(row["description"])) == ("Clux", len(old))
    meta = space_metadata_fields(
        replace(space, name="Cluy", description=old + "o", config_sequence=51)
    )
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_CONFIG_CHANGED,
            {"space_id": SID, "sequence": 51, "space_meta": meta},
            a,
        ),
    )
    row = await _space_row(a)
    assert (row["name"], row["description"], row["config_sequence"]) == (
        "Cluy",
        old,
        51,
    )


async def test_an_over_limit_space_created_is_seated_without_its_text(mesh, caplog):
    """``SPACE_CREATED`` is judged like any config: over the limit, the
    space is seated (its settings are not dropped) without the text that
    made it too large."""
    h, d = mesh["H"], mesh["D"]
    new = "sp-created-big"
    caplog.set_level(logging.WARNING)
    for space_id, description in (
        (new, "x" * SPACE_CONFIG_ITEM_MAX_BYTES),
        (new + "-ok", "fine"),
    ):
        event = FederationEvent(
            msg_id=f"m-created-{space_id}",
            event_type=FET.SPACE_CREATED,
            from_instance=h.iid,
            to_instance=d.iid,
            timestamp=datetime.now(UTC).isoformat(),
            payload={
                "space_id": space_id,
                "name": "Big",
                "identity_public_key": "cd" * 32,
                "join_mode": "open",
                "description": description,
            },
            space_id=space_id,
        )
        registry = d.app[federation_service_key]._event_registry
        for handler in registry.handlers_for(FET.SPACE_CREATED):
            await handler(event)
    row = await _space_row(d, new)
    assert (row["name"], row["description"]) == ("Big", None)
    assert "over the limit" in caplog.text
    assert (await _space_row(d, new + "-ok"))["description"] == "fine"


async def test_an_over_limit_page_version_from_the_host_is_not_stored(mesh):
    """The host never sequences a page over the limit; a live version that
    is over it and larger than the copy held is not stored on a member — a
    version within the limit from the same host is mirrored."""
    h, d = mesh["H"], mesh["D"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Plan", "content": "short"},
        headers=h.headers,
    )
    assert r.status in (200, 201)
    ((_to, created),) = _sends(h, FET.SPACE_PAGE_CREATED)[:1]
    big = {**created, "content": "x" * SPACE_PAGE_ITEM_MAX_BYTES}
    await _receive(d, _event(h, FET.SPACE_PAGE_CREATED, big, d))
    assert await _pages(d) == []
    await _receive(d, _event(h, FET.SPACE_PAGE_CREATED, created, d))
    assert [content for _title, content in await _pages(d)] == ["short"]
    # …and an edit that grows the held page past the limit.
    await _receive(d, _event(h, FET.SPACE_PAGE_UPDATED, {**big, "seq": 2}, d))
    assert [content for _title, content in await _pages(d)] == ["short"]
