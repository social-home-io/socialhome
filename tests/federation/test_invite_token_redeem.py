"""Tests for :class:`SpaceInviteTokenRedeemCoordinator` (§D2).

Covers both halves of the cross-instance round-trip:

* **Sender side** — ``request_redeem`` against an unpaired peer raises
  immediately; against a CONFIRMED peer ships REDEEM and parks an
  awaitable Future keyed on the nonce.
* **Issuer side** — ``_on_redeem`` consumes the token, seats the
  remote redeemer, and ships ACK; on a missing / expired / exhausted
  token (or storage error) ships DENY with a ``reason``.
* **Round-trip** — wire two coordinator instances against a shared
  in-memory federation bus so a REDEEM minted by one resolves the
  other's Future via the issuer's natural ACK.
* **Timeout** — when the issuer never responds the Future raises
  ``TimeoutError`` and the pending entry is cleaned up.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.federation.invite_bootstrap import (
    KIND_REDEEM,
    KIND_REDEEM_ACK,
    InviteBootstrapHint,
    sign_bootstrap_body,
)
from socialhome.federation.invite_token_redeem import (
    BOOTSTRAP_DENY_CLIENT_MESSAGE,
    REDEEM_DENY_REASON,
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    BOOTSTRAP_PER_SENDER_LIMIT,
    BOOTSTRAP_PER_SENDER_WINDOW_S,
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.rate_limiter import RateLimiter


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    """Stand-in for :class:`RemoteInstance` carrying just the fields the
    coordinator inspects."""

    id: str
    status: PairingStatus = PairingStatus.CONFIRMED


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None):
        self._instances = instances or {}

    async def get_instance(self, instance_id):
        return self._instances.get(instance_id)


class _FakeUserRepo:
    def __init__(self, users: dict[str, object] | None = None):
        self._users = users or {}

    async def get_by_user_id(self, user_id):
        return self._users.get(user_id)

    async def list_by_ids(self, ids):
        return [self._users[i] for i in ids if i in self._users]


class _FakeSpaceRepo:
    """In-memory ``space_repo`` slice.

    Holds an `invite_tokens` dict the test pre-seeds; ``consume_invite_token``
    decrements ``uses_remaining`` atomically and returns the row (or None on
    expired / exhausted). ``add_space_instance`` + ``is_banned`` round-trip
    via plain attributes so tests can assert on them.
    """

    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.space_instances: list[tuple[str, str]] = []
        self.bans: set[tuple[str, str]] = set()
        # Allow tests to force an exception out of consume_invite_token.
        self.consume_should_raise: Exception | None = None
        # §D1b stub bookkeeping — the coordinator now seats a local
        # stub + membership row after a successful redeem. Tests can
        # assert on these dicts.
        self.spaces: dict = {}
        self.members: list = []
        # Pre-seed ``get`` returns; tests that drive the issuer-side
        # _on_redeem should populate this so the ACK can carry meta.
        self.space_rows_for_get: dict = {}

    async def consume_invite_token(self, token, *, redeemer_user_id=None):
        if self.consume_should_raise is not None:
            raise self.consume_should_raise
        row = self.tokens.get(token)
        if row is None:
            return None
        if row.get("expired"):
            return None
        if row["uses_remaining"] <= 0:
            return None
        # Mirrors the real repo: the §13.7 ban is folded into the same
        # atomic statement, so a banned redeemer never moves the counter.
        if (
            redeemer_user_id is not None
            and (
                row["space_id"],
                redeemer_user_id,
            )
            in self.bans
        ):
            return None
        row["uses_remaining"] -= 1
        return {
            "space_id": row["space_id"],
            "created_by": row["created_by"],
            "uses_remaining": row["uses_remaining"],
            "expires_at": row.get("expires_at"),
            # Migration 0053 — the seat the issuer minted the link for.
            "role": row.get("role", SpaceRole.MEMBER.value),
        }

    async def add_space_instance(self, space_id, instance_id):
        self.space_instances.append((space_id, instance_id))

    async def is_banned(self, space_id, user_id):
        return (space_id, user_id) in self.bans

    async def get(self, space_id):
        return self.space_rows_for_get.get(space_id)

    async def save(self, space):
        self.spaces[space.id] = space
        return space

    async def save_member(self, member):
        self.members.append(member)

    async def list_members(self, space_id):
        return [m for m in self.members if getattr(m, "space_id", None) == space_id]


class _FakeRemoteMemberRepo:
    def __init__(self):
        self.added: list[dict] = []
        self.removed: list[tuple[str, str, str]] = []
        self.roles: list[tuple[str, str, str, str]] = []

    async def add(self, **kwargs):
        self.added.append(kwargs)

    async def set_role(self, space_id, instance_id, user_id, role):
        self.roles.append((space_id, instance_id, user_id, role))

    async def remove(self, space_id, instance_id, user_id):
        self.removed.append((space_id, instance_id, user_id))

    async def list_for_space(self, space_id):
        return []


class _FakeFederationService:
    """Captures outbound ``send_event`` calls for assertion."""

    def __init__(self, *, peer_min_version: int = 99, own_instance_id: str = "self"):
        self._event_registry = _FakeRegistry()
        self.sent: list[dict] = []
        # Tests can lower this to simulate a pre-v_6 issuer; the
        # coordinator's outbound gate checks ``peer_supports`` before
        # sending REDEEM, so a v_5 issuer should be 422'd up front.
        self._peer_min_version = peer_min_version
        self._own_instance_id = own_instance_id

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

        # Returning a SimpleNamespace mimics DeliveryResult enough for
        # the coordinator's needs — it never inspects the return value.
        return SimpleNamespace(ok=True, instance_id=to_instance_id)

    async def peer_supports(self, instance_id, *, min_version):
        return self._peer_min_version >= min_version


class _FakeRegistry:
    def __init__(self):
        self.bindings: dict = {}

    def register(self, event_type, handler):
        self.bindings.setdefault(event_type, []).append(handler)


class _LinkedFederationService(_FakeFederationService):
    """A ``send_event`` shim that, instead of stashing the envelope,
    delivers it straight into a paired coordinator's registry so the
    round-trip test can run without any real transport.
    """

    def __init__(self, *, own_instance_id: str = "self"):
        super().__init__(own_instance_id=own_instance_id)
        self.peer: SpaceInviteTokenRedeemCoordinator | None = None
        self.from_instance: str = ""
        self.to_instance: str = ""

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        if self.peer is None:
            return SimpleNamespace(ok=False, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id="m-" + event_type.value,
            event_type=event_type,
            from_instance=self.from_instance,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )
        # Dispatch on the peer's registry in a background task so the
        # caller's ``await`` returns immediately (mirrors the real
        # transport's fire-and-forget shape).
        peer_reg = self.peer._federation._event_registry  # type: ignore[attr-defined]
        handlers = peer_reg.bindings.get(event_type, [])
        for handler in handlers:
            await handler(ev)
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


# ── Coordinator factory ───────────────────────────────────────────────


def _make_user(user_id: str = "u-local"):
    return SimpleNamespace(
        username="alice",
        display_name="Alice",
        public_key="pk-alice",
        user_id=user_id,
    )


def _make_coordinator(
    *,
    federation=None,
    space_repo=None,
    remote_members=None,
    user_repo=None,
    federation_repo=None,
    timeout: float = 10.0,
    child_protection=None,
):
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return SpaceInviteTokenRedeemCoordinator(
        bus=bus,
        federation_service=federation or _FakeFederationService(),
        space_repo=space_repo or _FakeSpaceRepo(),
        space_remote_member_repo=remote_members or _FakeRemoteMemberRepo(),
        user_repo=user_repo or _FakeUserRepo({"u-local": _make_user()}),
        federation_repo=federation_repo or _FakeFederationRepo(),
        timeout=timeout,
        child_protection_service=child_protection,
    )


def _a_space(space_id, *, owner_instance_id="issuer-1", min_age=0):
    """A minimal host space the issuer ships in the ACK's space_meta."""
    return Space(
        id=space_id,
        name="Shared",
        owner_instance_id=owner_instance_id,
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        min_age=min_age,
    )


