"""A post that reaches a household AGAIN — a re-sent ``SPACE_POST_CREATED``
(a ``SPACE_SYNC_RESUME`` replay) or the host's §25.6 ``posts`` record — is
merged into the row held, never laid over it.

* **Last writer wins on ``edited_at``.** An older copy (a provider that
  missed the edit) never replaces the content of a newer edit held here,
  and a copy carrying a newer edit brings it. Whatever happened, the next
  edit still lands.
* **A calendar event's feed card is one per household.** Every household's
  bridge mints its own; the host's copy in the stream is stored linked only
  where no card for the event is held, so the bridge never mints a second.
* **Fields the copy does not own stay** (the event / highlight link, the
  bot, the "no link card" choice).
* A live post's file attachment is stored like the stream's.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.app_keys import space_sync_receiver_key
from socialhome.domain.calendar import CalendarEvent, calendar_event_to_wire_dict
from socialhome.domain.federation import FederationEventType as FET
from socialhome.federation.owner_bound_id import (
    SPACE_CALENDAR_EVENT_KIND,
    mint_owner_bound_id,
)
from socialhome.services.inbound_media_store import space_direct_media_name
from tests.protocol.test_content_file_hashes import _media, env
from tests.protocol.test_space_content_authorship import (
    AUTHOR,
    HOST,
    OTHER,
    SP,
    _deliver,
)

pytestmark = pytest.mark.security

__all__ = ["env"]

TEN = "2026-06-01T10:00:00+00:00"
NINE = "2026-06-01T09:00:00+00:00"
THREE_PM = "2026-06-01T15:00:00+00:00"


def _create(content: str, **extra) -> dict:
    return {"id": "p-1", "author": "u-a", "type": "text", "content": content, **extra}


def _record(content: str, **extra) -> dict:
    return {
        "id": "p-1",
        "author": "u-a",
        "type": "text",
        "content": content,
        "created_at": "2026-06-01T08:00:00+00:00",
        **extra,
    }


async def _edit(app, content: str, at: str) -> None:
    await _deliver(
        app,
        FET.SPACE_POST_UPDATED,
        {"id": "p-1", "content": content, "edited_at": at},
        sender=AUTHOR,
    )


async def _stream(app, *records: dict, provider: str = HOST) -> None:
    await app[space_sync_receiver_key]._dispatch(
        "posts", SP, list(records), provider=provider
    )


async def _content(db, post_id: str = "p-1") -> str | None:
    row = await db.fetchone("SELECT content FROM space_posts WHERE id=?", (post_id,))
    return None if row is None else row["content"]


# ── Last writer wins on edited_at ─────────────────────────────────────────


@pytest.mark.parametrize(
    "stale",
    [{}, {"edited_at": NINE}],
    ids=["never-edited copy", "older edit"],
)
async def test_a_stale_resent_create_never_replaces_a_newer_edit(env, stale):
    """A household that was offline for the edit replays its old copy. It
    once left the OLD words under the NEWER stamp — and the re-delivered
    edit was then refused as a duplicate: stuck."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _edit(app, "v2", TEN)
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1", **stale), sender=AUTHOR)
    assert await _content(db) == "v2"
    await _edit(app, "v3", THREE_PM)
    assert await _content(db) == "v3"


async def test_a_resent_create_brings_an_edit_this_household_missed(env):
    """The other order: we hold the first words, the replayed copy carries
    the edit — it lands with its stamp, and an older edit is then stale."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _deliver(
        app, FET.SPACE_POST_CREATED, _create("v2", edited_at=TEN), sender=AUTHOR
    )
    assert await _content(db) == "v2"
    row = await db.fetchone("SELECT edited_at FROM space_posts WHERE id='p-1'")
    assert row["edited_at"] == "2026-06-01 10:00:00.000000"
    await _edit(app, "late duplicate", NINE)
    assert await _content(db) == "v2"
    await _edit(app, "v3", THREE_PM)
    assert await _content(db) == "v3"


@pytest.mark.parametrize(
    "stale",
    [{}, {"edited_at": "2026-06-01T09:00:00"}],
    ids=["never-edited record", "older edit"],
)
async def test_a_stale_host_record_never_replaces_a_newer_edit(env, stale):
    """§25.6: the host missed the edit and streams its old copy."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _edit(app, "v2", TEN)
    await _stream(app, _record("v1", **stale))
    assert await _content(db) == "v2"


