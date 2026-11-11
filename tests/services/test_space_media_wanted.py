"""Tests for socialhome.services.space_media_wanted (v_92 SPACE_MEDIA_WANTED)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.sync.space import file_gate as file_gate_mod
from socialhome.federation.sync.space.file_gate import (
    BUDGET_MAX_FILES,
    BUDGET_WINDOW_S,
    SpaceFileBudget,
)
from socialhome.federation.sync.space.trusted_helpers import (
    HELPER_ROLES,
    helper_offset,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services import space_media_wanted as mod
from socialhome.services.inbound_media_store import space_direct_media_name
from socialhome.services.media_file_hasher import MediaFileHasher
from socialhome.services.space_media_wanted import (
    MAX_HELPERS,
    MAX_WANTED_FILES,
    NAME_COOLDOWN_S,
    SpaceMediaWanted,
    SpaceMediaWantedProvider,
)
from socialhome.services.space_writer_households import SpaceWriterHouseholds

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
        #: item → owner (``live_item_files``' author of the item's files).
        self.owners: dict[str, str] = {}

    async def space_rows_of(self, basename):
        self.asked.append(basename)
        return sorted(self.rows.get(basename, []))

    async def live_item_files(self, space_id, item_id):
        owner = self.owners.get(item_id)
        return [] if owner is None else [("f.webp", owner)]


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

    async def get_invited_by(self, space_id):
        return None


class _Seats:
    def __init__(self, helpers=(), seats=()):
        self.helpers = list(helpers)
        self.asked: list[frozenset[str]] = []
        #: ``(instance_id, user_id)`` live seats of the space.
        self.seats = list(seats)

    async def list_for_space(self, space_id):
        return [
            SimpleNamespace(instance_id=i, user_id=u, space_id=space_id)
            for i, u in self.seats
        ]

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

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload, space_id))
        ok = to_instance_id not in self.fail
        return DeliveryResult(
            instance_id=to_instance_id, ok=ok, error=None if ok else "no_route"
        )


class _Contact:
    def __init__(self, silent=()):
        self.silent = set(silent)

    def is_silent(self, space_id):
        return space_id in self.silent


def _wanted(
    *,
    refs=None,
    spaces=None,
    seats=None,
    fed=None,
    contact=None,
    coalesce_s=0.0,
    clock=None,
):
    kw = {} if clock is None else {"clock": clock}
    return SpaceMediaWanted(
        media_refs=refs or _Refs(),
        space_repo=spaces or _Spaces(),
        remote_members=seats or _Seats(),
        federation=fed or _Fed({HOST: V92}),
        host_contact=contact or _Contact(),
        own_instance_id=SELF,
        coalesce_s=coalesce_s,
        **kw,
    )


def _names(sent) -> list[str]:
    return [f["name"] for f in sent[2]["files"]]


# ── Requester ────────────────────────────────────────────────────────────


async def test_a_view_asks_the_host_for_the_file_and_its_item():
    a = _sc("a")
    fed = _Fed({HOST: V92})
    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed)
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


async def test_names_of_one_space_are_coalesced_into_one_request():
    a, b = _sc("a"), _sc("b")
    refs = _Refs({a: [(SPACE, "p1", "t")], b: [(SPACE, "p2", "t")]})
    fed = _Fed({HOST: V92})
    w = _wanted(refs=refs, fed=fed, coalesce_s=0.2)
    w.schedule(a)
    w.schedule(a)  # already waiting: once only
    w.schedule(b)
    await w.drain()
    assert len(fed.sent) == 1
    assert sorted(_names(fed.sent[0])) == sorted([a, b])


async def test_a_burst_of_misses_goes_in_requests_of_at_most_50_names():
    """A gallery grid opening 60 missing pictures at once: every name is
    asked for, 50 per request."""
    names = [_sc_n(i) for i in range(MAX_WANTED_FILES + 10)]
    refs = _Refs({n: [(SPACE, f"p{i}", "t")] for i, n in enumerate(names)})
    fed = _Fed({HOST: V92})
    w = _wanted(refs=refs, fed=fed, coalesce_s=0.05)
    for n in names:
        w.schedule(n)
    await w.drain()
    assert [_names(s) for s in fed.sent] == [
        names[:MAX_WANTED_FILES],
        names[MAX_WANTED_FILES:],
    ]


async def test_names_of_two_spaces_go_to_each_space_in_its_own_request():
    a = _sc("a")
    b = space_content_media_name("sp-2", "b" * 64, "x.webp")
    refs = _Refs({a: [(SPACE, "p1", "t")], b: [("sp-2", "p2", "t")]})
    fed = _Fed({HOST: V92})
    w = _wanted(refs=refs, fed=fed)
    w.schedule(a)
    w.schedule(b)
    await w.drain()
    assert sorted((s[3], tuple(_names(s))) for s in fed.sent) == [
        (SPACE, (a,)),
        ("sp-2", (b,)),
    ]


async def test_names_past_the_pending_bound_are_dropped_without_cooldown(monkeypatch):
    monkeypatch.setattr(mod, "MAX_PENDING_NAMES", 2)
    names = [_sc_n(i) for i in range(3)]
    refs = _Refs({n: [(SPACE, f"p{i}", "t")] for i, n in enumerate(names)})
    fed = _Fed({HOST: V92})
    w = _wanted(refs=refs, fed=fed)
    for n in names:
        w.schedule(n)
    await w.drain()
    assert sorted(n for s in fed.sent for n in _names(s)) == sorted(names[:2])
    # The dropped one is asked on its next view, not 10 minutes later.
    w.schedule(names[2])
    await w.drain()
    assert _names(fed.sent[-1]) == [names[2]]


async def test_the_cooldown_map_stays_bounded(monkeypatch):
    monkeypatch.setattr(mod, "MAX_PENDING_NAMES", 2)
    names = [_sc_n(i) for i in range(3)]
    refs = _Refs({n: [(SPACE, f"p{i}", "t")] for i, n in enumerate(names)})
    w = _wanted(refs=refs)
    for n in names:
        w.schedule(n)
        await w.drain()
    assert len(w._asked) == 2


async def test_a_name_without_a_space_row_is_not_left_on_cooldown():
    """Not queued (no live row yet) → asked again on the next view, not
    10 minutes later."""
    a = _sc("a")
    refs = _Refs()
    fed = _Fed({HOST: V92})
    w = _wanted(refs=refs, fed=fed)
    w.schedule(a)
    await w.drain()
    assert fed.sent == []
    refs.rows[a] = [(SPACE, "p1", "t")]  # the item arrived since
    w.schedule(a)
    await w.drain()
    assert [_names(s) for s in fed.sent] == [[a]]


async def test_a_name_a_follower_household_skipped_is_not_left_on_cooldown():
    a = _sc("a")
    spaces = _Spaces(role=SpaceRole.SUBSCRIBER)
    fed = _Fed({HOST: V92})
    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), spaces=spaces, fed=fed)
    w.schedule(a)
    await w.drain()
    assert fed.sent == []  # a follower keeps the GFS on-demand path (#1317)
    spaces.role = SpaceRole.MEMBER  # joined as a member since
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 1


async def test_a_name_whose_lookup_failed_is_not_left_on_cooldown():
    a = _sc("a")

    class _Flaky(_Refs):
        calls = 0

        async def space_rows_of(self, basename):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db busy")
            return await super().space_rows_of(basename)

    fed = _Fed({HOST: V92})
    w = _wanted(refs=_Flaky({a: [(SPACE, "p1", "t")]}), fed=fed)
    w.schedule(a)
    await w.drain()  # contained
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 1


async def test_a_non_space_or_unsafe_name_is_never_looked_up():
    """Only space file names are asked for: a content name (``sc-``) or a
    sender-bound one (``sd-``, asked of its author's household)."""
    refs = _Refs()
    w = _wanted(refs=refs)
    for name in ("a.webp", "sg-abc.webp", "sc-../x", "sd-../x", ""):
        w.schedule(name)
    await w.drain()
    assert refs.asked == []


async def test_a_name_is_asked_again_only_after_its_cooldown():
    a = _sc("a")
    now = [1000.0]
    fed = _Fed({HOST: V92})
    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed, clock=lambda: now[0])
    w.schedule(a)
    await w.drain()
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 1
    now[0] += NAME_COOLDOWN_S
    w.schedule(a)
    await w.drain()
    assert len(fed.sent) == 2


