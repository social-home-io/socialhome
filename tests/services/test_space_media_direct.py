"""A space file for one reader, straight from a household that holds it
(owner 2026-11-28 media routing, ``services/space_media_direct``)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import socialhome.services.space_media_direct as mod
from socialhome.domain.federation import FederationEventType
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.space_media_ref import SpaceMediaRef
from socialhome.services.space_media_direct import (
    SpaceMediaDirect,
    shares_a_seated_space,
)

SPACE = "sp-1"
OWN = "own-household"
HOST = "host-household"
AUTHOR = "author-household"
NAME = space_content_media_name(SPACE, "ab" * 32, "g.webp")


def _ref(local_name: str = NAME, origin: str = AUTHOR) -> SpaceMediaRef:
    return SpaceMediaRef(
        filename="g.webp",
        space_id=SPACE,
        blob_id="0" * 64,
        suite="s",
        epoch=2,
        size=10,
        scope="channel",
        addr_suite="sm1",
        item_id="g-1",
        origin=origin,
        local_name=local_name,
    )


@dataclass
class _Result:
    ok: bool


class _Fed:
    """Links that come up (or not) per household; a holder that delivers
    writes the file into the media dir, as the inbound blob handler does."""

    own_instance_id = OWN

    def __init__(self, media_dir, *, links=(), delivers=()):
        self.dir = media_dir
        self.links = set(links)
        self.delivers = set(delivers)
        self.opened: list[str] = []
        self.sent: list[tuple[str, dict]] = []
        self.closed: list[str] = []

    async def open_direct_link(self, instance_id, *, timeout_s):
        self.opened.append(instance_id)
        return instance_id in self.links

    async def close_seat_link(self, instance_id):
        self.closed.append(instance_id)

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        assert event_type is FederationEventType.SPACE_MEDIA_WANTED
        self.sent.append((to_instance_id, payload))
        if to_instance_id in self.delivers:
            for f in payload["files"]:
                (self.dir / f["name"]).write_bytes(b"pic")
        return _Result(ok=True)


class _Spaces:
    async def get(self, space_id):
        return SimpleNamespace(id=space_id, owner_instance_id=HOST)

    async def get_invited_by(self, space_id):
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mod, "DIRECT_FILE_WAIT_S", 0.05)
    monkeypatch.setattr(mod, "DIRECT_POLL_S", 0.01)


def _direct(tmp_path, fed):
    return SpaceMediaDirect(
        federation=fed, spaces=_Spaces(), seats=None, media_dir=tmp_path
    )


async def test_the_host_is_asked_first_over_a_direct_link(tmp_path):
    """The acting host first: one SPACE_MEDIA_WANTED for the file by its
    content name, over the link; the file is here when it returns True."""
    fed = _Fed(tmp_path, links={HOST, AUTHOR}, delivers={HOST})
    assert await _direct(tmp_path, fed).fetch(_ref())
    assert fed.opened == [HOST]
    assert fed.sent == [
        (HOST, {"space_id": SPACE, "files": [{"item": "g-1", "name": NAME}]})
    ]
    assert (tmp_path / NAME).read_bytes() == b"pic"


async def test_a_holder_without_a_link_is_skipped_for_the_next(tmp_path):
    """No link to the host (NAT, offline, older than v_97): nothing is
    sent to it; the household that delivered the item is asked."""
    fed = _Fed(tmp_path, links={AUTHOR}, delivers={AUTHOR})
    assert await _direct(tmp_path, fed).fetch(_ref())
    assert fed.opened == [HOST, AUTHOR]
    assert [to for to, _p in fed.sent] == [AUTHOR]


async def test_nobody_delivers_means_false_for_the_cache_to_try(tmp_path):
    fed = _Fed(tmp_path, links={HOST, AUTHOR})
    assert not await _direct(tmp_path, fed).fetch(_ref())
    assert [to for to, _p in fed.sent] == [HOST, AUTHOR]


async def test_only_a_content_name_is_asked_for(tmp_path):
    """A file with no content name (no hash known) can't be proven by the
    holder's provider: never asked for directly."""
    fed = _Fed(tmp_path, links={HOST}, delivers={HOST})
    assert not await _direct(tmp_path, fed).fetch(_ref(local_name="sg-abc.webp"))
    assert fed.opened == []


