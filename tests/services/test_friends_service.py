"""Unit tests for :mod:`socialhome.services.friends_service` — in-memory stubs."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.user import RemoteUser, User, UserStatus
import pytest

from socialhome.services.friends_service import FriendNotFoundError, FriendsService

_FUTURE = "2099-01-01T00:00:00+00:00"
_PAST = "2000-01-01T00:00:00+00:00"


class _Users:
    def __init__(self, local, remote, blocked=()):
        self.local = local
        self.remote = remote
        self.blocked = blocked
        self.calls: list[str] = []

    async def list_active(self):
        self.calls.append("list_active")
        return list(self.local)

    async def list_all_known_remote(self):
        self.calls.append("list_all_known_remote")
        return list(self.remote)

    async def get_by_user_id(self, user_id):
        self.calls.append("get_by_user_id")
        return next((u for u in self.local if u.user_id == user_id), None)

    async def get_remote(self, user_id):
        self.calls.append("get_remote")
        return next((r for r in self.remote if r.user_id == user_id), None)

    async def list_blocked(self, user_id):
        self.calls.append("list_blocked")
        return [(uid, "2026-01-01") for uid in self.blocked]


class _Fed:
    def __init__(self, instances, local=None):
        self.instances = instances
        self.local = local

    async def get_local_identity(self):
        return self.local

    async def list_social_instances(self):
        return list(self.instances)


class _CP:
    def __init__(self, counterparts):
        self.counterparts = counterparts

    async def guardian_block_counterparts(self, user_id):
        return frozenset(self.counterparts.get(user_id, ()))


class _Online:
    def __init__(self, online=(), seen=None):
        self.online = set(online)
        self.seen = seen or {}

    def is_online(self, uid):
        return uid in self.online

    def is_idle(self, uid):
        return False

    def last_seen(self, uid):
        return self.seen.get(uid)


class _Aliases:
    def __init__(self, aliases):
        self.aliases = aliases
        self.calls = 0

    async def resolve_users(self, viewer, targets):
        self.calls += 1
        return {t: self.aliases[t] for t in targets if t in self.aliases}


def _inst(iid: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


def _ru(uid: str, iid: str, **kw) -> RemoteUser:
    return RemoteUser(
        user_id=uid, instance_id=iid, remote_username=uid, display_name=uid, **kw
    )


def _u(uid: str, **kw) -> User:
    return User(user_id=uid, username=uid, display_name=uid, **kw)


def _members(payload: dict) -> dict[str, dict]:
    out = {m["user_id"]: m for m in payload["instance"]["members"]}
    for h in payload["households"]:
        out.update({m["user_id"]: m for m in h["members"]})
    return out


async def test_status_and_bio_for_local_and_remote():
    users = _Users(
        [
            _u("me", bio="Hello", status=UserStatus(emoji="🌴", text="Away")),
            _u("stale", status=UserStatus(text="old", expires_at=_PAST)),
            _u("blank", bio=""),
        ],
        [
            _ru(
                "r1",
                "peer-a",
                bio="Remote bio",
                status_json=UserStatus(text="Busy", expires_at=_FUTURE).to_json(),
            ),
            _ru(
                "r2",
                "peer-a",
                status_json=UserStatus(text="x", expires_at=_PAST).to_json(),
            ),
        ],
    )
    svc = FriendsService(users, _Fed([_inst("peer-a")]))
    m = _members(await svc.overview("me"))
    assert m["me"]["status"] == {"emoji": "🌴", "text": "Away", "expires_at": None}
    assert m["me"]["bio"] == "Hello"
    assert m["stale"]["status"] is None
    assert m["blank"]["bio"] is None
    assert m["r1"]["status"] == {"emoji": None, "text": "Busy", "expires_at": _FUTURE}
    assert m["r1"]["bio"] == "Remote bio"
    assert m["r2"]["status"] is None
    assert m["r2"]["bio"] is None


async def test_personal_and_guardian_blocks_hidden():
    users = _Users(
        [_u("me"), _u("blocked-local"), _u("cp-local")],
        [
            _ru("blocked-remote", "peer-a"),
            _ru("cp-remote", "peer-a"),
            _ru("ok", "peer-a"),
        ],
        blocked=("blocked-local", "blocked-remote"),
    )
    cp = _CP({"me": {"cp-local", "cp-remote"}})
    svc = FriendsService(users, _Fed([_inst("peer-a")]), child_protection=cp)
    payload = await svc.overview("me")
    assert set(_members(payload)) == {"me", "ok"}
    assert payload["households"][0]["member_count"] == 1
    assert payload["totals"] == {"households": 2, "people": 2}


async def test_remote_users_grouped_by_social_household_only():
    users = _Users(
        [_u("me")],
        [_ru("a1", "peer-a"), _ru("b1", "peer-b"), _ru("x1", "not-social")],
    )
    svc = FriendsService(users, _Fed([_inst("peer-b"), _inst("peer-a")]))
    payload = await svc.overview("me")
    assert [h["instance_id"] for h in payload["households"]] == ["peer-b", "peer-a"]
    assert [m["user_id"] for m in payload["households"][0]["members"]] == ["b1"]
    assert "x1" not in _members(payload)


async def test_reads_are_batched_regardless_of_household_count():
    """No N+1: one remote-user read for any number of households, one alias
    resolve for every member."""
    insts = [_inst(f"peer-{i}") for i in range(5)]
    users = _Users([_u("me")], [_ru(f"r{i}", f"peer-{i}") for i in range(5)])
    aliases = _Aliases({"r3": "Grandma"})
    svc = FriendsService(users, _Fed(insts), alias_resolver=aliases)
    payload = await svc.overview("me")
    assert users.calls.count("list_all_known_remote") == 1
    assert aliases.calls == 1
    assert _members(payload)["r3"]["personal_alias"] == "Grandma"


async def test_presence_triple_local_and_remote():
    seen = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    users = _Users(
        [_u("me", last_seen_at="2026-09-01 10:00:00"), _u("live")],
        [_ru("r-on", "peer-a"), _ru("r-off", "peer-a")],
    )
    online = _Online(online={"live", "r-on"}, seen={"live": seen, "r-on": seen})
    svc = FriendsService(users, _Fed([_inst("peer-a")]), online_status=online)
    m = _members(await svc.overview("me"))
    # Offline local → the persisted column.
    assert m["me"]["is_online"] is False
    assert m["me"]["last_seen_at"] == "2026-09-01 10:00:00"
    assert m["live"]["is_online"] is True
    assert m["live"]["last_seen_at"] == seen.isoformat()
    assert m["r-on"]["last_seen_at"] == seen.isoformat()
    assert m["r-off"]["last_seen_at"] is None


async def test_without_optional_services_and_local_identity():
    users = _Users([_u("me", picture_hash="0123456789abcdef")], [])
    payload = await FriendsService(users, _Fed([])).overview("me")
    assert payload["instance"]["instance_id"] is None
    assert payload["instance"]["display_name"] == "Home"
    me = payload["instance"]["members"][0]
    assert me["is_online"] is False
    assert me["personal_alias"] is None
    assert me["picture_url"] == "api/users/me/picture?v=0123456789abcdef"
    assert payload["totals"] == {"households": 1, "people": 1}


async def test_instance_dict_never_leaks_keys():
    users = _Users([_u("me")], [])
    payload = await FriendsService(
        users,
        _Fed([_inst("peer-a")], local={"instance_id": "self", "display_name": "Us"}),
    ).overview("me")
    h = payload["households"][0]
    for key in ("remote_inbox_url", "key_self_to_remote", "remote_identity_pk"):
        assert key not in h
    assert payload["instance"]["display_name"] == "Us"


# ─── person() — one row for the person sheet ─────────────────────────────


def _svc(users, instances=(), *, local=None, cp=None, online=None, aliases=None):
    return FriendsService(
        users,
        _Fed(list(instances), local=local),
        child_protection=cp,
        online_status=online,
        alias_resolver=aliases,
    )


async def test_person_local_row_has_own_household():
    users = _Users([_u("oma", bio="Knits", status=UserStatus(emoji="☕"))], [])
    svc = _svc(
        users,
        local={"instance_id": "me-inst", "display_name": "Vizeli"},
        online=_Online(online={"oma"}),
        aliases=_Aliases({"oma": "Grandma"}),
    )
    row = await svc.person("viewer", "oma")
    assert row["is_local"] is True
    assert row["username"] == "oma"
    assert row["personal_alias"] == "Grandma"
    assert row["is_online"] is True
    assert row["bio"] == "Knits"
    assert row["status"]["emoji"] == "☕"
    assert row["household"] == {
        "instance_id": "me-inst",
        "display_name": "Vizeli",
        "is_own": True,
    }
    # Never touches the remote table for a local hit.
    assert "get_remote" not in users.calls


async def test_person_local_household_falls_back_to_home():
    svc = _svc(_Users([_u("a")], []))
    row = await svc.person("viewer", "a")
    assert row["household"] == {
        "instance_id": None,
        "display_name": "Home",
        "is_own": True,
    }


async def test_person_remote_row_has_paired_household():
    svc = _svc(
        _Users([], [_ru("r1", "peer", bio="Hi", status_json='{"text": "away"}')]),
        [_inst("peer")],
    )
    row = await svc.person("viewer", "r1")
    assert row["is_local"] is False
    assert row["instance_id"] == "peer"
    assert row["bio"] == "Hi"
    assert row["status"]["text"] == "away"
    assert row["household"] == {
        "instance_id": "peer",
        "display_name": "peer",
        "is_own": False,
    }


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "blocked",
        "guardian",
        "inactive",
        "deleted",
        "deprovisioned",
        "unpaired",
    ],
)
async def test_person_hidden_people_raise_not_found(case):
    local = [
        _u("off", state="inactive"),
        _u("gone", deleted_at=_PAST),
        _u("blk"),
        _u("grd"),
    ]
    remote = [_ru("dep", "peer", deprovisioned_at=_PAST), _ru("away", "other")]
    target = {
        "unknown": "nobody",
        "blocked": "blk",
        "guardian": "grd",
        "inactive": "off",
        "deleted": "gone",
        "deprovisioned": "dep",
        "unpaired": "away",
    }[case]
    svc = _svc(
        _Users(local, remote, blocked=("blk",)),
        [_inst("peer")],
        cp=_CP({"viewer": {"grd"}}),
    )
    with pytest.raises(FriendNotFoundError):
        await svc.person("viewer", target)
