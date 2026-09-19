"""Tests for the §24.11 inbound validation middleware chain.

Each step is tested in isolation so a failure pinpoints the exact
validation phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import orjson
import pytest

from socialhome.crypto import REPLAY_CACHE_WINDOW, ReplayCache
from socialhome.domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
    InstanceSource,
)
from socialhome.federation.inbound_validator import (
    RELAY_QUEUE_TTL_SECONDS,
    RELAY_TIMESTAMP_SKEW_SECONDS,
    TRANSPORT_GFS_RELAY,
    InboundContext,
    InboundPipeline,
    make_ban_check,
    make_check_deprovisioned_author,
    make_check_peer_class,
    make_check_space_writer,
    make_check_replay,
    make_check_timestamp,
    make_idempotency_check,
    make_lookup_instance,
    make_parse_json,
    make_persist_replay,
)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _loads(raw):
    return orjson.loads(raw)


def _minimal_envelope(**overrides) -> dict:
    base = {
        "msg_id": "m1",
        "event_type": "space_post_created",
        "from_instance": "remote-iid",
        "to_instance": "self-iid",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": "nonce:ciphertext",
        "sig_suite": "ed25519",
        "signatures": {"ed25519": "sig"},
    }
    base.update(overrides)
    return base


# ─── Step 1: parse_json ──────────────────────────────────────────────────


async def test_parse_json_success():
    step = make_parse_json(loads=_loads)
    ctx = InboundContext(
        raw_body=orjson.dumps(_minimal_envelope()),
    )
    await step(ctx)
    assert ctx.envelope["msg_id"] == "m1"


async def test_parse_json_rejects_garbage():
    step = make_parse_json(loads=_loads)
    ctx = InboundContext(raw_body=b"not json")
    with pytest.raises(ValueError, match="Invalid JSON"):
        await step(ctx)


async def test_parse_json_rejects_missing_fields():
    step = make_parse_json(loads=_loads)
    ctx = InboundContext(raw_body=orjson.dumps({"msg_id": "m1"}))
    with pytest.raises(ValueError, match="Missing required fields"):
        await step(ctx)


async def test_parse_json_rejects_unknown_event_type():
    step = make_parse_json(loads=_loads)
    ctx = InboundContext(
        raw_body=orjson.dumps(_minimal_envelope(event_type="not_a_real_event")),
    )
    with pytest.raises(ValueError, match="Unknown event_type"):
        await step(ctx)


async def test_parse_json_accepts_space_route_stale():
    """The route-stale nack is a first-class ``FederationEventType`` —
    the §24.11 parse step must let it through to instance lookup /
    signature verify rather than rejecting it as unknown."""
    step = make_parse_json(loads=_loads)
    ctx = InboundContext(
        raw_body=orjson.dumps(_minimal_envelope(event_type="space_route_stale")),
    )
    await step(ctx)
    assert ctx.envelope["event_type"] == FederationEventType.SPACE_ROUTE_STALE


# ─── Step 2: lookup_instance ─────────────────────────────────────────────


class _FakeInstance:
    remote_identity_pk = "aa" * 32
    key_remote_to_self = "enc"
    from_instance = "remote-iid"


async def test_lookup_instance_resolves():
    async def _lookup(repo, wh_id):
        return _FakeInstance() if wh_id == "wh-1" else None

    step = make_lookup_instance(repo=None, lookup_fn=_lookup)
    ctx = InboundContext(inbox_id="wh-1")
    await step(ctx)
    assert ctx.instance is not None


async def test_lookup_instance_rejects_unknown():
    async def _lookup(repo, wh_id):
        return None

    step = make_lookup_instance(repo=None, lookup_fn=_lookup)
    ctx = InboundContext(inbox_id="unknown")
    with pytest.raises(ValueError, match="No instance found"):
        await step(ctx)


async def test_lookup_instance_rejects_provisional_row():
    """``AutoPairCoordinator.request_via`` plants a provisional row with
    an empty ``remote_identity_pk`` before the relay ack lands. If the
    other side's first envelope arrives during that window, the inbox
    lookup must surface a 404-equivalent so the outbox retries —
    otherwise sig verify runs against an empty pk and 403s, and the
    outbox drops the row terminally."""

    class _Provisional:
        # A row that exists (lookup succeeded) but isn't ready to
        # validate against yet.
        remote_identity_pk = ""
        key_remote_to_self = ""
        from_instance = "remote-iid"

    async def _lookup(repo, wh_id):
        return _Provisional() if wh_id == "wh-prov" else None

    step = make_lookup_instance(repo=None, lookup_fn=_lookup)
    ctx = InboundContext(inbox_id="wh-prov")
    with pytest.raises(ValueError, match="No instance found"):
        await step(ctx)


# ─── Step 3: check_timestamp ─────────────────────────────────────────────


async def test_check_timestamp_passes_for_recent():
    step = make_check_timestamp()
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope()
    await step(ctx)  # should not raise


async def test_check_timestamp_rejects_stale():
    step = make_check_timestamp()
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope(timestamp="2000-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="Timestamp skew"):
        await step(ctx)


async def test_check_timestamp_rejects_garbage():
    step = make_check_timestamp()
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope(timestamp="not-a-date")
    with pytest.raises(ValueError, match="Unparseable"):
        await step(ctx)


# ─── Step 5: check_replay ────────────────────────────────────────────────


class _FakeReplayCache:
    def __init__(self, *, already_seen=False):
        self._seen = already_seen

    def seen(self, msg_id, *, from_instance="", now=None):
        return self._seen


async def test_replay_passes_fresh():
    step = make_check_replay(replay_cache=_FakeReplayCache())
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope()
    await step(ctx)  # no raise


async def test_replay_rejects_duplicate():
    step = make_check_replay(replay_cache=_FakeReplayCache(already_seen=True))
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope()
    with pytest.raises(ValueError, match="Replay detected"):
        await step(ctx)


# ─── Step 8: idempotency ─────────────────────────────────────────────────


class _FakeIdempotencyCache:
    def __init__(self, *, accept=True):
        self._accept = accept

    def check_and_mark(self, key):
        return self._accept


async def test_idempotency_no_key_passes():
    step = make_idempotency_check(
        cache_holder=lambda: _FakeIdempotencyCache(),
    )
    ctx = InboundContext()
    ctx.event = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_POST_CREATED,
        from_instance="r",
        to_instance="s",
        timestamp="t",
        payload={"content": "hi"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_idempotency_duplicate_short_circuits():
    step = make_idempotency_check(
        cache_holder=lambda: _FakeIdempotencyCache(accept=False),
    )
    ctx = InboundContext()
    ctx.event = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_POST_CREATED,
        from_instance="r",
        to_instance="s",
        timestamp="t",
        payload={"idempotency_key": "ik-1"},
    )
    await step(ctx)
    assert ctx.early_response == {"status": "ok", "deduped": True}


# ─── Step 9: ban_check ──────────────────────────────────────────────────


class _FakeBanRepo:
    def __init__(self, banned_combos=None):
        self._banned = set(banned_combos or [])

    async def is_instance_banned_from_space(self, space_id, instance_id):
        return (space_id, instance_id) in self._banned


async def test_ban_check_passes_non_space_event():
    step = make_ban_check(federation_repo=_FakeBanRepo())
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope()
    await step(ctx)  # no space_id → skip


async def test_ban_check_passes_allowed():
    step = make_ban_check(federation_repo=_FakeBanRepo())
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope(space_id="sp-1")
    await step(ctx)  # not banned


async def test_ban_check_rejects_banned():
    step = make_ban_check(
        federation_repo=_FakeBanRepo(
            banned_combos=[("sp-1", "remote-iid")],
        ),
    )
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope(space_id="sp-1")
    with pytest.raises(ValueError, match="banned"):
        await step(ctx)


# ─── Step 10: persist_replay ─────────────────────────────────────────────


class _FakePersistRepo:
    def __init__(self):
        self.inserted: list[str] = []

    async def insert_replay_id(self, msg_id):
        self.inserted.append(msg_id)


async def test_persist_replay_inserts():
    repo = _FakePersistRepo()
    step = make_persist_replay(federation_repo=repo)
    ctx = InboundContext()
    ctx.envelope = _minimal_envelope()
    await step(ctx)
    assert "m1" in repo.inserted


# ─── Step 11: check_deprovisioned_author ─────────────────────────────────


class _FakeRemoteUser:
    """Stand-in for :class:`RemoteUser` — only ``deprovisioned_at`` is
    read by the filter."""

    def __init__(self, deprovisioned_at: str | None) -> None:
        self.deprovisioned_at = deprovisioned_at


class _FakeUserRepo:
    def __init__(self, by_user_id: dict[str, _FakeRemoteUser]) -> None:
        self._by_user_id = by_user_id

    async def get_remote(self, user_id: str):
        return self._by_user_id.get(user_id)


def _event(event_type: FederationEventType, payload: dict) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance="peer-x",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


async def test_deprovisioned_author_drops_user_scoped_event():
    """A MOMENT_CREATED whose ``author_user_id`` is a deprovisioned
    remote user is dropped — the pipeline's early-response is set so
    the dispatch handler never sees it."""
    user_repo = _FakeUserRepo(
        {"u-hidden": _FakeRemoteUser(deprovisioned_at="2026-05-19 16:48:41")},
    )
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.MOMENT_CREATED,
        {"author_user_id": "u-hidden", "content": "should be dropped"},
    )
    await step(ctx)
    assert ctx.early_response == {
        "status": "ok",
        "dropped": "deprovisioned-author",
    }


async def test_deprovisioned_author_passes_active_user():
    """Author with no ``deprovisioned_at`` (active remote user) passes
    the filter — the dispatch handler still gets the event."""
    user_repo = _FakeUserRepo(
        {"u-alive": _FakeRemoteUser(deprovisioned_at=None)},
    )
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.MOMENT_CREATED,
        {"author_user_id": "u-alive"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_deprovisioned_author_passes_when_no_remote_user_row():
    """A user we've never heard of (no ``remote_users`` row) passes
    through. The USER_UPDATED upsert path mints the row later — we
    don't preemptively block strangers."""
    user_repo = _FakeUserRepo({})
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.MOMENT_CREATED,
        {"author_user_id": "u-stranger"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_deprovisioned_author_skips_unmapped_event_types():
    """Space-scoped / routing-only events are not in the author-field
    map and pass through unchanged. ``space_post_created`` is the
    representative — its routing is via space_id, not author."""
    user_repo = _FakeUserRepo(
        {"u-hidden": _FakeRemoteUser(deprovisioned_at="2026-05-19 16:48:41")},
    )
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"author_user_id": "u-hidden"},  # field present but type unmapped
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_deprovisioned_author_does_not_drop_user_updated():
    """USER_UPDATED is the un-hide signal — the sender re-publishing
    a profile clears ``deprovisioned_at`` on our side via
    ``upsert_remote``. Filtering it would lock the user in the
    deprovisioned state forever and break the visibility-toggle
    round-trip. Must pass through unconditionally."""
    user_repo = _FakeUserRepo(
        {"u-hidden": _FakeRemoteUser(deprovisioned_at="2026-05-19 16:48:41")},
    )
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.USER_UPDATED,
        {"user_id": "u-hidden", "display_name": "Ada"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_deprovisioned_author_uses_per_event_field_name():
    """``HIGHLIGHT_FRAME_VIEWED`` keys off ``viewer_user_id``, not
    ``author_user_id`` — the receipt's actor is the viewer. A
    deprovisioned viewer means the local user that's hidden from the
    author's home instance; we should still drop it (the author would
    otherwise see a view-receipt from someone they shouldn't be
    receiving anything from)."""
    user_repo = _FakeUserRepo(
        {"u-hidden": _FakeRemoteUser(deprovisioned_at="2026-05-19 16:48:41")},
    )
    step = make_check_deprovisioned_author(user_repo=user_repo)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.HIGHLIGHT_FRAME_VIEWED,
        {
            "viewer_user_id": "u-hidden",
            "author_user_id": "u-author",  # not what's checked here
        },
    )
    await step(ctx)
    assert ctx.early_response is not None


