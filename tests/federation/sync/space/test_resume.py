"""Unit tests for :class:`SpaceSyncResumeProvider` (spec §4.4 / §11452)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.domain.post import Post, PostType
from socialhome.federation.sync.space.resume import (
    MAX_POSTS_PER_RESUME,
    SpaceSyncResumeProvider,
)


class _FakeFederation:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )


class _FakeSpaceRepo:
    def __init__(self, members: list[str]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members)


class _FakePostRepo:
    def __init__(self, posts: list[Post]) -> None:
        self._posts = posts
        self.last_since: str | None = None
        self.last_limit: int | None = None

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Post]:
        self.last_since = since
        self.last_limit = limit
        return [p for p in self._posts if p.created_at.isoformat() > since][:limit]


def _post(i: int, at: datetime) -> Post:
    return Post(
        id=f"p-{i}",
        author="alice",
        type=PostType.TEXT,
        created_at=at,
        content=f"hello {i}",
    )


def _event(from_instance: str, payload: dict, *, space_id: str = "sp-1"):
    return SimpleNamespace(
        event_type=FederationEventType.SPACE_SYNC_RESUME,
        from_instance=from_instance,
        space_id=space_id,
        payload=payload,
    )


@pytest.fixture
def provider_factory():
    """Build a ``SpaceSyncResumeProvider`` with the given posts + members."""

    def _factory(*, posts, members):
        fed = _FakeFederation()
        post_repo = _FakePostRepo(posts)
        provider = SpaceSyncResumeProvider(
            federation_service=fed,
            space_repo=_FakeSpaceRepo(members),
            space_post_repo=post_repo,
        )
        return provider, fed, post_repo

    return _factory


# ── Inbound (provider replays missed events) ────────────────────────


async def test_handle_request_replays_posts_since(provider_factory):
    """Each post newer than ``since`` is re-emitted as SPACE_POST_CREATED.

    ``since`` is exclusive — an equal timestamp is filtered out, matching
    the SQL ``created_at > ?`` clause in ``list_since``.
    """
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    # Three posts strictly after ``base``.
    posts = [_post(i, base + timedelta(minutes=i + 1)) for i in range(3)]
    provider, fed, _ = provider_factory(posts=posts, members=["peer-a"])

    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 3
    types = {s["type"] for s in fed.sent}
    assert types == {FederationEventType.SPACE_POST_CREATED}
    assert [s["payload"]["id"] for s in fed.sent] == ["p-0", "p-1", "p-2"]
    assert all(s["to"] == "peer-a" for s in fed.sent)
    assert all(s["space_id"] == "sp-1" for s in fed.sent)


async def test_handle_request_drops_non_member(provider_factory):
    """Spec §S-1 — peer that isn't a space member is silently dropped."""
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    posts = [_post(0, base + timedelta(minutes=1))]
    provider, fed, _ = provider_factory(
        posts=posts,
        members=["someone-else"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 0
    assert fed.sent == []


async def test_handle_request_rejects_malformed_since(provider_factory):
    """A non-ISO-8601 ``since`` is dropped before the SQL query runs."""
    provider, fed, post_repo = provider_factory(
        posts=[],
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": "yesterday"}),
    )
    assert sent == 0
    assert fed.sent == []
    # The repo was never queried.
    assert post_repo.last_since is None


async def test_handle_request_missing_fields(provider_factory):
    """Empty space_id or since → no-op, no replay."""
    provider, fed, _ = provider_factory(
        posts=[_post(0, datetime(2026, 4, 1, tzinfo=timezone.utc))],
        members=["peer-a"],
    )
    # Both event.space_id and payload['space_id'] absent → drop.
    assert (
        await provider.handle_request(
            _event("peer-a", {"since": "2026-04-01T00:00:00+00:00"}, space_id=""),
        )
        == 0
    )
    # Missing 'since' → drop.
    assert await provider.handle_request(_event("peer-a", {"space_id": "sp-1"})) == 0
    assert fed.sent == []


async def test_handle_request_uses_max_posts_cap(provider_factory):
    """Repo query is bounded by MAX_POSTS_PER_RESUME."""
    provider, _, post_repo = provider_factory(
        posts=[],
        members=["peer-a"],
    )
    await provider.handle_request(
        _event(
            "peer-a",
            {"space_id": "sp-1", "since": "2026-04-01T00:00:00+00:00"},
        ),
    )
    assert post_repo.last_limit == MAX_POSTS_PER_RESUME


async def test_handle_request_payload_matches_inbound_shape(provider_factory):
    """Replayed payload keys match what _post_from_payload expects."""
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    posts = [_post(0, base + timedelta(seconds=1))]
    provider, fed, _ = provider_factory(posts=posts, members=["peer-a"])
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    payload = fed.sent[0]["payload"]
    # Same keys the SPACE_POST_CREATED inbound handler reads.
    assert {"id", "author", "type", "content", "occurred_at"} <= set(payload)
    assert payload["type"] == "text"


# ── Outbound (requester sends RESUME) ───────────────────────────────


async def test_send_request_emits_canonical_payload(provider_factory):
    """``send_request`` posts SPACE_SYNC_RESUME to the right peer."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    await provider.send_request(
        space_id="sp-1",
        instance_id="peer-b",
        since="2026-04-01T00:00:00+00:00",
    )
    assert fed.sent == [
        {
            "to": "peer-b",
            "type": FederationEventType.SPACE_SYNC_RESUME,
            "payload": {
                "space_id": "sp-1",
                "since": "2026-04-01T00:00:00+00:00",
            },
            "space_id": "sp-1",
        }
    ]


async def test_send_request_validates_inputs(provider_factory):
    """Empty fields produce a no-op, not an error."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    await provider.send_request(space_id="", instance_id="peer-b", since="x")
    await provider.send_request(space_id="sp-1", instance_id="", since="x")
    await provider.send_request(space_id="sp-1", instance_id="peer-b", since="")
    assert fed.sent == []
