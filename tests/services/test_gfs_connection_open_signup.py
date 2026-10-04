"""Household side of open sign-up: :meth:`GfsConnectionService.pair_open_signup`.

The onboarding "Connect to the GFS" step pairs with the configured default
GFS without a QR scan. The household:

1. fetches ``/gfs/info`` once and verifies the signed capability block
   against the key in that same response (the key it is about to pin, TOFU —
   exactly what the QR flow trusts);
2. proceeds only when that block proves ``open_signup``;
3. asks ``POST /gfs/signup-token`` for a single-use token (no body);
4. registers with exactly what QR pairing sends — nothing new.

Most tests run against the REAL GFS app (real SQLite, real aiohttp), so the
token, the signature and the auto-accept policy are checked by the code that
runs in production.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import CAPS_SIG_SUITE_ED25519, sign_capabilities
from socialhome.crypto import generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.global_server.app_keys import gfs_admin_repo_key, gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionService,
    GfsSignupError,
)

OWN = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_inbox_url": "https://alpha.example/federation/inbox",
    "own_display_name": "Alpha House",
    "own_keywrap_public_key_hex": "cc" * 32,
    "own_keywrap_sig": "sig-over-keywrap",
}

#: The fields QR pairing already sends to ``/gfs/register``. Open sign-up must
#: not add a single one.
QR_REGISTER_FIELDS = {
    "token",
    "instance_id",
    "public_key",
    "inbox_url",
    "display_name",
    "keywrap_public_key",
    "kem_suite",
    "keywrap_sig",
}


class _Recorder:
    """Delegating aiohttp session that records every request the household
    makes (method, path, JSON body)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._s = session
        self.calls: list[tuple[str, str, object]] = []

    def get(self, url, **kw):
        self.calls.append(("GET", str(url), kw.get("json")))
        return self._s.get(url, **kw)

    def post(self, url, **kw):
        self.calls.append(("POST", str(url), kw.get("json")))
        return self._s.post(url, **kw)


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "household.db", batch_timeout_ms=10)
    await db.startup()
    yield SqliteGfsConnectionRepo(db)
    await db.shutdown()


async def _gfs(tmp_dir, *, open_signup: bool):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir / "gfs"),
        instance_id="gfs-project",
        open_signup=open_signup,
    )
    (tmp_dir / "gfs").mkdir(exist_ok=True)
    return TestClient(TestServer(create_gfs_app(cfg)))


def _url(tc: TestClient) -> str:
    return str(tc.make_url("")).rstrip("/")


async def test_open_signup_pairs_with_the_real_gfs(tmp_dir, repo):
    async with await _gfs(tmp_dir, open_signup=True) as tc:
        rec = _Recorder(tc.session)
        svc = GfsConnectionService(repo, http_client=rec)  # type: ignore[arg-type]
        conn = await svc.pair_open_signup(_url(tc), **OWN)

        assert conn.status == "active"
        assert conn.inbox_url == _url(tc)
        info = await (await tc.get("/gfs/info")).json()
        assert conn.display_name == info["server_name"]
        # Pinned exactly as the QR flow pins it: the key from /gfs/info.
        assert conn.public_key == info["public_key"]
        assert conn.gfs_instance_id == "gfs-project"
        assert (await repo.get(conn.id)) is not None

        # ONE descriptor fetch, one token, one registration — the key that was
        # verified is the key that was pinned.
        paths = [(m, u.removeprefix(_url(tc))) for m, u, _ in rec.calls]
        assert paths == [
            ("GET", "/gfs/info"),
            ("POST", "/gfs/signup-token"),
            ("POST", "/gfs/register"),
        ]
        # The token request carries nothing about this household.
        assert rec.calls[1][2] is None
        # Registration sends what QR pairing sends, and nothing new.
        assert set(rec.calls[2][2]) == QR_REGISTER_FIELDS  # type: ignore[arg-type]

        registered = await tc.server.app[gfs_fed_repo_key].get_instance("alpha.home")
        assert registered is not None
        assert registered.inbox_url == OWN["own_inbox_url"]
        assert registered.display_name == "Alpha House"