# ─── Pipeline composition ────────────────────────────────────────────────


async def test_pipeline_stops_on_error():
    called: list[str] = []

    async def step_a(ctx):
        called.append("a")

    async def step_b(ctx):
        called.append("b")
        raise ValueError("boom")

    async def step_c(ctx):
        called.append("c")

    pipeline = InboundPipeline([step_a, step_b, step_c])
    ctx = InboundContext()
    with pytest.raises(ValueError, match="boom"):
        await pipeline.run(ctx)
    assert called == ["a", "b"]


async def test_pipeline_stops_on_early_response():
    async def short_circuit(ctx):
        ctx.early_response = {"status": "ok", "deduped": True}

    async def unreachable(ctx):
        raise AssertionError("should not be called")

    pipeline = InboundPipeline([short_circuit, unreachable])
    ctx = InboundContext()
    result = await pipeline.run(ctx)
    assert result == {"status": "ok", "deduped": True}


# ─── Peer-class gate: what a space-scoped peer may send (§D2b) ───────────


class _SourcedInstance:
    """Minimal stand-in exposing the one attribute the gate reads."""

    def __init__(self, source):
        self.source = source
        self.from_instance = "remote-iid"


async def test_peer_class_gate_passes_every_type_for_a_manual_peer():
    """A QR-paired household is a social peer — nothing is filtered."""
    step = make_check_peer_class()
    for event_type in FederationEventType:
        ctx = InboundContext(
            envelope=_minimal_envelope(event_type=event_type.value),
            instance=_SourcedInstance(InstanceSource.MANUAL),
        )
        await step(ctx)


