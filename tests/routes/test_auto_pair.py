"""Tests for transitive auto-pair routes (§11 simple pairing)."""

from __future__ import annotations

from .conftest import _auth


async def test_auto_pair_via_requires_fields(client):
    r = await client.post(
        "/api/pairing/auto-pair-via",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_auto_pair_via_unknown_peer(client):
    r = await client.post(
        "/api/pairing/auto-pair-via",
        json={
            "via_instance_id": "nonexistent",
            "target_instance_id": "alsonope",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_auto_pair_requests_empty_inbox(client):
    r = await client.get(
        "/api/pairing/auto-pair-requests",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_auto_pair_approve_missing_returns_404(client):
    r = await client.post(
        "/api/pairing/auto-pair-requests/missing-id/approve",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_auto_pair_decline_missing_returns_404(client):
    r = await client.post(
        "/api/pairing/auto-pair-requests/missing-id/decline",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 404
