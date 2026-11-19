"""``receive_calendar``: calendar events, RSVPs and schedule polls by §25.6 sync."""

from __future__ import annotations

import logging

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.receive_calendar import _calendar_from_record
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from tests.federation.sync.space.receiver_fakes import (
    _CalendarRepoStub,
    _FakeCrypto,
    _FakeFedRepo,
    _FakeRepos,
    _GalleryRepoStub,
    _PageRepoStub,
    _PostRepoStub,
    _send,
    _SpaceRepoStub,
    _StickyRepoStub,
    _TaskRepoStub,
)


async def test_calendar(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "calendar",
        [
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "meeting",
                "start": "2026-04-18T10:00:00+00:00",
                "end": "2026-04-18T11:00:00+00:00",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.calendar) == 1
    _, event = c.calendar[0]
    assert event.id == "e-1"


def _calendar_record(event_id: str, **over) -> dict:
    return {
        "id": event_id,
        "calendar_id": "sp-1",
        "summary": "meeting",
        "start": "2026-04-18T10:00:00+00:00",
        "end": "2026-04-18T11:00:00+00:00",
        "created_by": "u-1",
        **over,
    }


async def test_an_over_limit_calendar_record_is_skipped_and_the_stream_goes_on(
    setup, small_limits, caplog
):
    """The one receiver rule, on the §25.6 stream: a record over the limit
    (measured whole — here by its location and attendees, which the old
    text sum skipped) and larger than our copy is not stored, with a
    WARNING; the records after it in the same chunk still land, so the
    stream is not stalled and its watermark moves on."""
    r, c, kp = setup
    caplog.set_level(logging.WARNING)
    await _send(
        r,
        kp,
        "calendar",
        [
            _calendar_record(
                "e-big",
                location="L" * 400,
                attendees=[f"{n:032d}" for n in range(40)],
            ),
            _calendar_record("e-ok", description="fine"),
        ],
    )
    assert [e.id for _, e in c.calendar] == ["e-ok"]
    assert "e-big" in caplog.text and "not stored" in caplog.text


async def test_an_invalid_calendar_record_is_skipped(setup, caplog):
    r, c, kp = setup
    caplog.set_level(logging.WARNING)
    await _send(
        r,
        kp,
        "calendar",
        [
            _calendar_record("e-rule", rrule="FREQ=DAILY;" + "X" * 600),
            _calendar_record("e-crlf", rrule="FREQ=DAILY\r\nURL:https://e.example"),
            # Saved before the checks: stays deliverable, cleaned.
            _calendar_record(
                "e-old", attendees=["", "u-1", "u-1", 5], rrule="FREQ=HOURLY"
            ),
        ],
    )
    [(_, event)] = c.calendar
    assert (event.id, event.attendees, event.rrule) == (
        "e-old",
        ("u-1",),
        "FREQ=HOURLY",
    )
    assert "e-rule" in caplog.text and "e-crlf" in caplog.text


async def test_a_calendar_event_held_over_the_limit_syncs_no_larger(
    setup, small_limits
):
    """An event stored before the limit keeps arriving by sync as long as it
    does not grow."""
    r, c, kp = setup
    held = _calendar_from_record(
        _calendar_record("e-old", description="o" * 3000), provider="peer-a"
    )
    r._space_calendar_repo.held["e-old"] = ("sp-1", held)
    await _send(r, kp, "calendar", [_calendar_record("e-old", description="p" * 3000)])
    await _send(r, kp, "calendar", [_calendar_record("e-old", description="p" * 3001)])
    assert [e.description for _, e in c.calendar] == ["p" * 3000]


class _FakePollRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_schedule_poll_in_space(
        self,
        *,
        space_id,
        post_id,
        title,
        deadline,
        slots,
    ):
        self.created.append(
            {
                "post_id": post_id,
                "title": title,
                "deadline": deadline,
                "slots": list(slots),
            },
        )
        return True


@pytest.fixture
def setup_with_poll_repo(bus, peer):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    poll = _FakePollRepo()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        poll_repo=poll,
    )
    return r, poll, peer_kp


async def test_schedules_persists_meta_and_slots(setup_with_poll_repo):
    """F5: schedule chunked sync routes through poll_repo.create_schedule_poll."""
    r, poll, kp = setup_with_poll_repo
    await _send(
        r,
        kp,
        "schedules",
        [
            {
                "post_id": "p-sched",
                "title": "Picnic?",
                "deadline": "2026-08-01",
                "slots": [
                    {
                        "id": "s1",
                        "slot_date": "2026-07-01",
                        "start_time": "14:00",
                        "end_time": "16:00",
                        "position": 0,
                    },
                ],
            },
        ],
    )
    assert len(poll.created) == 1
    rec = poll.created[0]
    assert rec["post_id"] == "p-sched"
    assert rec["title"] == "Picnic?"
    assert rec["slots"][0]["id"] == "s1"


async def test_schedules_malformed_record_dropped(setup_with_poll_repo):
    r, poll, kp = setup_with_poll_repo
    await _send(
        r,
        kp,
        "schedules",
        [
            {"post_id": "bad"},  # missing title + slots
            {"title": "no-post-id"},  # missing post_id
            {  # missing slots
                "post_id": "p-ok",
                "title": "OK",
                "slots": [],
            },
        ],
    )
    assert poll.created == []


async def test_schedules_skipped_when_no_poll_repo_wired(bus, peer):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # poll_repo deliberately omitted
    )
    # Must not raise; receiver branch silently logs + returns.
    await _send(
        r,
        peer_kp,
        "schedules",
        [
            {
                "post_id": "p-sched",
                "title": "Picnic?",
                "slots": [{"id": "s1", "slot_date": "2026-07-01"}],
            },
        ],
    )


async def test_a_held_event_still_takes_the_hosts_rsvps_when_its_record_is_skipped(
    setup, small_limits, monkeypatch
):
    """RSVPs are not part of the event's measured size: a host record the
    rule skips (it would grow our copy past the limit) still refreshes the
    RSVPs of the event we hold."""
    r, c, kp = setup
    held = _calendar_from_record(
        _calendar_record("e-old", description="o" * 3000), provider="peer-a"
    )
    r._space_calendar_repo.held["e-old"] = ("sp-1", held)
    taken: list = []

    async def _is_host(self, space_id, provider):
        return True

    async def _persist_rsvps(self, event, record, space_id):
        taken.append((event.id, record["rsvps"]))

    monkeypatch.setattr(SpaceSyncReceiver, "_is_host", _is_host)
    monkeypatch.setattr(SpaceSyncReceiver, "_persist_rsvps", _persist_rsvps)
    rsvps = [{"user_id": "u-2", "occurrence_at": "x", "status": "going"}]
    await _send(
        r,
        kp,
        "calendar",
        [_calendar_record("e-old", description="p" * 3001, rsvps=rsvps)],
    )
    assert c.calendar == []
    assert taken == [("e-old", rsvps)]
