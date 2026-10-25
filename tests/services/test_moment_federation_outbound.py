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
from socialhome.moment_origin import NO_RELAY_FIELD, verify_moment_origin
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
    assert all(c["payload"]["hop_count"] == 2 for c in sent)


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
        from_instance="peer-sender",
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
    then at hop 1 — relaying that one reaches two more hops, so it goes.
    A later hop-2 copy adds nothing."""
    out, fed, fed_repo, _user_repo = stack
    fed_repo.list_social_instances = AsyncMock(return_value=[_peer("peer-onward")])
    for hop in (2, 1, 2, 1):
        await out.relay_inbound(
            event_type=FederationEventType.MOMENT_CREATED,
            payload=_relayed({"hop_count": hop}),
            from_instance="peer-sender",
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
