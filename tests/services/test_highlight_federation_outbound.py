"""Outbound highlight federation — :class:`HighlightFederationOutbound`.

Covers:
* First frame → ``HIGHLIGHT_CREATED``; subsequent frames → ``HIGHLIGHT_FRAME_APPENDED``.
* ``audience_kind = 'all_paired'`` fans to every paired peer.
* ``audience_kind = 'households'`` fans to the listed instance_ids only.
* ``audience_kind = 'users'`` resolves user_ids → home instances.
* Echo-loop guard: events whose author lives on a peer instance never
  re-fan to peers (so inbound republished events stay local).
* ``HighlightFrameRemoved`` / ``HighlightRemoved`` map to the matching FET.
* Visibility filter: peers that have hidden the author are skipped in fan-out;
  back-channel sends are suppressed when the local viewer / reactor is hidden.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import (
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.services.highlight_federation_outbound import (
    HighlightFederationOutbound,
)


class _FakeVisibilityRepo:
    """In-memory stub for AbstractPeerUserVisibilityRepo used in unit tests."""

    def __init__(self, hidden: dict[str, frozenset[str]] | None = None) -> None:
        # Maps peer_instance_id → frozenset of hidden local user_ids
        self._hidden: dict[str, frozenset[str]] = hidden or {}

    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        return self._hidden.get(peer_id, frozenset())


def _frame_event(
    *,
    author: str = "uid-author",
    is_first: bool = True,
    audience_kind: str = "all_paired",
    audience: tuple[str, ...] = (),
) -> HighlightFrameAdded:
    return HighlightFrameAdded(
        highlight_id="s-1",
        frame_id="f-1",
        author_user_id=author,
        highlight_date="2026-05-05",
        sequence=1,
        is_first_frame=is_first,
        audience_kind=audience_kind,
        audience=audience,
        frame_type="image",
        media_url="/api/media/x.webp",
        caption_text=None,
        caption_emoji=None,
        duration_ms=None,
        expires_at="2026-06-04T00:00:00Z",
    )


@pytest.fixture
def stack():
    """Wire a HighlightFederationOutbound against fully mocked deps."""
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()  # not used in unit tests; .wire() is exercised separately
    out = HighlightFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
    )
    return out, federation, federation_repo, user_repo


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


async def test_first_frame_fans_highlight_created_to_all_paired(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    await out._on_frame_added(_frame_event(is_first=True))
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-a", "peer-b"}
    assert all(c["event_type"] is FederationEventType.HIGHLIGHT_CREATED for c in sent)


async def test_subsequent_frame_uses_frame_appended(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_added(_frame_event(is_first=False))
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.HIGHLIGHT_FRAME_APPENDED
    )


async def test_remote_author_skips_echo_loop(stack):
    """When the author lives on a peer, the same DomainEvent must not re-fan."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_added(_frame_event())
    assert fed.send_event.call_count == 0


async def test_users_audience_resolves_each_user_to_home_instance(stack):
    out, fed, fed_repo, user_repo = stack

    # Author is local; audience = two specific users on two peers.
    async def _home(uid):
        return {
            "uid-author": "self",
            "uid-bob": "peer-bob",
            "uid-carol": "peer-carol",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_social_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(audience_kind="users", audience=("uid-bob", "uid-carol")),
    )
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert sent == {"peer-bob", "peer-carol"}


async def test_households_audience_uses_listed_instance_ids(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(
            audience_kind="households",
            audience=("peer-a", "peer-b", "self"),  # own filtered out
        ),
    )
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert sent == {"peer-a", "peer-b"}


async def test_frame_removed_maps_to_frame_deleted(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_frame_removed(
        HighlightFrameRemoved(
            highlight_id="s-1",
            frame_id="f-1",
            author_user_id="uid-author",
            audience_kind="all_paired",
            audience=(),
        ),
    )
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.HIGHLIGHT_FRAME_DELETED
    )


async def test_highlight_removed_maps_to_highlight_deleted(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_highlight_removed(
        HighlightRemoved(
            highlight_id="s-1",
            author_user_id="uid-author",
            audience_kind="all_paired",
            audience=(),
        ),
    )
    assert fed.send_event.call_args.kwargs["event_type"] is (
        FederationEventType.HIGHLIGHT_DELETED
    )


async def test_own_instance_filtered_from_paired_peers(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("self"), _peer("peer-a")],
    )
    await out._on_frame_added(_frame_event())
    assert fed.send_event.call_count == 1
    assert fed.send_event.call_args.kwargs["to_instance_id"] == "peer-a"


