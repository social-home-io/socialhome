"""Owner-minted invite links: mint / revoke routes, the public ``/join`` page,
and the privacy rules both halves exist to keep.

Covers :mod:`socialhome.global_server.invites` and its aiohttp surface
:mod:`socialhome.global_server.routes.invites`.

The security-marked tests pin the three properties the feature is built on:
the connection server never learns who redeemed (nothing is written on a
fetch), only the owning household can mint or revoke, and a signature for one
invite action can never be replayed as the other.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.invites import (
    INVITE_BLOB_MAX_BYTES,
    INVITE_CODE_PREFIX,
    INVITE_MAX_TTL_SECONDS,
    INVITE_MINT_MAX_PER_MINUTE,
)
from socialhome.global_server.server import create_gfs_app

#: A plausible invite blob: base64url text, opaque to this server.
BLOB = "eyJ0b2tlbiI6ICJhYmMxMjMifQ"

OWNER = "owner.home"
SPACE = "sp-invite"


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


@pytest.fixture
async def owner(client):
    """Register the owning household + an actively-listed space.

    Returns the household's Ed25519 seed so a test can sign as the owner.
    """
    kp = generate_identity_keypair()
    fed_repo = client._app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=OWNER,
            display_name="Owner",
            public_key=kp.public_key.hex(),
            inbox_url="https://owner.example/inbox",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id=SPACE,
            owning_instance=OWNER,
            name="Invite Me",
            accent_color="#112233",
            status="active",
        )
    )
    return kp.private_key


def _now_iso(*, minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _sign(payload: dict, seed: bytes) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return b64url_encode(sign_ed25519(seed, canonical))


def _mint_body(
    seed: bytes,
    *,
    space_id: str = SPACE,
    owning_instance: str = OWNER,
    blob: object = BLOB,
    ttl_seconds: int = 3600,
    ts: str | None = None,
) -> dict:
    stamp = ts if ts is not None else _now_iso()
    return {
        "owning_instance": owning_instance,
        "blob": blob,
        "expires_at": int(time.time()) + ttl_seconds,
        "ts": stamp,
        "signature": _sign(
            {
                "action": "mint_invite",
                "owning_instance": owning_instance,
                "space_id": space_id,
                "ts": stamp,
            },
            seed,
        ),
    }


def _revoke_body(
    seed: bytes,
    gfs_token: str,
    *,
    space_id: str = SPACE,
    owning_instance: str = OWNER,
) -> dict:
    stamp = _now_iso()
    return {
        "owning_instance": owning_instance,
        "ts": stamp,
        "signature": _sign(
            {
                "action": "revoke_invite",
                "gfs_token": gfs_token,
                "owning_instance": owning_instance,
                "space_id": space_id,
                "ts": stamp,
            },
            seed,
        ),
    }


async def _mint(client, seed, **kw) -> tuple[int, dict]:
    resp = await client.post(
        f"/gfs/spaces/{kw.pop('space_id', SPACE)}/invite",
        json=_mint_body(seed, **kw),
    )
    try:
        body = await resp.json()
    except Exception:
        body = {}
    return resp.status, body


async def _rows(app) -> list[dict]:
    """Every invite row, as plain dicts — the DB-write tripwire."""
    rows = await app[gfs_db_key].fetchall(
        "SELECT gfs_token, space_id, source_instance_id, blob, uses, max_uses, "
        "created_at, expires_at FROM gfs_invite_tokens ORDER BY gfs_token",
    )
    return [dict(r) for r in rows]


# ─── Mint ────────────────────────────────────────────────────────────


async def test_mint_creates_row_and_returns_url(client, owner):
    status, body = await _mint(client, owner)
    assert status == 201, body
    token = body["gfs_token"]
    assert token
    assert body["url"] == f"http://gfs.test/join/{token}"

    rows = await _rows(client._app)
    assert len(rows) == 1
    assert rows[0]["space_id"] == SPACE
    assert rows[0]["source_instance_id"] == OWNER
    assert rows[0]["blob"] == BLOB


@pytest.mark.security
async def test_mint_rejects_forged_signature(client, owner):
    other = generate_identity_keypair().private_key
    status, body = await _mint(client, other)
    assert status == 403, body


@pytest.mark.security
async def test_mint_rejects_stale_timestamp(client, owner):
    status, body = await _mint(client, owner, ts=_now_iso(minutes=-10))
    assert status == 403, body
    assert await _rows(client._app) == []


@pytest.mark.security
async def test_mint_rejects_non_owner(client, owner):
    """A registered household that is NOT the space's owner cannot park an
    invite on it — space ids travel inside discovery links, so a signature
    alone must never be enough."""
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="stranger.home",
            display_name="Stranger",
            public_key=kp.public_key.hex(),
            inbox_url="https://stranger.example/inbox",
            status="active",
        )
    )
    status, body = await _mint(
        client,
        kp.private_key,
        owning_instance="stranger.home",
    )
    assert status == 403, body
    assert await _rows(client._app) == []


@pytest.mark.security
async def test_mint_rejects_withdrawn_space(client, owner):
    await client._app[gfs_fed_repo_key].set_space_withdrawn(SPACE, True)
    status, body = await _mint(client, owner)
    assert status == 403, body
    assert await _rows(client._app) == []


@pytest.mark.security
async def test_mint_rejects_inactive_space(client, owner):
    fed_repo = client._app[gfs_fed_repo_key]
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id=SPACE,
            owning_instance=OWNER,
            name="Invite Me",
            status="banned",
        )
    )
    status, body = await _mint(client, owner)
    assert status == 403, body


async def test_mint_rejects_oversize_blob(client, owner):
    status, body = await _mint(client, owner, blob="A" * (INVITE_BLOB_MAX_BYTES + 1))
    assert status == 400, body
    assert await _rows(client._app) == []


async def test_mint_rejects_non_base64url_blob(client, owner):
    status, body = await _mint(client, owner, blob="not a blob!! {}")
    assert status == 400, body


async def test_mint_rejects_non_string_blob(client, owner):
    status, body = await _mint(client, owner, blob={"token": "x"})
    assert status == 400, body


async def test_mint_rejects_ttl_past_the_cap(client, owner):
    status, body = await _mint(client, owner, ttl_seconds=INVITE_MAX_TTL_SECONDS + 60)
    assert status == 400, body


async def test_mint_rejects_expiry_in_the_past(client, owner):
    status, body = await _mint(client, owner, ttl_seconds=-60)
    assert status == 400, body


async def test_mint_requires_every_field(client, owner):
    body = _mint_body(owner)
    del body["expires_at"]
    resp = await client.post(f"/gfs/spaces/{SPACE}/invite", json=body)
    assert resp.status == 400


async def test_mint_rate_limited_per_instance(client, owner):
    """The mint is signed, so the limiter keys on the accountable household
    rather than on an address it could rotate."""
    for _ in range(INVITE_MINT_MAX_PER_MINUTE):
        status, body = await _mint(client, owner)
        assert status == 201, body
    status, body = await _mint(client, owner)
    assert status == 429, body


# ─── Revoke ──────────────────────────────────────────────────────────


async def test_revoke_removes_the_row_and_404s_the_page(client, owner):
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    assert (await client.get(f"/join/{token}")).status == 200

    resp = await client.delete(
        f"/gfs/spaces/{SPACE}/invite/{token}",
        json=_revoke_body(owner, token),
    )
    assert resp.status == 204
    assert await _rows(client._app) == []
    assert (await client.get(f"/join/{token}")).status == 404


async def test_revoke_is_idempotent(client, owner):
    resp = await client.delete(
        f"/gfs/spaces/{SPACE}/invite/no-such-token",
        json=_revoke_body(owner, "no-such-token"),
    )
    assert resp.status == 204


async def test_revoke_accepts_post_for_body_stripping_proxies(client, owner):
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    resp = await client.post(
        f"/gfs/spaces/{SPACE}/invite/{token}",
        json=_revoke_body(owner, token),
    )
    assert resp.status == 204


@pytest.mark.security
async def test_revoke_rejects_non_owner(client, owner):
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="stranger.home",
            display_name="Stranger",
            public_key=kp.public_key.hex(),
            inbox_url="https://stranger.example/inbox",
            status="active",
        )
    )
    resp = await client.delete(
        f"/gfs/spaces/{SPACE}/invite/{token}",
        json=_revoke_body(kp.private_key, token, owning_instance="stranger.home"),
    )
    assert resp.status == 403
    assert len(await _rows(client._app)) == 1


@pytest.mark.security
async def test_mint_signature_cannot_be_replayed_as_revoke(client, owner):
    """The ``action`` discriminator lives INSIDE the signed bytes, so a
    captured mint signature is not a revoke signature."""
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    mint_body = _mint_body(owner)
    resp = await client.delete(
        f"/gfs/spaces/{SPACE}/invite/{token}",
        json={
            "owning_instance": OWNER,
            "ts": mint_body["ts"],
            "signature": mint_body["signature"],
        },
    )
    assert resp.status == 403
    assert len(await _rows(client._app)) == 1


@pytest.mark.security
async def test_revoke_signature_cannot_be_replayed_as_mint(client, owner):
    revoke = _revoke_body(owner, "some-token")
    resp = await client.post(
        f"/gfs/spaces/{SPACE}/invite",
        json={
            "owning_instance": OWNER,
            "blob": BLOB,
            "expires_at": int(time.time()) + 3600,
            "ts": revoke["ts"],
            "signature": revoke["signature"],
        },
    )
    assert resp.status == 403
    assert await _rows(client._app) == []


@pytest.mark.security
async def test_revoke_signature_is_bound_to_its_token(client, owner):
    """A revoke signed for one token can't be redirected at another."""
    _, first = await _mint(client, owner)
    _, second = await _mint(client, owner)
    resp = await client.delete(
        f"/gfs/spaces/{SPACE}/invite/{second['gfs_token']}",
        json=_revoke_body(owner, first["gfs_token"]),
    )
    assert resp.status == 403
    assert len(await _rows(client._app)) == 2


