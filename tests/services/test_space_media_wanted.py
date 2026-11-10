"""Tests for socialhome.services.space_media_wanted (v_92 SPACE_MEDIA_WANTED)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

import pytest

from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.space import SpaceRole
from socialhome.federation.sync.space.resume import (
    RESUME_COOLDOWN_S,
    RESUME_MEDIA_MAX_BYTES,
    RESUME_MEDIA_MAX_FILES,
)
from socialhome.services import space_media_wanted as mod
from socialhome.services.media_file_hasher import MediaFileHasher
from socialhome.services.space_media_wanted import (
    MAX_PROVIDERS,
    MAX_WANTED_FILES,
    NAME_COOLDOWN_S,
    SpaceMediaWanted,
    SpaceMediaWantedProvider,
)

V92 = FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
SPACE = "sp-1"
SELF = "me"
HOST = "host"


def _sc(tag: str, ext: str = ".webp") -> str:
    return space_content_media_name(SPACE, tag * 64, "x" + ext)


def _sc_n(i: int) -> str:
    return space_content_media_name(SPACE, f"{i:064x}", "x.webp")


# ── Requester stubs ──────────────────────────────────────────────────────


class _Refs:
    def __init__(self, rows=None):
        #: basename → [(space, item, created_at)]
        self.rows: dict[str, list[tuple[str, str, str]]] = rows or {}
        self.asked: list[str] = []

    async def space_rows_of(self, basename):
        self.asked.append(basename)
        return sorted(self.rows.get(basename, []))


@dataclass
class _Seat:
    user_id: str
    role: SpaceRole


@dataclass
class _Space:
    id: str
    owner_instance_id: str


class _Spaces:
    def __init__(self, *, role=SpaceRole.MEMBER, host=HOST, members=()):
        self.role = role
        self.host = host
        self.members = set(members)

    async def get(self, space_id):
        return _Space(space_id, self.host) if space_id.startswith("sp-") else None

    async def list_local_member_user_ids(self, space_id):
        return ["u-me"]

    async def list_members(self, space_id):
        return [_Seat("u-me", self.role)]

    async def list_member_instances(self, space_id):
        return sorted(self.members)


class _Seats:
    def __init__(self, helpers=()):
        self.helpers = list(helpers)
        self.asked: list[frozenset[str]] = []

    async def list_instances_with_roles(self, space_id, roles):
        self.asked.append(frozenset(roles))
        return list(self.helpers)


@dataclass
class _Fed:
    versions: dict[str, int] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    sent: list[tuple[str, FederationEventType, dict, str | None]] = field(
        default_factory=list
    )
    on_send: object = None

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload, space_id))
        if callable(self.on_send):
            self.on_send(to_instance_id, payload)
        ok = to_instance_id not in self.fail
        return DeliveryResult(
            instance_id=to_instance_id, ok=ok, error=None if ok else "no_route"
        )


def _wanted(
    tmp_path,
    *,
    refs=None,
    spaces=None,
    seats=None,
    fed=None,
    coalesce_s=0.0,
    provider_wait_s=0.0,
    clock=None,
):
    kw = {} if clock is None else {"clock": clock}
    return SpaceMediaWanted(
        media_refs=refs or _Refs(),
        space_repo=spaces or _Spaces(),
        remote_members=seats or _Seats(),
        federation=fed or _Fed({HOST: V92}),
        own_instance_id=SELF,
        media_dir=tmp_path,
        coalesce_s=coalesce_s,
        provider_wait_s=provider_wait_s,
        **kw,
    )


def _names(sent) -> list[str]:
    return [f["name"] for f in sent[2]["files"]]


# ── Requester ────────────────────────────────────────────────────────────


async def test_a_view_asks_the_host_for_the_file_and_its_item(tmp_path):
    a = _sc("a")
    fed = _Fed({HOST: V92})
    w = _wanted(tmp_path, refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed)
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 1
    to, event_type, payload, space_id = fed.sent[0]
    assert (to, event_type, space_id) == (
        HOST,
        FederationEventType.SPACE_MEDIA_WANTED,
        SPACE,
    )
    assert payload == {"space_id": SPACE, "files": [{"item": "p1", "name": a}]}


async def test_names_of_one_space_are_coalesced_into_one_request(tmp_path):
    a, b = _sc("a"), _sc("b")
    refs = _Refs({a: [(SPACE, "p1", "t")], b: [(SPACE, "p2", "t")]})
    fed = _Fed({HOST: V92})
    w = _wanted(tmp_path, refs=refs, fed=fed, coalesce_s=0.2)
    w.schedule(a)
    w.schedule(b)
    await w.drain()
    assert len(fed.sent) == 1
    assert sorted(_names(fed.sent[0])) == sorted([a, b])


async def test_a_full_batch_goes_at_once_and_the_rest_waits_for_the_next(tmp_path):
    names = [_sc_n(i) for i in range(MAX_WANTED_FILES + 1)]
    refs = _Refs({n: [(SPACE, f"p{i}", "t")] for i, n in enumerate(names)})
    fed = _Fed({HOST: V92})
    w = _wanted(tmp_path, refs=refs, fed=fed, coalesce_s=30.0)
    for n in names:
        w.schedule(n)
        await asyncio.sleep(0)  # let each lookup run (≤ 8 at once)
    await asyncio.sleep(0.05)
    # The first 50 went without waiting the 30 s window.
    assert len(fed.sent) == 1
    assert len(fed.sent[0][2]["files"]) == MAX_WANTED_FILES
    await w.stop()


async def test_a_follower_household_never_asks(tmp_path):
    """A follower keeps the GFS on-demand path (#1317)."""
    a = _sc("a")
    fed = _Fed({HOST: V92})
    w = _wanted(
        tmp_path,
        refs=_Refs({a: [(SPACE, "p1", "t")]}),
        spaces=_Spaces(role=SpaceRole.SUBSCRIBER),
        fed=fed,
    )
    w.schedule(a)
    await w.drain()
    assert fed.sent == []


async def test_a_legacy_or_unsafe_name_is_never_looked_up(tmp_path):
    refs = _Refs()
    w = _wanted(tmp_path, refs=refs)
    w.schedule("a.webp")
    w.schedule("sd-abc.webp")
    w.schedule("sc-../x")
    w.schedule("")
    await w.drain()
    assert refs.asked == []


async def test_a_name_is_asked_again_only_after_its_cooldown(tmp_path):
    a = _sc("a")
    now = [1000.0]
    refs = _Refs({a: [(SPACE, "p1", "t")]})
    fed = _Fed({HOST: V92})
    w = _wanted(tmp_path, refs=refs, fed=fed, clock=lambda: now[0])
    w.schedule(a)
    await w.drain()
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 1
    now[0] += NAME_COOLDOWN_S
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 2


async def test_after_the_wait_missing_names_go_to_a_trusted_helper(tmp_path):
    """Host first; after the fixed wait only the names still not stored go
    to the next provider — trusted helpers in sorted order, never ourselves,
    at most MAX_PROVIDERS."""
    a, b = _sc("a"), _sc("b")
    refs = _Refs({a: [(SPACE, "p1", "t")], b: [(SPACE, "p2", "t")]})
    helpers = ["h-z", "h-a", SELF, HOST, "h-m", "h-b"]
    fed = _Fed({iid: V92 for iid in [HOST, *helpers]})

    def _host_sends_a(to, payload):
        if to == HOST:
            (tmp_path / a).write_bytes(b"x")

    fed.on_send = _host_sends_a
    seats = _Seats(helpers)
    w = _wanted(tmp_path, refs=refs, seats=seats, fed=fed, coalesce_s=0.1)
    w.schedule(a)
    w.schedule(b)
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST, "h-a", "h-b"][:MAX_PROVIDERS]
    assert sorted(_names(fed.sent[0])) == sorted([a, b])
    assert _names(fed.sent[1]) == [b]
    # Helpers are the content-authority seats.
    assert seats.asked and {"admin", "moderator"} <= set(seats.asked[0])