async def test_peer_class_gate_allows_the_space_families_from_a_link_joined_peer():
    step = make_check_peer_class()
    for event_type in SPACE_SESSION_ALLOWED_EVENT_TYPES:
        ctx = InboundContext(
            envelope=_minimal_envelope(event_type=event_type.value),
            instance=_SourcedInstance(InstanceSource.SPACE_SESSION),
        )
        await step(ctx)


async def test_peer_class_gate_rejects_every_unclassified_type_by_default():
    """Deny-by-default over the WHOLE enum.

    Enumerating ``set(FederationEventType)`` rather than a hand-written
    list is the point: a federation type added tomorrow is refused from a
    space-scoped peer until somebody puts it in the allow-list on
    purpose.
    """
    step = make_check_peer_class()
    denied = set(FederationEventType) - SPACE_SESSION_ALLOWED_EVENT_TYPES
    assert denied, "the allow-list must not swallow the whole enum"
    for event_type in denied:
        ctx = InboundContext(
            envelope=_minimal_envelope(event_type=event_type.value),
            instance=_SourcedInstance(InstanceSource.SPACE_SESSION),
        )
        with pytest.raises(ValueError, match="not permitted"):
            await step(ctx)


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        FederationEventType.SPACE_FIND_ROUTE,
        FederationEventType.SPACE_ROUTE_FOUND,
        FederationEventType.SPACE_ROUTED,
        FederationEventType.SPACE_JOIN_REQUEST,
        FederationEventType.SPACE_JOIN_REQUEST_VIA,
        FederationEventType.DM_MESSAGE,
        FederationEventType.USERS_SYNC,
        FederationEventType.USER_IDENTITY_RESOLVE,
        FederationEventType.CALL_OFFER,
        FederationEventType.PRESENCE_UPDATED,
        FederationEventType.MOMENT_CREATED,
        FederationEventType.NETWORK_SYNC,
        FederationEventType.URL_UPDATED,
    ],
)
async def test_peer_class_gate_names_the_types_the_review_probed(event_type):
    """The specific types an adversarial reviewer pushed through."""
    step = make_check_peer_class()
    ctx = InboundContext(
        envelope=_minimal_envelope(event_type=event_type.value),
        instance=_SourcedInstance(InstanceSource.SPACE_SESSION),
    )
    with pytest.raises(ValueError, match="not permitted"):
        await step(ctx)


