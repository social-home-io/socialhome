"""v_87 — a picture pushed through the GFS relay that never arrived (the
relay accepted it with a 202, then dropped it: saturation, a full queue,
the receiver's budget) is asked for again on the next view: the household
asks the space's host to replay the space since the row showing it
(``SPACE_SYNC_RESUME``), which re-pushes the row's files."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import InstanceSource
from socialhome.services.relay_media_refill import (
    REFILL_COOLDOWN_S,
    RelayMediaRefill,
)

HOST = "h" * 32
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
RECENT = "2026-11-08 10:00:00"


class _Refs:
    def __init__(self, rows):
        self.rows = rows

    async def space_rows_of(self, basename):
        return self.rows.get(basename, [])


class _Spaces:
    async def get(self, space_id):
        return SimpleNamespace(id=space_id, owner_instance_id=HOST)


class _Fed:
    def __init__(self, *, source=InstanceSource.SPACE_SESSION, relay=True):
        self.inst = SimpleNamespace(id=HOST, source=source)
        self.relay = relay

    async def get_instance(self, iid):
        return self.inst if iid == HOST else None

    async def relay_media_ok(self, instance):
        return self.relay


class _Resume:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_request(self, **kw):
        self.sent.append(kw)


def _refill(*, rows=None, fed=None, clock=None):
    fed = fed or _Fed()
    resume = _Resume()
    r = RelayMediaRefill(
        media_refs=_Refs(rows or {"sc-a.webp": [("sp1", RECENT, "posts")]}),
        space_repo=_Spaces(),
        federation_repo=fed,
        federation=fed,
        resume=resume,
        clock=clock or (lambda: 1000.0),
        now=lambda: NOW,
    )
    return r, resume


async def test_a_missing_relay_file_asks_the_host_to_replay_since_its_row():
    r, resume = _refill()
    assert await r.request("sc-a.webp") == 1
    [req] = resume.sent
    assert req["space_id"] == "sp1" and req["instance_id"] == HOST
    # A minute before the row, as an ISO-8601 timestamp the host parses,
    # scoped to the one resource that shows the file.
    assert req["since"] == "2026-11-08T09:59:00+00:00"
    assert req["refill"] == "posts"


async def test_one_request_per_space_per_cooldown():
    now = [1000.0]
    r, resume = _refill(clock=lambda: now[0])
    await r.request("sc-a.webp")
    await r.request("sc-a.webp")
    assert len(resume.sent) == 1
    now[0] += REFILL_COOLDOWN_S + 1
    await r.request("sc-a.webp")
    assert len(resume.sent) == 2


@pytest.mark.parametrize(
    ("name", "fed"),
    [
        ("sg-legacy.webp", None),  # not a content-addressed relay push
        ("sc-unknown.webp", None),  # no live row shows it
        ("sc-a.webp", _Fed(source=InstanceSource.MANUAL)),  # paired host
        ("sc-a.webp", _Fed(relay=False)),  # no binary relay path
    ],
)
async def test_nothing_is_asked_outside_the_relay_media_case(name, fed):
    r, resume = _refill(fed=fed)
    assert await r.request(name) == 0
    assert resume.sent == []


async def test_schedule_never_raises_and_runs_in_the_background():
    r, resume = _refill()
    r.schedule("sc-a.webp")
    await r.drain()
    assert len(resume.sent) == 1


async def test_an_old_row_is_never_refilled():
    r, resume = _refill(rows={"sc-a.webp": [("sp1", "2026-11-01 10:00:00", "posts")]})
    assert await r.request("sc-a.webp") == 0
    assert resume.sent == []


async def test_each_resource_has_its_own_cooldown():
    rows = {
        "sc-a.webp": [("sp1", RECENT, "posts")],
        "sc-c.webp": [("sp1", RECENT, "calendar")],
    }
    r, resume = _refill(rows=rows)
    await r.request("sc-a.webp")
    await r.request("sc-c.webp")
    assert [q["refill"] for q in resume.sent] == ["posts", "calendar"]
