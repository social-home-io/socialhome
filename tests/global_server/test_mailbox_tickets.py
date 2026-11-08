"""GFS mailbox registration tickets (Privacy Pass, RFC 9578 type 0x0002):
issuance to registered households, single-use redemption, key epochs, the
cluster-wide spent set, and the privacy properties. Real GFS app, real
SQLite, real aiohttp client."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome import privacy_pass as pp
from socialhome.canonical_json import canonical_bytes
from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_mailbox import MAILBOX_TICKET_CHALLENGE
from socialhome.federation.gfs_mailbox import (
    build_register_body,
    build_ticket_request,
    derive_mailbox_keys,
    register_mailbox,
)
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_fed_repo_key,
    gfs_mailbox_tickets_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.mailbox_tickets import (
    DAY_SECONDS,
    MailboxTicketService,
)
from socialhome.global_server.mailboxes import MailboxRefused
from socialhome.global_server.repositories import SqliteGfsMailboxTicketRepo
from socialhome.global_server.server import create_gfs_app
from tests.federation.test_gfs_mailbox import (
    HOME_ID,
    HOME_SEED,
    ROOT,
    _issuer,
    _ticket_cache,
)
from tests.global_server.test_mailbox_cluster import nodes  # noqa: F401
from tests.global_server.test_mailboxes import Box, _cfg

pytestmark = pytest.mark.security


async def _home(app, *, status: str = "active") -> None:
    await app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=HOME_ID,
            display_name="Home",
            public_key=ed25519_public_key(HOME_SEED).hex(),
            status=status,
        )
    )


@pytest.fixture
async def gfs(tmp_dir):
    app = create_gfs_app(_cfg(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        await _home(app)
        yield tc


async def _issue(tc, count: int = 1, **override):
    """POST a signed ticket request; ``(response, body, pending)``."""
    url = str(tc.make_url("")).rstrip("/")
    async with aiohttp.ClientSession() as session:
        issuer = await _issuer(session, url)
    body, pending = build_ticket_request(
        issuer, count, instance_id=HOME_ID, signing_key=HOME_SEED
    )
    if override:
        body = {k: v for k, v in body.items() if k != "signature"} | override
        body["signature"] = b64url_encode(
            sign_ed25519(
                HOME_SEED,
                canonical_bytes({k: v for k, v in body.items() if k != "signature"}),
            )
        )
    resp = await tc.post("/gfs/mailbox/tickets", json=body)
    return resp, body, (issuer, pending)


async def _tokens(tc, count: int = 1) -> list[str]:
    resp, _body, (issuer, pending) = await _issue(tc, count)
    assert resp.status == 200
    data = await resp.json()
    return [
        b64url_encode(
            pp.finalize_token(issuer.public_key, p, b64url_decode(r)).encode()
        )
        for p, r in zip(pending, data["responses"], strict=False)
    ]


async def _register(tc, token: str | None, box: Box | None = None):
    body = (box or Box()).register_body()
    if token is not None:
        body |= {"ticket_suite": pp.TICKET_SUITE_PP_BLINDRSA, "ticket": token}
    return await tc.post("/gfs/mailbox/register", json=body)


# ── Issuance ────────────────────────────────────────────────────────────


async def test_a_registered_household_gets_blind_signed_tickets(gfs):
    tokens = await _tokens(gfs, 3)
    assert len(tokens) == 3
    for token in tokens:
        assert (await _register(gfs, token)).status == 200


async def test_the_capability_is_signed_and_carries_the_suite(gfs):
    info = await (await gfs.get("/gfs/info")).json()
    cap = info["capabilities"]["mailbox_tickets"]
    assert cap["suite"] == pp.TICKET_SUITE_PP_BLINDRSA
    assert (cap["per_day"], cap["epoch_days"]) == (50, 30)
    assert set(cap) == {"suite", "issuer_key_id", "public_key", "per_day", "epoch_days"}


@pytest.mark.parametrize(
    ("override", "status", "error"),
    [
        ({"action": "mint_invite"}, 400, "invalid_body"),
        ({"suite": "privacypass-blindrsa4096"}, 400, "unsupported_ticket_suite"),
        ({"requests": []}, 400, "invalid_requests"),
        ({"requests": ["AAAA"]}, 400, "invalid_requests"),
        (
            {"requests": [b64url_encode(b"\x00\x02\x00" + b"\xff" * 256)]},
            400,
            "invalid_requests",
        ),
        ({"requests": ["A"] * 65}, 400, "invalid_requests"),
        ({"issuer_key_id": b64url_encode(bytes(32))}, 409, "stale_issuer_key"),
        ({"ts": "2020-01-01T00:00:00+00:00"}, 403, "unauthorized"),
    ],
)
async def test_a_bad_ticket_request_is_refused(gfs, override, status, error):
    resp, _body, _state = await _issue(gfs, 1, **override)
    assert resp.status == status
    assert (await resp.json())["error"] == error


async def test_an_unsigned_or_forged_request_is_refused(gfs):
    _resp, body, _state = await _issue(gfs, 1)
    body["signature"] = b64url_encode(bytes(64))
    resp = await gfs.post("/gfs/mailbox/tickets", json=body)
    assert (resp.status, (await resp.json())["error"]) == (403, "unauthorized")
    resp = await gfs.post("/gfs/mailbox/tickets", json={"instance_id": HOME_ID})
    assert resp.status == 400


async def test_a_pending_household_gets_no_tickets(tmp_dir):
    app = create_gfs_app(_cfg(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        await _home(app, status="pending")
        resp, _b, _s = await _issue(tc, 1)
        assert (resp.status, (await resp.json())["error"]) == (403, "unauthorized")


async def test_the_daily_batch_is_counted_per_household(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(mailbox_tickets_per_day=5))
    )
    async with TestClient(TestServer(app)) as tc:
        await _home(app)
        assert len(await _tokens(tc, 3)) == 3
        assert len(await _tokens(tc, 3)) == 2  # what is left today
        resp, _b, _s = await _issue(tc, 1)
        assert resp.status == 429
        assert (await resp.json())["error"] == "ticket_quota"
        assert resp.headers["Retry-After"] == "3600"


async def test_concurrent_requests_cannot_overshoot_the_batch(gfs_db):
    repo = SqliteGfsMailboxTicketRepo(gfs_db)
    granted = await asyncio.gather(
        *(repo.take_issuance("h" * 32, day=0, want=2, per_day=5) for _ in range(6))
    )
    assert sum(granted) == 5
    # A new day starts from zero; the row is reused (one per household).
    assert await repo.take_issuance("h" * 32, day=DAY_SECONDS, want=9, per_day=5) == 5


async def test_tickets_off_drops_route_and_capability(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(enabled=False))
    )
    async with TestClient(TestServer(app)) as tc:
        assert (await tc.post("/gfs/mailbox/tickets", json={})).status == 404
        info = await (await tc.get("/gfs/info")).json()
        assert "mailbox_tickets" not in info["capabilities"]


# ── Redemption ──────────────────────────────────────────────────────────


async def test_a_double_spend_is_refused(gfs):
    (token,) = await _tokens(gfs, 1)
    assert (await _register(gfs, token)).status == 200
    again = await _register(gfs, token)
    assert (again.status, (await again.json())["error"]) == (409, "ticket_spent")


async def test_a_refused_spend_gives_the_address_count_back(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(mailbox_max_per_ip=2))
    )
    async with TestClient(TestServer(app)) as tc:
        await _home(app)
        (token,) = await _tokens(tc, 1)
        assert (await _register(tc, token)).status == 200
        for _ in range(3):  # spent → refused, and nothing counted
            assert (await _register(tc, token)).status == 409
        (fresh,) = await _tokens(tc, 1)
        assert (await _register(tc, fresh)).status == 200


async def test_the_address_backstop_still_binds_and_keeps_the_ticket(tmp_dir):
    """Tickets replace the server-wide cap, not the per-address one: past
    it a ticketed register is refused 429 and the ticket is NOT spent."""
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(mailbox_max_per_ip=1))
    )
    async with TestClient(TestServer(app)) as tc:
        await _home(app)
        first, second = await _tokens(tc, 2)
        assert (await _register(tc, first)).status == 200
        capped = await _register(tc, second)
        assert (capped.status, (await capped.json())["error"]) == (429, "mailbox_cap")
        spent = await app[gfs_db_key].fetchall("SELECT nonce FROM gfs_spent_tickets")
        assert len(spent) == 1  # only the first ticket


async def test_a_forged_token_is_refused(gfs):
    (token,) = await _tokens(gfs, 1)
    raw = bytearray(b64url_decode(token))
    raw[-1] ^= 1  # flip one authenticator bit
    resp = await _register(gfs, b64url_encode(bytes(raw)))
    assert (resp.status, (await resp.json())["error"]) == (403, "invalid_ticket")
    # A nonce of a valid token with someone else's authenticator: refused too.
    raw = bytearray(b64url_decode(token))
    raw[2:34] = bytes(32)
    resp = await _register(gfs, b64url_encode(bytes(raw)))
    assert (resp.status, (await resp.json())["error"]) == (403, "invalid_ticket")


async def test_a_token_for_another_challenge_is_refused(gfs):
    live = await gfs.server.app[gfs_mailbox_tickets_key].current_key()
    other = pp.token_challenge(b"gfs.social-home.io", origin_info=b"elsewhere")
    request, pending = pp.token_request(live.public, other)
    token = pp.finalize_token(
        live.public,
        pending,
        pp.blind_sign(live.private, pp.parse_token_request(request)[1]),
    )
    resp = await _register(gfs, b64url_encode(token.encode()))
    assert (resp.status, (await resp.json())["error"]) == (403, "invalid_ticket")


async def test_a_token_signed_under_another_gfs_key_is_refused(gfs, tmp_path):
    other = create_gfs_app(_cfg(tmp_path))
    async with TestClient(TestServer(other)) as tc:
        await _home(other)
        (foreign,) = await _tokens(tc, 1)
    resp = await _register(gfs, foreign)
    assert (resp.status, (await resp.json())["error"]) == (403, "stale_ticket_key")


@pytest.mark.parametrize(
    ("suite", "ticket", "status", "error"),
    [
        (None, None, 403, "ticket_required"),
        ("privacypass-blindrsa4096", "AAAA", 400, "unsupported_ticket_suite"),
        (pp.TICKET_SUITE_PP_BLINDRSA, "AAAA", 403, "invalid_ticket"),
        (pp.TICKET_SUITE_PP_BLINDRSA, 7, 403, "invalid_ticket"),
    ],
)
async def test_a_malformed_ticket_is_refused(gfs, suite, ticket, status, error):
    body = Box().register_body()
    if suite is not None:
        body["ticket_suite"] = suite
        body["ticket"] = ticket
    resp = await gfs.post("/gfs/mailbox/register", json=body)
    assert (resp.status, (await resp.json())["error"]) == (status, error)


# ── Key epochs ──────────────────────────────────────────────────────────


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _local_token(live) -> bytes:
    request, pending = pp.token_request(live.public, MAILBOX_TICKET_CHALLENGE)
    blinded = pp.parse_token_request(request)[1]
    return pp.finalize_token(
        live.public, pending, pp.blind_sign(live.private, blinded)
    ).encode()


async def test_a_ticket_outlives_its_epoch_by_one(gfs_db):
    epoch_s = 30 * DAY_SECONDS
    clock = _Clock(float(epoch_s * 100 + 5))
    repo = SqliteGfsMailboxTicketRepo(gfs_db)
    svc = MailboxTicketService(
        repo=repo,
        config=EnvelopeMailboxConfig(),
        federation=None,
        clock=clock,  # type: ignore[arg-type]
    )
    first = await svc.current_key()
    token = b64url_encode(_local_token(first))
    clock.t += epoch_s  # next epoch: a new key signs, the old one redeems
    second = await svc.current_key()
    assert second.row.key_id != first.row.key_id
    nonce, expires_at = await svc.check(pp.TICKET_SUITE_PP_BLINDRSA, token)
    assert expires_at == first.row.created_at + 2 * epoch_s
    clock.t += epoch_s + 1  # two epochs on: retired
    svc._keys_at = -1e9
    with pytest.raises(MailboxRefused) as err:
        await svc.check(pp.TICKET_SUITE_PP_BLINDRSA, token)
    assert err.value.error == "stale_ticket_key"
    # Spent nonces and keys past their life are pruned.
    await svc.spend(nonce, expires_at)
    await svc.prune(now=int(clock.t) + DAY_SECONDS + 1)
    db = gfs_db
    assert await db.fetchall("SELECT * FROM gfs_spent_tickets") == []
    keys = await db.fetchall("SELECT epoch FROM gfs_ticket_issuer_keys")
    assert [r["epoch"] for r in keys] == [second.row.epoch]


async def test_two_services_on_one_db_share_the_epoch_key(gfs_db):
    clock = _Clock(1_000_000.0)
    make = lambda: MailboxTicketService(
        repo=SqliteGfsMailboxTicketRepo(gfs_db),
        config=EnvelopeMailboxConfig(),
        federation=None,  # type: ignore[arg-type]
        clock=clock,
    )
    a, b = make(), make()
    ka, kb = await asyncio.gather(a.current_key(), b.current_key())
    assert ka.row.key_id == kb.row.key_id
    assert "private" not in repr(ka.row).lower()


# ── Privacy ─────────────────────────────────────────────────────────────


async def _all_cells(db) -> list[object]:
    tables = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    cells: list[object] = []
    for t in tables:
        for row in await db.fetchall(f'SELECT * FROM "{t["name"]}"'):
            cells.extend(dict(row).values())
    return cells


async def test_the_gfs_keeps_no_issuance_to_redemption_mapping(gfs):
    resp, body, (issuer, pending) = await _issue(gfs, 2)
    data = await resp.json()
    tokens = [
        pp.finalize_token(
            issuer.public_key,
            p,
            b64url_decode(r),
        )
        for p, r in zip(pending, data["responses"], strict=True)
    ]
    for t in tokens:
        assert (await _register(gfs, b64url_encode(t.encode()))).status == 200
    db = gfs.server.app[gfs_db_key]
    # Issuance: one row per household, a day and a count — nothing per token.
    rows = [dict(r) for r in await db.fetchall("SELECT * FROM gfs_ticket_issuance")]
    assert rows == [{"instance_id": HOME_ID, "day": rows[0]["day"], "issued": 2}]
    # Redemption: the nonce and when it may be forgotten — nobody's id.
    spent = [dict(r) for r in await db.fetchall("SELECT * FROM gfs_spent_tickets")]
    assert {frozenset(r) for r in spent} == {frozenset({"nonce", "expires_at"})}
    # Neither the blinded messages nor the blind signatures are kept anywhere.
    blinded = [r for r in body["requests"]] + list(data["responses"])
    raw = [b64url_decode(x) for x in blinded]
    for cell in await _all_cells(db):
        if isinstance(cell, bytes | str):
            text = cell if isinstance(cell, str) else cell.hex()
            for b64, r in zip(blinded, raw, strict=True):
                assert b64 not in text and r.hex() not in text
                assert r[3:].hex() not in text


async def test_no_token_or_request_reaches_the_logs(gfs, caplog):
    caplog.set_level(logging.DEBUG)
    resp, body, (issuer, pending) = await _issue(gfs, 1)
    data = await resp.json()
    (token,) = [
        b64url_encode(
            pp.finalize_token(issuer.public_key, p, b64url_decode(r)).encode()
        )
        for p, r in zip(pending, data["responses"], strict=True)
    ]
    await _register(gfs, token)
    await _register(gfs, token)  # the refused double spend, too
    text = caplog.text
    for secret in [token, *body["requests"], *data["responses"]]:
        assert secret not in text
        assert secret[:40] not in text
    assert HOME_ID not in text


# ── Cluster ─────────────────────────────────────────────────────────────


async def test_a_ticket_issued_on_node_a_is_redeemed_on_node_b_once(nodes):  # noqa: F811
    """Keys and the spent set live in the shared DB: a ticket fetched from
    node A registers on node B, and the same ticket on node A is refused."""
    a, b, session = nodes
    url_a = str(a.make_url("")).rstrip("/")
    url_b = str(b.make_url("")).rstrip("/")
    tickets = await _ticket_cache(a.app, session, url_a, batch=1)
    await tickets.refill()
    (ticket,) = tickets._tickets
    keys = derive_mailbox_keys(ROOT, "gfs-shared", "space-x", 0)
    assert await register_mailbox(session, url_b, keys, "gfs-shared", tickets=tickets)
    other = derive_mailbox_keys(ROOT, "gfs-shared", "space-y", 0)
    async with session.post(
        url_a + "/gfs/mailbox/register",
        json=build_register_body(other, "gfs-shared", ticket=ticket),
    ) as resp:
        assert (resp.status, (await resp.json())["error"]) == (409, "ticket_spent")


async def test_a_spent_nonce_outlives_its_key_by_a_day(gfs_db):
    """Prune keeps a day of margin past the key's last accepted second, so a
    node whose clock runs ahead never forgets a nonce a sibling would still
    accept."""
    clock = _Clock(float(30 * DAY_SECONDS * 50))
    svc = MailboxTicketService(
        repo=SqliteGfsMailboxTicketRepo(gfs_db),
        config=EnvelopeMailboxConfig(),
        federation=None,  # type: ignore[arg-type]
        clock=clock,
    )
    live = await svc.current_key()
    nonce, expires_at = await svc.check(
        pp.TICKET_SUITE_PP_BLINDRSA, b64url_encode(_local_token(live))
    )
    await svc.spend(nonce, expires_at)
    for now in (expires_at, expires_at + DAY_SECONDS - 1):
        await svc.prune(now=now)
        assert len(await gfs_db.fetchall("SELECT 1 FROM gfs_spent_tickets")) == 1
    await svc.prune(now=expires_at + DAY_SECONDS + 1)
    assert await gfs_db.fetchall("SELECT 1 FROM gfs_spent_tickets") == []