async def test_nothing_more_is_asked_once_every_file_arrived(tmp_path):
    a = _sc("a")
    fed = _Fed({HOST: V92, "h-a": V92})
    fed.on_send = lambda to, payload: (tmp_path / a).write_bytes(b"x")
    w = _wanted(
        tmp_path,
        refs=_Refs({a: [(SPACE, "p1", "t")]}),
        seats=_Seats(["h-a"]),
        fed=fed,
    )
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST]


async def test_a_provider_below_v92_is_skipped(tmp_path):
    a = _sc("a")
    fed = _Fed({HOST: V92 - 1, "h-a": V92 - 1, "h-b": V92})
    w = _wanted(
        tmp_path,
        refs=_Refs({a: [(SPACE, "p1", "t")]}),
        seats=_Seats(["h-a", "h-b"]),
        fed=fed,
    )
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == ["h-b"]


async def test_a_failed_send_moves_to_the_next_provider_at_once(tmp_path):
    a = _sc("a")
    fed = _Fed({HOST: V92, "h-a": V92}, fail={HOST})
    w = _wanted(
        tmp_path,
        refs=_Refs({a: [(SPACE, "p1", "t")]}),
        seats=_Seats(["h-a"]),
        fed=fed,
        provider_wait_s=30.0,
    )
    w.schedule(a)
    await asyncio.wait_for(w.drain(), timeout=5)
    assert [s[0] for s in fed.sent] == [HOST, "h-a"]


