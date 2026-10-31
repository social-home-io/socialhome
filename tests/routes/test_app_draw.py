"""Gift draw routes (v_78) — ``/api/apps/{app_id}/groups/{id}/draw…`` against
the real app: a draw among four people of one household (every step runs
on this household; no envelope leaves)."""

from __future__ import annotations

import asyncio

from socialhome.app_keys import app_draw_service_key

from .conftest import _auth
from .test_app_groups import APP, _install, _member, _own


async def _settle(client) -> None:
    """Let the household's own draw steps (tasks) run to the end."""
    draw = client.app[app_draw_service_key]
    for _ in range(400):
        pending = [t for t in draw._tasks if not t.done()]
        if not pending:
            await asyncio.sleep(0.01)
            if not [t for t in draw._tasks if not t.done()]:
                return
            continue
        await asyncio.wait(pending, timeout=1.0)
    raise AssertionError("the draw never settled")


async def _table(client) -> tuple[str, dict[str, str]]:
    db = client._db
    await _install(db)
    toks = {u: await _member(db, u) for u in ("bob", "carol", "dave")}
    own = await _own(client)
    targets = [
        {"instance_id": own, "user_ref": f"{u}-id", "is_local": True} for u in toks
    ]
    r = await client.post(
        f"/api/apps/{APP}/groups",
        json={"targets": targets},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["group"]["session_id"]
    for tok in toks.values():
        r = await client.post(f"/api/apps/{APP}/groups/{sid}/join", headers=_auth(tok))
        assert r.status == 200, await r.text()
    return sid, toks


async def test_a_draw_over_the_routes(client):
    sid, toks = await _table(client)
    base = f"/api/apps/{APP}/groups/{sid}/draw"
    host = client._tok

    r = await client.get(base, headers=_auth(host))
    assert r.status == 200
    draw = (await r.json())["draw"]
    assert (draw["state"], draw["is_host"]) == ("gathering", True)

    r = await client.patch(
        base,
        json={"name": "Familie Müller", "budget": "20 €", "day": "2026-12-24"},
        headers=_auth(host),
    )
    assert r.status == 200, await r.text()
    r = await client.patch(base, json={"name": "x"}, headers=_auth(toks["bob"]))
    assert (r.status, (await r.json())["error"]["code"]) == (403, "APP_DRAW_NOT_HOST")
    r = await client.put(
        f"{base}/wish", json={"text": "Lego"}, headers=_auth(toks["bob"])
    )
    assert r.status == 200 and (await r.json())["draw"]["wish"] == "Lego"
    r = await client.put(f"{base}/wish", json={"text": 7}, headers=_auth(toks["bob"]))
    assert r.status == 400
    r = await client.get(f"{base}/result", headers=_auth(toks["bob"]))
    assert (r.status, (await r.json())["error"]["code"]) == (404, "APP_DRAW_NO_RESULT")

    r = await client.post(f"{base}/start", json={}, headers=_auth(host))
    assert r.status == 200, await r.text()
    await _settle(client)

    names = set()
    for tok in (host, *toks.values()):
        r = await client.get(base, headers=_auth(tok))
        draw = (await r.json())["draw"]
        assert (draw["state"], draw["has_result"]) == ("ready", True)
        r = await client.get(f"{base}/result", headers=_auth(tok))
        assert r.status == 200, await r.text()
        result = (await r.json())["result"]
        assert (result["budget"], result["day"]) == ("20 €", "2026-12-24")
        names.add(result["name"])
    # Four people, four different recipients.
    assert len(names) == 4

    r = await client.post(f"{base}/start", json={"again": "yes"}, headers=_auth(host))
    assert r.status == 400
    r = await client.post(f"{base}/start", json={}, headers=_auth(host))
    assert (r.status, (await r.json())["error"]["code"]) == (409, "APP_DRAW_STATE")
    r = await client.post(f"{base}/start", json={"again": True}, headers=_auth(host))
    assert r.status == 200, await r.text()
    await _settle(client)
    r = await client.get(f"{base}/result", headers=_auth(host))
    assert (await r.json())["result"]["again"] == 1


async def test_a_stranger_sees_no_draw(client):
    sid, _toks = await _table(client)
    stranger = await _member(client._db, "eve")
    r = await client.get(f"/api/apps/{APP}/groups/{sid}/draw", headers=_auth(stranger))
    assert r.status == 404
    r = await client.get(
        f"/api/apps/{APP}/groups/{sid}/draw/result", headers=_auth(stranger)
    )
    assert r.status == 404


async def test_settings_out_of_shape(client):
    sid, _toks = await _table(client)
    base = f"/api/apps/{APP}/groups/{sid}/draw"
    for body in (
        {"day": "24.12.2026"},
        {"pairs": [[1, 1]]},
        {"pairs": [[1, 2], [2, 3]]},
        {"name": "x" * 81},
    ):
        r = await client.patch(base, json=body, headers=_auth(client._tok))
        assert r.status == 422, body
    r = await client.patch(base, json={"pairs": [[1, 77]]}, headers=_auth(client._tok))
    assert (r.status, (await r.json())["error"]["code"]) == (422, "APP_DRAW_PAIRS")
