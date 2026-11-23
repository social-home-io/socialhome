"""Outbound moment federation — :class:`MomentFederationOutbound`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.domain.federation_retention import relayed_moment_key
from socialhome.moment_origin import NO_RELAY_FIELD, REACH_FIELD, verify_moment_origin
from socialhome.services import moment_federation_outbound as outbound_mod
from socialhome.services.moment_federation_outbound import (
    MomentFederationOutbound,
)

_KEY = generate_identity_keypair()


def _ago(hours: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _federation() -> MagicMock:
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.own_identity_seed = _KEY.private_key
    federation.own_identity_pk = _KEY.public_key
    federation.send_event = AsyncMock()
    return federation


class _FakeVisibilityRepo:
    """In-memory stub for AbstractPeerUserVisibilityRepo used in unit tests."""

    def __init__(self, hidden: dict[str, frozenset[str]] | None = None) -> None:
        self._hidden: dict[str, frozenset[str]] = hidden or {}

    def hide(self, peer: str, user_id: str) -> None:
        existing = self._hidden.get(peer, frozenset())
        self._hidden[peer] = existing | {user_id}

    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer_id, set()))


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


def _create_event(
    *,
    author: str = "uid-author",
    parent: str | None = None,
) -> MomentCreated:
    return MomentCreated(
        moment_id="m-1",
        author_user_id=author,
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=parent,
        parent_author_user_id=None,
        origin_instance_id="self",
        expires_at="2026-06-01T00:00:00+00:00",
    )


@pytest.fixture
def stack():
    federation = _federation()
    federation_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()
    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
    )
    return out, federation, federation_repo, user_repo


# ── Origin fan-out (bus path) ────────────────────────────────────────────


async def test_create_fans_to_all_paired_with_hop_1(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-a"), _peer("peer-b")],
    )
    await out._on_created(_create_event())
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-a", "peer-b"}
    assert all(c["event_type"] is FederationEventType.MOMENT_CREATED for c in sent)
    assert all(c["payload"]["hop_count"] == 1 for c in sent)
    # v_35: every create carries this household's origin signature.
    assert all(
        verify_moment_origin(
            identity_pk=_KEY.public_key,
            event_type=FederationEventType.MOMENT_CREATED,
            payload=c["payload"],
        )
        for c in sent
    )


async def test_remote_author_skips_origin_fan(stack):
    """Inbound republished MomentCreated must not re-fan from the bus."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="peer-source")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_created(_create_event())
    fed.send_event.assert_not_called()


async def test_delete_fans_with_hop_1(stack):
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_deleted(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="uid-author",
            origin_instance_id="self",
        )
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.MOMENT_DELETED
    assert kwargs["payload"]["hop_count"] == 1
    assert verify_moment_origin(
        identity_pk=_KEY.public_key,
        event_type=FederationEventType.MOMENT_DELETED,
        payload=kwargs["payload"],
    )


async def test_relay_forwards_the_origin_signature_verbatim(stack):
    out, fed, fed_repo, _ = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    inbound = {
        "moment_id": "m-1",
        "author_user_id": "uid-far",
        "origin_instance_id": "peer-origin",
        "occurred_at": _ago(),
        "hop_count": 1,
        "origin_sig": "sig",
        "origin_sig_suite": "ed25519",
        "origin_identity_pk": "ab" * 32,
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=inbound,
        from_instance="peer-origin",
    )
    forwarded = fed.send_event.call_args.kwargs["payload"]
    assert forwarded == {**inbound, "hop_count": 2}


async def test_reaction_set_unicasts_to_author_home(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {
            "uid-reactor": "self",
            "uid-author": "peer-author",
        }.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        )
    )
    fed.send_event.assert_awaited_once()
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["to_instance_id"] == "peer-author"
    assert kwargs["event_type"] is FederationEventType.MOMENT_REACTED


async def test_reaction_cleared_uses_reaction_removed(stack):
    out, fed, _fed_repo, user_repo = stack

    async def _home(uid):
        return {"uid-reactor": "self", "uid-author": "peer-x"}.get(uid)

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji=None,
        )
    )
    kwargs = fed.send_event.call_args.kwargs
    assert kwargs["event_type"] is FederationEventType.MOMENT_REACTION_REMOVED


async def test_reaction_skipped_when_author_local(stack):
    """Author + reactor both local → no federation."""
    out, fed, _fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="👍",
        )
    )
    fed.send_event.assert_not_called()


# ── Relay (3-hop) ────────────────────────────────────────────────────────


