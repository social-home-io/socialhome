"""Tests for PersonSummaryService — names for bare user ids (§Privacy)."""

from __future__ import annotations

from dataclasses import dataclass

from socialhome.domain.user import PersonSummary, RemoteUser, User
from socialhome.services.alias_service import AliasResolver
from socialhome.services.person_summary_service import PersonSummaryService


class _FakeUserRepo:
    def __init__(self, local: list[User], remote: list[RemoteUser]) -> None:
        self.local = {u.user_id: u for u in local}
        self.remote = {r.user_id: r for r in remote}
        self.calls: list[str] = []

    async def list_by_ids(self, user_ids: set[str]) -> list[User]:
        self.calls.append("list_by_ids")
        return [self.local[u] for u in user_ids if u in self.local]

    async def list_remote_by_ids(self, user_ids: set[str]) -> list[RemoteUser]:
        self.calls.append("list_remote_by_ids")
        return [self.remote[u] for u in user_ids if u in self.remote]


@dataclass
class _Inst:
    id: str
    effective_display_name: str


class _FakeFederationRepo:
    def __init__(self, own_name: str | None, instances: list[_Inst]) -> None:
        self.own_name = own_name
        self.instances = instances
        self.calls: list[str] = []

    async def get_local_identity(self) -> dict | None:
        self.calls.append("get_local_identity")
        if self.own_name is None:
            return None
        return {"instance_id": "self", "display_name": self.own_name}

    async def list_instances(self) -> list[_Inst]:
        self.calls.append("list_instances")
        return self.instances


class _FakeAliasRepo:
    def __init__(self, aliases: dict[tuple[str, str], str]) -> None:
        self.aliases = aliases
        self.calls = 0

    async def get_user_aliases(self, viewer_user_id, target_user_ids):
        self.calls += 1
        return {
            t: self.aliases[(viewer_user_id, t)]
            for t in target_user_ids
            if (viewer_user_id, t) in self.aliases
        }


def _svc(
    *,
    local: list[User] | None = None,
    remote: list[RemoteUser] | None = None,
    own_name: str | None = "Vizeli",
    instances: list[_Inst] | None = None,
    aliases: dict[tuple[str, str], str] | None = None,
) -> tuple[PersonSummaryService, _FakeUserRepo, _FakeFederationRepo, _FakeAliasRepo]:
    users = _FakeUserRepo(local or [], remote or [])
    fed = _FakeFederationRepo(own_name, instances or [])
    alias_repo = _FakeAliasRepo(aliases or {})
    return (
        PersonSummaryService(users, fed, AliasResolver(alias_repo)),  # type: ignore[arg-type]
        users,
        fed,
        alias_repo,
    )


def _ruth(**kw) -> RemoteUser:
    return RemoteUser(
        user_id="ru-ruth",
        instance_id="peer-v",
        remote_username="ruth",
        display_name="Grandma Ruth",
        **kw,
    )


async def test_empty_ids_touch_no_repo():
    svc, users, fed, aliases = _svc()
    assert await svc.summarize("uid-me", []) == {}
    assert users.calls == [] and fed.calls == [] and aliases.calls == 0


async def test_local_person_is_named_after_own_household():
    svc, *_ = _svc(
        local=[
            User(
                username="tablet",
                user_id="uid-tab",
                display_name="Guest room tablet",
                picture_hash="0123456789abcdef",
            )
        ],
    )
    out = await svc.summarize("uid-me", ["uid-tab"])
    assert out["uid-tab"] == PersonSummary(
        user_id="uid-tab",
        display_name="Guest room tablet",
        personal_alias=None,
        picture_url="api/users/uid-tab/picture?v=0123456789abcdef",
        household="Vizeli",
    )


async def test_own_household_falls_back_when_identity_has_no_name():
    svc, *_ = _svc(
        local=[User(username="bob", user_id="uid-bob", display_name="Bob")],
        own_name=None,
    )
    out = await svc.summarize("uid-me", ["uid-bob"])
    assert out["uid-bob"].household == "Home"
    assert out["uid-bob"].picture_url is None


async def test_remote_person_with_household_and_viewer_nickname():
    svc, *_ = _svc(
        remote=[_ruth(picture_hash="fedcba9876543210")],
        instances=[_Inst("peer-v", "Vizeli")],
        aliases={("uid-me", "ru-ruth"): "Oma"},
    )
    out = await svc.summarize("uid-me", ["ru-ruth"])
    assert out["ru-ruth"].display_name == "Grandma Ruth"
    assert out["ru-ruth"].personal_alias == "Oma"
    assert out["ru-ruth"].household == "Vizeli"
    assert out["ru-ruth"].picture_url == (
        "api/users/ru-ruth/picture?v=fedcba9876543210"
    )


async def test_nickname_is_per_viewer():
    svc, *_ = _svc(
        remote=[_ruth()],
        instances=[_Inst("peer-v", "Vizeli")],
        aliases={("uid-other", "ru-ruth"): "Mum"},
    )
    out = await svc.summarize("uid-me", ["ru-ruth"])
    assert out["ru-ruth"].personal_alias is None


