"""Tests for the shared visibility mixin."""

from __future__ import annotations

from socialhome.services.visibility import VisibilityMixin


class _FakeRepo:
    def __init__(self, mapping: dict[str, set[str]]) -> None:
        self._mapping = mapping

    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        return frozenset(self._mapping.get(peer_id, set()))


class _BrokenRepo:
    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        raise RuntimeError("db went away")


class _Service(VisibilityMixin):
    """Minimal concrete subclass used to exercise the mixin in isolation."""

    __slots__ = ()

    def __init__(self, visibility_repo) -> None:
        self._visibility_repo = visibility_repo


async def test_hidden_for_peer_returns_set_for_known_peer():
    svc = _Service(_FakeRepo({"peer-1": {"u-alice", "u-bob"}}))
    assert await svc.hidden_for_peer("peer-1") == {"u-alice", "u-bob"}


async def test_hidden_for_peer_returns_empty_for_unknown_peer():
    svc = _Service(_FakeRepo({"peer-1": {"u-alice"}}))
    assert await svc.hidden_for_peer("peer-2") == set()


async def test_hidden_for_peer_returns_empty_when_repo_is_none():
    """Back-compat: tests that don't wire the repo default to visible."""
    svc = _Service(None)
    assert await svc.hidden_for_peer("peer-anything") == set()


async def test_hidden_for_peer_swallows_repo_errors_and_defaults_visible():
    """Fail-soft: a transient repo error must not block federation
    outbound. Default-visible matches the existing
    ``ProfileFederationOutbound`` behaviour."""
    svc = _Service(_BrokenRepo())
    assert await svc.hidden_for_peer("peer-1") == set()