async def test_relay_inbound_bumps_hop_and_excludes_origin_and_sender(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(
        return_value=[
            _peer("peer-origin"),
            _peer("peer-sender"),
            _peer("peer-onward"),
        ],
    )
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "occurred_at": _ago(),
        "hop_count": 1,
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-onward"}
    # v_96 relay clamp: a copy not sent by its origin is at least hop 2,
    # whatever it claims — passed on as hop 3.
    assert all(c["payload"]["hop_count"] == 3 for c in sent)


async def test_relay_inbound_stops_at_max_hops(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "hop_count": 3,  # already at the cap
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


async def test_relay_inbound_skips_with_invalid_hop(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload={"hop_count": "weird", "origin_instance_id": "peer-origin"},
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


# ── §Momentum-public no-redistribute rule ───────────────────────────────


async def test_relay_inbound_skips_when_received_via_gfs(stack):
    """The §Momentum-public no-redistribute rule: a moment that
    arrived through a GFS public-share fan-out must NOT bleed back
    into the household federation mesh, even when ``hop_count`` is
    still under the 3-hop cap."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-remote",
        "origin_instance_id": "peer-origin",
        "hop_count": 1,  # well under MOMENT_MAX_HOPS
        "received_via": "gfs",  # the no-redistribute marker
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


async def test_relay_inbound_still_relays_household_arrival(stack):
    """Sanity: the guard only short-circuits ``received_via='gfs'``;
    the standard ``household`` arrival path still relays."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-remote",
        "origin_instance_id": "peer-origin",
        "occurred_at": _ago(),
        "hop_count": 1,
        "received_via": "household",
    }
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-origin",
    )
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert {c["to_instance_id"] for c in sent} == {"peer-onward"}
    assert all(c["payload"]["hop_count"] == 2 for c in sent)


async def test_send_failure_swallowed_per_peer(stack):
    """A misbehaving peer doesn't break the relay loop."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-bad"), _peer("peer-ok")],
    )

    async def _maybe_fail(**kwargs):
        if kwargs["to_instance_id"] == "peer-bad":
            raise RuntimeError("peer down")

    fed.send_event.side_effect = _maybe_fail
    # Should NOT raise — should still reach peer-ok.
    await out._on_created(_create_event())
    sent = {c.kwargs["to_instance_id"] for c in fed.send_event.call_args_list}
    assert "peer-ok" in sent


async def test_list_instances_failure_returns_no_peers(stack):
    """A failing federation_repo.list_instances logs and returns []."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(side_effect=RuntimeError("db down"))
    await out._on_created(_create_event())
    fed.send_event.assert_not_called()


# ── Visibility gate ──────────────────────────────────────────────────────


async def test_moment_created_skips_peer_that_hid_author():
    """A peer that hid the local author must not receive MOMENT_CREATED."""
    federation = _federation()
    fed_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()

    visibility_repo = _FakeVisibilityRepo()
    visibility_repo.hide("peer-hider", "uid-author")

    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=fed_repo,
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-hider"), _peer("peer-ok")],
    )
    await out._on_created(_create_event(author="uid-author"))

    sent = {c.kwargs["to_instance_id"] for c in federation.send_event.call_args_list}
    assert sent == {"peer-ok"}


async def test_moment_deleted_skips_peer_that_hid_author():
    """A peer that hid the local author must not receive MOMENT_DELETED."""
    federation = _federation()
    fed_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()

    visibility_repo = _FakeVisibilityRepo()
    visibility_repo.hide("peer-hider", "uid-author")

    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=fed_repo,
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-hider"), _peer("peer-ok")],
    )
    await out._on_deleted(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="uid-author",
            origin_instance_id="self",
        )
    )

    sent = {c.kwargs["to_instance_id"] for c in federation.send_event.call_args_list}
    assert sent == {"peer-ok"}


async def test_moment_reaction_suppressed_when_reactor_hidden_from_author_peer():
    """When the local reactor is hidden from the author's home instance,
    no MOMENT_REACTED (or REACTION_REMOVED) envelope is sent."""
    federation = _federation()
    fed_repo = MagicMock()
    user_repo = MagicMock()
    bus = MagicMock()

    # Hide the local reactor from the remote author's home instance.
    visibility_repo = _FakeVisibilityRepo()
    visibility_repo.hide("peer-author", "uid-reactor")

    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=fed_repo,
        user_repo=user_repo,
        visibility_repo=visibility_repo,
    )

    async def _home(uid: str) -> str:
        return {"uid-reactor": "self", "uid-author": "peer-author"}.get(uid, "self")

    user_repo.get_instance_for_user = AsyncMock(side_effect=_home)
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="🔥",
        )
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

    async def is_protected(self, user_id):
        return False


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
    await out._on_created(_create_event(author=author))


# ─── §CP.R: a protected account's moments stay one hop (v_38) ────────────


class _ProtectedAuthorCp:
    """``uid-kid`` is a protected account."""

    def register_gate(self, gate):
        pass

    async def is_protected(self, user_id):
        return user_id == "uid-kid"

    async def guardian_blocks_household(self, user_id, instance_id):
        return False


