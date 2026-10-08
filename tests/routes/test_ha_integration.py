"""Route tests for the HA integration bridge — /api/ha/integration/* (§7, §11)."""

from __future__ import annotations


import logging

from socialhome.app_keys import (
    db_key as _db_key,
    federation_repo_key,
    platform_adapter_key,
    url_update_outbound_key,
)
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.platform.federation_base import MANUAL_BASE_KEY
from socialhome.platform.ha.adapter import HaAdapter

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


async def test_put_base_rejects_url_peers_would_refuse(client):
    """Same household-address rules peers apply when they receive it."""
    for bad in ("https://user:pw@ha.example", "https://", "https://ha.example/a b"):
        r = await client.put(
            "/api/ha/integration/federation-base",
            json={"base": bad},
            headers=_auth(client._tok),
        )
        assert r.status == 422, bad


async def test_put_base_rejects_empty_string(client):
    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


class _RecordingOutbound:
    """Stands in for ``UrlUpdateOutbound`` — the route's contract is the
    base it publishes. The fan-out itself is
    ``tests/services/test_url_update_outbound.py``."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    async def publish(self, *, new_inbox_base_url: str) -> int:
        self.captured.append(new_inbox_base_url)
        return 2


def _ha_mode(client, tmp_path) -> _RecordingOutbound:
    """Swap in the real HA adapter (reading this app's ``instance_config``)
    and a recording outbound."""
    adapter = HaAdapter(ha_url="http://ha.local", ha_token="t", data_dir=str(tmp_path))
    adapter._db = client.app[_db_key]
    client.app[platform_adapter_key] = adapter
    outbound = _RecordingOutbound()
    client.app[url_update_outbound_key] = outbound
    return outbound


async def test_put_base_fans_out_the_full_inbox_base(client, tmp_path):
    """Peers get the adapter's EFFECTIVE base — the pushed URL plus the
    HA-hosted forwarder path — never the bare pushed URL."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-a", "wh-a"))
    await fed_repo.save_instance(_peer("peer-b", "wh-b"))
    outbound = _ha_mode(client, tmp_path)

    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://new.example"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["peers_notified"] == 2
    assert outbound.captured == ["https://new.example/api/socialhome/inbox"]

    # Unchanged push: nothing published again.
    await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://new.example"},
        headers=_auth(client._tok),
    )
    assert len(outbound.captured) == 1


async def test_put_base_publishes_nothing_while_an_admin_override_wins(
    client, tmp_path
):
    """An admin-set base wins over the pushed one: a new push does not
    change where peers reach us, so nobody is told a wrong address."""
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?,?)",
        (MANUAL_BASE_KEY, "https://direct.example"),
    )
    outbound = _ha_mode(client, tmp_path)

    r = await client.put(
        "/api/ha/integration/federation-base",
        json={"base": "https://pushed.example"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["changed"] is True
    assert body["peers_notified"] == 0
    assert outbound.captured == []


async def test_put_base_logs_a_warning_when_the_base_cannot_resolve(client, caplog):
    class _Broken:
        async def get_federation_base(self):
            raise RuntimeError("db gone")

    client.app[platform_adapter_key] = _Broken()
    outbound = _RecordingOutbound()
    client.app[url_update_outbound_key] = outbound
    with caplog.at_level(logging.WARNING, logger="socialhome.routes.ha_integration"):
        r = await client.put(
            "/api/ha/integration/federation-base",
            json={"base": "https://pushed.example"},
            headers=_auth(client._tok),
        )
    assert r.status == 200
    assert outbound.captured == []
    assert "could not resolve federation base" in caplog.text


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


async def test_put_base_when_the_base_fails_to_resolve_after_the_write(client, caplog):
    """The push is stored, but the effective base can't be read back:
    WARNING, no fan-out (never publish a guess), and a normal answer."""

    class _FailsAfterWrite:
        calls = 0

        async def get_federation_base(self):
            type(self).calls += 1
            if type(self).calls == 1:
                return "https://old.example/api/socialhome/inbox"
            raise RuntimeError("db gone")

    client.app[platform_adapter_key] = _FailsAfterWrite()
    outbound = _RecordingOutbound()
    client.app[url_update_outbound_key] = outbound
    with caplog.at_level(logging.WARNING, logger="socialhome.routes.ha_integration"):
        r = await client.put(
            "/api/ha/integration/federation-base",
            json={"base": "https://pushed.example"},
            headers=_auth(client._tok),
        )
    assert r.status == 200
    body = await r.json()
    assert body == {
        "ok": True,
        "base": "https://pushed.example",
        "changed": True,
        "peers_notified": 0,
    }
    assert outbound.captured == []
    assert _FailsAfterWrite.calls == 2
    assert "could not resolve federation base" in caplog.text
    r = await client.get(
        "/api/ha/integration/federation-base", headers=_auth(client._tok)
    )
    assert (await r.json())["base"] == "https://pushed.example"
