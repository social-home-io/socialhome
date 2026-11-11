"""§25.6 item state — votes, answers, picks, RSVPs, reactions, comment
edits, timetable deletes and bazaar bids heal from the host without ever
freezing, outgrowing a chunk, or reverting a newer answer (v_92).

The generic one-row-per-type table lives in :mod:`.test_sync_coverage`;
these tests reuse its two real households (``homes``) and seed helpers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from socialhome.domain.calendar import CalendarEvent, CalendarRSVP
from socialhome.domain.events import PollCreated
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import (
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Comment,
    CommentType,
    PostType,
)
from socialhome.domain.space import (
    SpaceMember,
    SpaceRole,
)
from socialhome.domain.space_item import stamp_to_db
from socialhome.federation.sync.space.exporter import (
    RESOURCE_ORDER,
    collect_batches,
    record_batches,
)
from socialhome.federation.sync.space.exporters import polls as polls_exporter_mod
from socialhome.federation.sync.space.exporters import posts as posts_exporter_mod
from socialhome.federation.sync.space.exporters import (
    schedules as schedules_exporter_mod,
)
from socialhome.federation.sync.space.provider import SpaceSyncService
from tests.federation.sync.space.test_sync_coverage import (
    _OCC,
    ANNA,
    BOB,
    CARL,
    HOST,
    SPACE,
    T0,
    Home,
    _bid,
    _change_rsvps,
    _delete_timetable,
    _edit_comment,
    _event,
    _event_with_rsvps,
    _listing,
    _poll,
    _post,
    _react,
    _schedule,
    _stream,
    _timetable,
    _view_comment,
    _view_poll,
    _view_post,
    _view_rsvps,
    _view_schedule,
    _view_timetable,
    _vote_and_close,
    homes,
)

__all__ = ["homes"]


# ── v_92 item state: whose rows a stream may change ──────────────────────


async def test_our_own_peoples_votes_answers_and_rsvps_stay_ours(homes):
    """The host's snapshot replaces everybody's votes, answers and RSVPs but
    this household's own people's: theirs are authored here, and one the
    host has not heard yet must not be wiped by its older snapshot."""
    host, member = homes
    await _poll(host)
    await _schedule_on_other_post(host)
    await _event(host)
    await _stream(host, member)
    await member.repos.space_poll.set_votes_in_space(
        space_id=SPACE, post_id="post-1", option_ids=["opt-b"], voter_user_id=BOB
    )
    await member.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-9", user_id=BOB, response="yes"
    )
    await member.repos.space_cal.upsert_rsvp(
        CalendarRSVP(
            event_id="ev-1",
            user_id=BOB,
            status="going",
            updated_at="2026-11-02T10:00:00+00:00",
            occurrence_at=_OCC,
        ),
        space_id=SPACE,
    )
    await _stream(host, member)
    assert {"option_id": "opt-b", "voter_user_id": BOB} in (
        await member.repos.space_poll.list_votes("post-1")
    )
    answers = await member.repos.space_poll.list_schedule_responses("post-2")
    assert {"slot_id": "slot-9", "user_id": BOB, "availability": "yes"} in answers
    assert [r.user_id for r in await member.repos.space_cal.list_rsvps("ev-1")] == [BOB]


async def test_a_member_household_cannot_set_item_state_by_stream(homes):
    """Votes, closure, answers, picks, RSVPs, comment edits, reaction stamps
    and timetable deletes are the host's to stream: the same records from a
    household that is not the space's host change nothing here."""
    host, member = homes
    await _poll(host)
    await _schedule_on_other_post(host)
    await _event_with_rsvps(host)
    await host.repos.space_post.add_comment(
        Comment(
            id="c-1",
            post_id="post-1",
            author=CARL,
            type=CommentType.TEXT,
            created_at=T0,
            content="first",
        ),
        space_id=SPACE,
    )
    await _timetable(host)
    await _post(host, "post-3", type=PostType.BAZAAR, content="Lamp")
    await host.repos.bazaar.save_listing(
        BazaarListing(
            post_id="post-3",
            space_id=SPACE,
            seller_user_id=ANNA,
            mode=BazaarMode.FIXED,
            title="Lamp",
            end_time="2027-01-01T00:00:00+00:00",
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=T0.isoformat(),
            price=50,
        ),
        space_id=SPACE,
    )

    async def bid(bid_id: str, amount: int) -> None:
        await host.repos.bazaar.place_bid(
            BazaarBid(
                id=bid_id,
                listing_post_id="post-3",
                bidder_user_id=CARL,
                amount=amount,
                created_at=T0.isoformat(),
            ),
            space_id=SPACE,
        )

    await bid("bid-1", 40)
    await _stream(host, member)
    # The other household speaks for Carl — an admin of the space — so its
    # records pass every per-author rule a member household's can.
    await member.repos.space_remote_member.add(
        space_id=SPACE,
        instance_id="other-household",
        user_id=CARL,
        user_pk=None,
        display_name=None,
    )
    await member.repos.space.save_member(
        SpaceMember(
            space_id=SPACE,
            user_id=CARL,
            role=SpaceRole.ADMIN.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )

    async def views() -> list[Any]:
        return [
            await _view_poll(member),
            await _view_schedule_of(member, "post-2"),
            await _view_rsvps(member),
            await _view_comment(member),
            await _view_post(member),
            await _view_timetable(member),
            [
                (b.id, b.accepted, b.rejected, b.withdrawn)
                for b in await member.repos.bazaar.list_bids("post-3")
            ],
        ]

    before = await views()
    await bid("bid-2", 45)
    await host.repos.bazaar.reject_offer("bid-1", reason="too low")
    await _vote_and_close(host)
    await host.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-9", user_id=CARL, response="no"
    )
    await host.repos.space_poll.finalize_schedule_poll_in_space(
        space_id=SPACE, post_id="post-2", slot_id="slot-9"
    )
    await _change_rsvps(host)
    await _edit_comment(host)
    await _react(host)
    await _delete_timetable(host)
    for resource in RESOURCE_ORDER:
        exporter = host.exporters.get(resource)
        if exporter is None:
            continue
        records = await collect_batches(record_batches(exporter, SPACE))
        if records:
            await member.receiver._apply(
                resource, SPACE, records, provider="other-household"
            )
    assert await views() == before