async def test_protected_authors_moment_is_no_relay_and_skips_older_peers(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_ProtectedAuthorCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-new"), _peer("peer-old")],
    )

    async def _supports(instance_id, *, min_version):
        return instance_id == "peer-new"

    fed.peer_supports = _supports
    await out._on_created(_create_event(author="uid-kid"))
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    # An older household may relay it onward, so it gets nothing at all.
    assert {c["to_instance_id"] for c in sent} == {"peer-new"}
    assert sent[0]["payload"][NO_RELAY_FIELD] is True
    assert verify_moment_origin(
        identity_pk=_KEY.public_key,
        event_type=FederationEventType.MOMENT_CREATED,
        payload=sent[0]["payload"],
    )


async def test_other_authors_moments_carry_no_mark(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_ProtectedAuthorCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-old")])
    await out._on_created(_create_event(author="uid-author"))
    sent = [c.kwargs for c in fed.send_event.call_args_list]
    assert [c["to_instance_id"] for c in sent] == ["peer-old"]
    assert NO_RELAY_FIELD not in sent[0]["payload"]


async def test_relay_inbound_never_forwards_a_no_relay_moment(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload={
            "moment_id": "m-1",
            "author_user_id": "uid-kid",
            "origin_instance_id": "peer-origin",
            "hop_count": 1,
            NO_RELAY_FIELD: True,
        },
        from_instance="peer-origin",
    )
    fed.send_event.assert_not_called()


# ── Relay seen-map: one relay per moment, unless a copy arrives closer ──


def _relayed(payload_over: dict | None = None) -> dict:
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "occurred_at": _ago(),
        "hop_count": 1,
    }
    payload.update(payload_over or {})
    return payload


async def test_a_second_copy_of_a_relayed_moment_is_not_relayed_again(stack):
    """Before, every copy that reached us (one per relaying neighbour) was
    fanned out again to all our peers."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-x"), _peer("peer-y"), _peer("peer-z")],
    )
    for sender in ("peer-x", "peer-y", "peer-z"):
        await out.relay_inbound(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=_relayed({"hop_count": 2}),
            from_instance=sender,
        )
    assert fed.send_event.await_count == 2  # first copy only: y + z


async def test_a_copy_from_closer_to_the_origin_is_relayed_again(stack):
    """First seen at hop 2 (relayed on with 3: one more hop of reach),
    then at hop 1 from the origin itself — relaying that one reaches two
    more hops, so it goes. A later copy adds nothing."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    for hop, sender in ((2, "peer-sender"), (1, "peer-origin"), (2, "peer-sender")):
        await out.relay_inbound(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=_relayed({"hop_count": hop}),
            from_instance=sender,
        )
    hops = [c.kwargs["payload"]["hop_count"] for c in fed.send_event.call_args_list]
    assert hops == [3, 2]


async def test_create_and_delete_of_one_moment_are_tracked_apart(stack):
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    for event_type in (
        FederationEventType.MOMENT_CREATED,
        FederationEventType.MOMENT_DELETED,
        FederationEventType.MOMENT_DELETED,
    ):
        await out.relay_inbound(
            event_type=event_type,
            payload=_relayed(),
            from_instance="peer-sender",
        )
    types = [c.kwargs["event_type"] for c in fed.send_event.call_args_list]
    assert types == [
        FederationEventType.MOMENT_CREATED,
        FederationEventType.MOMENT_DELETED,
    ]