async def test_an_edit_after_the_hosts_record_the_same_day_still_lands(env):
    """§25.6: the host's record carries the edit (its stamp naive UTC, as
    stored there). It lands — and so does an edit later the same UTC day
    (the stamp once landed as ``…T…``, which sorted after every one)."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _stream(app, _record("v2", edited_at="2026-06-01 10:00:00"))
    assert await _content(db) == "v2"
    await _edit(app, "late duplicate", NINE)
    assert await _content(db) == "v2"
    await _edit(app, "v3", THREE_PM)
    assert await _content(db) == "v3"


# ── Fields the copy does not own ──────────────────────────────────────────


async def test_the_hosts_record_keeps_what_it_does_not_carry(env):
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await db.enqueue(
        "UPDATE space_posts SET no_link_preview=1, linked_highlight_id='hl-1'"
        " WHERE id='p-1'"
    )
    await _stream(app, _record("v1"))
    row = await db.fetchone(
        "SELECT no_link_preview, linked_highlight_id FROM space_posts WHERE id='p-1'"
    )
    assert (row["no_link_preview"], row["linked_highlight_id"]) == (1, "hl-1")


# ── A calendar event's feed card: one per household ───────────────────────


def _event_id() -> str:
    return mint_owner_bound_id(
        SPACE_CALENDAR_EVENT_KIND, space_id=SP, owner_user_id="u-a"
    )


async def _announce(app, event_id: str) -> None:
    """u-a's household announces a calendar event (the live event): our
    bridge mints our own feed card for it."""
    start = datetime.now(UTC) + timedelta(days=30)
    await _deliver(
        app,
        FET.SPACE_CALENDAR_EVENT_CREATED,
        calendar_event_to_wire_dict(
            CalendarEvent(
                id=event_id,
                calendar_id=SP,
                summary="Street party",
                start=start,
                end=start + timedelta(hours=2),
                created_by="u-a",
                announce_in_feed=True,
            ),
            SP,
        ),
        sender=AUTHOR,
    )


def _card(event_id: str, post_id: str = "p-host-card") -> dict:
    return {
        "id": post_id,
        "author": "u-a",
        "type": "event",
        "content": "Street party",
        "created_at": "2026-06-01T08:00:00+00:00",
        "linked_event_id": event_id,
    }


async def _cards(db, event_id: str) -> list[tuple[str, str | None]]:
    rows = await db.fetchall(
        "SELECT id, linked_event_id FROM space_posts WHERE type='event'"
        " AND deleted=0 AND (linked_event_id=? OR content='Street party')",
        (event_id,),
    )
    return [(r["id"], r["linked_event_id"]) for r in rows]


async def test_the_hosts_card_is_not_a_second_card_next_to_ours(env):
    """We hold the event and our bridge's card; the host streams ITS card
    (another id). It once landed unlinked: two cards for one event."""
    app, db = env
    event_id = _event_id()
    await _announce(app, event_id)
    ours = await _cards(db, event_id)
    assert len(ours) == 1 and ours[0][1] == event_id
    await _stream(app, _card(event_id))
    assert await _cards(db, event_id) == ours


async def test_the_hosts_card_lands_linked_so_the_bridge_mints_no_second(env):
    """A newcomer gets the host's card from the stream before the event:
    stored LINKED, so when the event arrives the bridge finds the card."""
    app, db = env
    event_id = _event_id()
    await _stream(app, _card(event_id))
    assert await _cards(db, event_id) == [("p-host-card", event_id)]
    await _announce(app, event_id)
    assert await _cards(db, event_id) == [("p-host-card", event_id)]
    # A later stream keeps the link on the card held.
    await _stream(app, _card(event_id))
    assert await _cards(db, event_id) == [("p-host-card", event_id)]


async def test_a_member_households_card_is_never_taken_from_its_stream(env):
    """Only the host's card stands in for one we lack: a member household's
    own card (a helper's stream of its people's rows) is dropped."""
    app, db = env
    event_id = _event_id()
    await _stream(app, _card(event_id, "p-member-card"), provider=AUTHOR)
    assert await _cards(db, event_id) == []


async def test_a_card_linked_to_another_spaces_event_is_refused(env):
    app, db = env
    event_id = mint_owner_bound_id(
        SPACE_CALENDAR_EVENT_KIND, space_id="sp-elsewhere", owner_user_id="u-a"
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-elsewhere', 'E', ?, 'x', ?)",
        (HOST, "cc" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES(?, 'sp-elsewhere', 'Elsewhere',"
        " '2027-01-01T10:00:00+00:00', '2027-01-01T11:00:00+00:00', 'u-a')",
        (event_id,),
    )
    await _stream(app, _card(event_id))
    assert await _cards(db, event_id) == []


# ── A live post's file attachment ─────────────────────────────────────────


async def test_a_live_posts_file_attachment_is_stored_and_its_bytes_land(env, tmp_dir):
    """The live receiver once read no ``file_meta`` at all: the attachment
    showed only after the host's §25.6 stream, and the bytes its author
    pushed were refused. It is stored (through the same sanitiser as the
    stream's) and kept by a re-sent create that carries none."""
    app, db = env
    meta = {
        "url": "api/media/clip.webm",
        "mime_type": "video/webm",
        "original_name": "clip.webm",
        "size_bytes": 4,
    }
    post = {"id": "p-1", "author": "u-a", "type": "file", "content": "see"}
    await _deliver(
        app, FET.SPACE_POST_CREATED, {**post, "file_meta": meta}, sender=AUTHOR
    )
    local = space_direct_media_name(SP, AUTHOR, "clip.webm")
    row = await db.fetchone("SELECT file_meta_json FROM space_posts WHERE id='p-1'")
    assert local in (row["file_meta_json"] or "")
    assert "clip.webm" in row["file_meta_json"]
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        {
            "correlation_id": "p-1",
            "filename": "clip.webm",
            "bytes_b64": base64.b64encode(b"webm").decode(),
        },
        sender=AUTHOR,
    )
    assert (_media(tmp_dir) / local).read_bytes() == b"webm"
    await _deliver(app, FET.SPACE_POST_CREATED, post, sender=AUTHOR)
    row = await db.fetchone("SELECT file_meta_json FROM space_posts WHERE id='p-1'")
    assert local in (row["file_meta_json"] or "")
    # A reserved (DM) name is never a space file: the attachment is dropped.
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {
            **post,
            "id": "p-2",
            "file_meta": {**meta, "url": "api/media/dm-stolen.webm"},
        },
        sender=AUTHOR,
    )
    row = await db.fetchone("SELECT file_meta_json FROM space_posts WHERE id='p-2'")
    assert row is not None and row["file_meta_json"] is None