async def test_a_recent_reaction_removal_heals_by_its_stamp(homes):
    """Within the tombstone horizon the removal travels as a stamp, and a
    newer reaction this household holds is not undone by the host's older
    snapshot."""
    host, member = homes
    now = datetime.now(UTC)

    def at(minutes: int) -> str:
        return stamp_to_db(now - timedelta(minutes=minutes))

    await _post(host)
    await host.repos.space_post.add_reaction(
        "post-1", "👍", CARL, space_id=SPACE, stamp=at(30)
    )
    await _stream(host, member)
    assert (await _view_post(member))[2] == {"👍": [CARL]}
    await host.repos.space_post.remove_reaction(
        "post-1", "👍", CARL, space_id=SPACE, stamp=at(20)
    )
    # Bob reacted here a moment ago; the host has not heard it yet.
    await member.repos.space_post.add_reaction(
        "post-1", "❤️", BOB, space_id=SPACE, stamp=at(1)
    )
    await _stream(host, member)
    assert (await _view_post(member))[2] == {"❤️": [BOB]}


async def test_an_older_requester_gets_the_older_record_shapes(homes):
    """v_92 gates the new fields and resource: a requester below it (it
    drops them anyway) is sent the shapes it knows."""
    host, _member = homes
    svc = SpaceSyncService(builder=MagicMock(), exporters=host.exporters)

    async def records(resource: str, version: int) -> list[dict] | None:
        session = SimpleNamespace(
            space_id=SPACE, requester_instance_id="peer", peer_version=version
        )
        exporter = await svc._shaped_exporter(resource, session)
        if exporter is None:
            return None
        return await collect_batches(record_batches(exporter, SPACE))

    await _poll(host)
    await _schedule_on_other_post(host)
    await host.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-9", user_id=CARL, response="no"
    )
    await _event_with_rsvps(host)
    await _react(host)
    await _timetable(host)
    await _delete_timetable(host)
    await _post(host, "post-3", type=PostType.BAZAAR, content="Lamp")
    await host.repos.bazaar.save_listing(
        BazaarListing(
            post_id="post-3",
            space_id=SPACE,
            seller_user_id=ANNA,
            mode=BazaarMode.AUCTION,
            title="Lamp",
            end_time="2027-01-01T00:00:00+00:00",
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=T0.isoformat(),
            start_price=5,
        ),
        space_id=SPACE,
    )
    new = FederationCapability.MIN_FOR_SYNC_ITEM_STATE
    for resource, field in (
        ("polls", "votes"),
        ("schedules", "responses"),
        ("calendar", "rsvps"),
        ("posts", "reaction_stamps"),
        ("bazaar", "bids"),
    ):
        old = await records(resource, new - 1)
        assert old and all(field not in r for r in old), resource
        assert any(field in r for r in await records(resource, new) or []), resource
    assert await records("timetables_deleted", new - 1) is None
    assert await records("timetables_deleted", new)