async def test_a_refused_copy_is_not_remembered(stack):
    """A copy the relay policy refused (an open report) leaves no mark: once
    the report is resolved the next copy still goes."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    policy = MagicMock()
    policy.allow_relay = AsyncMock(side_effect=[False, True])
    out._relay_policy = policy
    for _ in range(2):
        await out.relay_inbound(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=_relayed(),
            from_instance="peer-sender",
        )
    assert fed.send_event.await_count == 1


async def test_the_seen_map_is_bounded(stack, monkeypatch):
    monkeypatch.setattr(outbound_mod, "RELAY_SEEN_MAX", 3)
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    for i in range(5):
        await out.relay_inbound(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=_relayed({"moment_id": f"m-{i}"}),
            from_instance="peer-sender",
        )
    assert len(out._relayed) == 3
    # The oldest fell out: a late copy of it is relayed once more.
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=_relayed({"moment_id": "m-0"}),
        from_instance="peer-sender",
    )
    assert fed.send_event.await_count == 6


async def test_an_entry_expires(stack, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(outbound_mod, "monotonic", lambda: clock[0])
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=_relayed(),
        from_instance="peer-sender",
    )
    clock[0] += outbound_mod.RELAY_SEEN_TTL_S + 1
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=_relayed(),
        from_instance="peer-sender",
    )
    assert fed.send_event.await_count == 2


async def test_a_moment_in_a_31_household_clique_costs_900_envelopes():
    """Every household paired with every other (31 of them), one moment.
    Before the seen-map each household re-relayed every copy it got: the
    origin's 30 + 30 × 29 at hop 2 + 30 × 29 × 28 at hop 3 = 25 260
    envelopes. Now each household relays once, on the first copy: 30 +
    30 × 29 = 900. (Breadth-first, as copies arrive in the mesh.)"""
    n = 31
    ids = [f"h{i}" for i in range(n)]
    queue: list[tuple[str, str, FederationEventType, dict]] = []
    nodes: dict[str, MomentFederationOutbound] = {}
    for iid in ids:
        federation = _federation()
        federation.own_instance_id = iid

        async def _send(*, to_instance_id, event_type, payload, _src=iid, **_kw):
            queue.append((_src, to_instance_id, event_type, payload))

        federation.send_event = AsyncMock(side_effect=_send)
        repo = MagicMock()
        repo.list_social_instances = AsyncMock(
            return_value=[_peer(other) for other in ids if other != iid]
        )
        users = MagicMock()
        users.get_instance_for_user = AsyncMock(return_value="h0")
        nodes[iid] = MomentFederationOutbound(
            bus=MagicMock(),
            federation_service=federation,
            federation_repo=repo,
            user_repo=users,
        )
    await nodes["h0"]._on_created(
        MomentCreated(
            moment_id="m-1",
            author_user_id="uid-author",
            content="hello",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="h0",
            expires_at="2026-06-01T00:00:00+00:00",
        )
    )
    sent = 0
    while queue:
        src, dst, event_type, payload = queue.pop(0)
        sent += 1
        await nodes[dst].relay_inbound(
            event_type=event_type, payload=payload, from_instance=src
        )
    assert sent == (n - 1) + (n - 1) * (n - 2) == 900


# ── v_65: a create older than 48 h is kept, not relayed ────────────────


@pytest.mark.parametrize(
    ("age_h", "relayed"),
    [
        (1, True),
        (47, True),
        (49, False),
        (24 * 20, False),
        (None, False),
        ("not a date", False),
    ],
)
async def test_a_create_is_relayed_only_within_48_hours(stack, age_h, relayed):
    occurred_at = _ago(age_h) if isinstance(age_h, int) else age_h
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=_relayed({"occurred_at": occurred_at}),
        from_instance="peer-sender",
    )
    assert (fed.send_event.await_count == 1) is relayed
    # A refused old create leaves no seen-map mark.
    assert bool(out._relayed) is relayed


async def test_a_delete_is_not_age_gated(stack):
    """Only creates are cut at 48 h: a delete for a month-old moment must
    still reach the households that keep it (the inbound handler relays it
    only when it changed something there)."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_DELETED,
        payload=_relayed({"occurred_at": _ago(24 * 29)}),
        from_instance="peer-sender",
    )
    assert fed.send_event.await_count == 1


def test_the_seen_map_remembers_as_long_as_a_create_relays():
    assert outbound_mod.RELAY_SEEN_TTL_S == 48 * 3600.0


# ── v_67: reactions further away ─────────────────────────────────────────


class _FakeRouter:
    def __init__(self, direct: str | None) -> None:
        self.direct = direct
        self.queued: list[dict] = []
        self.stamped: list[str] = []
        self.direct_moment_ids: list[str | None] = []

    def origin_keywrap_fields(self) -> dict:
        return {"origin_keywrap_pk": "kw", "origin_keywrap_suite": "x25519"}

    def stamp_relayed_create(
        self, payload, *, received_hop, from_instance, to_instance
    ):
        self.stamped.append(to_instance)
        return {**payload, "rt": [f"tok-for-{to_instance}"]}

    async def direct_target(self, author_user_id, *, moment_id=None):
        self.direct_moment_ids.append(moment_id)
        return self.direct

    async def queue_reaction(self, **kwargs):
        self.queued.append(kwargs)
        return True


async def test_a_create_carries_the_origin_key_wrap_key_outside_its_signature(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_reaction_router(_FakeRouter(None))
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_created(_create_event())
    payload = fed.send_event.call_args.kwargs["payload"]
    assert payload["origin_keywrap_pk"] == "kw"
    # The origin signature still verifies: the new fields are outside it.
    assert verify_moment_origin(
        identity_pk=_KEY.public_key,
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
    )


async def test_a_no_relay_create_carries_no_key_wrap_key(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_reaction_router(_FakeRouter(None))
    cp = MagicMock()
    cp.is_protected = AsyncMock(return_value=True)
    cp.guardian_blocks_household = AsyncMock(return_value=False)
    out._child_protection = cp
    fed.peer_supports = AsyncMock(return_value=True)
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-a")])
    await out._on_created(_create_event())
    payload = fed.send_event.call_args.kwargs["payload"]
    assert NO_RELAY_FIELD in payload and "origin_keywrap_pk" not in payload


async def test_a_relayed_create_gets_a_token_per_peer(stack):
    out, fed, fed_repo, _ = stack
    router = _FakeRouter(None)
    out.attach_reaction_router(router)
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-b"), _peer("peer-c")]
    )
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload={
            "moment_id": "m-1",
            "author_user_id": "uid-far",
            "origin_instance_id": "peer-origin",
            "occurred_at": _ago(),
            "hop_count": 1,
        },
        from_instance="peer-origin",
    )
    sent = {
        c.kwargs["to_instance_id"]: c.kwargs["payload"]
        for c in fed.send_event.call_args_list
    }
    assert sent["peer-b"]["rt"] == ["tok-for-peer-b"]
    assert sent["peer-c"]["rt"] == ["tok-for-peer-c"]
    assert sorted(router.stamped) == ["peer-b", "peer-c"]


