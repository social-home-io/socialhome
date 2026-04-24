"""Route tests for the HA integration bridge — /api/ha/integration/* (§7, §11)."""

from __future__ import annotations

from socialhome.app_keys import (
    db_key as _db_key,
    federation_repo_key,
    url_update_outbound_key,
)
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


def _peer(iid: str, local_inbox_id: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://peer/{iid}",
        local_inbox_id=local_inbox_id,
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def test_put_base_persists_and_reads_back(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://xx.ui.nabu.casa/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["base"] == "https://xx.ui.nabu.casa/api/social_home/inbox"
    assert body["changed"] is True
    # First push has no existing peers → 0 notified
    assert body["peers_notified"] == 0

    # Round-trip GET returns the same value.
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["base"] == "https://xx.ui.nabu.casa/api/social_home/inbox"


async def test_put_base_idempotent_when_unchanged(client):
    await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["changed"] is False
    assert body["peers_notified"] == 0


async def test_put_base_strips_trailing_slash(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://example/api/social_home/inbox/"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["base"] == "https://example/api/social_home/inbox"


async def test_put_base_rejects_missing(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_rejects_bad_scheme(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "ftp://nope.example/x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_rejects_empty_string(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_base_fans_out_url_updated_to_confirmed_peers(client):
    """Seed two confirmed peers, push a new base, expect fan-out."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-a", "wh-a"))
    await fed_repo.save_instance(_peer("peer-b", "wh-b"))

    # Swap the outbound service with a recorder so we don't need a real
    # transport. The wiring is tested by
    # ``test_outbound_service_wired_on_app``; here we only care that the
    # route calls publish() with the right base.
    captured: list[str] = []

    class _RecordingOutbound:
        async def publish(self, *, new_inbox_base_url: str) -> int:
            captured.append(new_inbox_base_url)
            return 2

    client.app[url_update_outbound_key] = _RecordingOutbound()

    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://new.example/api/social_home/inbox"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["peers_notified"] == 2
    assert captured == ["https://new.example/api/social_home/inbox"]


async def test_get_base_returns_null_when_unset(client):
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["base"] is None


async def test_get_base_requires_admin(client):
    """Non-admin user cannot read the base."""
    # Demote the admin in the seeded test client.
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/ha/integration/federation-base",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_outbound_service_wired_on_app(client):
    """The UrlUpdateOutbound service is registered under url_update_outbound_key."""
    assert client.app.get(url_update_outbound_key) is not None
