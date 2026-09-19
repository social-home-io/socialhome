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
    run_post_decrypt_gates,
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


async def test_the_space_writer_gate_is_a_step_in_the_shipped_pipeline():
    """Mutation guard: the Follower gate has to be IN the chain — and
    LAST, so the replay-id is persisted whether or not the write is kept
    (otherwise the sender's outbox redelivers the refused envelope
    forever)."""
    owner = _StubPipelineOwner()
    owner._space_repo = object()  # type: ignore[assignment]
    owner._space_remote_member_repo = object()  # type: ignore[assignment]
    from socialhome.federation.federation_service import FederationService

    steps = FederationService._common_pipeline_steps(
        owner,  # type: ignore[arg-type]
        lookup_step=_noop_step,
    )
    names = [getattr(s, "__name__", "") for s in steps]
    assert names[-1] == "check_space_writer"
    assert names.index("persist_replay") < names.index("check_space_writer")


async def test_the_mesh_gate_set_carries_both_the_ban_and_writer_checks():
    """Mutation guard for the routed path: the inner event of a
    SPACE_ROUTED is judged by the SAME steps, ban check included (the
    pipeline only ever saw the relay's envelope)."""
    owner = _StubPipelineOwner()
    owner._space_repo = object()  # type: ignore[assignment]
    owner._space_remote_member_repo = object()  # type: ignore[assignment]
    names = [
        getattr(s, "__name__", "")
        for s in owner.post_decrypt_gate_steps(include_ban_check=True)
    ]
    assert names == ["ban_check", "check_space_writer"]


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

    def post_decrypt_gate_steps(self, *, include_ban_check: bool = False):
        # The real method, so the mutation guards below exercise the
        # actual composition rather than a stand-in.
        from socialhome.federation.federation_service import FederationService

        return FederationService.post_decrypt_gate_steps(
            self,  # type: ignore[arg-type]
            include_ban_check=include_ban_check,
        )


# ─── Step 12: check_space_writer (the read-only Follower gate) ───────────
#
# The rule under test, in one line: a household that holds only Follower
# seats in a space — or only tombstoned ones — has every space-content
# write refused, on EVERY receiving household, keyed on the signed
# ``from_instance`` and never on a sender-written author field.


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
    def __init__(
        self,
        role: str,
        *,
        user_id: str = "u-follower",
        tombstoned: bool = False,
    ) -> None:
        self.role = role
        self.user_id = user_id
        self.tombstoned = tombstoned


class _FakeRemoteMemberRepo:
    def __init__(self, seats: dict) -> None:
        self._seats = seats

    async def list_for_instance(
        self,
        space_id,
        instance_id,
        *,
        include_tombstoned: bool = True,
    ):
        rows = self._seats.get((space_id, instance_id), [])
        if include_tombstoned:
            return list(rows)
        return [r for r in rows if not r.tombstoned]


OWN = "own-instance"