async def test_peer_class_gate_logs_type_and_instance_but_never_payload(caplog):
    step = make_check_peer_class()
    ctx = InboundContext(
        envelope=_minimal_envelope(
            event_type=FederationEventType.DM_MESSAGE.value,
            encrypted_payload="secret-ciphertext-marker",
        ),
        instance=_SourcedInstance(InstanceSource.SPACE_SESSION),
    )
    with caplog.at_level("INFO"):
        with pytest.raises(ValueError):
            await step(ctx)
    text = caplog.text
    assert "dm_message" in text
    assert "remote-iid" in text
    assert "secret-ciphertext-marker" not in text


async def test_peer_class_gate_is_a_step_in_both_shipped_pipelines():
    """Mutation guard: the gate has to be IN the chain, not merely exist."""
    from socialhome.federation.federation_service import FederationService

    svc = FederationService.__new__(FederationService)
    steps = FederationService._common_pipeline_steps(
        _StubPipelineOwner(),
        lookup_step=_noop_step,
    )
    names = [getattr(s, "__name__", "") for s in steps]
    assert "check_peer_class" in names
    # Runs AFTER the instance lookup — it reads ``ctx.instance.source``.
    assert names.index("check_peer_class") > names.index("_noop_step")
    assert svc is not None


async def _noop_step(ctx):
    return None