def _helper_case(*, contact=None, fail=(), host_version=V92):
    a = _sc("a")
    helpers = ["h-z", "h-a", SELF, HOST, "h-m"]
    fed = _Fed({**{h: V92 for h in helpers}, HOST: host_version}, fail=set(fail))
    seats = _Seats(helpers)
    w = _wanted(
        refs=_Refs({a: [(SPACE, "p1", "t")]}), seats=seats, fed=fed, contact=contact
    )
    w.schedule(a)
    return w, fed, seats


def _rotated() -> list[str]:
    ordered = ["h-a", "h-m", "h-z"]
    start = helper_offset(SELF, SPACE) % len(ordered)
    return ordered[start:] + ordered[:start]


async def test_a_host_that_answers_is_the_only_one_asked():
    """The host online (however slow): a helper never learns what a member
    views."""
    w, fed, seats = _helper_case()
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST]
    assert seats.asked == []


async def test_an_unreachable_host_sends_the_names_to_one_helper():
    w, fed, seats = _helper_case(fail={HOST})
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST, _rotated()[0]]
    # Helpers are admin / moderator seats — the owner is the host.
    assert seats.asked == [HELPER_ROLES]


async def test_a_silent_hosts_batch_goes_to_one_helper_only():
    """The host left its last sync round unanswered: the batch goes to ONE
    helper and not also to the host — never both (a send to an offline
    host's GFS mailbox "succeeds" yet nothing would answer it)."""
    w, fed, _seats = _helper_case(contact=_Contact({SPACE}))
    await w.drain()
    assert [s[0] for s in fed.sent] == [_rotated()[0]]