@pytest.mark.security
async def test_withdrawing_the_space_cascades_to_its_invites(client, owner):
    """A withdrawn listing must not keep a working public invite page."""
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    stamp = _now_iso()
    resp = await client.post(
        f"/gfs/spaces/{SPACE}/unpublish",
        json={
            "owning_instance": OWNER,
            "ts": stamp,
            "signature": _sign(
                {
                    "action": "unpublish",
                    "owning_instance": OWNER,
                    "space_id": SPACE,
                    "ts": stamp,
                },
                owner,
            ),
        },
    )
    assert resp.status == 200
    assert await _rows(client._app) == []
    assert (await client.get(f"/join/{token}")).status == 404


async def test_deleting_the_space_cascades_to_its_invites(client, owner):
    """The FK's ON DELETE CASCADE covers a hard space delete too."""
    await _mint(client, owner)
    await client._app[gfs_db_key].enqueue(
        "DELETE FROM global_spaces WHERE space_id=?",
        (SPACE,),
    )
    assert await _rows(client._app) == []


# ─── The public /join page ───────────────────────────────────────────


async def test_join_page_renders_the_exact_invite_code(client, owner):
    _, minted = await _mint(client, owner)
    resp = await client.get(f"/join/{minted['gfs_token']}")
    assert resp.status == 200
    text = await resp.text()
    assert f"{INVITE_CODE_PREFIX}{BLOB}" in text
    assert "socialhome://invite#eyJ0b2tlbiI6ICJhYmMxMjMifQ" in text
    assert "Invite Me" in text
    # QR of the same string + the copy affordance the landing page uses.
    assert 'src="data:image/png;base64,' in text
    assert 'id="invite-code"' in text
    assert 'id="copy-invite-btn"' in text