# ── Sender-side tests ─────────────────────────────────────────────────


async def test_redeem_against_unpaired_issuer_raises():
    """An issuer with no CONFIRMED ``RemoteInstance`` row rejects
    immediately — the SPA should pair first."""
    coord = _make_coordinator(federation_repo=_FakeFederationRepo({}))
    with pytest.raises(SpacePermissionError) as exc:
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )
    assert "not a confirmed peer" in str(exc.value)


async def test_redeem_against_pending_issuer_raises():
    """A peer in PENDING_SENT — i.e. handshake half-done — is not yet
    eligible to receive REDEEM events."""
    fr = _FakeFederationRepo(
        {"peer-1": _FakeInstance("peer-1", status=PairingStatus.PENDING_SENT)},
    )
    coord = _make_coordinator(federation_repo=fr)
    with pytest.raises(SpacePermissionError):
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )


async def test_redeem_missing_local_user_raises():
    """If the redeemer's local user row vanished between auth and
    redeem, surface a permission-shape error rather than a 500."""
    fr = _FakeFederationRepo({"peer-1": _FakeInstance("peer-1")})
    coord = _make_coordinator(federation_repo=fr, user_repo=_FakeUserRepo({}))
    with pytest.raises(SpacePermissionError):
        await coord.request_redeem(
            "tkn",
            viewer_user_id="u-local",
            issuer_instance_id="peer-1",
        )


# ── Round-trip tests ──────────────────────────────────────────────────


def _wire_pair(
    token_row: dict,
    *,
    redeemer_banned: bool = False,
    sender_child_protection=None,
):
    """Create a sender + issuer coordinator pair linked through
    ``_LinkedFederationService`` so the round-trip runs in-memory.

    Returns ``(sender, issuer, sender_fed, issuer_fed)``.
    """
    sender_fed = _LinkedFederationService()
    issuer_fed = _LinkedFederationService()
    issuer_repo = _FakeSpaceRepo()
    issuer_repo.tokens["good-token"] = token_row
    if redeemer_banned:
        issuer_repo.bans.add((token_row["space_id"], "u-local"))
    issuer_members = _FakeRemoteMemberRepo()

    sender = _make_coordinator(
        federation=sender_fed,
        federation_repo=_FakeFederationRepo(
            {"issuer-1": _FakeInstance("issuer-1")},
        ),
        child_protection=sender_child_protection,
    )
    issuer = _make_coordinator(
        federation=issuer_fed,
        space_repo=issuer_repo,
        remote_members=issuer_members,
    )
    sender.attach_to(sender_fed)
    issuer.attach_to(issuer_fed)
    sender_fed.peer = issuer
    sender_fed.from_instance = "sender-1"
    sender_fed.to_instance = "issuer-1"
    issuer_fed.peer = sender
    issuer_fed.from_instance = "issuer-1"
    issuer_fed.to_instance = "sender-1"
    return (sender, issuer, sender_fed, issuer_fed, issuer_repo, issuer_members)


async def test_redeem_round_trip_success():
    """A live token + CONFIRMED peer → ACK arrives, sender's Future
    resolves with ``{space_id, role}``. Issuer's side seats the
    redeemer as a SpaceRemoteMember and adds the receiver's
    space_instance row."""
    sender, _issuer, _sf, _if, issuer_repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-1",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result == {"space_id": "sp-1", "role": "member"}
    # Issuer seated the redeemer with the identity we shipped.
    assert len(issuer_members.added) == 1
    seat = issuer_members.added[0]
    assert seat["space_id"] == "sp-1"
    assert seat["instance_id"] == "sender-1"
    assert seat["user_id"] == "u-local"
    assert seat["user_pk"] == "pk-alice"
    assert seat["display_name"] == "Alice"
    # Issuer also registered the receiver as a space_instance + token
    # is now exhausted.
    assert ("sp-1", "sender-1") in issuer_repo.space_instances
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 0


async def test_redeem_round_trip_persists_local_space_instance():
    """On ACK the receiver adds its own ``space_instances`` row so the
    issuer's household will receive subsequent fan-outs."""
    sender, _issuer, *_rest = _wire_pair(
        {
            "space_id": "sp-2",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    # The sender's space_repo (separate from the issuer's) should
    # learn the (space_id, issuer_instance_id) mapping after ACK.
    sender_repo = sender._spaces  # type: ignore[attr-defined]
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert ("sp-2", "issuer-1") in sender_repo.space_instances


async def test_redeem_seats_local_stub_and_membership_from_ack_meta():
    """Core §D1b fix: when the issuer's ACK carries space_meta, the
    redeemer's side seats a local stub + membership so /api/spaces shows
    the space. Regression — _on_redeem_ack used to drop space_meta, so this
    block never ran and a cross-household invite-link redeem surfaced
    nothing locally."""
    sender, _issuer, *_rest, issuer_repo, _im = _wire_pair(
        {"space_id": "sp-meta", "created_by": "owner", "uses_remaining": 1},
    )
    # Seed the issuer's space so its ACK ships a space_meta snapshot.
    issuer_repo.space_rows_for_get["sp-meta"] = _a_space("sp-meta")
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    sender_repo = sender._spaces  # type: ignore[attr-defined]
    # Stub + membership now seated locally (were never created before).
    assert "sp-meta" in sender_repo.spaces
    assert any(
        m.space_id == "sp-meta" and m.user_id == "u-local" for m in sender_repo.members
    )
    assert ("sp-meta", "issuer-1") in sender_repo.space_instances


async def test_redeem_blocks_underage_minor_and_seats_nothing():
    """§CP.F1 — a protected minor redeeming a link to a remote 18+ space is
    refused locally: nothing is seated (no stub, membership, or
    space_instance) and a SpacePermissionError (→ 403) propagates."""
    cp = SimpleNamespace(is_age_allowed=AsyncMock(return_value=False))
    sender, _issuer, *_rest, issuer_repo, _im = _wire_pair(
        {"space_id": "sp-18", "created_by": "owner", "uses_remaining": 1},
        sender_child_protection=cp,
    )
    issuer_repo.space_rows_for_get["sp-18"] = _a_space("sp-18", min_age=18)
    with pytest.raises(SpacePermissionError, match="18"):
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    sender_repo = sender._spaces  # type: ignore[attr-defined]
    assert sender_repo.spaces == {}
    assert sender_repo.members == []
    assert sender_repo.space_instances == []  # not even the routing mapping
    cp.is_age_allowed.assert_awaited_once_with("u-local", 18)


async def test_redeem_coerces_out_of_set_min_age_no_crash():
    """A malicious issuer shipping a non-conforming min_age (e.g. 15) must
    not crash the redeemer — it coerces to 0 (no restriction) and seats."""
    cp = SimpleNamespace(is_age_allowed=AsyncMock(return_value=True))
    sender, _issuer, *_rest, issuer_repo, _im = _wire_pair(
        {"space_id": "sp-bad", "created_by": "owner", "uses_remaining": 1},
        sender_child_protection=cp,
    )
    issuer_repo.space_rows_for_get["sp-bad"] = _a_space("sp-bad", min_age=15)
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    # Coerced to 0 before the gate — is_age_allowed called with 0, seated.
    cp.is_age_allowed.assert_awaited_once_with("u-local", 0)
    assert "sp-bad" in sender._spaces.spaces  # type: ignore[attr-defined]


async def test_redeem_allows_old_enough_minor():
    """A 16-year-old minor IS seated into a 13+ space — is_age_allowed True."""
    cp = SimpleNamespace(is_age_allowed=AsyncMock(return_value=True))
    sender, _issuer, *_rest, issuer_repo, _im = _wire_pair(
        {"space_id": "sp-13", "created_by": "owner", "uses_remaining": 1},
        sender_child_protection=cp,
    )
    issuer_repo.space_rows_for_get["sp-13"] = _a_space("sp-13", min_age=13)
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert "sp-13" in sender._spaces.spaces  # type: ignore[attr-defined]


async def test_redeem_refuses_when_space_owned_by_another_host():
    """§D1b anti-hijack — a redeem ACK whose space_id collides with a space
    we already hold under a DIFFERENT host is refused: nothing seated, 403.
    Guards against a malicious issuer clobbering our config + content key."""
    sender, _issuer, *_rest, issuer_repo, _im = _wire_pair(
        {"space_id": "sp-own", "created_by": "owner", "uses_remaining": 1},
    )
    issuer_repo.space_rows_for_get["sp-own"] = _a_space("sp-own")
    # We already hold sp-own locally, owned by a DIFFERENT host.
    sender._spaces.space_rows_for_get["sp-own"] = _a_space(  # type: ignore[attr-defined]
        "sp-own",
        owner_instance_id="the-real-host",
    )
    with pytest.raises(SpacePermissionError, match="conflicts"):
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert sender._spaces.members == []  # type: ignore[attr-defined]
    assert sender._spaces.space_instances == []  # type: ignore[attr-defined]


async def test_redeem_with_expired_token_sends_deny():
    """An issuer-side ``consume_invite_token`` miss → DENY with a
    user-visible reason → receiver's Future raises
    ``SpacePermissionError`` carrying that reason."""
    sender, _issuer, _sf, _if, issuer_repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-3",
            "created_by": "owner",
            "uses_remaining": 1,
            "expired": True,
        },
    )
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert str(exc.value) == REDEEM_DENY_REASON
    # No member should have been seated.
    assert issuer_members.added == []