async def test_other_is_not_the_author(env):
    """Control: the merge rules above are reached only past the authorship
    gate — another member household's re-sent create changes nothing."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _deliver(
        app, FET.SPACE_POST_CREATED, _create("hijack", edited_at=TEN), sender=OTHER
    )
    assert await _content(db) == "v1"


# ── Hostile shapes ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"size_bytes": None},
        {"size_bytes": [1]},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"mime_type": None},
        {"original_name": 7},
    ],
    ids=str,
)
@pytest.mark.parametrize("path", ["live", "stream"])
async def test_a_malformed_file_attachment_never_reaches_the_row(env, bad, path):
    """A stored ``file_meta`` with a wrong-typed field made every read of
    the row raise — the feed of the whole space. The post lands without
    the attachment, and reads back."""
    app, db = env
    meta = {
        "url": "api/media/clip.webm",
        "mime_type": "video/webm",
        "original_name": "clip.webm",
        "size_bytes": 4,
        **bad,
    }
    if path == "live":
        await _deliver(
            app,
            FET.SPACE_POST_CREATED,
            {**_create("see"), "type": "file", "file_meta": meta},
            sender=AUTHOR,
        )
    else:
        await _stream(app, {**_record("see"), "type": "file", "file_meta": meta})
    row = await db.fetchone("SELECT file_meta_json FROM space_posts WHERE id='p-1'")
    assert row is not None and row["file_meta_json"] is None
    held = await app[space_sync_receiver_key]._space_post_repo.get("p-1")
    assert held is not None and held[1].file_meta is None


async def test_a_far_future_edit_stamp_on_a_create_freezes_nothing(env):
    """A stamp years ahead would beat every later edit: it is not taken
    (the live skew bound), and the author's next edit lands."""
    app, db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _create("v1"), sender=AUTHOR)
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        _create("v1", edited_at="2999-01-01T00:00:00+00:00"),
        sender=AUTHOR,
    )
    row = await db.fetchone("SELECT edited_at FROM space_posts WHERE id='p-1'")
    assert row["edited_at"] is None
    await _edit(app, "v2", TEN)
    assert await _content(db) == "v2"


@pytest.mark.parametrize(
    "event_id",
    [
        mint_owner_bound_id(
            SPACE_CALENDAR_EVENT_KIND, space_id="sp-elsewhere", owner_user_id="u-a"
        ),
        mint_owner_bound_id(
            SPACE_CALENDAR_EVENT_KIND, space_id=SP, owner_user_id="u-somebody-else"
        ),
        "legacy-event-id",
    ],
    ids=["bound to another space", "bound to another creator", "legacy, not held"],
)
async def test_a_card_for_an_event_not_held_must_prove_it_is_this_spaces(env, event_id):
    """``posts`` streams before ``calendar``: on a first sync the event is
    never held yet. The link is then taken only on the id's own proof — it
    is bound to the card's author in THIS space — or a host could plant a
    card that another space's event would later write its words into."""
    app, db = env
    await _stream(app, _card(event_id))
    assert await _cards(db, event_id) == []