async def test_join_page_404s_for_unknown_token(client, owner):
    """The dead-link page is a page, not a bare sentence on a blank
    document. Whoever lands here followed a link somebody sent them and
    has no idea what went wrong — so it says where they are, what
    happened, and the one thing that helps (ask for a fresh link). It
    still echoes NO token: the URL is in their address bar, but putting
    it in the body invites copy-pasting a dead credential around."""
    resp = await client.get("/join/no-such-token")
    assert resp.status == 404
    text = await resp.text()
    assert "expired or was revoked" in text
    # The same styled shell as the 200 page…
    assert "<h1>" in text
    assert "Manrope" in text
    # …named, so the visitor knows which server told them this…
    assert "My Global Server" in text
    # …with a next step.
    assert "fresh link" in text
    # …and never the token.
    assert "no-such-token" not in text


async def test_join_page_404s_after_expiry(client, owner):
    _, minted = await _mint(client, owner, ttl_seconds=3600)
    token = minted["gfs_token"]
    # Age the row past its expiry without touching the code path under test.
    await client._app[gfs_db_key].enqueue(
        "UPDATE gfs_invite_tokens SET expires_at=? WHERE gfs_token=?",
        (int(time.time()) - 1, token),
    )
    assert (await client.get(f"/join/{token}")).status == 404


