"""HTTP/WebSocket tests for /api/ws."""

from __future__ import annotations

import asyncio
import json

import pytest


async def test_ws_requires_auth(client):
    """Unauthenticated WebSocket handshake must be rejected."""
    r = await client.get("/api/ws")
    # Without auth → 401 (the handler returns json_response before upgrade).
    assert r.status == 401


async def test_ws_upgrade_succeeds_with_token(client):
    """Authenticated WS upgrade and registration."""
    ws = await client.ws_connect(f"/api/ws?token={client._tok}")
    try:
        # Send ping → expect pong.
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()


async def test_ws_receives_realtime_post_event(client):
    """Creating a post should fan out a post.created frame to the user's WS."""
    from social_home.app_keys import ws_manager_key

    ws = await client.ws_connect(f"/api/ws?token={client._tok}")
    try:
        # Wait until the manager actually has the registration.
        manager = client.server.app[ws_manager_key]
        for _ in range(20):
            if manager.connection_count() >= 1:
                break
            await asyncio.sleep(0.05)
        # Create a post via REST.
        r = await client.post(
            "/api/feed/posts",
            json={"type": "text", "content": "live update"},
            headers={"Authorization": f"Bearer {client._tok}"},
        )
        assert r.status == 201
        # Receive the WS frame (skip any non-text noise).
        for _ in range(5):
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            if msg.data and msg.data != "pong":
                payload = json.loads(msg.data)
                if payload.get("type") == "post.created":
                    assert payload["post"]["content"] == "live update"
                    return
        pytest.fail("did not receive post.created frame")
    finally:
        await ws.close()
