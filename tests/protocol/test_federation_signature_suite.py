"""§25.8 wire-format guards — release-blocker protocol tests.

The federation envelope's ``sig_suite`` + ``signatures`` contract is
what makes the PQ migration path possible. These tests guard against
regressions in how the parser + verifier enforce it.

Marked ``security`` so CI can run them as a release-blocker subset.
"""

from __future__ import annotations

import orjson
import pytest

from social_home.federation.inbound_validator import (
    InboundContext,
    make_parse_json,
)


pytestmark = pytest.mark.security


def _loads(raw):
    return orjson.loads(raw)


async def test_parse_rejects_envelope_missing_sig_suite():
    """An envelope without ``sig_suite`` fails fast with a helpful message."""
    step = make_parse_json(loads=_loads)
    body = orjson.dumps(
        {
            "msg_id": "m1",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "signatures": {"ed25519": "z"},
            # sig_suite missing
        }
    )
    ctx = InboundContext(raw_body=body)
    with pytest.raises(ValueError, match="Missing required fields"):
        await step(ctx)


async def test_parse_rejects_envelope_missing_signatures():
    """Envelope with ``sig_suite`` but no ``signatures`` map is rejected."""
    step = make_parse_json(loads=_loads)
    body = orjson.dumps(
        {
            "msg_id": "m1",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "sig_suite": "ed25519",
        }
    )
    ctx = InboundContext(raw_body=body)
    with pytest.raises(ValueError, match="Missing required fields"):
        await step(ctx)


async def test_parse_rejects_signatures_not_a_dict():
    """The ``signatures`` field must be a JSON object."""
    step = make_parse_json(loads=_loads)
    body = orjson.dumps(
        {
            "msg_id": "m1",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "sig_suite": "ed25519",
            "signatures": "not-a-dict",
        }
    )
    ctx = InboundContext(raw_body=body)
    with pytest.raises(ValueError, match="signatures must be a dict"):
        await step(ctx)


async def test_parse_accepts_both_classical_and_hybrid_shapes():
    """Happy path: parse step accepts both classical and hybrid envelopes."""
    step = make_parse_json(loads=_loads)
    classical = orjson.dumps(
        {
            "msg_id": "m1",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "sig_suite": "ed25519",
            "signatures": {"ed25519": "z"},
        }
    )
    hybrid = orjson.dumps(
        {
            "msg_id": "m2",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "sig_suite": "ed25519+mldsa65",
            "signatures": {"ed25519": "z", "mldsa65": "q"},
        }
    )
    await step(InboundContext(raw_body=classical))
    await step(InboundContext(raw_body=hybrid))
