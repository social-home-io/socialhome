"""WS typing dispatch — covers the _on_text branch in routes/ws.py."""

from __future__ import annotations

import asyncio
import json


async def _ws_connect(client):
    return await client.ws_connect(f"/api/ws?token={client._tok}")


async def test_ws_typing_event_routes_to_typing_service(client):
    """Sending a typing JSON frame triggers TypingService."""
    from social_home.app_keys import typing_service_key

    db = client._db
    # Create a 1:1 conversation between admin and bob.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('bob', 'bob-id', 'Bob')",
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_by) VALUES('c-typing', 'dm', ?)",
        (client._uid,),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username) VALUES('c-typing', 'admin')",
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username) VALUES('c-typing', 'bob')",
    )

    typing = client.server.app[typing_service_key]
    # Connect as admin.
    ws = await _ws_connect(client)
    try:
        await ws.send_str(json.dumps({"type": "typing", "conversation_id": "c-typing"}))
        # Give the handler a moment to dispatch.
        for _ in range(20):
            if typing.is_typing("c-typing", client._uid):
                break
            await asyncio.sleep(0.05)
    finally:
        await ws.close()
    assert typing.is_typing("c-typing", client._uid) is True


async def test_ws_typing_missing_conv_id_silent(client):
    """Typing payload without conversation_id should not crash the loop."""
    ws = await _ws_connect(client)
    try:
        await ws.send_str(json.dumps({"type": "typing"}))
        # Send a follow-up ping to verify the connection survived.
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()


async def test_ws_unknown_command_ignored(client):
    """Unknown JSON command must not crash the loop."""
    ws = await _ws_connect(client)
    try:
        await ws.send_str(json.dumps({"type": "doSomethingWeird"}))
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()


async def test_ws_non_json_text_ignored(client):
    """Random text that isn't ping or JSON is ignored, connection stays up."""
    ws = await _ws_connect(client)
    try:
        await ws.send_str("garbage that is not json")
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()


async def test_ws_json_array_ignored(client):
    """JSON arrays (not dicts) are ignored too."""
    ws = await _ws_connect(client)
    try:
        await ws.send_str("[1,2,3]")
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()
