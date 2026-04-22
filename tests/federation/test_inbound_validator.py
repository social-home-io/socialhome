"""Tests for the §24.11 inbound validation middleware chain.

Each step is tested in isolation so a failure pinpoints the exact
validation phase.
"""

from __future__ import annotations

from datetime import datetime, timezone

import orjson
import pytest

from socialhome.federation.inbound_validator import (
    InboundContext,
    InboundPipeline,
    make_ban_check,
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


# ─── Step 2: lookup_instance ─────────────────────────────────────────────


class _FakeInstance:
    remote_identity_pk = "aa" * 32
    key_remote_to_self = "enc"
    from_instance = "remote-iid"


async def test_lookup_instance_resolves():
    async def _lookup(repo, wh_id):
        return _FakeInstance() if wh_id == "wh-1" else None

    step = make_lookup_instance(repo=None, lookup_fn=_lookup)
    ctx = InboundContext(webhook_id="wh-1")
    await step(ctx)
    assert ctx.instance is not None


async def test_lookup_instance_rejects_unknown():
    async def _lookup(repo, wh_id):
        return None

    step = make_lookup_instance(repo=None, lookup_fn=_lookup)
    ctx = InboundContext(webhook_id="unknown")
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