async def _schedule_on_other_post(h: Home) -> None:
    await _post(h, "post-2", type=PostType.SCHEDULE, content="When?")
    await h.repos.space_poll.create_schedule_poll_in_space(
        space_id=SPACE,
        post_id="post-2",
        title="When?",
        deadline=None,
        slots=[{"id": "slot-9", "slot_date": "2026-11-20", "position": 0}],
    )


async def _view_schedule_of(h: Home, post_id: str) -> Any:
    meta = await h.repos.space_poll.get_schedule_meta(post_id)
    return (
        meta,
        sorted(
            (r["slot_id"], r["user_id"], r["availability"])
            for r in await h.repos.space_poll.list_schedule_responses(post_id)
        ),
    )


# ── Synced item state: never frozen, never too big, never reverting ──────


def _ago(minutes: float) -> str:
    return stamp_to_db(datetime.now(UTC) - timedelta(minutes=minutes))


def _iso_ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


async def test_a_far_future_reaction_stamp_from_the_host_never_freezes_it(homes):
    """A host stamp years ahead would win every later removal (last writer
    wins): the receiver runs each stamp through the live rule (≤ 300 s
    ahead), so the reaction lands unstamped and the person can still take
    it back."""
    host, member = homes
    await _post(host)
    future = stamp_to_db(datetime.now(UTC) + timedelta(days=3650))
    await host.repos.space_post.add_reaction(
        "post-1", "🎉", CARL, space_id=SPACE, stamp=future
    )
    for _ in range(2):  # the post new here, then held here
        await _stream(host, member)
        assert (await _view_post(member))[2] == {"🎉": [CARL]}
        stamps = await member.repos.space_post.list_reaction_stamps(["post-1"])
        assert future not in repr(stamps)
    # The removal Carl's household sends next is not stale here …
    await member.repos.space_post.remove_reaction(
        "post-1", "🎉", CARL, space_id=SPACE, stamp=_ago(0)
    )
    # … and the host's next stream does not put it back.
    await _stream(host, member)
    assert (await _view_post(member))[2] == {}


async def test_a_reaction_stamp_breaking_the_live_emoji_rule_is_dropped(homes):
    host, member = homes
    await _post(host)
    (record,) = await collect_batches(record_batches(host.exporters["posts"], SPACE))
    await _stream(host, member)
    record["reaction_stamps"] = {
        f"{CARL}\x00{'x' * 40}": [_ago(1), True],  # longer than the live cap
        f"{CARL}\x00é": [_ago(1), True],  # not NFC
        f"{CARL}\x00👍": [_ago(1), True],
    }
    await member.receiver._apply("posts", SPACE, [record], provider=HOST)
    assert (await _view_post(member))[2] == {"👍": [CARL]}