async def test_a_relayed_delete_gets_no_token(stack):
    out, fed, fed_repo, _ = stack
    router = _FakeRouter(None)
    out.attach_reaction_router(router)
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-b")])
    await out.relay_inbound(
        event_type=FederationEventType.MOMENT_DELETED,
        payload={
            "moment_id": "m-1",
            "author_user_id": "uid-far",
            "origin_instance_id": "peer-origin",
            "occurred_at": _ago(),
            "hop_count": 1,
        },
        from_instance="peer-origin",
    )
    assert router.stamped == []
    assert "rt" not in fed.send_event.call_args.kwargs["payload"]


async def test_a_reaction_to_a_far_author_is_routed(stack):
    out, fed, _fed_repo, user_repo = stack
    router = _FakeRouter(None)
    out.attach_reaction_router(router)
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-far",
            emoji="👏",
        )
    )
    fed.send_event.assert_not_called()
    assert router.queued[0]["moment_id"] == "m-1"
    assert router.queued[0]["emoji"] == "👏"


async def test_a_reaction_to_a_paired_author_keeps_the_direct_path(stack):
    out, fed, _fed_repo, user_repo = stack
    router = _FakeRouter("peer-author")
    out.attach_reaction_router(router)
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    out._visibility_repo = _FakeVisibilityRepo()
    await out._on_reaction_changed(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id="uid-reactor",
            author_user_id="uid-author",
            emoji="👏",
        )
    )
    assert router.queued == []
    # The moment is named so a paired origin is found even without a roster
    # row for its author (#1178 F1).
    assert router.direct_moment_ids == ["m-1"]
    assert fed.send_event.call_args.kwargs["to_instance_id"] == "peer-author"
    assert (
        fed.send_event.call_args.kwargs["event_type"]
        is FederationEventType.MOMENT_REACTED
    )


# ─── v_96: reach ──────────────────────────────────────────────────────────

FET = FederationEventType

#: Paired households by the version they advertise.
_VERSIONS = {"p37": 37, "p38": 38, "p95": 95, "p96": 96}


def _versioned(fed, fed_repo, versions=None, looks=None) -> None:
    versions = versions or _VERSIONS
    looks = looks or {}
    fed_repo.list_social_instances = AsyncMock(
        return_value=[_peer(iid) for iid in versions]
    )
    # How far each peer said its people look (in memory, decision C).
    fed.peer_moment_look_hops = looks.get

    async def _supports(instance_id, *, min_version):
        return versions.get(instance_id, 0) >= min_version

    fed.peer_supports = _supports


def _sent(fed) -> list[dict]:
    return [c.kwargs for c in fed.send_event.call_args_list]


def _targets(fed) -> set[str]:
    return {c["to_instance_id"] for c in _sent(fed)}


def _reach_event(reach: int) -> MomentCreated:
    return MomentCreated(
        moment_id="m-1",
        author_user_id="uid-author",
        content="hello",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="self",
        expires_at="2026-06-01T00:00:00+00:00",
        reach=reach,
    )


@pytest.mark.parametrize(
    ("reach", "mark", "targets"),
    [
        # Reach 1: the v_38 no_relay mark, to households that honour it.
        (1, {NO_RELAY_FIELD: True}, {"p38", "p95", "p96"}),
        # Reach 2: only to v_96 (an older one would relay it to hop 3).
        (2, {REACH_FIELD: 2}, {"p96"}),
        # Reach 3: nothing new, to everyone.
        (3, {}, {"p37", "p38", "p95", "p96"}),
        # Reach 4–5: to every paired household (a direct delivery).
        (4, {REACH_FIELD: 4}, {"p37", "p38", "p95", "p96"}),
        (5, {REACH_FIELD: 5}, {"p37", "p38", "p95", "p96"}),
    ],
)
@pytest.mark.parametrize("kind", ["create", "delete"])
async def test_the_origin_signs_the_reach_and_gates_it(
    stack, reach, mark, targets, kind
):
    """A create and its delete carry the same signed mark and go to the same
    households."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo)
    if kind == "create":
        await out._on_created(_reach_event(reach))
        et = FET.MOMENT_CREATED
    else:
        await out._on_deleted(
            MomentDeleted(
                moment_id="m-1",
                author_user_id="uid-author",
                origin_instance_id="self",
                reach=reach,
            )
        )
        et = FET.MOMENT_DELETED
    assert _targets(fed) == targets
    for call in _sent(fed):
        payload = call["payload"]
        assert {
            k: payload[k] for k in (NO_RELAY_FIELD, REACH_FIELD) if k in payload
        } == (mark)
        assert verify_moment_origin(
            identity_pk=_KEY.public_key, event_type=et, payload=payload
        )


async def test_a_protected_accounts_delete_is_no_relay(stack):
    """The leak on main: a protected account's delete went unmarked and was
    relayed 3 hops. Its row's reach is 1, so the delete is ``no_relay``."""
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_ProtectedAuthorCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo)
    await out._on_deleted(
        MomentDeleted(
            moment_id="m-1",
            author_user_id="uid-kid",
            origin_instance_id="self",
            reach=1,
        )
    )
    assert _targets(fed) == {"p38", "p95", "p96"}
    assert all(c["payload"][NO_RELAY_FIELD] is True for c in _sent(fed))