@pytest.mark.security
async def test_join_page_writes_nothing(client, owner):
    """PRIVACY: a fetch must leave no trace — no use counter, no row.

    This is the test that fails if anyone ever "improves" the page by
    counting redemptions.
    """
    _, minted = await _mint(client, owner)
    before = await _rows(client._app)
    for _ in range(3):
        assert (await client.get(f"/join/{minted['gfs_token']}")).status == 200
    assert await _rows(client._app) == before
    assert before[0]["uses"] == 0


@pytest.mark.security
async def test_join_page_never_logs_the_token(client, owner, caplog):
    _, minted = await _mint(client, owner)
    token = minted["gfs_token"]
    with caplog.at_level(logging.DEBUG, logger="socialhome.global_server"):
        assert (await client.get(f"/join/{token}")).status == 200
    records = [
        r for r in caplog.records if r.name.startswith("socialhome.global_server")
    ]
    for record in records:
        assert token not in record.getMessage()
        assert BLOB not in record.getMessage()


@pytest.mark.security
async def test_mint_never_logs_the_token_or_blob(client, owner, caplog):
    with caplog.at_level(logging.DEBUG, logger="socialhome.global_server"):
        _, minted = await _mint(client, owner)
    for record in caplog.records:
        if not record.name.startswith("socialhome.global_server"):
            continue
        assert minted["gfs_token"] not in record.getMessage()
        assert BLOB not in record.getMessage()


async def test_join_page_rate_limited_per_ip(client, owner):
    """``/join/`` rides the public-listing limiter — it is the page an
    attacker would hammer to walk the token space."""
    last = None
    for _ in range(40):
        last = await client.get("/join/whatever")
        if last.status == 429:
            break
    assert last is not None and last.status == 429


# ─── Capability + dead-scheme sweep ──────────────────────────────────


async def test_gfs_info_advertises_invite_links_in_the_signed_block(client):
    resp = await client.get("/gfs/info")
    assert resp.status == 200
    body = await resp.json()
    assert body["capabilities"]["invite_links"] is True
    # Inside the SIGNED block, not a bare top-level flag an on-path stripper
    # could remove without breaking the signature.
    assert body["capabilities_sig"]
    assert body["capabilities_sig_suite"]


async def test_no_page_renders_the_dead_sh_scheme(client, owner):
    """``sh://`` was never registered by any client. No public page may
    still offer it as a call to action."""
    _, minted = await _mint(client, owner)
    for path in ("/", f"/spaces/{SPACE}", f"/join/{minted['gfs_token']}"):
        resp = await client.get(path)
        assert resp.status == 200, path
        assert "sh://" not in await resp.text(), path