async def test_redeem_with_no_remaining_uses_sends_deny():
    """A token already burned through its uses_remaining → DENY,
    receiver raises with the same reason."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-4",
            "created_by": "owner",
            "uses_remaining": 0,
        },
    )
    with pytest.raises(SpacePermissionError):
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert issuer_members.added == []


async def test_redeem_with_banned_redeemer_sends_deny():
    """A live token can still be vetoed by an issuer-side ban."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-5",
            "created_by": "owner",
            "uses_remaining": 1,
        },
        redeemer_banned=True,
    )
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert str(exc.value) == REDEEM_DENY_REASON
    assert issuer_members.added == []


async def test_redeem_with_unknown_token_sends_deny():
    """A token the issuer's repo doesn't recognise → DENY."""
    sender, _issuer, _sf, _if, _repo, _members = _wire_pair(
        # _wire_pair seeds "good-token"; we'll redeem a *different*
        # token below so the consume returns None.
        {
            "space_id": "sp-x",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    with pytest.raises(SpacePermissionError):
        await sender.request_redeem(
            "unknown-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )


async def test_redeem_issuer_storage_error_sends_deny():
    """If ``consume_invite_token`` raises on the issuer side, send DENY
    rather than leaving the receiver hanging."""
    sender, _issuer, _sf, _if, issuer_repo, _members = _wire_pair(
        {
            "space_id": "sp-6",
            "created_by": "owner",
            "uses_remaining": 1,
        },
    )
    issuer_repo.consume_should_raise = RuntimeError("disk full")
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert str(exc.value) == REDEEM_DENY_REASON


# ── Timeout tests ─────────────────────────────────────────────────────


async def test_redeem_timeout_raises_timeout_error():
    """When the issuer never sends ACK / DENY, the Future resolves with
    ``TimeoutError`` and the pending dict is cleaned up so a late ACK
    won't trip an assertion."""
    fed = _FakeFederationService()  # absorbs send_event; never ACKs back
    coord = _make_coordinator(
        federation=fed,
        federation_repo=_FakeFederationRepo(
            {"issuer-1": _FakeInstance("issuer-1")},
        ),
        timeout=0.05,
    )
    with pytest.raises(TimeoutError):
        await coord.request_redeem(
            "any-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    # Pending registry is drained — a late ACK is a no-op.
    assert coord._pending == {}


# ── Attach / wiring tests ─────────────────────────────────────────────


async def test_attach_to_registers_three_handlers():
    """`attach_to` wires the three event-type → handler bindings."""
    coord = _make_coordinator()
    reg = _FakeRegistry()

    class _FakeFedSvc:
        def __init__(self):
            self._event_registry = reg

    coord.attach_to(_FakeFedSvc())  # type: ignore[arg-type]
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM in reg.bindings
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK in reg.bindings
    assert FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY in reg.bindings


async def test_late_ack_after_timeout_is_noop():
    """A delayed ACK for a nonce that already timed out must not raise
    — the receiver may simply have moved on."""
    coord = _make_coordinator()
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        from_instance="issuer-1",
        to_instance="sender-1",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "stale", "space_id": "sp-x"},
    )
    await coord._on_redeem_ack(ev)  # no exception


async def test_redeem_missing_nonce_in_payload_skipped():
    """A REDEEM with no nonce — likely a malformed peer — is dropped."""
    fed = _FakeFederationService()
    coord = _make_coordinator(federation=fed)
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="peer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"invite_token": "tkn"},  # no redeem_nonce
    )
    await coord._on_redeem(ev)
    assert fed.sent == []  # no DENY shipped — we can't key it


async def test_redeem_missing_redeemer_user_id_sends_deny():
    """A REDEEM with a nonce but no redeemer_user_id → DENY (we have
    enough info to reply, but not enough to seat anyone)."""
    fed = _FakeFederationService()
    coord = _make_coordinator(federation=fed)
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="peer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "n1", "invite_token": "tkn"},
    )
    await coord._on_redeem(ev)
    assert len(fed.sent) == 1
    assert (
        fed.sent[0]["event_type"] is FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    )


async def test_redeem_deny_with_no_reason_uses_default():
    """A DENY shipped without a ``reason`` still resolves the Future
    with a non-empty SpacePermissionError."""
    coord = _make_coordinator()
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    coord._pending["n1"] = fut
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
        from_instance="issuer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "n1"},
    )
    await coord._on_redeem_deny(ev)
    with pytest.raises(SpacePermissionError) as exc:
        fut.result()
    assert str(exc.value)  # non-empty


# ── Edge-case coverage tests for late ACK / DENY paths + storage errors ──


async def test_redeem_deny_missing_nonce_is_silent_noop():
    """A DENY envelope missing ``redeem_nonce`` is dropped silently —
    can't resolve a Future without an id to look up. Belt-and-suspenders
    for malformed inbound traffic."""
    coord = _make_coordinator()
    ev = FederationEvent(
        msg_id="m-no-nonce",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
        from_instance="issuer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"reason": "no nonce attached"},
    )
    # Should not raise.
    await coord._on_redeem_deny(ev)


async def test_redeem_deny_with_no_matching_future_is_silent_noop():
    """A late DENY (after the receiver's Future already timed out and
    was purged) is dropped silently — there's nobody waiting."""
    coord = _make_coordinator()
    # No future registered for nonce 'gone'.
    ev = FederationEvent(
        msg_id="m-late-deny",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
        from_instance="issuer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"redeem_nonce": "gone", "reason": "too late"},
    )
    await coord._on_redeem_deny(ev)