async def test_open_signup_is_pending_when_the_gfs_wants_approval(tmp_dir, repo):
    async with await _gfs(tmp_dir, open_signup=True) as tc:
        await tc.server.app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
        svc = GfsConnectionService(repo, http_client=tc.session)
        conn = await svc.pair_open_signup(_url(tc), **OWN)
        assert conn.status == "pending"


async def test_open_signup_refused_when_the_gfs_does_not_offer_it(tmp_dir, repo):
    async with await _gfs(tmp_dir, open_signup=False) as tc:
        rec = _Recorder(tc.session)
        svc = GfsConnectionService(repo, http_client=rec)  # type: ignore[arg-type]
        with pytest.raises(GfsSignupError) as exc:
            await svc.pair_open_signup(_url(tc), **OWN)
        assert exc.value.reason == "closed"
        # Never asked for a token, never registered, nothing saved.
        assert [m for m, _u, _b in rec.calls] == ["GET"]
        assert await repo.list_all() == []
        assert (
            await tc.server.app[gfs_fed_repo_key].get_instance("alpha.home")
        ) is None


async def test_open_signup_refused_when_already_connected(tmp_dir, repo):
    async with await _gfs(tmp_dir, open_signup=True) as tc:
        svc = GfsConnectionService(repo, http_client=tc.session)
        await svc.pair_open_signup(_url(tc), **OWN)
        with pytest.raises(GfsSignupError) as exc:
            await svc.pair_open_signup(_url(tc) + "/", **OWN)
        assert exc.value.reason == "already_connected"


# ─── Stubbed GFS answers (the shapes the real one can't produce) ─────────