@pytest.mark.parametrize(
    ("row_reach", "author", "no_relay"),
    [
        # B: posted at reach 3 before its author became protected — it went
        # 3 hops, so its delete goes as far, unmarked.
        (3, "uid-kid", False),
        # A row written before v_96 by an author not protected: 3 hops.
        (None, "uid-author", False),
        # A row written before v_96 by a protected author: its create went
        # ``no_relay`` (one hop), so its delete does too — as on main.
        # Accepted edge: an author protected only after posting a legacy
        # moment gets a one-hop delete for it.
        (None, "uid-kid", True),
    ],
)
async def test_a_delete_follows_the_row(stack, row_reach, author, no_relay):
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_ProtectedAuthorCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo)
    await out._on_deleted(
        MomentDeleted(
            moment_id="m-1",
            author_user_id=author,
            origin_instance_id="self",
            reach=row_reach,
        )
    )
    if no_relay:
        assert _targets(fed) == {"p38", "p95", "p96"}
        assert all(c["payload"][NO_RELAY_FIELD] is True for c in _sent(fed))
    else:
        assert _targets(fed) == {"p37", "p38", "p95", "p96"}
        for call in _sent(fed):
            assert NO_RELAY_FIELD not in call["payload"]
            assert REACH_FIELD not in call["payload"]


async def test_a_protected_accounts_create_is_one_hop_whatever_the_event_says(
    stack,
):
    out, fed, fed_repo, user_repo = stack
    out.attach_child_protection(_ProtectedAuthorCp())
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo)
    event = _reach_event(5)
    await out._on_created(
        MomentCreated(**{**_fields(event), "author_user_id": "uid-kid"})
    )
    assert all(c["payload"][NO_RELAY_FIELD] is True for c in _sent(fed))
    assert all(REACH_FIELD not in c["payload"] for c in _sent(fed))


def _fields(event) -> dict:
    return {f: getattr(event, f) for f in event.__dataclass_fields__}


async def test_a_chosen_reach_1_carries_no_key_wrap_key(stack):
    out, fed, fed_repo, user_repo = stack
    out.attach_reaction_router(_FakeRouter("peer-x"))
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo)
    await out._on_created(_reach_event(1))
    assert all("origin_keywrap_pk" not in c["payload"] for c in _sent(fed))
    fed.send_event.reset_mock()
    await out._on_created(_reach_event(4))
    assert all("origin_keywrap_pk" in c["payload"] for c in _sent(fed))


def _hop_payload(reach: int | None, hop: int, **over) -> dict:
    payload = _relayed({"hop_count": hop, **over})
    if reach == 1:
        payload[NO_RELAY_FIELD] = True
    elif reach is not None and reach != 3:
        payload[REACH_FIELD] = reach
    return payload


# (reach, hop it arrives at, from the origin itself) → forwarded at hop …
# Relays stop at ``min(reach, MOMENT_MAX_HOPS)`` — the cap is 5 (v_96); the
# peer here looks 5 hops, so hops 4–5 reach it (decision C).
_REACH_TABLE = [
    (1, 1, True, None),
    (2, 1, True, 2),
    (2, 2, False, None),
    (3, 1, True, 2),
    (3, 2, False, 3),
    (3, 3, False, None),
    (4, 1, True, 2),
    (4, 2, False, 3),
    (4, 3, False, 4),
    (4, 4, False, None),
    (5, 1, True, 2),
    (5, 2, False, 3),
    (5, 3, False, 4),
    (5, 4, False, 5),
    (5, 5, False, None),
]