async def test_redeem_ack_missing_nonce_is_silent_noop():
    """ACK without a nonce can't resolve any Future. Silent drop."""
    coord = _make_coordinator()
    ev = FederationEvent(
        msg_id="m-ack-no-nonce",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        from_instance="issuer-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={"space_id": "s-x"},
    )
    await coord._on_redeem_ack(ev)


async def test_a_banned_redeemer_never_burns_a_use():
    """The ban is inside the atomic consume now.

    It used to be a second query AFTER the consume, with no refund: a
    banned household could spend a twenty-use invite link in twenty
    requests, and the distinct "banned from this space" reason told it its
    own ban status one attempt at a time. Both halves are closed here —
    the counter does not move, and the reason is the same opaque string
    every other denial ships.
    """
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo()
    repo.tokens["tok-banned"] = {
        "space_id": "s-banned",
        "created_by": "u-issuer",
        "uses_remaining": 20,
    }
    repo.bans.add(("s-banned", "u-x"))
    coord = _make_coordinator(federation=fed, space_repo=repo)

    for attempt in range(3):
        ev = FederationEvent(
            msg_id=f"m-redeem-banned-{attempt}",
            event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            from_instance="receiver-1",
            to_instance="me",
            timestamp="2026-05-22T00:00:00Z",
            payload={
                "redeem_nonce": f"n-banned-{attempt}",
                "invite_token": "tok-banned",
                "redeemer_user_id": "u-x",
            },
        )
        await coord._on_redeem(ev)

    assert repo.tokens["tok-banned"]["uses_remaining"] == 20
    denies = [
        s
        for s in fed.sent
        if s["event_type"] == FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    ]
    assert len(denies) == 3
    assert {d["payload"]["reason"] for d in denies} == {REDEEM_DENY_REASON}


async def test_every_deny_path_is_byte_identical():
    """No oracle: unknown token, expired, exhausted, banned and an issuer
    storage fault must be indistinguishable on the wire."""
    reasons = set()

    async def _deny_reason_for(repo, *, user_id="u-x"):
        fed = _FakeFederationService()
        coord = _make_coordinator(federation=fed, space_repo=repo)
        await coord._on_redeem(
            FederationEvent(
                msg_id="m-oracle",
                event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
                from_instance="receiver-1",
                to_instance="me",
                timestamp="2026-05-22T00:00:00Z",
                payload={
                    "redeem_nonce": "n-oracle",
                    "invite_token": "tok",
                    "redeemer_user_id": user_id,
                },
            ),
        )
        denies = [
            s
            for s in fed.sent
            if s["event_type"] == FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
        ]
        assert len(denies) == 1
        return denies[0]["payload"]["reason"]

    # Unknown token.
    reasons.add(await _deny_reason_for(_FakeSpaceRepo()))

    # Expired.
    expired = _FakeSpaceRepo()
    expired.tokens["tok"] = {
        "space_id": "s",
        "created_by": "o",
        "uses_remaining": 1,
        "expired": True,
    }
    reasons.add(await _deny_reason_for(expired))

    # Exhausted.
    spent = _FakeSpaceRepo()
    spent.tokens["tok"] = {"space_id": "s", "created_by": "o", "uses_remaining": 0}
    reasons.add(await _deny_reason_for(spent))

    # Banned.
    banned = _FakeSpaceRepo()
    banned.tokens["tok"] = {"space_id": "s", "created_by": "o", "uses_remaining": 5}
    banned.bans.add(("s", "u-x"))
    reasons.add(await _deny_reason_for(banned))

    # Issuer storage fault.
    broken = _FakeSpaceRepo()
    broken.tokens["tok"] = {"space_id": "s", "created_by": "o", "uses_remaining": 5}
    broken.consume_should_raise = RuntimeError("storage down")
    reasons.add(await _deny_reason_for(broken))

    assert reasons == {REDEEM_DENY_REASON}


async def test_issuer_seat_failure_sends_deny():
    """A storage error during ``add_remote_member`` / ``add_space_instance``
    ships a DENY rather than half-committing state. Mirrors the
    is_banned-raises path."""
    fed = _FakeFederationService()
    space_repo = _FakeSpaceRepo()
    space_repo.tokens["tok-seat-fail"] = {
        "space_id": "s-seat",
        "created_by": "u-issuer",
        "uses_remaining": 1,
    }

    class _ExplodingMembers(_FakeRemoteMemberRepo):
        async def add(self, **kwargs):
            raise RuntimeError("FK violation")

    coord = _make_coordinator(
        federation=fed,
        space_repo=space_repo,
        remote_members=_ExplodingMembers(),
    )
    ev = FederationEvent(
        msg_id="m-redeem-seat",
        event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        from_instance="receiver-1",
        to_instance="me",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "redeem_nonce": "n-seat",
            "invite_token": "tok-seat-fail",
            "redeemer_user_id": "u-y",
        },
    )
    await coord._on_redeem(ev)
    deny = [
        s
        for s in fed.sent
        if s["event_type"] == FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
    ]
    assert len(deny) == 1
    assert deny[0]["payload"]["reason"] == REDEEM_DENY_REASON


async def test_send_deny_swallows_transport_errors():
    """A failed DENY ship-back is logged but doesn't propagate — the
    receiver's timeout is the same observable outcome and a raise here
    would leave the issuer's task crashing on every malformed inbound.
    """

    class _ExplodingFed(_FakeFederationService):
        async def send_event(self, **kwargs):
            raise RuntimeError("transport down")

    coord = _make_coordinator(federation=_ExplodingFed())
    # Should not raise.
    await coord._send_deny("issuer-1", "n-explode", "test reason")


# ── Mesh-routing tests ────────────────────────────────────────────────


class _FakeRouteService:
    """Stand-in for :class:`RouteDiscoveryService` used by the redeem
    coordinator when the issuer isn't a direct peer.

    Returns whatever ``discover_route`` was preconfigured to surface;
    the matching ``lookup_target_eph_priv`` resolves the priv for
    the pub the test minted.
    """

    def __init__(self):
        self._result: tuple[list[str], str] | None = None
        self._eph_store: dict[str, str] = {}
        #: Hex identity pk the real service pins on the cached route at
        #: discovery; the coordinator hands it to ``send_routed`` so the
        #: origin holds a ``SPACE_ROUTE_STALE`` nack against it.
        self.pinned_identity_pk: str | None = None

    def configure(self, path: list[str], pub_b64: str, priv_b64: str) -> None:
        self._result = (path, pub_b64)
        self._eph_store[pub_b64] = priv_b64

    async def discover_route(self, target_instance_id):
        return self._result

    def cached_target_identity_pk(self, target_instance_id) -> str | None:
        return self.pinned_identity_pk

    def lookup_target_eph_priv(self, pub_b64: str) -> str | None:
        return self._eph_store.get(pub_b64)


async def test_redeem_via_mesh_pins_the_issuer_identity_pk():
    """The routed REDEEM carries the identity pk discovery pinned for the
    issuer, so a later ``SPACE_ROUTE_STALE`` nack is checked against the
    key WE verified rather than the origin's empty-pin fallback (same shape
    as ``FederationService.send_with_mesh_fallback``)."""
    route_svc = _FakeRouteService()
    route_svc.configure(["self", "issuer-1"], "eph-pub", "eph-priv")
    route_svc.pinned_identity_pk = "ab" * 32
    routed_handler = AsyncMock()
    sender = _make_coordinator(federation_repo=_FakeFederationRepo(), timeout=0.01)
    sender._route_service = route_svc
    sender._routed_handler = routed_handler
    with pytest.raises(TimeoutError):
        await sender.request_redeem(
            "tkn", viewer_user_id="u-local", issuer_instance_id="issuer-1"
        )
    routed_handler.send_routed.assert_awaited_once()
    kwargs = routed_handler.send_routed.await_args.kwargs
    assert kwargs["path"] == ["self", "issuer-1"]
    assert kwargs["target_eph_pk_b64"] == "eph-pub"
    assert kwargs["target_identity_pk"] == "ab" * 32