#: Every content family the gate covers, one representative each, so a
#: type dropped from ``SPACE_WRITE_EVENT_TYPES`` fails a named test and
#: not only the domain tripwire.
_WRITE_SAMPLES = [
    (FederationEventType.SPACE_POST_CREATED, {"author": "u-follower"}),
    (FederationEventType.SPACE_POST_UPDATED, {"id": "p1"}),
    (FederationEventType.SPACE_POST_DELETED, {"post_id": "p1"}),
    (FederationEventType.SPACE_MEDIA_BLOB, {"blob_id": "b1"}),
    (FederationEventType.SPACE_COMMENT_UPDATED, {"comment_id": "c1"}),
    (FederationEventType.SPACE_COMMENT_DELETED, {"comment_id": "c1"}),
    (FederationEventType.SPACE_PAGE_CREATED, {"id": "pg1"}),
    (FederationEventType.SPACE_TASK_CREATED, {"id": "t1"}),
    (FederationEventType.SPACE_TASK_DELETED, {"id": "t1"}),
    (FederationEventType.SPACE_POLL_VOTE_CAST, {"post_id": "p1"}),
    (FederationEventType.SPACE_POLL_CLOSED, {"post_id": "p1"}),
    (FederationEventType.SPACE_STICKY_CREATED, {"id": "s1"}),
    (FederationEventType.SPACE_CALENDAR_EVENT_CREATED, {"event_id": "e1"}),
    (FederationEventType.SPACE_CALENDAR_EVENT_DELETED, {"event_id": "e1"}),
    (FederationEventType.SPACE_RSVP_UPDATED, {"event_id": "e1"}),
    (FederationEventType.SPACE_SCHEDULE_FINALIZED, {"post_id": "p1"}),
    (FederationEventType.SPACE_GALLERY_ITEM_CREATED, {"id": "g1"}),
    (FederationEventType.SPACE_GALLERY_ITEM_DELETED, {"id": "g1"}),
    (FederationEventType.BAZAAR_LISTING_CREATED, {"post_id": "p1"}),
    (FederationEventType.BAZAAR_BID_PLACED, {"bid_id": "b1"}),
    (FederationEventType.BAZAAR_OFFER_ACCEPTED, {"bid_id": "b1"}),
    (FederationEventType.SPACE_LOCATION_UPDATED, {"user_id": "u-follower"}),
    (FederationEventType.SPACE_ZONE_UPSERTED, {"id": "z1"}),
    (FederationEventType.SPACE_ZONE_DELETED, {"id": "z1"}),
]

REFUSED = {"status": "ok", "dropped": "subscriber-write"}


def _writer_step(
    *,
    owner=OWN,
    seats=None,
    allow_comment=False,
    space_id="sp-1",
):
    if seats is None:
        seats = [_FakeSeat("subscriber")]
    return make_check_space_writer(
        space_repo=_FakeSpaceRepo(
            {space_id: _FakeSpace(owner, allow_comment=allow_comment)}
        ),
        remote_member_repo=_FakeRemoteMemberRepo({(space_id, "peer-x"): seats}),
    )


def _space_event(event_type, payload, *, space_id="sp-1"):
    """A write envelope with the ROUTING ``space_id`` set — what every
    sender we ship produces on the direct path."""
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance="peer-x",
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


async def _run(step, event_type, payload, *, space_id="sp-1"):
    ctx = InboundContext()
    ctx.event = _space_event(event_type, payload, space_id=space_id)
    await step(ctx)
    return ctx


@pytest.mark.parametrize("event_type,payload", _WRITE_SAMPLES)
async def test_space_writer_refuses_every_write_family_from_a_follower(
    event_type,
    payload,
):
    """The whole content vocabulary, not just posts and comments.

    A Follower household holds the epoch content key, so it can produce a
    perfectly valid, correctly-signed envelope of ANY of these types — and
    the handlers persist them with no membership check of their own. This
    is the parametrisation that closes that: tasks, stickies, calendar
    events, poll votes, bazaar bids, zones, media bytes and every
    ``*_UPDATED`` / ``*_DELETED`` sibling are refused exactly like a post.
    """
    ctx = await _run(_writer_step(), event_type, payload)
    assert ctx.early_response == REFUSED


async def test_space_writer_refuses_a_spoofed_author():
    """The decision is keyed on the SIGNED ``from_instance``, never on the
    payload's author field, which the sender writes.

    The first cut of this gate looked the author up as a seat and passed
    when it found none — so ``author="anybody"`` walked straight through.
    A household is a Follower or it is not; who it claims to be writing as
    changes nothing.
    """
    ctx = await _run(
        _writer_step(),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-nobody-has-ever-heard-of", "content": "hi"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_refuses_an_unresolvable_space():
    """No routing ``space_id`` and none in the payload → refuse.

    Every writer we ship sets the routing ``space_id``
    (``broadcast_to_space_members`` passes it per peer) or carries it in
    the payload, so an envelope with neither is a sender that removed it.
    Passing it would be the C3 hole: a handler that keys on a row id alone
    (a comment, an RSVP, a calendar delete) would then apply the write to
    whatever space that row lives in.
    """
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-follower", "post_id": "p1"},
    )
    await _writer_step()(ctx)
    assert ctx.early_response == REFUSED