async def test_a_silent_host_is_asked_when_no_helper_takes_the_batch():
    """No helper reachable: the host still gets it (its mailbox keeps it
    until it is back)."""
    w, fed, _seats = _helper_case(contact=_Contact({SPACE}), fail=set(_rotated()))
    await w.drain()
    assert [s[0] for s in fed.sent] == [*_rotated()[:MAX_HELPERS], HOST]


async def test_a_failed_helper_send_tries_the_next_up_to_the_cap():
    first, second, third = _rotated()
    w, fed, _seats = _helper_case(fail={HOST, first, second, third})
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST, first, second]
    assert MAX_HELPERS == 2


async def test_an_older_host_that_answers_is_left_to_its_catch_up():
    """A host below v_92 cannot answer; while it is not silent no helper is
    asked (its §25.6 catch-up heals the file). Silent: a helper is."""
    w, fed, _seats = _helper_case(host_version=V92 - 1)
    await w.drain()
    assert fed.sent == []
    w, fed, _seats = _helper_case(host_version=V92 - 1, contact=_Contact({SPACE}))
    await w.drain()
    assert [s[0] for s in fed.sent] == [_rotated()[0]]


async def test_a_helper_below_v92_is_never_asked():
    a = _sc("a")
    fed = _Fed({HOST: V92, "h-a": V92 - 1}, fail={HOST})
    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), seats=_Seats(["h-a"]), fed=fed)
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == [HOST]


async def test_we_never_ask_ourselves_as_the_host():
    a = _sc("a")
    fed = _Fed({SELF: V92})
    w = _wanted(
        refs=_Refs({a: [(SPACE, "p1", "t")]}), spaces=_Spaces(host=SELF), fed=fed
    )
    w.schedule(a)
    await w.drain()
    assert fed.sent == []