async def test_redeem_via_mesh_without_a_pin_sends_the_empty_fallback():
    """No pinned route (cache raced out between discovery and send) → the
    documented ``""`` fallback, never ``None``."""
    route_svc = _FakeRouteService()
    route_svc.configure(["self", "issuer-1"], "eph-pub", "eph-priv")
    routed_handler = AsyncMock()
    sender = _make_coordinator(federation_repo=_FakeFederationRepo(), timeout=0.01)
    sender._route_service = route_svc
    sender._routed_handler = routed_handler
    with pytest.raises(TimeoutError):
        await sender.request_redeem(
            "tkn", viewer_user_id="u-local", issuer_instance_id="issuer-1"
        )
    assert routed_handler.send_routed.await_args.kwargs["target_identity_pk"] == ""


async def test_redeem_round_trip_via_mesh_routing():
    """End-to-end: origin's issuer is *not* a direct peer; the redeem
    coordinator runs route discovery (mocked), wraps REDEEM inside
    SPACE_ROUTED, ships along the discovered path, the issuer unwraps
    the inner event, processes the token, and ships an ACK as another
    SPACE_ROUTED with direction=reply. The origin unseals the ACK and
    resolves the in-flight Future with ``{space_id, role}``.

    Demonstrates that relays never see plaintext: assert the on-wire
    SPACE_ROUTED payload bears no resemblance to the redeem fields.
    """
    from socialhome.federation import routed_crypto
    from socialhome.federation.routed_envelope import SpaceRoutedHandler

    # ── Sender side ───────────────────────────────────────────────────
    sender_fed = _LinkedFederationService(own_instance_id="sender-1")
    # Crucially, the sender's federation_repo has NO entry for
    # "issuer-1" — so direct-peer path is rejected and the mesh path
    # is taken.
    sender_repo = _FakeFederationRepo()
    sender_route_svc = _FakeRouteService()
    # ``send_routed`` ships against ``path[1]`` — make that the issuer
    # directly (single-hop mesh). The forward leg lands at the issuer,
    # the reply leg comes straight back.
    target_priv, target_pub = routed_crypto.generate_ephemeral_keypair()
    sender_route_svc.configure(["sender-1", "issuer-1"], target_pub, target_priv)

    # ── Issuer side ───────────────────────────────────────────────────
    issuer_fed = _LinkedFederationService(own_instance_id="issuer-1")
    issuer_repo = _FakeSpaceRepo()
    issuer_repo.tokens["good-token"] = {
        "space_id": "sp-routed",
        "created_by": "owner",
        "uses_remaining": 1,
    }
    issuer_members = _FakeRemoteMemberRepo()
    # The issuer's route service holds the priv for the pub it would
    # have minted in case 4 of FIND_ROUTE. We seed it here so the
    # routed handler can unseal the inbound forward leg.
    issuer_route_svc = _FakeRouteService()
    issuer_route_svc._eph_store[target_pub] = target_priv

    # ── Wire SpaceRoutedHandlers (forward = sender→issuer, reply = issuer→sender) ──
    # Each handler re-dispatches the unwrapped inner event through
    # the local federation registry so coordinator handlers
    # (_on_redeem*) fire — same shape as production
    # ``FederationService._dispatch_event``.
    sender_routed = SpaceRoutedHandler(
        federation_service=sender_fed,  # type: ignore[arg-type]
        federation_repo=_FakeFederationRepo(),  # type: ignore[arg-type]
        event_dispatcher=lambda ev: _dispatch_via_registry(sender_fed, ev),
        target_eph_lookup=sender_route_svc.lookup_target_eph_priv,
    )
    issuer_routed = SpaceRoutedHandler(
        federation_service=issuer_fed,  # type: ignore[arg-type]
        federation_repo=_FakeFederationRepo(),  # type: ignore[arg-type]
        event_dispatcher=lambda ev: _dispatch_via_registry(issuer_fed, ev),
        target_eph_lookup=issuer_route_svc.lookup_target_eph_priv,
    )

    # ── Coordinators ──────────────────────────────────────────────────
    sender = _make_coordinator(
        federation=sender_fed,
        federation_repo=sender_repo,
    )
    sender._route_service = sender_route_svc
    sender._routed_handler = sender_routed
    issuer = _make_coordinator(
        federation=issuer_fed,
        space_repo=issuer_repo,
        remote_members=issuer_members,
    )
    issuer._route_service = issuer_route_svc
    issuer._routed_handler = issuer_routed
    sender.attach_to(sender_fed)
    issuer.attach_to(issuer_fed)

    # The sender's SpaceRoutedHandler ships SPACE_ROUTED via
    # ``sender_fed.send_event``. Re-route those into the issuer's
    # SpaceRoutedHandler (and vice-versa for the reply leg).
    # ``_LinkedFederationService`` dispatches via the peer
    # coordinator's registry — register SPACE_ROUTED there too.
    sender_fed._event_registry.bindings[FederationEventType.SPACE_ROUTED] = [
        sender_routed._on_routed,
    ]
    issuer_fed._event_registry.bindings[FederationEventType.SPACE_ROUTED] = [
        issuer_routed._on_routed,
    ]
    sender_fed.peer = issuer
    sender_fed.from_instance = "sender-1"
    sender_fed.to_instance = "issuer-1"
    issuer_fed.peer = sender
    issuer_fed.from_instance = "issuer-1"
    issuer_fed.to_instance = "sender-1"

    # ── Run the round-trip ────────────────────────────────────────────
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result == {"space_id": "sp-routed", "role": "member"}

    # The sender's outbound was wrapped in SPACE_ROUTED, not a bare
    # SPACE_INVITE_TOKEN_REDEEM.
    outbound = [
        s
        for s in sender_fed.sent
        if s["event_type"] is FederationEventType.SPACE_ROUTED
    ]
    assert outbound, "redeem coordinator did not ship via SPACE_ROUTED"
    # Plaintext leak check: the on-wire envelope must not contain the
    # invite_token or redeemer_user_id verbatim.
    import json as _json

    wire = _json.dumps(outbound[0]["payload"], sort_keys=True)
    assert "good-token" not in wire
    assert "u-local" not in wire
    # The reply leg ships SPACE_ROUTED back with direction=reply.
    reply = [
        s
        for s in issuer_fed.sent
        if s["event_type"] is FederationEventType.SPACE_ROUTED
        and s["payload"].get("direction") == "reply"
    ]
    assert reply, "issuer did not ship ACK via SPACE_ROUTED reply leg"
    # Issuer seated the redeemer + token consumed.
    assert len(issuer_members.added) == 1
    assert issuer_repo.tokens["good-token"]["uses_remaining"] == 0


async def _noop_dispatcher(_ev):
    pass


async def _dispatch_via_registry(fed_svc, ev):
    """Re-dispatch a synthesised event through ``fed_svc``'s
    registry — emulates what FederationService._dispatch_event does
    in production for the unwrap step."""
    handlers = fed_svc._event_registry.bindings.get(ev.event_type, [])
    for h in handlers:
        await h(ev)


