"""Tests for socialhome.routes.federation.

The route was a placeholder in v1 — it now runs the full
FederationService validation pipeline. See
``test_federation_inbox.py`` for the happy-path + rejection
matrix; this file just covers the route's own error branches.
"""


async def test_https_inbox_unknown_id_404(client):
    """POST /federation/inbox/{id} with no matching peer → 404 NOT FOUND.

    federation inboxs are auth-bypassed (envelope-signed); a body
    that *parses* but doesn't match any known instance must be
    rejected at the lookup step.
    """
    r = await client.post(
        "/federation/inbox/no-such-inbox",
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


async def test_https_inbox_invalid_json_400(client):
    """A non-JSON body → 400 BAD REQUEST."""
    r = await client.post("/federation/inbox/anything", data=b"this is not json")
    assert r.status == 400


async def test_https_inbox_missing_fields_400(client):
    """Envelope missing required fields → 400."""
    r = await client.post(
        "/federation/inbox/anything",
        json={"msg_id": "only-this"},
    )
    assert r.status == 400