async def _delegated_space(db) -> tuple[SqliteSpaceRepo, SqliteSpaceRemoteMemberRepo]:
    """``SPACE`` owned by ``owner-hh``; we were invited by the delegated
    admin household ``admin-hh`` (host-equivalent here, like the scheduler's
    BEGIN target); ``mod-hh`` holds a moderator seat (a trusted helper)."""
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('me','u-me','M')"
    )
    await spaces.save(
        Space(
            id=SPACE,
            name="S",
            owner_instance_id="owner-hh",
            owner_username="o",
            identity_public_key="aa" * 32,
            config_sequence=1,
            features=SpaceFeatures(delegated_admin_authority=True),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await spaces.save_member(
        SpaceMember(space_id=SPACE, user_id="u-me", role="member", joined_at="t")
    )
    for instance_id, role in (
        ("admin-hh", SpaceRole.ADMIN),
        ("mod-hh", SpaceRole.MODERATOR),
    ):
        await seats.add(
            space_id=SPACE,
            instance_id=instance_id,
            user_id=f"u-{instance_id}",
            user_pk=None,
            display_name="A",
            role=role.value,
        )
    await spaces.set_invited_by(SPACE, "admin-hh", 0)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await spaces.set_owner_statement(
        SPACE,
        json.dumps(
            {"authority_pk": "aa" * 32, "authority_key_epoch": 0, "issued_at": now_ms}
        ),
    )
    return spaces, seats


async def test_the_inviting_admin_acting_as_host_is_asked_as_the_host(db):
    """ "The host" is the household acting as host here — the owner, or the
    delegated admin that invited us (the scheduler BEGINs both): an owner
    below v_92 cannot answer, the inviting admin can, so it gets the
    request as the host and no helper learns what we view."""
    spaces, seats = await _delegated_space(db)
    a = _sc("a")
    fed = _Fed({"owner-hh": V92 - 1, "admin-hh": V92, "mod-hh": V92})
    w = SpaceMediaWanted(
        media_refs=_Refs({a: [(SPACE, "p1", "t")]}),
        space_repo=spaces,
        remote_members=seats,
        federation=fed,
        host_contact=_Contact(),
        own_instance_id=SELF,
        coalesce_s=0.0,
    )
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == ["admin-hh"]


async def test_hosts_are_asked_owner_first_and_never_again_as_helpers(db):
    """Owner first, then the inviting admin; both unreachable → a helper
    that is not one of them."""
    spaces, seats = await _delegated_space(db)
    a = _sc("a")
    fed = _Fed(
        {"owner-hh": V92, "admin-hh": V92, "mod-hh": V92},
        fail={"owner-hh", "admin-hh"},
    )
    w = SpaceMediaWanted(
        media_refs=_Refs({a: [(SPACE, "p1", "t")]}),
        space_repo=spaces,
        remote_members=seats,
        federation=fed,
        host_contact=_Contact(),
        own_instance_id=SELF,
        coalesce_s=0.0,
    )
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == ["owner-hh", "admin-hh", "mod-hh"]


async def test_stop_ends_a_drain_waiting_for_its_window():
    a = _sc("a")
    fed = _Fed({HOST: V92})
    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=fed, coalesce_s=60.0)
    w.schedule(a)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(w.stop(), timeout=2)
    assert fed.sent == []
    w.schedule(_sc("b"))  # after stop: nothing scheduled
    await w.drain()
    assert fed.sent == []


async def test_a_send_error_is_contained():
    a, b = _sc("a"), _sc("b")

    class _Boom(_Fed):
        async def send_with_mesh_fallback(self, **kw):
            raise RuntimeError("down")

    w = _wanted(refs=_Refs({a: [(SPACE, "p1", "t")]}), fed=_Boom({HOST: V92}))
    w.schedule(a)
    await w.drain()  # never raises
    w.schedule(b)
    await w.drain()


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

    def ship(self, owner, urls):
        if self.all or self.fills or owner in self.own:
            return list(urls)
        return []


class _Gates:
    def __init__(self, gate):
        self.gate = gate
        self.asked: list[tuple[str, bool]] = []

    async def for_space(self, space_id, *, fills=False):
        # SPACE_MEDIA_WANTED proved the bytes: it always asks with fills.
        self.asked.append((space_id, fills))
        return self.gate


class _Outbox:
    def __init__(self):
        self.queued: list[dict] = []

    async def enqueue_for_blob(self, **kw):
        self.queued.append(kw)


class _Roster:
    """The roster mirror: household → its seat roles in SPACE."""

    def __init__(self, seats):
        self.seats: dict[str, str] = dict(seats)

    async def list_instances_with_roles(self, space_id, roles):
        if space_id != SPACE:
            return []
        return [iid for iid, role in self.seats.items() if role in roles]


def _provider(tmp_path, items, *, gate=None, roster=None, clock=None, budget=None):
    outbox = _Outbox()
    roster = roster or _Roster({"req": "member"})
    p = SpaceMediaWantedProvider(
        member_gate=SpaceWriterHouseholds(roster, own_instance_id=SELF).may_receive,
        media_refs=_ItemFiles(items),
        file_gates=_Gates(gate or _Gate(all_files=True)),
        hasher=MediaFileHasher(tmp_path),
        media_sync=outbox,
        media_dir=tmp_path,
        budget=budget or SpaceFileBudget(clock=clock or (lambda: 0.0)),
        own_instance_id=SELF,
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
            "wire_names": {a: a},
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
    assert outbox.queued[0]["wire_names"] == {"orig.webp": name}
    # A different file's content name matches nothing.
    assert await p.handle(_event([{"item": "p1", "name": _sc("f")}])) == 0


async def test_a_non_member_household_gets_nothing(tmp_path):
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    p, outbox = _provider(
        tmp_path, {(SPACE, "p1"): [(a, "u1")]}, roster=_Roster({"other": "member"})
    )
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 0
    assert outbox.queued == []


async def test_a_follower_only_household_gets_nothing(tmp_path):
    """Only a household holding a writer seat asks with SPACE_MEDIA_WANTED
    (the same predicate as the §25.6 member-only data): a follower-only
    household keeps its GFS's v_83 on-demand path."""
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    roster = _Roster({"req": "subscriber"})
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1")]}, roster=roster)
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 0
    assert outbox.queued == []
    for role in ("member", "moderator", "admin", "owner"):
        roster.seats["req"] = role
        p._budget = SpaceFileBudget()
        assert await p.handle(_event([{"item": "p1", "name": a}])) == 1, role


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


async def test_a_helper_ships_another_persons_file_it_holds_under_a_legacy_name(
    tmp_path,
):
    """A trusted helper that got the item first as the public GFS copy (no
    hash map) keeps the file under its own ``sg-`` name. The bytes hash to
    the asked-for content name, so it is a hashed file: the helper ships
    it (the receiver checks the bytes against its row's hash anyway)."""
    data = b"someone else's picture"
    legacy = "sg-0123456789abcdef0123456789abcdef01234567.webp"
    (tmp_path / legacy).write_bytes(data)
    name = space_content_media_name(SPACE, hashlib.sha256(data).hexdigest(), legacy)
    items = {(SPACE, "p1"): [(legacy, "u-other")]}
    helper, outbox = _provider(tmp_path, items, gate=_Gate(fills=True))
    assert await helper.handle(_event([{"item": "p1", "name": name}])) == 1
    assert outbox.queued[0]["media_urls"] == [legacy]
    # Sent under the content name: a receiver refuses a reserved ``sg-`` one.
    assert outbox.queued[0]["wire_names"] == {legacy: name}
    plain, outbox = _provider(tmp_path, items, gate=_Gate())
    assert await plain.handle(_event([{"item": "p1", "name": name}])) == 0


async def test_the_budget_is_50_files_and_32_mib_per_requester_and_space(
    tmp_path, caplog
):
    now = [0.0]
    names = [_sc_n(i) for i in range(BUDGET_MAX_FILES + 1)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    items = {(SPACE, f"p{i}"): [(n, "u1")] for i, n in enumerate(names)}
    roster = _Roster({"req": "member"})
    p, _outbox = _provider(tmp_path, items, roster=roster, clock=lambda: now[0])
    first = [
        {"item": f"p{i}", "name": n} for i, n in enumerate(names[:MAX_WANTED_FILES])
    ]
    assert await p.handle(_event(first)) == BUDGET_MAX_FILES
    with caplog.at_level(logging.INFO):
        assert await p.handle(_event([{"item": "p50", "name": names[50]}])) == 0
    assert "budget" in caplog.text
    # Another requester has its own budget; after the window, so does this one.
    roster.seats["req-2"] = "member"
    assert await p.handle(_event([{"item": "p50", "name": names[50]}], sender="req-2"))
    now[0] += BUDGET_WINDOW_S
    assert await p.handle(_event([{"item": "p50", "name": names[50]}])) == 1


async def test_an_exhausted_budget_drops_the_request_before_any_lookup(tmp_path):
    """Over the (requester, space) window: the whole request is dropped
    before a single item query or hash."""
    names = [_sc_n(i) for i in range(BUDGET_MAX_FILES + 1)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    items = {(SPACE, f"p{i}"): [(n, "u1")] for i, n in enumerate(names)}
    p, _outbox = _provider(tmp_path, items)
    first = [
        {"item": f"p{i}", "name": n} for i, n in enumerate(names[:MAX_WANTED_FILES])
    ]
    assert await p.handle(_event(first)) == BUDGET_MAX_FILES
    looked: list[tuple[str, str]] = []
    real = p._refs.live_item_files

    async def _counting(space_id, item_id):
        looked.append((space_id, item_id))
        return await real(space_id, item_id)

    p._refs.live_item_files = _counting
    assert await p.handle(_event([{"item": "p50", "name": names[50]}])) == 0
    assert looked == []


async def test_the_byte_budget_skips_a_file_that_would_pass_it(tmp_path, monkeypatch):
    monkeypatch.setattr(file_gate_mod, "BUDGET_MAX_BYTES", 15)
    a, b = _sc("a"), _sc("b")
    (tmp_path / a).write_bytes(b"x" * 10)
    (tmp_path / b).write_bytes(b"y" * 10)
    items = {(SPACE, "p1"): [(a, "u1")], (SPACE, "p2"): [(b, "u1")]}
    p, _outbox = _provider(tmp_path, items)
    files = [{"item": "p1", "name": a}, {"item": "p2", "name": b}]
    assert await p.handle(_event(files)) == 1


async def test_files_a_resume_already_sent_count_against_the_same_budget(tmp_path):
    """ONE budget per (requester, space) across SPACE_MEDIA_WANTED and the
    resume's files: a window the resume spent drops the request."""
    a = _sc("a")
    (tmp_path / a).write_bytes(b"x")
    budget = SpaceFileBudget(clock=lambda: 0.0)
    for _ in range(BUDGET_MAX_FILES):
        assert budget.take("req", SPACE, 1)
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [(a, "u1")]}, budget=budget)
    assert await p.handle(_event([{"item": "p1", "name": a}])) == 0
    assert outbox.queued == []


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


async def test_the_host_that_misses_a_file_asks_the_items_author_household():
    """The host itself lacks a member's picture (its push was lost on a
    stale route): it asks the household of the item's author — which holds
    the file as its own people's — before any helper."""
    a = _sc("a")
    refs = _Refs({a: [(SPACE, "p1", "t")]})
    refs.owners = {"p1": "u-d"}
    fed = _Fed({"hh-d": V92, "h-a": V92})
    seats = _Seats(["h-a"], seats=[("hh-d", "u-d"), ("h-a", "u-a")])
    w = _wanted(refs=refs, spaces=_Spaces(host=SELF), seats=seats, fed=fed)
    w.schedule(a)
    await w.drain()
    assert [s[0] for s in fed.sent] == ["hh-d"]
    assert fed.sent[0][2]["files"] == [{"item": "p1", "name": a}]


async def test_a_sender_bound_name_is_asked_of_the_items_author_household_only():
    """A legacy (unhashed) file shows a sender-bound ``sd-`` name: only the
    household it is bound to — the item's author's, which delivered it —
    can match it, so neither the host nor a helper is asked."""
    sd = space_direct_media_name(SPACE, "hh-d", "orig.webp")
    refs = _Refs({sd: [(SPACE, "p1", "t")]})
    refs.owners = {"p1": "u-d"}
    for spaces in (_Spaces(), _Spaces(host=SELF)):
        fed = _Fed({"hh-d": V92, "h-a": V92, HOST: V92})
        seats = _Seats(["h-a"], seats=[("hh-d", "u-d"), ("h-a", "u-a")])
        w = _wanted(refs=refs, spaces=spaces, seats=seats, fed=fed)
        w.schedule(sd)
        await w.drain()
        assert [s[0] for s in fed.sent] == ["hh-d"]
        assert fed.sent[0][2]["files"] == [{"item": "p1", "name": sd}]


async def test_the_author_ships_a_file_under_its_own_sender_bound_name(tmp_path):
    """The author's household matches ``sd(S, us, basename)`` of its own
    file and sends it under the basename — the requester binds it to us
    again, so it lands under the very name its row shows."""
    (tmp_path / "orig.webp").write_bytes(b"d's picture")
    sd = space_direct_media_name(SPACE, SELF, "orig.webp")
    p, outbox = _provider(tmp_path, {(SPACE, "p1"): [("orig.webp", "u1")]})
    assert await p.handle(_event([{"item": "p1", "name": sd}])) == 1
    assert outbox.queued[0]["media_urls"] == ["orig.webp"]
    assert outbox.queued[0].get("wire_names") is None
    # Bound to another household: not ours to deliver.
    other = space_direct_media_name(SPACE, "hh-x", "orig.webp")
    assert await p.handle(_event([{"item": "p1", "name": other}])) == 0