async def test_redeem_against_sub_v6_issuer_raises_with_upgrade_hint():
    """Pre-v_6 issuers can't process SPACE_INVITE_TOKEN_REDEEM. Fail
    fast with a clear "upgrade" message rather than ship the envelope
    into a 10 s timeout."""
    fed = _FakeFederationService(peer_min_version=5)
    fed_repo = _FakeFederationRepo(
        {
            "issuer-1": _FakeInstance("issuer-1", status=PairingStatus.CONFIRMED),
        }
    )
    users = _FakeUserRepo({"u-local": _make_user()})
    coord = _make_coordinator(
        federation=fed,
        federation_repo=fed_repo,
        user_repo=users,
    )
    with pytest.raises(SpacePermissionError) as exc:
        await coord.request_redeem(
            "tok",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert (
        "older protocol" in str(exc.value).lower()
        or "upgrade" in str(exc.value).lower()
    )
    # No envelope shipped — the gate fired before send_event.
    assert not fed.sent


# ── §D2b bootstrap redeem (no pre-existing relationship) ──────────────


class _FakeKeyManager:
    """Reversible stand-in for the KEK wrapper — hex, not encryption."""

    def encrypt(self, raw: bytes) -> str:
        return raw.hex()

    def decrypt(self, blob: str) -> bytes:
        return bytes.fromhex(blob)


class _Party:
    """One household's identity + key-wrap material for the §D2b path."""

    def __init__(self, display_name: str) -> None:
        ident = generate_identity_keypair()
        self.seed = ident.private_key
        self.identity_pk = ident.public_key
        self.instance_id = derive_instance_id(ident.public_key)
        kw = generate_x25519_keypair()
        self.keywrap_priv = kw.private_key
        self.keywrap_pub = kw.public_key
        self.keywrap_sig = b64url_encode(sign_ed25519(self.seed, kw.public_key))
        self.display_name = display_name


class _RelayBus:
    """In-process stand-in for the connection server.

    Holds one coordinator per instance id and hands each sealed envelope
    straight to the addressed one. Records every envelope so tests can
    assert on what a real relay would have been able to see.
    """

    def __init__(self) -> None:
        self.coordinators: dict = {}
        self.envelopes: list[tuple[str, dict]] = []
        #: The connection server each blob was addressed through — the
        #: redeemer hands its request to the server that served the
        #: invite blob, the issuer answers on the one it arrived on.
        self.gfs_urls: list[str] = []
        self.deliver = True

    def sender_for(self, _from_instance: str):
        bus = self

        class _Sender:
            async def send_sealed_envelope(
                self,
                *,
                to_instance_id,
                envelope,
                gfs_url="",
            ):
                bus.envelopes.append((to_instance_id, envelope))
                bus.gfs_urls.append(gfs_url)
                if not bus.deliver:
                    return False
                target = bus.coordinators.get(to_instance_id)
                if target is None:
                    return False
                # A real connection server pushes the blob down the
                # socket it holds, so the receiver learns which server
                # carried it — and answers on that one.
                await target.handle_relayed_envelope(envelope, gfs_url=gfs_url)
                return True

        return _Sender()


class _BootstrapFederationService(_FakeFederationService):
    """``_FakeFederationService`` plus the identity + replay surface the
    bootstrap path reads."""

    def __init__(self, party: _Party) -> None:
        super().__init__(own_instance_id=party.instance_id)
        self._party = party
        self._seen: set[str] = set()

    @property
    def own_identity_pk(self) -> bytes:
        return self._party.identity_pk

    @property
    def own_identity_seed(self) -> bytes:
        return self._party.seed

    async def note_replay_id(self, msg_id: str) -> bool:
        if msg_id in self._seen:
            return True
        self._seen.add(msg_id)
        return False


class _BootstrapFederationRepo(_FakeFederationRepo):
    def __init__(self, party: _Party, instances=None):
        super().__init__(instances)
        self.party = party
        self.saved: list = []

    async def get_local_identity(self):
        return {"display_name": self.party.display_name}

    async def save_instance(self, instance):
        self.saved.append(instance)
        self._instances[instance.id] = instance


def _hint_for(
    party: _Party,
    *,
    space_id="space-1",
    token="tok-1",
    proto=OURS,
    gfs_url="https://gfs.example.org",
):
    return InviteBootstrapHint(
        gfs_url=gfs_url,
        invite_token=token,
        space_id=space_id,
        instance_id=party.instance_id,
        identity_pk=party.identity_pk.hex(),
        keywrap_pk=party.keywrap_pub.hex(),
        keywrap_sig=party.keywrap_sig,
        proto_version=proto,
        display_hint=party.display_name,
    )


def _bootstrap_pair(
    *,
    min_age=0,
    banned=False,
    token_uses=1,
    expired=False,
    role=SpaceRole.MEMBER.value,
):
    """Wire a redeemer + an issuer coordinator over a fake relay."""
    relay = _RelayBus()
    redeemer_party = _Party("Redeemer household")
    issuer_party = _Party("Issuer household")

    r_fed = _BootstrapFederationService(redeemer_party)
    i_fed = _BootstrapFederationService(issuer_party)
    r_repo = _BootstrapFederationRepo(redeemer_party)
    i_repo = _BootstrapFederationRepo(issuer_party)

    r_spaces = _FakeSpaceRepo()
    i_spaces = _FakeSpaceRepo()
    i_spaces.tokens["tok-1"] = {
        "space_id": "space-1",
        "created_by": "u-owner",
        "uses_remaining": token_uses,
        "expired": expired,
        "role": role,
    }
    i_spaces.space_rows_for_get["space-1"] = _a_space(
        "space-1",
        owner_instance_id=issuer_party.instance_id,
        min_age=min_age,
    )
    if banned:
        i_spaces.bans.add(("space-1", "u-local"))

    redeemer = _make_coordinator(
        federation=r_fed,
        federation_repo=r_repo,
        space_repo=r_spaces,
    )
    i_members = _FakeRemoteMemberRepo()
    issuer = _make_coordinator(
        federation=i_fed,
        federation_repo=i_repo,
        space_repo=i_spaces,
        remote_members=i_members,
    )
    for coord, party in ((redeemer, redeemer_party), (issuer, issuer_party)):
        coord.attach_bootstrap(
            relay_sender=relay.sender_for(party.instance_id),
            keywrap_private_key=party.keywrap_priv,
            keywrap_public_key=party.keywrap_pub,
            keywrap_sig=party.keywrap_sig,
            key_manager=_FakeKeyManager(),
        )
    relay.coordinators[redeemer_party.instance_id] = redeemer
    relay.coordinators[issuer_party.instance_id] = issuer
    return SimpleNamespace(
        relay=relay,
        redeemer=redeemer,
        issuer=issuer,
        redeemer_party=redeemer_party,
        issuer_party=issuer_party,
        redeemer_repo=r_repo,
        issuer_repo=i_repo,
        redeemer_spaces=r_spaces,
        issuer_spaces=i_spaces,
        issuer_members=i_members,
        hint=_hint_for(issuer_party),
    )


async def test_bootstrap_redeem_round_trip_seats_the_space():
    """A stranger's invite link joins the space with no pairing."""
    env = _bootstrap_pair()
    result = await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    assert result["space_id"] == "space-1"
    assert result["role"] == SpaceRole.MEMBER.value
    # Issuer seated us; we seated the issuer's space mapping.
    assert (
        "space-1",
        env.redeemer_party.instance_id,
    ) in env.issuer_spaces.space_instances
    assert (
        "space-1",
        env.issuer_party.instance_id,
    ) in env.redeemer_spaces.space_instances
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 0


async def test_bootstrap_seats_a_space_scoped_instance_on_both_sides():
    """The row must be ``space_session``, never a full social peer."""
    env = _bootstrap_pair()
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    for repo in (env.redeemer_repo, env.issuer_repo):
        assert len(repo.saved) == 1
        row = repo.saved[0]
        assert row.source is InstanceSource.SPACE_SESSION
        assert row.status is PairingStatus.CONFIRMED
        # No address is exchanged on this path by design.
        assert row.remote_inbox_url == ""


async def test_bootstrap_session_keys_mirror_across_the_pair():
    env = _bootstrap_pair()
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    km = _FakeKeyManager()
    r_row = env.redeemer_repo.saved[0]
    i_row = env.issuer_repo.saved[0]
    assert km.decrypt(r_row.key_self_to_remote) == km.decrypt(i_row.key_remote_to_self)
    assert km.decrypt(i_row.key_self_to_remote) == km.decrypt(r_row.key_remote_to_self)


async def test_bootstrap_relay_sees_only_the_recipient():
    """Identity-free routing envelope (#677)."""
    env = _bootstrap_pair()
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    assert env.relay.envelopes
    for to_instance, envelope in env.relay.envelopes:
        assert set(envelope) == {"to_instance", "sealed"}
        blob = json.dumps(envelope)
        assert "tok-1" not in blob
        assert "space-1" not in blob
        assert "u-local" not in blob
        # The sender never appears — only the addressee.
        senders = {env.redeemer_party.instance_id, env.issuer_party.instance_id}
        assert senders - {to_instance} != set()
        assert (senders - {to_instance}).pop() not in blob


async def test_bootstrap_rejected_when_issuer_is_too_old():
    """Sub-v_29 issuer → the unchanged "pair with them" message."""
    env = _bootstrap_pair()
    stale_hint = _hint_for(env.issuer_party, proto=OURS - 1)
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=stale_hint,
        )
    assert "no route to issuer — pair with them" in str(exc.value)
    assert env.relay.envelopes == []


