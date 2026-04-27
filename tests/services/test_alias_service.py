"""Tests for AliasService + AliasResolver (§4.1.6)."""

from __future__ import annotations

import pytest

from socialhome.domain.user import RemoteUser, User
from socialhome.services.alias_service import (
    AliasInvalidError,
    AliasNotFoundError,
    AliasResolver,
    AliasService,
)


class _FakeAliasRepo:
    def __init__(self) -> None:
        self.aliases: dict[tuple[str, str], str] = {}

    async def set_user_alias(self, *, viewer_user_id, target_user_id, alias):
        self.aliases[(viewer_user_id, target_user_id)] = alias

    async def clear_user_alias(self, *, viewer_user_id, target_user_id):
        self.aliases.pop((viewer_user_id, target_user_id), None)

    async def get_user_aliases(self, viewer_user_id, target_user_ids):
        return {
            t: self.aliases[(viewer_user_id, t)]
            for t in target_user_ids
            if (viewer_user_id, t) in self.aliases
        }

    async def list_user_aliases(self, viewer_user_id):
        return {t: a for (v, t), a in self.aliases.items() if v == viewer_user_id}


class _FakeUserRepo:
    def __init__(
        self,
        local: dict[str, User] | None = None,
        remote: dict[str, RemoteUser] | None = None,
    ) -> None:
        self.local = local or {}
        self.remote = remote or {}

    async def get_by_user_id(self, user_id):
        return self.local.get(user_id)

    async def get_remote(self, user_id):
        return self.remote.get(user_id)


def _local(uid: str = "uid-bob") -> User:
    return User(username="bob", user_id=uid, display_name="Bob")


def _remote(uid: str = "uid-r") -> RemoteUser:
    return RemoteUser(
        user_id=uid,
        instance_id="peer-inst",
        remote_username="anna",
        display_name="Anna",
    )


# ─── AliasService.set_user_alias ─────────────────────────────────────────


async def test_set_alias_happy_path():
    aliases = _FakeAliasRepo()
    users = _FakeUserRepo(local={"uid-bob": _local()})
    svc = AliasService(aliases, users)
    await svc.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="Mr B",
    )
    assert aliases.aliases == {("uid-alice", "uid-bob"): "Mr B"}


async def test_set_alias_trims_whitespace():
    aliases = _FakeAliasRepo()
    users = _FakeUserRepo(local={"uid-bob": _local()})
    svc = AliasService(aliases, users)
    await svc.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="  Mr B  ",
    )
    assert aliases.aliases[("uid-alice", "uid-bob")] == "Mr B"


async def test_set_alias_resolves_remote_user():
    aliases = _FakeAliasRepo()
    users = _FakeUserRepo(remote={"uid-anna": _remote("uid-anna")})
    svc = AliasService(aliases, users)
    await svc.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-anna",
        alias="Mom",
    )
    assert aliases.aliases == {("uid-alice", "uid-anna"): "Mom"}


async def test_set_alias_rejects_empty():
    svc = AliasService(_FakeAliasRepo(), _FakeUserRepo())
    with pytest.raises(AliasInvalidError):
        await svc.set_user_alias(
            viewer_user_id="uid-alice",
            target_user_id="uid-bob",
            alias="   ",
        )


async def test_set_alias_rejects_too_long():
    svc = AliasService(_FakeAliasRepo(), _FakeUserRepo(local={"uid-bob": _local()}))
    with pytest.raises(AliasInvalidError):
        await svc.set_user_alias(
            viewer_user_id="uid-alice",
            target_user_id="uid-bob",
            alias="x" * 81,
        )


async def test_set_alias_rejects_self_alias():
    svc = AliasService(
        _FakeAliasRepo(), _FakeUserRepo(local={"uid-alice": _local("uid-alice")})
    )
    with pytest.raises(AliasInvalidError):
        await svc.set_user_alias(
            viewer_user_id="uid-alice",
            target_user_id="uid-alice",
            alias="Me",
        )


async def test_set_alias_unknown_target_raises():
    svc = AliasService(_FakeAliasRepo(), _FakeUserRepo())
    with pytest.raises(AliasNotFoundError):
        await svc.set_user_alias(
            viewer_user_id="uid-alice",
            target_user_id="uid-ghost",
            alias="X",
        )


async def test_set_alias_missing_ids_raises():
    svc = AliasService(_FakeAliasRepo(), _FakeUserRepo())
    with pytest.raises(AliasInvalidError):
        await svc.set_user_alias(
            viewer_user_id="",
            target_user_id="uid-bob",
            alias="X",
        )


# ─── AliasService.clear_user_alias / list_aliases ───────────────────────


async def test_clear_user_alias():
    aliases = _FakeAliasRepo()
    users = _FakeUserRepo(local={"uid-bob": _local()})
    svc = AliasService(aliases, users)
    await svc.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="Mr B",
    )
    await svc.clear_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
    )
    assert aliases.aliases == {}


async def test_list_aliases():
    aliases = _FakeAliasRepo()
    users = _FakeUserRepo(local={"uid-bob": _local()})
    svc = AliasService(aliases, users)
    await svc.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B",
    )
    rows = await svc.list_aliases("uid-alice")
    assert rows == {"uid-bob": "B"}


async def test_list_aliases_empty_viewer_returns_empty():
    svc = AliasService(_FakeAliasRepo(), _FakeUserRepo())
    assert await svc.list_aliases("") == {}


# ─── AliasResolver ──────────────────────────────────────────────────────


async def test_resolver_returns_aliases_for_viewer():
    aliases = _FakeAliasRepo()
    aliases.aliases[("uid-alice", "uid-bob")] = "Mr B"
    resolver = AliasResolver(aliases)
    out = await resolver.resolve_users("uid-alice", ["uid-bob", "uid-c"])
    assert out == {"uid-bob": "Mr B"}


async def test_resolver_returns_empty_for_anonymous_viewer():
    aliases = _FakeAliasRepo()
    aliases.aliases[("uid-alice", "uid-bob")] = "Mr B"
    resolver = AliasResolver(aliases)
    # An empty viewer_id must not leak somebody else's aliases.
    assert await resolver.resolve_users("", ["uid-bob"]) == {}