async def test_no_provider_no_request(tmp_path, caplog):
    a = _sc("a")
    fed = _Fed({})
    w = _wanted(tmp_path, refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed)
    with caplog.at_level(logging.INFO):
        w.schedule(a)
        await w.drain()
    assert fed.sent == []


async def test_stop_ends_a_batch_waiting_for_its_window(tmp_path):
    a = _sc("a")
    fed = _Fed({HOST: V92})
    w = _wanted(
        tmp_path, refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed, coalesce_s=60.0
    )
    w.schedule(a)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(w.stop(), timeout=2)
    assert fed.sent == []
    w.schedule(_sc("b"))  # after stop: nothing scheduled
    await w.drain()


async def test_a_lookup_error_is_contained(tmp_path):
    class _Boom(_Refs):
        async def space_rows_of(self, basename):
            raise RuntimeError("db gone")

    w = _wanted(tmp_path, refs=_Boom())
    w.schedule(_sc("a"))
    await w.drain()  # never raises


# ── Provider stubs ───────────────────────────────────────────────────────


class _ItemFiles:
    def __init__(self, items):
        #: (space, item) → [(basename, owner)]
        self.items = items

    async def live_item_files(self, space_id, item_id):
        return list(self.items.get((space_id, item_id), []))


class _Gate:
    def __init__(self, *, all_files=False, own=(), fills=False):
        self.all = all_files
        self.own = set(own)
        self.fills = fills

    async def ship(self, owner, urls):
        if self.all or owner in self.own:
            return list(urls)
        return [u for u in urls if self.fills and u.startswith("sc-")]


class _Gates:
    def __init__(self, gate):
        self.gate = gate
        self.asked: list[tuple[str, str]] = []

    async def for_requester(self, space_id, requester):
        self.asked.append((space_id, requester))
        return self.gate


class _Outbox:
    def __init__(self):
        self.queued: list[dict] = []

    async def enqueue_for_blob(self, **kw):
        self.queued.append(kw)


def _provider(tmp_path, items, *, gate=None, members=("req",), clock=None):
    outbox = _Outbox()
    kw = {} if clock is None else {"clock": clock}
    p = SpaceMediaWantedProvider(
        space_repo=_Spaces(members=members),
        media_refs=_ItemFiles(items),
        file_gates=_Gates(gate or _Gate(all_files=True)),
        hasher=MediaFileHasher(tmp_path),
        media_sync=outbox,
        media_dir=tmp_path,
        **kw,
    )
    return p, outbox


def _event(files, *, space_id=SPACE, sender="req"):
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_MEDIA_WANTED,
        from_instance=sender,
        to_instance=SELF,
        timestamp="2026-11-10T00:00:00+00:00",
        payload={"space_id": space_id, "files": files},
        space_id=space_id,
    )


async def test_the_provider_ships_a_held_content_named_file(tmp_path):
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x" * 10)
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1")]})
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 1
    assert outbox.queued == [
        {
            "space_id": SPACE,
            "correlation_id": "p1",
            "target_instance_ids": ["req"],
            "media_urls": [a],
            "push": True,
        }
    ]


async def test_an_unhashed_local_file_matches_by_its_computed_content_name(tmp_path):
    """The host stores its own upload under its original name: it hashes
    it (off-loop, cached) and compares the content name."""
    data = b"host's own picture"
    (tmp_path / "orig.webp").write_bytes(data)
    name = space_content_media_name(
        SPACE, hashlib.sha256(data).hexdigest(), "orig.webp"
    )
    p, outbox = _provider(
        tmp_path, {(SPACE, "p1"): [("other.webp", "u1"), ("orig.webp", "u1")]}
    )
    assert await p.handle(_event([{"item": "p1", "name": name}])) == 1
    assert outbox.queued[0]["media_urls"] == ["orig.webp"]
    # A different file's content name matches nothing.
    assert await p.handle(_event([{"item": "p1", "name": _sc("f")}])) == 0


async def test_a_non_member_household_gets_nothing(tmp_path):
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1")]}, members=("other",))
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 0
    assert outbox.queued == []