async def test_bootstrap_unknown_token_denies_and_seats_nothing():
    env = _bootstrap_pair()
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "no-such-token",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE
    assert env.issuer_repo.saved == []
    assert env.issuer_spaces.space_instances == []


async def test_bootstrap_expired_token_denies():
    env = _bootstrap_pair(expired=True)
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE


async def test_bootstrap_exhausted_token_denies():
    env = _bootstrap_pair(token_uses=0)
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE


async def test_bootstrap_banned_redeemer_denies():
    env = _bootstrap_pair(banned=True)
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE


async def test_bootstrap_age_gate_blocks_underage_redeemer():
    """§CP.F1 — the host's min_age rides the ACK and gates seating."""
    cp = SimpleNamespace(is_age_allowed=AsyncMock(return_value=False))
    env = _bootstrap_pair(min_age=18)
    env.redeemer._child_protection = cp
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert "restricted to users aged 18+" in str(exc.value)
    # Nothing seated locally — not even the space-session instance row.
    assert env.redeemer_repo.saved == []
    assert env.redeemer_spaces.space_instances == []


async def test_bootstrap_replayed_envelope_is_rejected():
    env = _bootstrap_pair(token_uses=2)
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    first_to, first_env = env.relay.envelopes[0]
    assert first_to == env.issuer_party.instance_id
    with pytest.raises(ValueError, match="Replay detected"):
        await env.issuer.handle_relayed_envelope(first_env)
    # The replay must not have consumed a second use.
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 1


async def test_bootstrap_spoofed_identity_is_rejected():
    """§4.1.2 — a body claiming somebody else's instance_id is refused."""
    env = _bootstrap_pair()
    victim = _Party("Victim")
    body = {
        "kind": KIND_REDEEM,
        "invite_token": "tok-1",
        "space_id": "space-1",
        "redeem_nonce": "n" * 32,
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance_id": victim.instance_id,  # not ours
        "identity_pk": env.redeemer_party.identity_pk.hex(),
        "keywrap_pk": env.redeemer_party.keywrap_pub.hex(),
        "keywrap_sig": env.redeemer_party.keywrap_sig,
        "display_name": "spoofed",
        "redeemer_user_id": "u-evil",
        "redeemer_display_name": "Evil",
        "redeemer_public_key": None,
    }
    signed = sign_bootstrap_body(body, identity_seed=env.redeemer_party.seed)
    envelope = {
        "to_instance": env.issuer_party.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=env.issuer_party.keywrap_pub,
            plaintext=json.dumps(signed).encode(),
        ),
    }
    with pytest.raises(ValueError, match="does not match identity_pk"):
        await env.issuer.handle_relayed_envelope(envelope)
    # Fail-closed: the token was never consumed.
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 1
    assert env.issuer_repo.saved == []


async def test_bootstrap_unbound_keywrap_key_is_rejected():
    """We must not seal a space snapshot to a key nobody vouched for."""
    env = _bootstrap_pair()
    attacker_kw = generate_x25519_keypair()
    body = {
        "kind": KIND_REDEEM,
        "invite_token": "tok-1",
        "space_id": "space-1",
        "redeem_nonce": "n" * 32,
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance_id": env.redeemer_party.instance_id,
        "identity_pk": env.redeemer_party.identity_pk.hex(),
        "keywrap_pk": attacker_kw.public_key.hex(),
        "keywrap_sig": env.redeemer_party.keywrap_sig,
        "display_name": "sneaky",
        "redeemer_user_id": "u-local",
        "redeemer_display_name": "Ada",
        "redeemer_public_key": None,
    }
    signed = sign_bootstrap_body(body, identity_seed=env.redeemer_party.seed)
    envelope = {
        "to_instance": env.issuer_party.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=env.issuer_party.keywrap_pub,
            plaintext=json.dumps(signed).encode(),
        ),
    }
    with pytest.raises(ValueError, match="not bound to its identity"):
        await env.issuer.handle_relayed_envelope(envelope)
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 1


async def test_bootstrap_reply_from_another_household_is_dropped():
    """The reply leg is pinned to the identity the invite blob named."""
    env = _bootstrap_pair()
    imposter = _Party("Imposter")
    # Park a pending redeem by hand so we can aim a reply at its nonce.
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    env.redeemer._pending["n1"] = fut
    env.redeemer._bootstrap_hints["n1"] = env.hint
    body = {
        "kind": KIND_REDEEM_ACK,
        "redeem_nonce": "n1",
        "space_id": "space-1",
        "role": "member",
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance_id": imposter.instance_id,
        "identity_pk": imposter.identity_pk.hex(),
        "keywrap_pk": imposter.keywrap_pub.hex(),
        "keywrap_sig": imposter.keywrap_sig,
        "display_name": "Imposter",
    }
    signed = sign_bootstrap_body(body, identity_seed=imposter.seed)
    envelope = {
        "to_instance": env.redeemer_party.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=env.redeemer_party.keywrap_pub,
            plaintext=json.dumps(signed).encode(),
        ),
    }
    with pytest.raises(ValueError, match="did not address"):
        await env.redeemer.handle_relayed_envelope(envelope)
    assert not fut.done()


async def test_bootstrap_relay_failure_surfaces_as_permission_error():
    env = _bootstrap_pair()
    env.relay.deliver = False
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
            bootstrap=env.hint,
        )
    assert "connection server" in str(exc.value)


async def test_bootstrap_not_offered_without_a_hint():
    """Without the invite blob there is nothing to address — unchanged."""
    env = _bootstrap_pair()
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id=env.issuer_party.instance_id,
        )
    assert "not a confirmed peer" in str(exc.value)


async def test_bootstrap_hint_for_a_different_issuer_is_ignored():
    env = _bootstrap_pair()
    with pytest.raises(SpacePermissionError) as exc:
        await env.redeemer.request_redeem(
            "tok-1",
            viewer_user_id="u-local",
            issuer_instance_id="some-other-household",
            bootstrap=env.hint,
        )
    assert "not a confirmed peer" in str(exc.value)