async def test_send_failure_does_not_abort_other_peers(stack):
    """A bad peer doesn't stop fan-out to the rest of the audience."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-bad"), _peer("peer-ok")],
    )

    async def _maybe_fail(**kwargs):
        if kwargs["to_instance_id"] == "peer-bad":
            raise RuntimeError("peer down")

    fed.send_event.side_effect = _maybe_fail
    await out._on_frame_added(_frame_event())
    sent = [c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list]
    assert sent == ["peer-bad", "peer-ok"] or sent == ["peer-ok", "peer-bad"]


# ─── Back-channel: viewer/reactor → author ───────────────────────────────


async def test_frame_viewed_unicast_to_author_home(stack):
    """A local viewer's view receipt is sent only to the author's home."""
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {
            "uid-viewer": "self",  # local viewer
            "uid-author": "peer-author",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_frame_viewed(
        HighlightFrameViewed(
            highlight_id="s-1",
            frame_id="f-1",
            viewer_user_id="uid-viewer",
            author_user_id="uid-author",
        ),
    )
    fed.send_event.assert_awaited_once()
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["to_instance_id"] == "peer-author"
    assert kwargs["event_type"] is FederationEventType.HIGHLIGHT_FRAME_VIEWED


async def test_frame_viewed_skipped_when_viewer_remote(stack):
    """The author's instance gets the inbound republished event but
    must not re-fan it back to the viewer's instance."""
    out, fed, _fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    await out._on_frame_viewed(
        HighlightFrameViewed(
            highlight_id="s-1",
            frame_id="f-1",
            viewer_user_id="uid-viewer",  # remote
            author_user_id="uid-author",
        ),
    )
    fed.send_event.assert_not_called()


async def test_frame_viewed_skipped_when_author_local_too(stack):
    """Local viewer + local author = nothing to federate."""
    out, fed, _fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    await out._on_frame_viewed(
        HighlightFrameViewed(
            highlight_id="s-1",
            frame_id="f-1",
            viewer_user_id="uid-viewer",
            author_user_id="uid-author",
        ),
    )
    fed.send_event.assert_not_called()


async def test_reaction_set_uses_reacted_event_type(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {"uid-reactor": "self", "uid-author": "peer-author"}.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        HighlightFrameReactionChanged(
            highlight_id="s-1",
            frame_id="f-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        ),
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.HIGHLIGHT_FRAME_REACTED
    assert kwargs["payload"]["emoji"] == "🔥"


async def test_reaction_cleared_uses_reaction_removed_event_type(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {"uid-reactor": "self", "uid-author": "peer-author"}.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        HighlightFrameReactionChanged(
            highlight_id="s-1",
            frame_id="f-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji=None,  # cleared
        ),
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.HIGHLIGHT_FRAME_REACTION_REMOVED
    assert kwargs["payload"]["emoji"] is None


async def test_reaction_changed_skipped_when_reactor_remote(stack):
    out, fed, _fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    await out._on_reaction_changed(
        HighlightFrameReactionChanged(
            highlight_id="s-1",
            frame_id="f-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        ),
    )
    fed.send_event.assert_not_called()


# ─── Visibility filter tests ──────────────────────────────────────────────────


async def test_fan_to_audience_skips_peer_that_hid_author():
    """Fan-out must skip a peer that has hidden the author."""
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    user_repo = MagicMock()
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    federation_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    # peer-a has hidden uid-author; peer-b has not
    visibility_repo = _FakeVisibilityRepo({"peer-a": frozenset(["uid-author"])})
    out = HighlightFederationOutbound(
        bus=MagicMock(),
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )
    await out._on_frame_added(_frame_event(author="uid-author", is_first=True))
    sent = {c.kwargs["to_instance_id"] for c in federation.send_event.call_args_list}
    assert "peer-b" in sent
    assert "peer-a" not in sent


async def test_back_channel_view_suppressed_when_viewer_hidden_from_author_peer():
    """HighlightFrameViewed must not be sent when the local viewer is hidden
    from the author's home instance."""
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    user_repo = MagicMock()

    async def _home(uid: str) -> str:
        return {"uid-viewer": "self", "uid-author": "peer-author"}.get(uid, "self")

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    # peer-author has hidden uid-viewer
    visibility_repo = _FakeVisibilityRepo({"peer-author": frozenset(["uid-viewer"])})
    out = HighlightFederationOutbound(
        bus=MagicMock(),
        federation_service=federation,
        federation_repo=MagicMock(),
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )
    await out._on_frame_viewed(
        HighlightFrameViewed(
            highlight_id="s-1",
            frame_id="f-1",
            viewer_user_id="uid-viewer",
            author_user_id="uid-author",
        ),
    )
    federation.send_event.assert_not_called()


async def test_back_channel_reaction_suppressed_when_reactor_hidden_from_author_peer():
    """HighlightFrameReactionChanged must not be sent when the local reactor
    is hidden from the author's home instance."""
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    user_repo = MagicMock()

    async def _home(uid: str) -> str:
        return {"uid-reactor": "self", "uid-author": "peer-author"}.get(uid, "self")

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    # peer-author has hidden uid-reactor
    visibility_repo = _FakeVisibilityRepo({"peer-author": frozenset(["uid-reactor"])})
    out = HighlightFederationOutbound(
        bus=MagicMock(),
        federation_service=federation,
        federation_repo=MagicMock(),
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )
    await out._on_reaction_changed(
        HighlightFrameReactionChanged(
            highlight_id="s-1",
            frame_id="f-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        ),
    )
    federation.send_event.assert_not_called()