async def test_space_writer_reads_the_space_id_out_of_the_payload():
    """Mesh-routed envelopes carry no plaintext routing ``space_id`` (the
    relay must not learn which space), so the payload's copy is the one
    the gate reads there."""
    ctx = InboundContext()
    ctx.event = _event(
        FederationEventType.SPACE_POST_CREATED,
        {"space_id": "sp-1", "author": "u-follower"},
    )
    await _writer_step()(ctx)
    assert ctx.early_response == REFUSED


async def test_space_writer_refuses_a_household_with_only_tombstoned_seats():
    """A kicked household must not read like a household we never met.

    ``get`` / ``list_for_space`` filter tombstones, so a removed follower
    used to read as "no row" — and "no row" is the one answer this gate is
    lenient about. The gate reads tombstones deliberately.
    """
    ctx = await _run(
        _writer_step(seats=[_FakeSeat("member", tombstoned=True)]),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_passes_a_household_holding_one_live_member():
    """The unit is the HOUSEHOLD: a mixed household (a Follower seat and a
    real member seat) may write, because a real member of it may."""
    ctx = await _run(
        _writer_step(
            seats=[
                _FakeSeat("subscriber", user_id="u-follower"),
                _FakeSeat("member", user_id="u-real"),
            ]
        ),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response is None


async def test_space_writer_passes_an_admin_seat():
    ctx = await _run(
        _writer_step(seats=[_FakeSeat("admin")]),
        FederationEventType.SPACE_TASK_CREATED,
        {"id": "t1"},
    )
    assert ctx.early_response is None


async def test_space_writer_passes_a_household_we_hold_no_row_for():
    """Roster convergence, and the ONLY leniency left.

    Zero rows means the mirror has not converged (a household seated on
    the host before the roster gossip reached us). Refusing there would
    drop real content from real members whenever a roster lagged. A
    household we hold ANY row for gets no such benefit.
    """
    ctx = await _run(
        _writer_step(seats=[]),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response is None


async def test_space_writer_enforces_on_a_household_that_does_not_host():
    """Space content fans out peer-to-peer from the ORIGINATING household
    (``broadcast_to_space_members``), so a member household receives a
    Follower's writes directly and is a first-class enforcement point. The
    first cut only enforced on the host, which the follower could simply
    route around — it learns every member household's instance id from the
    roster snapshot in its own redeem ACK.
    """
    ctx = await _run(
        _writer_step(owner="some-other-host"),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_allows_a_comment_only_with_the_opt_in():
    """``allow_subscriber_comment`` is the one opt-in, and it is the same
    admin toggle that governs a LOCAL subscriber."""
    blocked = await _run(
        _writer_step(allow_comment=False),
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-follower", "content": "nice"},
    )
    assert blocked.early_response == REFUSED

    allowed = await _run(
        _writer_step(allow_comment=True),
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-follower", "content": "nice"},
    )
    assert allowed.early_response is None


async def test_space_writer_binds_the_comment_opt_in_to_a_real_seat():
    """Even under the opt-in the author must name a LIVE ``subscriber``
    seat of that same household — otherwise the exception would be the
    author-spoof hole again, wearing a feature flag."""
    ctx = await _run(
        _writer_step(allow_comment=True),
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-somebody-else", "content": "nice"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_never_extends_the_comment_opt_in_to_a_post():
    """The opt-in is per action: a space that lets followers comment still
    never lets them post."""
    ctx = await _run(
        _writer_step(allow_comment=True),
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_refuses_a_comment_opt_in_on_an_unknown_space():
    """The exception needs the space's features to exist. A space we hold
    no row for cannot have opted in, so the refusal stands."""
    step = make_check_space_writer(
        space_repo=_FakeSpaceRepo({}),
        remote_member_repo=_FakeRemoteMemberRepo(
            {("sp-1", "peer-x"): [_FakeSeat("subscriber")]}
        ),
    )
    ctx = await _run(
        step,
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_skips_reader_event_types():
    """A Follower is a real participant: roster, sync, key-exchange and
    report traffic passes untouched."""
    for event_type in (
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_REPORT,
        FederationEventType.SPACE_SYNC_BEGIN,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_INSTANCE_LEFT,
    ):
        ctx = await _run(_writer_step(), event_type, {"author": "u-follower"})
        assert ctx.early_response is None, event_type


async def test_space_writer_refuses_an_authorless_comment_under_the_opt_in():
    """No author field, nothing to bind the opt-in to — refuse."""
    ctx = await _run(
        _writer_step(allow_comment=True),
        FederationEventType.SPACE_COMMENT_CREATED,
        {"content": "nice"},
    )
    assert ctx.early_response == REFUSED


async def test_space_writer_keeps_the_refusal_when_the_features_read_fails():
    """The inverse of the seat lookup's fail-soft: the opt-in is an
    exception to a "no", so an unreadable flag leaves the "no"."""

    class _Exploding:
        async def get(self, *a, **kw):
            raise RuntimeError("db is having a day")

    step = make_check_space_writer(
        space_repo=_Exploding(),
        remote_member_repo=_FakeRemoteMemberRepo(
            {("sp-1", "peer-x"): [_FakeSeat("subscriber")]}
        ),
    )
    ctx = await _run(
        step,
        FederationEventType.SPACE_COMMENT_CREATED,
        {"author": "u-follower"},
    )
    assert ctx.early_response == REFUSED


async def test_post_decrypt_gates_treat_a_raising_step_as_a_refusal():
    """The ban check rejects by raising ``ValueError``, so the mesh seam
    has to read that as "drop", not let it escape into the unwrap."""

    async def _ban(ctx):
        raise ValueError("Instance 'peer-x' is banned from space 'sp-1'")

    allowed = await run_post_decrypt_gates(InboundContext(), steps=[_ban])
    assert allowed is False


async def test_post_decrypt_gates_stop_at_the_first_early_response():
    """A step that sets ``early_response`` (the writer gate's refusal)
    short-circuits the rest, exactly as the pipeline runner does."""
    calls: list[str] = []

    async def _refuse(ctx):
        calls.append("refuse")
        ctx.early_response = {"status": "ok", "dropped": "subscriber-write"}

    async def _never(ctx):  # pragma: no cover — must not run
        calls.append("never")

    allowed = await run_post_decrypt_gates(
        InboundContext(),
        steps=[_refuse, _never],
    )
    assert allowed is False
    assert calls == ["refuse"]


async def test_post_decrypt_gates_pass_an_event_no_step_objects_to():
    calls: list[str] = []

    async def _ok(ctx):
        calls.append("ran")

    allowed = await run_post_decrypt_gates(InboundContext(), steps=[_ok, _ok])
    assert allowed is True
    assert calls == ["ran", "ran"]


async def test_space_writer_fails_soft_on_a_seat_lookup_error():
    """A transient DB hiccup must not start dropping legitimate space
    content — the seat is durable, so the next envelope is gated again.
    The lookup RETURNING nothing is a different thing entirely and is
    never a pass for a household that has rows."""

    class _Exploding:
        async def list_for_instance(self, *a, **kw):
            raise RuntimeError("db is having a day")

    step = make_check_space_writer(
        space_repo=_FakeSpaceRepo({}),
        remote_member_repo=_Exploding(),
    )
    ctx = await _run(
        step,
        FederationEventType.SPACE_POST_CREATED,
        {"author": "u-follower"},
    )
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