@pytest.mark.parametrize(("reach", "hop", "from_origin", "forwarded"), _REACH_TABLE)
@pytest.mark.parametrize("et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED])
async def test_a_relay_honours_the_reach_at_each_hop(
    stack, reach, hop, from_origin, forwarded, et
):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96}, looks={"p96": 5})
    await out.relay_inbound(
        event_type=et,
        payload=_hop_payload(reach, hop),
        from_instance="peer-origin" if from_origin else "peer-sender",
    )
    hops = [c["payload"]["hop_count"] for c in _sent(fed)]
    assert hops == ([] if forwarded is None else [forwarded])
    assert outbound_mod.MOMENT_MAX_HOPS == 5


# ── Decision C: hops 4–5 only toward who looks ──────────────────────────

#: v_96 peers by how far they look (``None``: not heard since our start), and an older one
#: that (impossibly) claims to look 5 hops.
_LOOK_VERSIONS = {"l1": 96, "l3": 96, "l4": 96, "l5": 96, "lx": 96, "old5": 95}
_LOOKS = {"l1": 1, "l3": 3, "l4": 4, "l5": 5, "lx": None, "old5": 5}


@pytest.mark.parametrize(
    ("hop", "targets"),
    [
        # Hops 1–3 are unchanged: every (v_96, for a reach-carrying copy)
        # peer, whatever it looks.
        (1, {"l1", "l3", "l4", "l5", "lx"}),
        (2, {"l1", "l3", "l4", "l5", "lx"}),
        # Hop 4 / 5: only a v_96 peer that looks that far — never one that
        # has not said since our start, never an older one.
        (3, {"l4", "l5"}),
        (4, {"l5"}),
    ],
)
@pytest.mark.parametrize(
    "et", [FET.MOMENT_CREATED, FET.MOMENT_DELETED, FET.MOMENT_REACTIONS_COUNTED]
)
async def test_hops_4_and_5_go_only_to_households_that_look_that_far(
    stack, hop, targets, et
):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, _LOOK_VERSIONS, looks=_LOOKS)
    sender = "peer-origin" if hop == 1 else "peer-sender"
    if et is FET.MOMENT_REACTIONS_COUNTED:
        await _relay_counts(
            out, held_reach=5, over={"hop_count": hop}, from_instance=sender
        )
    else:
        await out.relay_inbound(
            event_type=et, payload=_hop_payload(5, hop), from_instance=sender
        )
    assert _targets(fed) == targets
    assert {c["payload"]["hop_count"] for c in _sent(fed)} == {hop + 1}


async def test_a_hop_4_copy_goes_only_to_v96_whatever_its_marks(stack):
    """A copy at hop ≥ 4 is always a reach-4–5 one (it carries ``reach``),
    but the version gate stands on its own: an older household refuses any
    hop above 3, so it is never sent one."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"old5": 95, "l5": 96}, looks={"old5": 5, "l5": 5})
    payload = _hop_payload(5, 3)
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED, payload=payload, from_instance="peer-sender"
    )
    assert _targets(fed) == {"l5"}


async def test_the_origins_own_send_ignores_how_far_peers_look(stack):
    """The bound is on relays: the origin's hop-1 copy of a reach-5 moment
    still goes to every paired household, even one that looks 1 hop."""
    out, fed, fed_repo, user_repo = stack
    user_repo.get_instance_for_user = AsyncMock(return_value="self")
    _versioned(fed, fed_repo, {"l1": 96, "lx": 96}, looks={"l1": 1})
    await out._on_created(_reach_event(5))
    assert _targets(fed) == {"l1", "lx"}


async def test_the_relay_clamp_forwards_a_relayed_copy_claiming_hop_1_as_hop_3(
    stack,
):
    """F1: hop_count is outside the signature. A copy not sent by its origin
    is at least 2 hops away whatever it claims, so it goes on as hop 3."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(3, 1),
        from_instance="peer-sender",
    )
    assert [c["payload"]["hop_count"] for c in _sent(fed)] == [3]


async def test_a_relayed_reach_2_copy_claiming_hop_1_goes_nowhere(stack):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(2, 1),
        from_instance="peer-sender",
    )
    fed.send_event.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        {REACH_FIELD: 3},
        {REACH_FIELD: 1},
        {REACH_FIELD: 7},
        {REACH_FIELD: True},
        {REACH_FIELD: "5"},
        {REACH_FIELD: 5, NO_RELAY_FIELD: True},
    ],
)
async def test_an_invalid_reach_is_never_relayed(stack, bad):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_relayed(bad),
        from_instance="peer-origin",
    )
    fed.send_event.assert_not_called()


