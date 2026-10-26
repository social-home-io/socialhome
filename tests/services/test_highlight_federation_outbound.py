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
    HighlightAudienceChanged,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.domain.highlight import HighlightFrame, HighlightFrameType
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


def _audience_by_target(fed) -> dict[str, list[str]]:
    return {
        c.kwargs["to_instance_id"]: c.kwargs["payload"]["audience"]
        for c in fed.send_event.call_args_list
    }


@pytest.mark.parametrize("is_first", [True, False])
async def test_users_audience_is_trimmed_to_the_targets_own_people(stack, is_first):
    out, fed, fed_repo, user_repo = stack

    async def _home(uid):
        return {
            "uid-author": "self",
            "uid-housemate": "self",
            "uid-bob": "peer-bob",
            "uid-bea": "peer-bob",
            "uid-carol": "peer-carol",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_social_instances = AsyncMock(return_value=[])
    audience = ("uid-housemate", "uid-bob", "uid-carol", "uid-bea", "uid-gone")
    event = _frame_event(is_first=is_first, audience_kind="users", audience=audience)
    await out._on_frame_added(event)
    assert _audience_by_target(fed) == {
        "peer-bob": ["uid-bob", "uid-bea"],
        "peer-carol": ["uid-carol"],
    }
    # The author's own event keeps the whole list.
    assert event.audience == audience


@pytest.mark.parametrize("is_first", [True, False])
async def test_households_audience_is_trimmed_to_the_target(stack, is_first):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(
            is_first=is_first,
            audience_kind="households",
            audience=("peer-a", "peer-b", "self"),
        ),
    )
    assert _audience_by_target(fed) == {"peer-a": ["peer-a"], "peer-b": ["peer-b"]}


async def test_all_paired_audience_is_empty_for_every_target(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    # A stray list on an all_paired event never rides along.
    await out._on_frame_added(
        _frame_event(audience_kind="all_paired", audience=("peer-a", "uid-x")),
    )
    assert _audience_by_target(fed) == {"peer-a": [], "peer-b": []}


async def test_trimmed_payloads_are_separate_copies(stack):
    out, fed, fed_repo, user_repo = stack

    async def _home(uid):
        return {"uid-author": "self", "uid-bob": "peer-bob"}.get(uid) or ("peer-carol")

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_social_instances = AsyncMock(return_value=[])
    await out._on_frame_added(
        _frame_event(audience_kind="users", audience=("uid-bob", "uid-carol")),
    )
    payloads = [c.kwargs["payload"] for c in fed.send_event.call_args_list]
    assert len(payloads) == 2
    assert payloads[0] is not payloads[1]
    assert payloads[0]["audience"] != payloads[1]["audience"]


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


# ─── §CP.F2: nothing of a protected account reaches a blocked household ──


class _HouseholdBlockCp:
    """``uid-kid`` is protected; a guardian blocked a user homed on
    ``peer-blocked``."""

    def register_gate(self, gate):
        pass

    async def guardian_blocks_household(self, user_id, instance_id):
        return user_id == "uid-kid" and instance_id == "peer-blocked"


async def test_protected_authors_content_skips_a_blocked_persons_household(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_HouseholdBlockCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-ok"), _peer("peer-blocked")],
    )
    await _FIRE(out, "uid-kid")
    assert {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list} == {
        "peer-ok"
    }
    fed.send_event.reset_mock()
    # Anyone else's content still reaches both.
    await _FIRE(out, "uid-author")
    assert {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list} == {
        "peer-ok",
        "peer-blocked",
    }


async def _FIRE(out, author):
    await out._on_frame_added(_frame_event(author=author, is_first=True))


# ─── Change for all of today (HighlightAudienceChanged) ──────────────────


def _hl_frames(n: int) -> tuple[HighlightFrame, ...]:
    return tuple(
        HighlightFrame(
            id=f"f-{i}",
            highlight_id="s-1",
            sequence=i,
            frame_type=HighlightFrameType.VIDEO if i == 2 else HighlightFrameType.IMAGE,
            media_url=f"/api/media/{i}.webp",
            caption_text=f"caption {i}",
            duration_ms=4000 if i == 2 else None,
        )
        for i in range(1, n + 1)
    )


def _audience_event(
    old_kind: str,
    old: tuple[str, ...],
    kind: str,
    new: tuple[str, ...],
    *,
    frames: int = 3,
    author: str = "uid-author",
) -> HighlightAudienceChanged:
    return HighlightAudienceChanged(
        highlight_id="s-1",
        author_user_id=author,
        highlight_date="2026-05-05",
        expires_at="2026-06-04T00:00:00Z",
        old_audience_kind=old_kind,
        old_audience=old,
        audience_kind=kind,
        audience=new,
        frames=_hl_frames(frames),
    )


_HOMES = {
    "uid-author": "self",
    "uid-housemate": "self",
    "uid-bob": "peer-bob",
    "uid-bea": "peer-bob",
    "uid-dora": "peer-dora",
}


def _sent(fed) -> list[tuple[str, FederationEventType, dict]]:
    return [
        (c.kwargs["to_instance_id"], c.kwargs["event_type"], c.kwargs["payload"])
        for c in fed.send_event.call_args_list
    ]


@pytest.fixture
def audience_stack(stack):
    out, fed, fed_repo, user_repo = stack

    async def _home(uid):
        return _HOMES.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-bob"), _peer("peer-dora")],
    )
    return out, fed


