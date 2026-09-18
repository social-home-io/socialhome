"""Tests for the §24.11 inbound validation middleware chain.

Each step is tested in isolation so a failure pinpoints the exact
validation phase.
"""

from __future__ import annotations

from datetime import datetime, timezone

import orjson
import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.inbound_validator import (
    InboundContext,
    InboundPipeline,
    make_ban_check,
    make_check_deprovisioned_author,
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
    from socialhome.domain.federation import FederationEvent, FederationEventType

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
    from socialhome.domain.federation import FederationEvent, FederationEventType

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