async def test_remote_person_whose_household_is_not_paired():
    svc, *_ = _svc(remote=[_ruth()], instances=[])
    out = await svc.summarize("uid-me", ["ru-ruth"])
    assert out["ru-ruth"].display_name == "Grandma Ruth"
    assert out["ru-ruth"].household is None


async def test_deprovisioned_remote_person_keeps_their_name():
    svc, *_ = _svc(
        remote=[_ruth(deprovisioned_at="2026-10-01T00:00:00+00:00")],
        instances=[_Inst("peer-v", "Vizeli")],
    )
    out = await svc.summarize("uid-me", ["ru-ruth"])
    assert out["ru-ruth"].display_name == "Grandma Ruth"


async def test_unknown_id_has_no_name_but_keeps_a_nickname():
    """Never the raw id: an id nobody knows has ``display_name=None``; a
    nickname the viewer set before the household went away still shows."""
    svc, *_ = _svc(aliases={("uid-me", "ru-gone"): "Uncle Joe"})
    out = await svc.summarize("uid-me", ["ru-gone"])
    assert out["ru-gone"] == PersonSummary(
        user_id="ru-gone",
        display_name=None,
        personal_alias="Uncle Joe",
        picture_url=None,
        household=None,
    )


async def test_mixed_list_resolves_in_a_fixed_number_of_reads():
    """No N+1: one read per source however long the list is, and the
    household tables are only read when someone needs them."""
    local = [
        User(username=f"u{i}", user_id=f"uid-{i}", display_name=f"U{i}")
        for i in range(5)
    ]
    remote = [
        RemoteUser(
            user_id=f"ru-{i}",
            instance_id="peer-v",
            remote_username=f"r{i}",
            display_name=f"R{i}",
        )
        for i in range(5)
    ]
    svc, users, fed, aliases = _svc(
        local=local, remote=remote, instances=[_Inst("peer-v", "Vizeli")]
    )
    ids = [u.user_id for u in local] + [r.user_id for r in remote] + ["ru-x"]
    out = await svc.summarize("uid-me", ids)
    assert set(out) == set(ids)
    assert users.calls == ["list_by_ids", "list_remote_by_ids"]
    assert sorted(fed.calls) == ["get_local_identity", "list_instances"]
    assert aliases.calls == 1


async def test_household_reads_are_skipped_when_unneeded():
    svc, _users, fed, _ = _svc()
    await svc.summarize("uid-me", ["ru-gone"])
    assert fed.calls == []


async def test_enrich_merges_summary_and_keeps_row_order_and_keys():
    svc, *_ = _svc(
        local=[User(username="bob", user_id="uid-bob", display_name="Bob")],
        remote=[_ruth()],
        instances=[_Inst("peer-v", "Vizeli")],
    )
    rows = [
        {"user_id": "ru-ruth", "blocked_at": "2026-10-15 10:00:00"},
        {"user_id": "uid-bob", "blocked_at": "2026-10-14 10:00:00"},
    ]
    out = await svc.enrich("uid-me", rows)
    assert [r["user_id"] for r in out] == ["ru-ruth", "uid-bob"]
    assert out[0] == {
        "user_id": "ru-ruth",
        "blocked_at": "2026-10-15 10:00:00",
        "display_name": "Grandma Ruth",
        "personal_alias": None,
        "picture_url": None,
        "household": "Vizeli",
    }
    assert out[1]["household"] == "Vizeli"


async def test_enrich_empty_rows():
    svc, *_ = _svc()
    assert await svc.enrich("uid-me", []) == []


class _Cp:
    def register_gate(self, gate):
        pass

    async def guardian_block_counterparts(self, user_id):
        return (
            frozenset({"uid-kid", "ru-ruth"}) if user_id == "uid-bob" else frozenset()
        )


async def test_guardian_block_counterpart_is_named_without_a_picture():
    """§CP.F2: across a guardian block (either way) a person reads by name
    only — the picture is withheld, local or remote."""
    svc, *_ = _svc(
        local=[
            User(
                username="kid",
                user_id="uid-kid",
                display_name="Kid",
                picture_hash="0123456789abcdef",
            )
        ],
        remote=[_ruth(picture_hash="fedcba9876543210")],
        instances=[_Inst("peer-v", "Grandma's")],
    )
    svc.attach_child_protection(_Cp())  # type: ignore[arg-type]
    out = await svc.summarize("uid-bob", ["uid-kid", "ru-ruth"])
    assert out["uid-kid"].display_name == "Kid"
    assert out["uid-kid"].picture_url is None
    assert out["ru-ruth"].display_name == "Grandma Ruth"
    assert out["ru-ruth"].picture_url is None
    other = await svc.summarize("uid-carol", ["uid-kid", "ru-ruth"])
    assert other["uid-kid"].picture_url
    assert other["ru-ruth"].picture_url