async def test_our_own_peoples_old_reactions_stay_ours(homes):
    """Past the tombstone horizon the host's sets settle what no stamp
    orders — but never for this household's own people: Bob's reaction is
    authored here, the host may simply not have it."""
    host, member = homes
    await _post(host)
    await _stream(host, member)
    await member.repos.space_post.add_reaction(
        "post-1", "👋", BOB, space_id=SPACE, stamp=_ago(3 * 24 * 60)
    )
    await _stream(host, member)
    assert (await _view_post(member))[2] == {"👋": [BOB]}


async def test_a_post_record_carries_at_most_the_newest_reaction_stamps(
    homes, monkeypatch
):
    host, _member = homes
    monkeypatch.setattr(posts_exporter_mod, "MAX_SYNCED_REACTION_STAMPS", 2)
    await _post(host)
    for n, user in enumerate(("u-1", "u-2", "u-3")):
        await host.repos.space_post.add_reaction(
            "post-1", "👍", user, space_id=SPACE, stamp=_ago(10 - n)
        )
    (record,) = await collect_batches(record_batches(host.exporters["posts"], SPACE))
    assert sorted(record["reaction_stamps"]) == ["u-2\x00👍", "u-3\x00👍"]


async def test_a_cut_stamp_list_carries_removals_before_adds(homes, monkeypatch):
    """A removal travels only as its stamp, so when a post holds more
    stamps than one record carries, the removals go first (newest first),
    then the newest adds — an add left out still shows in the reaction
    sets, a removal left out would be missed for good."""
    host, _member = homes
    monkeypatch.setattr(posts_exporter_mod, "MAX_SYNCED_REACTION_STAMPS", 2)
    await _post(host)
    await host.repos.space_post.add_reaction(
        "post-1", "👍", "u-1", space_id=SPACE, stamp=_ago(30)
    )
    await host.repos.space_post.remove_reaction(
        "post-1", "👍", "u-1", space_id=SPACE, stamp=_ago(20)
    )
    for n, user in enumerate(("u-2", "u-3", "u-4")):
        await host.repos.space_post.add_reaction(
            "post-1", "👍", user, space_id=SPACE, stamp=_ago(10 - n)
        )
    (record,) = await collect_batches(record_batches(host.exporters["posts"], SPACE))
    assert sorted(record["reaction_stamps"]) == ["u-1\x00👍", "u-4\x00👍"]
    assert record["reaction_stamps"]["u-1\x00👍"][1] is False


async def test_an_old_free_text_reaction_still_syncs(homes):
    """Reactions made before v_93 may be short free text: a catch-up keeps
    carrying them (sets and stamps) — only NEW ones must be emoji."""
    host, member = homes
    await _post(host)
    await _stream(host, member)
    (record,) = await collect_batches(record_batches(host.exporters["posts"], SPACE))
    record["reactions"] = {"nice": [CARL]}
    record["reaction_stamps"] = {f"{CARL}\x00nice": [_ago(5), True]}
    await member.receiver._apply("posts", SPACE, [record], provider=HOST)
    assert (await _view_post(member))[2] == {"nice": [CARL]}
    record["reactions"] = {}
    record["reaction_stamps"] = {f"{CARL}\x00nice": [_ago(1), False]}
    await member.receiver._apply("posts", SPACE, [record], provider=HOST)
    assert (await _view_post(member))[2] == {}