class _StubPipelineOwner:
    """Just enough of :class:`FederationService` for the step builder."""

    _encoder = None
    _replay_cache = None
    _key_manager = None
    _federation_repo = None
    _idempotency_cache = None
    _user_repo = None
    _space_repo = None
    _space_remote_member_repo = None
    _own_instance_id = "own-1"


# ─── Step 12: check_space_writer (the read-only Follower gate) ───────────


class _FakeFeatures:
    def __init__(self, allow_subscriber_comment: bool = False) -> None:
        self.allow_subscriber_comment = allow_subscriber_comment
        self.allow_subscriber_react = False


class _FakeSpace:
    def __init__(self, owner_instance_id: str, *, allow_comment: bool = False) -> None:
        self.owner_instance_id = owner_instance_id
        self.features = _FakeFeatures(allow_comment)


class _FakeSpaceRepo:
    def __init__(self, by_id: dict) -> None:
        self._by_id = by_id

    async def get(self, space_id: str):
        return self._by_id.get(space_id)


class _FakeSeat:
    def __init__(self, role: str) -> None:
        self.role = role


class _FakeRemoteMemberRepo:
    def __init__(self, seats: dict) -> None:
        self._seats = seats

    async def get(self, space_id, instance_id, user_id):
        return self._seats.get((space_id, instance_id, user_id))


OWN = "own-instance"


def _writer_step(
    *,
    owner=OWN,
    role="subscriber",
    allow_comment=False,
    space_id="sp-1",
):
    return make_check_space_writer(
        space_repo=_FakeSpaceRepo(
            {space_id: _FakeSpace(owner, allow_comment=allow_comment)}
        ),
        remote_member_repo=_FakeRemoteMemberRepo(
            {(space_id, "peer-x", "u-follower"): _FakeSeat(role)}
        ),
        own_instance_id=OWN,
    )


async def test_space_writer_drops_a_post_from_a_follower_household():
    """The flagship refusal: a household seated as a read-only Follower
    holds the space's content key, so it can produce a perfectly valid,
    correctly-signed SPACE_POST_CREATED. The HOST's seat is the
    authority, and it says no."""
    step = _writer_step()
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower", "content": "hi"},
    )
    await step(ctx)
    assert ctx.early_response == {"status": "ok", "dropped": "subscriber-write"}


async def test_space_writer_drops_a_comment_unless_the_space_opted_in():
    """``allow_subscriber_comment`` is the same admin opt-in that decides
    what a LOCAL subscriber may do — a remote follower is the same kind
    of seat, so it governs both."""
    blocked = _writer_step(allow_comment=False)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_COMMENT_CREATED,
        {"space_id": "sp-1", "author": "u-follower", "content": "nice"},
    )
    await blocked(ctx)
    assert ctx.early_response == {"status": "ok", "dropped": "subscriber-write"}

    allowed = _writer_step(allow_comment=True)
    ctx2 = InboundContext()
    ctx2.event = _event(
        FederationEventType.SPACE_COMMENT_CREATED,
        {"space_id": "sp-1", "author": "u-follower", "content": "nice"},
    )
    await allowed(ctx2)
    assert ctx2.early_response is None


async def test_space_writer_never_blocks_a_comment_opt_in_for_a_post():
    """The opt-in is per action: a space that lets followers comment
    still never lets them post."""
    step = _writer_step(allow_comment=True)
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await step(ctx)
    assert ctx.early_response == {"status": "ok", "dropped": "subscriber-write"}


