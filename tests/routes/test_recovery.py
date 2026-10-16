"""HTTP tests for POST /api/recovery-kit — admin-only build + download.

The route seals the household trust layer behind a user passphrase and
returns the ``.shrk`` file as a binary download. The passphrase travels in
the request body (never the URL/query) so it can't leak into access logs.
"""

from __future__ import annotations

from socialhome.auth import sha256_token_hash
from socialhome.services.recovery_crypto import unseal_kit

from .conftest import _auth


async def test_admin_builds_and_downloads_kit(client):
    r = await client.post(
        "/api/recovery-kit",
        json={"passphrase": "correct horse staple"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert "attachment" in r.headers["Content-Disposition"]
    assert "socialhome-recovery-kit.shrk" in r.headers["Content-Disposition"]
    body = await r.read()
    assert body
    header, _payload = unseal_kit(body, "correct horse staple")
    assert isinstance(header["instance_id"], str)
    assert header["instance_id"]


async def test_short_passphrase_rejected(client):
    r = await client.post(
        "/api/recovery-kit",
        json={"passphrase": "short"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_missing_passphrase_rejected(client):
    r = await client.post(
        "/api/recovery-kit",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_unauthenticated_rejected(client):
    r = await client.post(
        "/api/recovery-kit",
        json={"passphrase": "correct horse staple"},
    )
    assert r.status in (401, 403)


async def test_non_admin_forbidden(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/recovery-kit",
        json={"passphrase": "correct horse staple"},
        headers=_auth(raw),
    )
    assert r.status == 403