async def test_a_cut_vote_list_never_deletes(homes, monkeypatch):
    """A poll record carries at most ``MAX_VOTES_PER_POLL`` votes (one
    record must fit one chunk); a cut list says so, and the receiver then
    only adds — it never drops a vote the list could not carry."""
    host, member = homes
    monkeypatch.setattr(polls_exporter_mod, "MAX_VOTES_PER_POLL", 2)
    await _poll(host)
    await _stream(host, member)
    for voter in ("u-v1", "u-v2", "u-v3"):
        await host.repos.space_poll.set_votes_in_space(
            space_id=SPACE, post_id="post-1", option_ids=["opt-b"], voter_user_id=voter
        )
    (record,) = await collect_batches(record_batches(host.exporters["polls"], SPACE))
    assert record["votes_partial"] is True and 0 < len(record["votes"]) <= 2
    await _stream(host, member)
    held = await member.repos.space_poll.list_votes("post-1")
    assert {"option_id": "opt-a", "voter_user_id": ANNA} in held
    assert all(v in held for v in record["votes"])


async def test_a_cut_answer_list_never_deletes(homes, monkeypatch):
    host, member = homes
    monkeypatch.setattr(schedules_exporter_mod, "MAX_RESPONSES_PER_SCHEDULE", 1)
    await _schedule(host)
    await _stream(host, member)
    await asyncio.sleep(0.01)
    await host.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-2", user_id=ANNA, response="yes"
    )
    (record,) = await collect_batches(
        record_batches(host.exporters["schedules"], SPACE)
    )
    assert record["responses_partial"] is True and len(record["responses"]) == 1
    await _stream(host, member)
    got = (await _view_schedule(member))[4]
    assert ("slot-1", CARL, "maybe") in got and ("slot-2", ANNA, "yes") in got


async def _rsvp(h: Home, user: str, status: str, at: str, occ: str = _OCC) -> None:
    await h.repos.space_cal.upsert_rsvp(
        CalendarRSVP(
            event_id="ev-1",
            user_id=user,
            status=status,
            updated_at=at,
            occurrence_at=occ,
        ),
        space_id=SPACE,
    )


async def test_a_live_rsvp_newer_than_the_hosts_snapshot_survives(homes):
    """Last writer wins on ``updated_at``: an RSVP that reached this
    household live after the host read its list is neither overwritten by
    the host's older answer nor dropped for being absent from it."""
    host, member = homes
    await _event(host)
    await _rsvp(host, CARL, "going", _iso_ago(60))
    await _stream(host, member)
    snapshot = await collect_batches(record_batches(host.exporters["calendar"], SPACE))
    # Live, after the host's snapshot: Carl changes his mind, Dave answers.
    await _rsvp(member, CARL, "declined", _iso_ago(-1))
    await _rsvp(member, "u-dave", "maybe", _iso_ago(-1))
    await member.receiver._apply("calendar", SPACE, snapshot, provider=HOST)
    assert await _view_rsvps(member) == [
        (CARL, _OCC, "declined"),
        ("u-dave", _OCC, "maybe"),
    ]


async def test_a_live_answer_newer_than_the_hosts_snapshot_survives(homes):
    host, member = homes
    await _schedule(host)
    await _stream(host, member)
    snapshot = await collect_batches(record_batches(host.exporters["schedules"], SPACE))
    await asyncio.sleep(0.01)
    await member.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-1", user_id=CARL, response="no"
    )
    await member.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-2", user_id="u-dave", response="yes"
    )
    await member.receiver._apply("schedules", SPACE, snapshot, provider=HOST)
    assert (await _view_schedule(member))[4] == [
        ("slot-1", CARL, "no"),
        ("slot-2", "u-dave", "yes"),
    ]