class _Resp:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self._body = body if body is not None else {}
        self.content = self

    async def read(self, n: int = -1) -> bytes:
        return b""

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Stub:
    """Answers by path; an ``Exception`` value is raised instead."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []

    def _answer(self, method: str, url: str, body: object):
        self.calls.append((method, url, body))
        path = url.split("://", 1)[1].split("/", 1)[1]
        ans = self.routes["/" + path]
        if isinstance(ans, Exception):
            raise ans
        return ans

    def get(self, url, **kw):
        return self._answer("GET", url, kw.get("json"))

    def post(self, url, **kw):
        return self._answer("POST", url, kw.get("json"))


GFS_URL = "https://gfs.example.org"
_KP = generate_identity_keypair()


def _info(caps: dict | None, *, sign_with=None, suite=CAPS_SIG_SUITE_ED25519) -> dict:
    body: dict = {
        "gfs_instance_id": "gfs-x",
        "public_key": _KP.public_key.hex(),
        "server_name": "GFS X",
    }
    if caps is not None:
        sig, _ = sign_capabilities((sign_with or _KP).private_key, "gfs-x", caps)
        body.update(
            capabilities=caps, capabilities_sig=sig, capabilities_sig_suite=suite
        )
    return body


def _svc(repo, stub) -> GfsConnectionService:
    return GfsConnectionService(repo, http_client=stub)  # type: ignore[arg-type]


async def _reason(repo, stub) -> str:
    with pytest.raises(GfsSignupError) as exc:
        await _svc(repo, stub).pair_open_signup(GFS_URL, **OWN)
    return exc.value.reason


async def test_unsigned_open_signup_flag_is_not_trusted(repo):
    info = _info(None)
    info["open_signup"] = True
    info["capabilities"] = {"open_signup": True}  # no signature
    stub = _Stub({"/gfs/info": _Resp(200, info)})
    assert await _reason(repo, stub) == "closed"
    assert len(stub.calls) == 1


async def test_capability_signed_by_another_key_is_not_trusted(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(
                200, _info({"open_signup": True}, sign_with=generate_identity_keypair())
            )
        }
    )
    assert await _reason(repo, stub) == "closed"
    assert len(stub.calls) == 1


async def test_unknown_capability_suite_is_not_trusted(repo):
    stub = _Stub({"/gfs/info": _Resp(200, _info({"open_signup": True}, suite="rot13"))})
    assert await _reason(repo, stub) == "closed"


async def test_signup_token_404_means_closed(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(404, {"error": "not_found"}),
        }
    )
    assert await _reason(repo, stub) == "closed"


async def test_signup_token_429_means_busy(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(429, {"error": "rate_limited"}),
        }
    )
    assert await _reason(repo, stub) == "busy"


async def test_signup_token_without_a_token_is_refused(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(200, {"token": ""}),
        }
    )
    assert await _reason(repo, stub) == "refused"


async def test_unreachable_gfs(repo):
    stub = _Stub({"/gfs/info": aiohttp.ClientConnectionError("down")})
    assert await _reason(repo, stub) == "unreachable"


async def test_gfs_info_server_error_is_unreachable(repo):
    stub = _Stub({"/gfs/info": _Resp(503)})
    assert await _reason(repo, stub) == "unreachable"


async def test_token_request_unreachable(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": aiohttp.ClientConnectionError("reset"),
        }
    )
    assert await _reason(repo, stub) == "unreachable"


async def test_registration_refused(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(200, {"token": "t", "expires_in": 600}),
            "/gfs/register": _Resp(401, {"error": "invalid_or_expired_token"}),
        }
    )
    assert await _reason(repo, stub) == "refused"
    assert await repo.list_all() == []


async def test_registration_server_error_is_unreachable(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(200, {"token": "t", "expires_in": 600}),
            "/gfs/register": _Resp(502),
        }
    )
    assert await _reason(repo, stub) == "unreachable"


@pytest.mark.parametrize(
    "url",
    [
        "http://gfs.example.org",
        "ftp://gfs.example.org",
        "",
        "https://u:p@gfs.example.org",
    ],
)
async def test_insecure_or_malformed_url_sends_nothing(repo, url):
    stub = _Stub({})
    with pytest.raises(GfsSignupError) as exc:
        await _svc(repo, stub).pair_open_signup(url, **OWN)
    assert exc.value.reason == "invalid_url"
    assert stub.calls == []


async def test_seeds_the_capability_cache_from_the_verified_block(repo):
    caps = {"anonymous_publish": True, "open_signup": True}
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info(caps)),
            "/gfs/signup-token": _Resp(200, {"token": "t", "expires_in": 600}),
            "/gfs/register": _Resp(200, {"status": "registered"}),
        }
    )
    svc = _svc(repo, stub)
    conn = await svc.pair_open_signup(GFS_URL, **OWN)
    assert await svc._anonymous_publish_supported(conn) is True
    assert len(stub.calls) == 3


@pytest.mark.parametrize(("status", "reason"), [(500, "unreachable"), (400, "refused")])
async def test_signup_token_other_statuses(repo, status, reason):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(status),
        }
    )
    assert await _reason(repo, stub) == reason


async def test_descriptor_that_is_not_an_object_is_unreachable(repo):
    stub = _Stub({"/gfs/info": _Resp(200, ["not", "an", "object"])})
    assert await _reason(repo, stub) == "unreachable"


async def test_capability_block_without_a_suite_is_not_trusted(repo):
    info = _info({"open_signup": True})
    info["capabilities_sig_suite"] = None
    stub = _Stub({"/gfs/info": _Resp(200, info)})
    assert await _reason(repo, stub) == "closed"


async def test_missing_own_identity_sends_nothing(repo):
    stub = _Stub({})
    with pytest.raises(GfsSignupError) as exc:
        await _svc(repo, stub).pair_open_signup(GFS_URL, **{**OWN, "own_inbox_url": ""})
    assert exc.value.reason == "refused"
    assert stub.calls == []


async def test_register_answer_that_is_not_an_object_counts_as_registered(repo):
    stub = _Stub(
        {
            "/gfs/info": _Resp(200, _info({"open_signup": True})),
            "/gfs/signup-token": _Resp(200, {"token": "t"}),
            "/gfs/register": _Resp(200, ["odd"]),
        }
    )
    conn = await _svc(repo, stub).pair_open_signup(GFS_URL, **OWN)
    assert conn.status == "active"
