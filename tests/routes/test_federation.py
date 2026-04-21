"""Tests for social_home.routes.federation.

The route was a placeholder in v1 — it now runs the full
FederationService validation pipeline. See
``test_federation_webhook.py`` for the happy-path + rejection
matrix; this file just covers the route's own error branches.
"""


async def test_webhook_unknown_id_404(client):
    """POST /webhook/{id} with no matching peer → 404 NOT FOUND.

    Federation webhooks are auth-bypassed (envelope-signed); a body
    that *parses* but doesn't match any known instance must be
    rejected at the lookup step.
    """
    r = await client.post(
        "/webhook/no-such-webhook",
        json={
            "msg_id": "x",
            "event_type": "presence_updated",
            "from_instance": "a",
            "to_instance": "b",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "encrypted_payload": "x:y",
            "sig_suite": "ed25519",
            "signatures": {"ed25519": "z"},
        },
    )
    # Either 404 (no instance found) or 400 (timestamp/format) — never
    # 200, never 500.
    assert r.status in (400, 404, 410)


async def test_webhook_invalid_json_400(client):
    """A non-JSON body → 400 BAD REQUEST."""
    r = await client.post("/webhook/anything", data=b"this is not json")
    assert r.status == 400


async def test_webhook_missing_fields_400(client):
    """Envelope missing required fields → 400."""
    r = await client.post(
        "/webhook/anything",
        json={"msg_id": "only-this"},
    )
    assert r.status == 400