async def test_the_hosts_organiser_settles_our_peoples_requests(homes):
    """Our own people's RSVPs are authored here — except for exactly the
    transitions the live rule lets another household make: a request
    approved or denied, a waitlisted seat promoted. Anything else of
    theirs the host holds is not taken."""
    host, member = homes
    await host.repos.space_cal.save_event(
        CalendarEvent(
            id="ev-1",
            calendar_id=SPACE,
            summary="Picnic",
            start=T0 + timedelta(days=3),
            end=T0 + timedelta(days=3, hours=2),
            created_by=ANNA,
            rsvp_enabled=True,
            capacity=5,
        ),
        space_id=SPACE,
    )
    await _stream(host, member)
    second = (T0 + timedelta(days=10)).isoformat()
    third = (T0 + timedelta(days=17)).isoformat()
    # Approved, and promoted from the waitlist, on the host …
    await _rsvp(member, BOB, "requested", _iso_ago(30))
    await _rsvp(host, BOB, "going", _iso_ago(20))
    await _rsvp(member, BOB, "waitlist", _iso_ago(30), second)
    await _rsvp(host, BOB, "going", _iso_ago(20), second)
    # … and denied there (gone from the host's list).
    await _rsvp(member, BOB, "requested", _iso_ago(30), third)
    await _stream(host, member)
    assert sorted(
        (r.occurrence_at, r.status)
        for r in await member.repos.space_cal.list_rsvps("ev-1")
    ) == [(_OCC, "going"), (second, "going")]
    # Bob's own answer is never replaced by the host's.
    await _rsvp(member, BOB, "going", _iso_ago(10))
    await _rsvp(host, BOB, "declined", _iso_ago(5))
    await _stream(host, member)
    assert (_OCC, "going") in {
        (r.occurrence_at, r.status)
        for r in await member.repos.space_cal.list_rsvps("ev-1")
    }


async def test_the_hosts_bids_never_speak_for_our_own_people(homes):
    """Bob's bids are authored here: the host's copy never adds one, never
    withdraws one — it only carries the seller's accept / decline. A missed
    bid of somebody else lands stamped with its arrival time, as live."""
    host, member = homes
    await _listing(host)
    await _stream(host, member)
    for h in (host, member):
        await h.repos.bazaar.place_bid(
            BazaarBid(
                id="bid-bob",
                listing_post_id="post-1",
                bidder_user_id=BOB,
                amount=30,
                created_at="2026-11-01T10:00:00+00:00",
            ),
            space_id=SPACE,
        )
    await host.repos.bazaar.place_bid(
        BazaarBid(
            id="bid-bob-2",
            listing_post_id="post-1",
            bidder_user_id=BOB,
            amount=31,
            created_at="2026-11-01T11:00:00+00:00",
        ),
        space_id=SPACE,
    )
    await _bid(host, "bid-carl", 35)
    await host.db.enqueue(
        "UPDATE bazaar_bids SET withdrawn=1, rejected=1 WHERE id='bid-bob'"
    )
    await _stream(host, member)
    held = {b.id: b for b in await member.repos.bazaar.list_bids("post-1")}
    assert set(held) == {"bid-bob", "bid-carl"}
    assert held["bid-bob"].rejected and not held["bid-bob"].withdrawn
    host_carl = await host.repos.bazaar.get_bid("bid-carl")
    assert host_carl is not None
    assert held["bid-carl"].created_at != host_carl.created_at


async def test_a_poll_the_stream_creates_is_announced_once(homes):
    """A poll this household lacked and the host's stream created is told
    to the household's bus — the open SPA shows it, and a vote or a close
    held for it (``PendingParentBuffer``, released on ``PollCreated``)
    replays — naming the provider, so it is never broadcast again. A second
    stream of the same poll announces nothing."""
    host, member = homes
    seen: list[PollCreated] = []

    async def _keep(event: PollCreated) -> None:
        seen.append(event)

    member.receiver._bus.subscribe(PollCreated, _keep)
    await _poll(host)
    await _stream(host, member)
    assert [(e.post_id, e.space_id, e.origin_instance_id) for e in seen] == [
        ("post-1", SPACE, HOST)
    ]
    assert [o["id"] for o in seen[0].options] == ["opt-a", "opt-b"]
    await _stream(host, member)
    assert len(seen) == 1
