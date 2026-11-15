"""``receive_timetables``: timetables by §25.6 sync."""

from __future__ import annotations

from datetime import UTC, datetime, time

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import TimetableSaved
from socialhome.domain.timetable import (
    Timetable,
    TimetableEntry,
    entry_to_dict,
    to_wire_dict,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.owner_bound_id import (
    SPACE_TIMETABLE_KIND,
    mint_owner_bound_id,
)
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


class _TimetableRepoStub:
    def __init__(self, *, applies: bool = True):
        self.applied: list = []
        self._applies = applies

    async def apply_remote(self, tt, *, space_id):
        self.applied.append((space_id, tt))
        return self._applies

    async def get(self, timetable_id):
        return None


def _timetable_receiver(bus, peer, repo):
    peer_inst, _kp = peer
    collector = _FakeRepos()
    return SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        timetable_repo=repo,
    )


def _tt_record(owner: str = "u-adm", space_id: str = "sp-1", **extra) -> dict:
    at = datetime(2026, 6, 1, tzinfo=UTC)
    return {
        **to_wire_dict(
            Timetable(
                id=mint_owner_bound_id(
                    SPACE_TIMETABLE_KIND, space_id=space_id, owner_user_id=owner
                ),
                name="5b",
                created_by=owner,
                created_at=at,
                updated_at=at,
                updated_by=owner,
            )
        ),
        **extra,
    }


async def test_timetables_apply_last_writer_wins_and_publish(bus, peer):
    repo = _TimetableRepoStub()
    r = _timetable_receiver(bus, peer, repo)
    seen: list = []

    async def _rec(ev):
        seen.append(ev)

    bus.subscribe(TimetableSaved, _rec)
    good = _tt_record()
    await _send(r, peer[1], "timetables", [good])
    [(space_id, tt)] = repo.applied
    assert (space_id, tt.id, tt.assignees) == ("sp-1", good["id"], ())
    [ev] = seen
    assert (ev.space_id, ev.origin_instance_id) == ("sp-1", "peer-a")


async def test_a_timetable_with_an_icon_the_emoji_rule_now_refuses_still_applies(
    bus, peer
):
    """An older peer may stream an icon the looser pre-2026.10 emoji rule
    accepted: only that icon is dropped, never the whole timetable."""
    repo = _TimetableRepoStub()
    r = _timetable_receiver(bus, peer, repo)
    lesson = TimetableEntry(
        id="e1",
        weekday=0,
        start=time(8, 0),
        end=time(8, 45),
        title="Mathe",
        icon="🏫" + "\U000e0067" * 3,
    )
    await _send(r, peer[1], "timetables", [_tt_record(entries=[entry_to_dict(lesson)])])
    [(_space_id, tt)] = repo.applied
    assert (tt.entries[0].title, tt.entries[0].icon) == ("Mathe", None)


async def test_a_stale_timetable_copy_is_not_published(bus, peer):
    r = _timetable_receiver(bus, peer, _TimetableRepoStub(applies=False))
    seen: list = []

    async def _rec(ev):
        seen.append(ev)

    bus.subscribe(TimetableSaved, _rec)
    await _send(r, peer[1], "timetables", [_tt_record()])
    assert seen == []


async def test_bad_timetable_records_are_skipped_the_rest_apply(bus, peer, caplog):
    repo = _TimetableRepoStub()
    r = _timetable_receiver(bus, peer, repo)
    good = _tt_record()
    with caplog.at_level("WARNING"):
        await _send(
            r,
            peer[1],
            "timetables",
            [
                {"id": "x"},  # malformed
                _tt_record(schema=2),  # wrong schema
                _tt_record(assignees=["u-adm"]),  # assignees
                _tt_record(space_id="sp-2"),  # bound to another space
                {**_tt_record(), "id": "0123456789abcdef0123456789abcdef"},
                "not-an-object",
                good,
            ],
        )
    assert [tt.id for _sid, tt in repo.applied] == [good["id"]]
    assert "timetable" in caplog.text


async def test_timetables_skipped_when_repo_not_wired(bus, peer):
    r = _timetable_receiver(bus, peer, None)
    await _send(r, peer[1], "timetables", [_tt_record()])  # no raise