async def test_space_writer_passes_a_member_seat():
    """A ``member`` seat is exactly what it says — untouched."""
    step = _writer_step(role="member")
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_space_writer_ignores_a_space_we_do_not_host():
    """A member household holds a MIRROR of the roster, not authority
    over it. Enforcing on a mirror would drop real content whenever the
    mirror lagged; the host is the single place the decision is made."""
    step = _writer_step(owner="some-other-host")
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_space_writer_passes_a_sender_with_no_seat_row():
    """No row means "not in our mirror", which is the pre-existing state
    for plenty of legitimate senders (a roster that has not converged
    yet). Inventing a rejection there would fail in the direction of
    losing real content."""
    step = _writer_step()
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-stranger"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_space_writer_skips_unmapped_event_types():
    """Only the two author-bearing space CREATE events are gated; a
    roster / routing envelope passes untouched."""
    step = _writer_step()
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_MEMBER_JOINED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await step(ctx)
    assert ctx.early_response is None


async def test_space_writer_fails_soft_on_a_lookup_error():
    """A transient DB hiccup must not start dropping legitimate space
    content — the seat is durable, so the next envelope is gated again."""

    class _Exploding:
        async def get(self, *a, **kw):
            raise RuntimeError("db is having a day")

    step = make_check_space_writer(
        space_repo=_Exploding(),
        remote_member_repo=_Exploding(),
        own_instance_id=OWN,
    )
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await step(ctx)
    assert ctx.early_response is None


# ─── Relay-carried envelopes: the wider skew window (§D2b) ───────────────


async def test_timestamp_step_keeps_the_300s_window_on_a_live_wire():
    step = make_check_timestamp()
    stale = datetime.now(timezone.utc) - timedelta(seconds=400)
    ctx = InboundContext(envelope=_minimal_envelope(timestamp=stale.isoformat()))
    with pytest.raises(ValueError, match="Timestamp skew too large"):
        await step(ctx)


async def test_timestamp_step_accepts_a_20h_old_relay_queued_envelope():
    """The relay holds an envelope for a sleeping household for up to its
    TTL and answers ``202`` to the sender either way — so the sender never
    used the outbox. Judging the drained bytes against the live-wire
    window loses the event outright."""
    step = make_check_timestamp()
    queued = datetime.now(timezone.utc) - timedelta(hours=20)
    ctx = InboundContext(
        envelope=_minimal_envelope(timestamp=queued.isoformat()),
        transport=TRANSPORT_GFS_RELAY,
    )
    await step(ctx)


async def test_the_relay_window_still_has_an_upper_bound():
    step = make_check_timestamp()
    ancient = datetime.now(timezone.utc) - timedelta(hours=30)
    ctx = InboundContext(
        envelope=_minimal_envelope(timestamp=ancient.isoformat()),
        transport=TRANSPORT_GFS_RELAY,
    )
    with pytest.raises(ValueError, match="Timestamp skew too large"):
        await step(ctx)


def test_the_relay_ttl_constant_matches_the_connection_servers():
    from socialhome.global_server.envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS

    assert RELAY_QUEUE_TTL_SECONDS == ENVELOPE_QUEUE_TTL_SECONDS


def test_replay_retention_covers_the_whole_relay_skew_window():
    """The invariant that pays for the wider window.

    A timestamp window is only as safe as the replay memory behind it: if
    the durable ``federation_replay_cache`` forgot a ``msg_id`` while the
    timestamp step would still accept it, a captured relay envelope could
    be replayed into the gap. Shrinking ``REPLAY_CACHE_WINDOW`` back to
    24 h fails here.
    """
    assert REPLAY_CACHE_WINDOW.total_seconds() > RELAY_TIMESTAMP_SKEW_SECONDS, (
        "replay retention must outlast the relay timestamp window"
    )


def test_a_relay_envelope_replayed_10h_later_is_still_remembered():
    """The in-memory cache (sized by the same window) rejects it."""
    cache = ReplayCache(window=REPLAY_CACHE_WINDOW)
    t0 = datetime.now(timezone.utc) - timedelta(hours=20)
    assert cache.seen("relayed-msg", from_instance="b", now=t0) is False
    later = t0 + timedelta(hours=10)
    assert cache.seen("relayed-msg", from_instance="b", now=later) is True