async def test_bootstrap_does_not_overwrite_an_existing_relationship():
    """A real pairing must never be re-keyed by an invite link."""
    env = _bootstrap_pair()
    existing = _FakeInstance(env.redeemer_party.instance_id)
    env.issuer_repo._instances[env.redeemer_party.instance_id] = existing
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    assert env.issuer_repo.saved == []
    assert env.issuer_repo._instances[env.redeemer_party.instance_id] is existing


async def test_bootstrap_inbound_is_rate_limited_process_wide():
    """The relay hands us blobs from anyone — cap them before the unseal."""
    env = _bootstrap_pair()
    limiter = RateLimiter()
    env.issuer._rate_limiter = limiter
    for _ in range(BOOTSTRAP_INBOUND_LIMIT):
        assert limiter.is_allowed(
            "invite-bootstrap:inbound",
            limit=BOOTSTRAP_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        )
    with pytest.raises(ValueError, match="inbound rate limit"):
        await env.issuer.handle_relayed_envelope(
            {"to_instance": env.issuer_party.instance_id, "sealed": {}},
        )


async def test_bootstrap_inbound_is_rate_limited_per_sender():
    """One household can't burn the whole process-wide budget."""
    env = _bootstrap_pair(token_uses=5)
    limiter = RateLimiter()
    env.issuer._rate_limiter = limiter
    bucket = f"invite-bootstrap:sender:{env.redeemer_party.instance_id}"
    for _ in range(BOOTSTRAP_PER_SENDER_LIMIT):
        assert limiter.is_allowed(
            bucket,
            limit=BOOTSTRAP_PER_SENDER_LIMIT,
            window_s=BOOTSTRAP_PER_SENDER_WINDOW_S,
        )
    body = sign_bootstrap_body(
        {
            "kind": KIND_REDEEM,
            "invite_token": "tok-1",
            "space_id": "space-1",
            "redeem_nonce": "n" * 32,
            "ts": datetime.now(timezone.utc).isoformat(),
            "instance_id": env.redeemer_party.instance_id,
            "identity_pk": env.redeemer_party.identity_pk.hex(),
            "keywrap_pk": env.redeemer_party.keywrap_pub.hex(),
            "keywrap_sig": env.redeemer_party.keywrap_sig,
            "display_name": "Redeemer",
            "redeemer_user_id": "u-local",
            "redeemer_display_name": "Ada",
            "redeemer_public_key": None,
        },
        identity_seed=env.redeemer_party.seed,
    )
    envelope = {
        "to_instance": env.issuer_party.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=env.issuer_party.keywrap_pub,
            plaintext=json.dumps(body).encode(),
        ),
    }
    with pytest.raises(ValueError, match="per-sender rate limit"):
        await env.issuer.handle_relayed_envelope(envelope)
    # Throttled before the token was touched.
    assert env.issuer_spaces.tokens["tok-1"]["uses_remaining"] == 5


async def test_bootstrap_both_legs_travel_through_the_blob_s_server():
    """The redeemer hands its request to the connection server that served
    the invite blob, and the issuer answers on the same one — a household
    paired with several servers must not reply somewhere the requester
    isn't listening."""
    env = _bootstrap_pair()
    hint = _hint_for(env.issuer_party, gfs_url="https://relay-b.example")
    await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=hint,
    )
    assert env.relay.gfs_urls == ["https://relay-b.example"] * 2


# ── Invite-link roles (migration 0053) ────────────────────────────────


async def test_redeem_of_an_admin_link_seats_an_admin():
    """The seat comes off the ISSUER's stored row. An ``admin`` link
    lands the redeemer's household as an admin of the space — on the
    issuer's roster and in the ACK the receiver mirrors."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-admin",
            "created_by": "owner",
            "uses_remaining": 1,
            "role": SpaceRole.ADMIN.value,
        },
    )
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result["role"] == SpaceRole.ADMIN.value
    assert issuer_members.roles == [
        ("sp-admin", "sender-1", "u-local", SpaceRole.ADMIN.value)
    ]


async def test_admin_seat_shares_the_delegated_admin_signing_seed():
    """An admin seated by link gets what an admin seated by promotion
    gets — the same seam ``set_remote_member_role`` uses, so the new
    admin can act with the owner offline."""
    sender, issuer, _sf, _if, _repo, _members = _wire_pair(
        {
            "space_id": "sp-seed",
            "created_by": "owner",
            "uses_remaining": 1,
            "role": SpaceRole.ADMIN.value,
        },
    )
    shared: list[tuple[str, str]] = []

    async def _share(space_id, *, instance_id):
        shared.append((space_id, instance_id))

    issuer.attach_admin_seed_sharer(_share)
    await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert shared == [("sp-seed", "sender-1")]


async def test_member_seat_shares_no_signing_seed():
    """The seed is an ADMIN-only consequence — a plain member link must
    never trigger it."""
    sender, issuer, _sf, _if, _repo, _members = _wire_pair(
        {"space_id": "sp-plain", "created_by": "owner", "uses_remaining": 1},
    )
    shared: list = []

    async def _share(space_id, *, instance_id):
        shared.append((space_id, instance_id))

    issuer.attach_admin_seed_sharer(_share)
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result["role"] == SpaceRole.MEMBER.value
    assert shared == []


async def test_a_failing_seed_share_still_seats_the_admin():
    """Fail-soft: a share that raises must not un-seat an admin who
    legitimately redeemed."""
    sender, issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-seed-fail",
            "created_by": "owner",
            "uses_remaining": 1,
            "role": SpaceRole.ADMIN.value,
        },
    )

    async def _boom(space_id, *, instance_id):
        raise RuntimeError("peer unreachable")

    issuer.attach_admin_seed_sharer(_boom)
    result = await sender.request_redeem(
        "good-token",
        viewer_user_id="u-local",
        issuer_instance_id="issuer-1",
    )
    assert result["role"] == SpaceRole.ADMIN.value
    assert issuer_members.added


async def test_subscriber_link_is_refused_across_households():
    """``space_remote_members.role`` admits member|admin only (migration
    0009 — a subscriber has no row there at all). Rather than silently
    upgrading a reader to a writer, the cross-household redeem of a
    subscriber link fails closed with a reason the SPA can render."""
    sender, _issuer, _sf, _if, _repo, issuer_members = _wire_pair(
        {
            "space_id": "sp-sub",
            "created_by": "owner",
            "uses_remaining": 1,
            "role": SpaceRole.SUBSCRIBER.value,
        },
    )
    with pytest.raises(SpacePermissionError) as exc:
        await sender.request_redeem(
            "good-token",
            viewer_user_id="u-local",
            issuer_instance_id="issuer-1",
        )
    assert "household that issued it" in str(exc.value)
    assert issuer_members.added == []


async def test_bootstrap_redeem_of_an_admin_link_seats_an_admin():
    """The §D2b stranger path shares ``_consume_seat_and_build_ack``, so
    the role reaches a household that has never federated with us too."""
    env = _bootstrap_pair(role=SpaceRole.ADMIN.value)
    shared: list = []

    async def _share(space_id, *, instance_id):
        shared.append((space_id, instance_id))

    env.issuer.attach_admin_seed_sharer(_share)
    result = await env.redeemer.request_redeem(
        "tok-1",
        viewer_user_id="u-local",
        issuer_instance_id=env.issuer_party.instance_id,
        bootstrap=env.hint,
    )
    assert result["role"] == SpaceRole.ADMIN.value
    assert env.issuer_members.roles == [
        ("space-1", env.redeemer_party.instance_id, "u-local", SpaceRole.ADMIN.value)
    ]
    assert shared == [("space-1", env.redeemer_party.instance_id)]