@pytest.mark.parametrize(
    ("reach", "targets"),
    [(3, {"p37", "p38", "p95", "p96"}), (4, {"p96"}), (5, {"p96"})],
)
async def test_a_relayed_copy_carrying_reach_goes_only_to_v96(stack, reach, targets):
    """An older household verifies the v1 bytes only and would drop a v3
    copy — or, if it took it, relay it ignoring the reach."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo)
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(reach, 1),
        from_instance="peer-origin",
    )
    assert _targets(fed) == targets


async def test_a_closer_copy_is_relayed_again_at_most_once(stack):
    """F8: a relay claiming hops 4, 3, 2 gets one extra wave, not two."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96}, looks={"p96": 5})
    for hop in (4, 3, 2):
        await out.relay_inbound(
            event_type=FET.MOMENT_CREATED,
            payload=_hop_payload(5, hop),
            from_instance="peer-sender",
        )
    assert [c["payload"]["hop_count"] for c in _sent(fed)] == [5, 4]


async def test_the_origins_own_copy_is_relayed_even_after_the_re_relay(stack):
    """C: a relay can burn the one closer re-relay, but not the origin's
    own copy — its hop 1 is envelope-authenticated."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96}, looks={"p96": 5})
    for hop, sender in ((4, "peer-sender"), (3, "peer-sender"), (1, "peer-origin")):
        await out.relay_inbound(
            event_type=FET.MOMENT_CREATED,
            payload=_hop_payload(5, hop),
            from_instance=sender,
        )
    assert [c["payload"]["hop_count"] for c in _sent(fed)] == [5, 4, 2]
    # Nothing closer than the origin's own copy: a later one is dropped.
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(5, 1),
        from_instance="peer-origin",
    )
    assert len(_sent(fed)) == 3


async def test_relayed_creates_and_totals_are_queued_under_the_relayed_key(stack):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_relayed(),
        from_instance="peer-origin",
    )
    await out.relay_inbound(
        event_type=FET.MOMENT_REACTIONS_COUNTED,
        payload=_counts(),
        from_instance="peer-origin",
        now_ms=_NOW_MS,
    )
    keys = [c["outbox_key"] for c in _sent(fed)]
    assert keys == [
        relayed_moment_key("create", "peer-origin", "m-1"),
        relayed_moment_key("counts", "peer-origin", "m-1"),
    ]


_NOW_MS = 1_800_000_000_000


def _counts(**over) -> dict:
    payload = {
        "moment_id": "m-1",
        "author_user_id": "uid-author",
        "origin_instance_id": "peer-origin",
        "rev": _NOW_MS,
        "counts": {"🔥": 1},
        "total": 1,
        "hop_count": 1,
    }
    payload.update(over)
    return payload


async def _relay_counts(out, **kw) -> int:
    return await out.relay_inbound(
        event_type=FET.MOMENT_REACTIONS_COUNTED,
        payload=_counts(**kw.pop("over", {})),
        from_instance=kw.pop("from_instance", "peer-origin"),
        now_ms=_NOW_MS,
        **kw,
    )


@pytest.mark.parametrize(
    ("reach", "targets"),
    [(2, {"p96"}), (3, {"p95", "p96"}), (4, {"p96"}), (5, {"p96"})],
)
async def test_totals_follow_a_relayed_create_and_its_reach(stack, reach, targets):
    """A relay passes totals on only for a create it relayed (seen-map),
    to v_69 households — v_96 only when the reach isn't 3 (S1: an older
    household can't hold such a moment through this relay)."""
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p37": 37, "p95": 95, "p96": 96})
    assert await _relay_counts(out) == 0  # create not relayed yet
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(reach, 1),
        from_instance="peer-origin",
    )
    fed.send_event.reset_mock()
    await _relay_counts(out)
    assert _targets(fed) == targets


async def test_totals_for_a_reach_2_moment_stop_at_hop_2(stack):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    await out.relay_inbound(
        event_type=FET.MOMENT_CREATED,
        payload=_hop_payload(2, 1),
        from_instance="peer-origin",
    )
    fed.send_event.reset_mock()
    # A relay's totals copy is at hop ≥ 2 — at the moment's reach already.
    assert await _relay_counts(out, from_instance="peer-sender") == 0


@pytest.mark.parametrize(
    ("held_hop", "held_reach", "relayed"),
    [(1, 3, True), (2, 3, True), (1, 2, True), (2, 2, False), (1, 1, False)],
)
async def test_a_holder_passes_totals_on_only_within_the_moments_reach(
    stack, held_hop, held_reach, relayed
):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p96": 96})
    sent = await _relay_counts(
        out,
        held_reach=held_reach,
        over={"hop_count": held_hop},
        from_instance="peer-origin" if held_hop == 1 else "peer-sender",
    )
    assert (sent > 0) is relayed


@pytest.mark.parametrize(("reach", "targets"), [(2, {"p96"}), (3, {"p95", "p96"})])
async def test_the_origins_totals_follow_the_moments_reach(stack, reach, targets):
    out, fed, fed_repo, _ = stack
    _versioned(fed, fed_repo, {"p37": 37, "p95": 95, "p96": 96})
    await out.fan_counts(
        _counts(origin_instance_id="self"), author_user_id="uid-author", reach=reach
    )
    assert _targets(fed) == targets