async def test_narrowing_deletes_at_dropped_households_only(audience_stack):
    """All paired → Bob's household: Dora's drops out (one small
    HIGHLIGHT_DELETED, no audience, no frames); Bob's stays but its slice
    changed (kind), so it gets the highlight re-sent with its own slice."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("all_paired", (), "households", ("peer-bob",))
    )
    sent = _sent(fed)
    assert [(to, ev) for to, ev, _ in sent if to == "peer-dora"] == [
        ("peer-dora", FederationEventType.HIGHLIGHT_DELETED)
    ]
    deleted = next(p for to, _, p in sent if to == "peer-dora")
    assert set(deleted) == {"highlight_id", "author_user_id", "occurred_at"}
    bob = [(ev, p) for to, ev, p in sent if to == "peer-bob"]
    assert [ev for ev, _ in bob] == [FederationEventType.HIGHLIGHT_CREATED]
    assert bob[0][1]["audience_kind"] == "households"
    assert bob[0][1]["audience"] == ["peer-bob"]
    # Re-sent as the first frame of the day so an older receiver re-upserts.
    assert bob[0][1]["frame_id"] == "f-1"


async def test_narrowing_people_within_a_household_regates_it(audience_stack):
    """Bob + Bea → only Bea: Bob's household stays and gets the trimmed
    list (Bea only); Dora's drops out."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event(
            "users",
            ("uid-bob", "uid-bea", "uid-dora", "uid-housemate"),
            "users",
            ("uid-bea", "uid-housemate"),
        )
    )
    sent = _sent(fed)
    assert {(to, ev) for to, ev, _ in sent} == {
        ("peer-bob", FederationEventType.HIGHLIGHT_CREATED),
        ("peer-dora", FederationEventType.HIGHLIGHT_DELETED),
    }
    created = next(p for to, ev, p in sent if to == "peer-bob")
    assert created["audience"] == ["uid-bea"]


async def test_an_unchanged_slice_sends_nothing(audience_stack):
    """Dropping only a housemate changes nothing any peer stores."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("users", ("uid-bob", "uid-housemate"), "users", ("uid-bob",))
    )
    assert _sent(fed) == []


async def test_widening_sends_the_day_only_to_new_households(audience_stack):
    """Bob's household → all paired: Dora's is new and gets the day
    (CREATED with frame 1, then APPENDED for each later frame, in order);
    Bob's only gets its slice re-sent (kind changed), not the frames."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("households", ("peer-bob",), "all_paired", (), frames=3)
    )
    sent = _sent(fed)
    dora = [(ev, p) for to, ev, p in sent if to == "peer-dora"]
    assert [ev for ev, _ in dora] == [
        FederationEventType.HIGHLIGHT_CREATED,
        FederationEventType.HIGHLIGHT_FRAME_APPENDED,
        FederationEventType.HIGHLIGHT_FRAME_APPENDED,
    ]
    assert [p["frame_id"] for _, p in dora] == ["f-1", "f-2", "f-3"]
    assert [p["sequence"] for _, p in dora] == [1, 2, 3]
    assert all(p["audience"] == [] for _, p in dora)
    assert all(p["audience_kind"] == "all_paired" for _, p in dora)
    assert dora[1][1]["frame_type"] == "video"
    assert dora[1][1]["duration_ms"] == 4000
    assert dora[2][1]["caption_text"] == "caption 3"
    bob = [ev for to, ev, _ in sent if to == "peer-bob"]
    assert bob == [FederationEventType.HIGHLIGHT_CREATED]


async def test_widening_people_on_a_household_already_in_regates(audience_stack):
    """Bea → Bea + Bob: same household, wider slice — one re-sent CREATED,
    never the frames again."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("users", ("uid-bea",), "users", ("uid-bea", "uid-bob"))
    )
    sent = _sent(fed)
    assert [(to, ev) for to, ev, _ in sent] == [
        ("peer-bob", FederationEventType.HIGHLIGHT_CREATED)
    ]
    assert sorted(sent[0][2]["audience"]) == ["uid-bea", "uid-bob"]


async def test_a_new_household_with_no_frames_left_gets_nothing(audience_stack):
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("households", (), "households", ("peer-dora",), frames=0)
    )
    assert _sent(fed) == []


async def test_a_kept_household_with_no_frames_left_is_regated(audience_stack):
    """No frames left: the re-sent CREATED carries no frame, only the
    highlight row's new audience."""
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event(
            "users", ("uid-bob", "uid-bea"), "users", ("uid-bea",), frames=0
        )
    )
    [(to, ev, payload)] = _sent(fed)
    assert (to, ev) == ("peer-bob", FederationEventType.HIGHLIGHT_CREATED)
    assert "frame_id" not in payload
    assert payload["audience"] == ["uid-bea"]


async def test_hidden_households_get_nothing(stack):
    out, fed, fed_repo, user_repo = stack

    async def _home(uid):
        return _HOMES.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-bob"), _peer("peer-dora")],
    )
    out._visibility_repo = _FakeVisibilityRepo({"peer-dora": frozenset({"uid-author"})})
    await out._on_audience_changed(_audience_event("households", (), "all_paired", ()))
    assert {to for to, _, _ in _sent(fed)} == {"peer-bob"}


async def test_a_remote_authors_change_is_never_re_fanned(audience_stack):
    out, fed = audience_stack
    await out._on_audience_changed(
        _audience_event("all_paired", (), "households", (), author="uid-bob")
    )
    assert _sent(fed) == []