async def test_wrong_space_unshown_deleted_or_missing_entries_are_skipped(tmp_path):
    a, b, c = _sc("a"), _sc("b"), _sc("c")
    for n in (a, b):
        (tmp_path / n).write_bytes(b"x")
    # p1 shows a (and c, which is not stored here); the deleted p2 is not a
    # live item (the repo returns nothing for it); sp-2's item is elsewhere.
    items = {(SPACE, "p1"): [(a, "u1"), (c, "u1")], ("sp-2", "p3"): [(b, "u1")]}
    p, outbox = _provider(tmp_path, items)
    sent = await p.handle(
        _event(
            [
                {"item": "p1", "name": b},  # not shown by p1
                {"item": "p2", "name": a},  # deleted / unknown here
                {"item": "p3", "name": b},  # an item of another space
                {"item": "p1", "name": c},  # shown, but not stored here
                {"item": "p1", "name": a},  # the one good entry
            ]
        )
    )
    assert sent == 1
    assert [q["media_urls"] for q in outbox.queued] == [[a]]


async def test_malformed_entries_are_skipped_and_a_malformed_request_dropped(tmp_path):
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1"), ("a.webp", "u1")]})
    sent = await p.handle(
        _event(
            [
                "nope",
                {"item": "p1"},
                {"item": "p1", "name": "a.webp"},  # not a content name
                {"item": "p1", "name": "sc-../../etc"},  # unsafe
                {"item": "x" * 300, "name": a},  # oversized id
                {"item": 7, "name": a},
                {"item": "p1", "name": a},
                {"item": "p1", "name": a},  # repeated: once
            ]
        )
    )
    assert sent == 1
    too_many = [{"item": "p1", "name": a}] * (MAX_WANTED_FILES + 1)
    assert await p.handle(_event(too_many)) == 0
    assert await p.handle(_event([])) == 0
    assert await p.handle(_event("x")) == 0
    assert await p.handle(_event([{"item": "p1", "name": a}], space_id="")) == 0
    assert len(outbox.queued) == 1


async def test_the_file_gate_decides_what_a_plain_member_ships(tmp_path):
    """A plain member household ships only its own people's files; a
    trusted one other people's hashed files too (the v_86 gate)."""
    a, b = _sc("a"), _sc("b")
    for n in (a, b):
        (tmp_path / n).write_bytes(b"x")
    items = {(SPACE, "p1"): [(a, "u-mine")], (SPACE, "p2"): [(b, "u-other")]}
    files = [{"item": "p1", "name": a}, {"item": "p2", "name": b}]
    plain, outbox = _provider(tmp_path, items, gate=_Gate(own={"u-mine"}))
    assert await plain.handle(_event(files)) == 1
    assert [q["media_urls"] for q in outbox.queued] == [[a]]
    trusted, outbox = _provider(tmp_path, items, gate=_Gate(own={"u-mine"}, fills=True))
    assert await trusted.handle(_event(files)) == 2


async def test_the_budget_is_50_files_and_32_mib_per_requester_and_space(
    tmp_path, caplog
):
    now = [0.0]
    names = [_sc_n(i) for i in range(RESUME_MEDIA_MAX_FILES + 1)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    items = {(SPACE, f"p{i}"): [(n, "u1")] for i, n in enumerate(names)}
    p, _outbox = _provider(tmp_path, items, clock=lambda: now[0])
    first = [
        {"item": f"p{i}", "name": n} for i, n in enumerate(names[:MAX_WANTED_FILES])
    ]
    assert await p.handle(_event(first)) == RESUME_MEDIA_MAX_FILES
    with caplog.at_level(logging.INFO):
        assert await p.handle(_event([{"item": "p50", "name": names[50]}])) == 0
    assert "budget" in caplog.text
    # Another requester has its own budget; after the window, so does this one.
    p._spaces.members.add("req-2")
    assert await p.handle(_event([{"item": "p50", "name": names[50]}], sender="req-2"))
    now[0] += RESUME_COOLDOWN_S
    assert await p.handle(_event([{"item": "p50", "name": names[50]}])) == 1


async def test_the_byte_budget_skips_a_file_that_would_pass_it(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "RESUME_MEDIA_MAX_BYTES", 15)
    a, b = _sc("a"), _sc("b")
    (tmp_path / a).write_bytes(b"x" * 10)
    (tmp_path / b).write_bytes(b"y" * 10)
    items = {(SPACE, "p1"): [(a, "u1")], (SPACE, "p2"): [(b, "u1")]}
    p, _outbox = _provider(tmp_path, items)
    files = [{"item": "p1", "name": a}, {"item": "p2", "name": b}]
    assert await p.handle(_event(files)) == 1
    assert RESUME_MEDIA_MAX_BYTES == 32 * 1024 * 1024


@pytest.mark.parametrize("missing", ["items", "outbox"])
async def test_an_error_while_queueing_is_contained(tmp_path, missing):
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1")]})

    async def _boom(*args, **kw):
        raise RuntimeError("down")

    if missing == "items":
        p._refs.live_item_files = _boom
    else:
        outbox.enqueue_for_blob = _boom
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 0