async def test_we_never_ask_ourselves(tmp_path):
    fed = _Fed(tmp_path, links={HOST})
    await _direct(tmp_path, fed).fetch(_ref(origin=OWN))
    assert OWN not in fed.opened


async def test_a_holder_that_failed_is_not_asked_again_for_a_while(tmp_path):
    """Fable A6: a holder whose link did not come up (or that delivered
    nothing) costs one wait — later files skip it until the backoff ends."""
    fed = _Fed(tmp_path, links={AUTHOR}, delivers={AUTHOR})
    d = _direct(tmp_path, fed)
    assert await d.fetch(_ref())
    (tmp_path / NAME).unlink()
    assert await d.fetch(_ref())
    assert fed.opened == [HOST, AUTHOR, AUTHOR]


async def test_a_holders_budget_for_us_is_never_overrun(tmp_path, monkeypatch):
    """Fable A6: past the provider's budget (files per window) the holder
    would drop the request unanswered — it is not asked."""
    monkeypatch.setattr(mod, "HOLDER_FILES_PER_WINDOW", 1)
    fed = _Fed(tmp_path, links={HOST}, delivers={HOST})
    d = _direct(tmp_path, fed)
    assert await d.fetch(_ref())
    (tmp_path / NAME).unlink()
    assert not await d.fetch(_ref())
    assert len(fed.sent) == 1


async def test_a_file_larger_than_its_blob_is_dropped(tmp_path):
    """Fable A11: the delivered file may not be larger than the sealed
    blob the sync policy and the storage judged."""

    class _Big(_Fed):
        async def send_event(
            self, *, to_instance_id, event_type, payload, space_id=None
        ):
            for f in payload["files"]:
                (self.dir / f["name"]).write_bytes(b"x" * 1000)
            return _Result(ok=True)

    fed = _Big(tmp_path, links={HOST})
    assert not await _direct(tmp_path, fed).fetch(_ref())
    assert not (tmp_path / NAME).exists()


async def test_the_reaper_closes_idle_links_on_its_schedule(monkeypatch):
    """Fable A3/A4: idle seat links are reaped by a scheduler (the _stop
    event lifecycle), not only after a fetch."""
    monkeypatch.setattr(mod, "REAP_INTERVAL_S", 0.01)
    calls: list[float] = []

    class _F:
        async def reap_seat_links(self, *, idle_s):
            calls.append(idle_s)
            return 1

    reaper = mod.SeatLinkReaper(_F())
    await reaper.start()
    await asyncio.sleep(0.05)
    await reaper.stop()
    assert calls and calls[0] == mod.LINK_IDLE_S


class _Seats:
    def __init__(self, seated):
        self.seated = seated

    async def list_for_instance(self, space_id, instance_id, *, include_tombstoned):
        assert include_tombstoned is False
        return ["seat"] if (space_id, instance_id) in self.seated else []


class _AllSpaces:
    def __init__(self, local):
        self.local = local

    async def list_all(self):
        return [SimpleNamespace(id="sp-1"), SimpleNamespace(id="sp-2")]

    async def list_local_member_user_ids(self, space_id):
        return ["u-me"] if space_id in self.local else []


async def test_a_link_needs_a_space_both_households_are_seated_in():
    """v_97 gate: the household holds a live seat in a space a local
    person is seated in — no shared seat (or only one side), no link."""
    seats = _Seats({("sp-1", "peer")})
    assert await shares_a_seated_space(_AllSpaces({"sp-1"}), seats, "peer")
    assert not await shares_a_seated_space(_AllSpaces({"sp-2"}), seats, "peer")
    assert not await shares_a_seated_space(_AllSpaces({"sp-1"}), seats, "other")


async def test_the_gate_fails_closed():
    class _Broken(_AllSpaces):
        async def list_all(self):
            raise RuntimeError("db gone")

    assert not await shares_a_seated_space(_Broken(set()), _Seats(set()), "peer")
