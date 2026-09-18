"""Tests for ``GfsEnvelopeSender`` — the §D2b relay leg (POST /gfs/envelope).

The sender is the production :class:`~socialhome.federation.invite_bootstrap
.RelayEnvelopeSender`: it hands a sealed invite blob to a paired connection
server. What matters here is what the server is allowed to learn (a recipient
and a ciphertext — nothing else), and that an un-upgraded server fails the
redeem with a sentence rather than a timeout.
"""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.capabilities_sig import sign_capabilities
from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import GfsConnection
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_envelope_sender import (
    EnvelopeRelayUnavailable,
    GfsEnvelopeSender,
    _normalize_base,
)


try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


GFS_INSTANCE_ID = "gfs-inst-1"
SEALED = {"kem_suite": "x25519", "eph_pk": "aa" * 32, "ciphertext": "deadbeef"}
ENVELOPE = {"to_instance": "b" * 32, "sealed": SEALED}


class _FakeRepo:
    """Minimal ``AbstractGfsConnectionRepo`` stand-in — list_active only."""

    def __init__(self, conns: list[GfsConnection]) -> None:
        self._conns = conns

    async def list_active(self) -> list[GfsConnection]:
        return list(self._conns)


def _conn(gfs_id: str, url: str, pub_hex: str, *, status: str = "active"):
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=GFS_INSTANCE_ID,
        display_name=f"Server {gfs_id}",
        public_key=pub_hex,
        inbox_url=url,
        status=status,
        paired_at="2026-01-01T00:00:00+00:00",
    )


class _FakeGfs:
    """A connection server implementing the two endpoints the sender uses."""

    def __init__(self, *, capabilities: dict | None = None, envelope_status=202):
        kp = generate_identity_keypair()
        self.seed = kp.private_key
        self.public_key_hex = kp.public_key.hex()
        self.gfs_instance_id = GFS_INSTANCE_ID
        self.capabilities = (
            capabilities if capabilities is not None else {"envelope_relay": True}
        )
        self.sign_caps = True
        self.envelope_status = envelope_status
        self.received: list[dict] = []
        self.info_calls = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/gfs/info", self._info)
        app.router.add_post("/gfs/envelope", self._envelope)
        return app

    async def _info(self, _request: web.Request) -> web.Response:
        self.info_calls += 1
        body: dict = {
            "instance_id": self.gfs_instance_id,
            "public_key": self.public_key_hex,
            "capabilities": self.capabilities,
        }
        if self.sign_caps:
            sig, suite = sign_capabilities(
                self.seed,
                self.gfs_instance_id,
                self.capabilities,
            )
            body["capabilities_sig"] = sig
            body["capabilities_sig_suite"] = suite
        return web.json_response(body)

    async def _envelope(self, request: web.Request) -> web.Response:
        self.received.append(await request.json())
        if self.envelope_status >= 300:
            return web.json_response({"error": "nope"}, status=self.envelope_status)
        return web.json_response({"status": "accepted"}, status=202)


@pytest.fixture
async def gfs():
    fake = _FakeGfs()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


def _wire(fake, http_session, *, conns=None):
    conn = _conn("gfs-1", fake.url, fake.public_key_hex)
    repo = _FakeRepo(conns if conns is not None else [conn])
    service = GfsConnectionService(repo, http_client=http_session)
    return GfsEnvelopeSender(gfs_service=service, gfs_repo=repo), conn


# ── URL matching ──────────────────────────────────────────────────────────


def test_normalize_base_ignores_case_slash_and_path():
    assert _normalize_base("https://GFS.example.org/") == "https://gfs.example.org"
    assert _normalize_base("https://gfs.example.org/gfs") == "https://gfs.example.org"


def test_normalize_base_keeps_the_port():
    assert _normalize_base("http://localhost:8124") == "http://localhost:8124"


# ── Happy path ────────────────────────────────────────────────────────────


async def test_posts_the_sealed_blob_and_reports_acceptance(gfs, http_session):
    sender, conn = _wire(gfs, http_session)
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
        gfs_url=conn.inbox_url,
    )
    assert ok is True
    assert gfs.received == [{"to_instance": "b" * 32, "sealed": SEALED}]


async def test_the_relay_sees_only_the_recipient_and_the_ciphertext(gfs, http_session):
    """No sender id, no space, no token — the identity-free outer shape."""
    sender, conn = _wire(gfs, http_session)
    await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        # A caller that grows extra fields must not leak them onward.
        envelope={
            **ENVELOPE,
            "from_instance": "sender-instance-id",
            "space_id": "sp-1",
        },
        gfs_url=conn.inbox_url,
    )
    body = gfs.received[0]
    assert set(body) == {"to_instance", "sealed"}
    raw = json.dumps(body)
    assert "sender-instance-id" not in raw
    assert "sp-1" not in raw


async def test_trailing_slash_on_the_invite_url_still_matches(gfs, http_session):
    sender, conn = _wire(gfs, http_session)
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
        gfs_url=conn.inbox_url.upper() + "/",
    )
    assert ok is True


async def test_capability_is_probed_once_and_cached(gfs, http_session):
    sender, conn = _wire(gfs, http_session)
    for _ in range(3):
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url=conn.inbox_url,
        )
    assert gfs.info_calls == 1
    assert len(gfs.received) == 3


# ── Capability gate ───────────────────────────────────────────────────────


async def test_refuses_a_server_without_the_capability(gfs, http_session):
    """The mutation guard: drop the capability check and this test fails."""
    gfs.capabilities = {"anonymous_publish": True}
    sender, conn = _wire(gfs, http_session)
    with pytest.raises(EnvelopeRelayUnavailable) as exc:
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url=conn.inbox_url,
        )
    assert "can't relay invites yet" in str(exc.value)
    assert gfs.received == []


async def test_refuses_an_unsigned_capability_block(gfs, http_session):
    """An on-path stripper must not be able to grant the capability."""
    gfs.sign_caps = False
    sender, conn = _wire(gfs, http_session)
    with pytest.raises(EnvelopeRelayUnavailable):
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url=conn.inbox_url,
        )
    assert gfs.received == []


async def test_refuses_a_capability_block_signed_by_the_wrong_key(gfs, http_session):
    """A block that fails verification against the pinned key is not a
    capability — it is tampering."""
    other = generate_identity_keypair()
    conn = _conn("gfs-1", gfs.url, other.public_key.hex())
    repo = _FakeRepo([conn])
    service = GfsConnectionService(repo, http_client=http_session)
    sender = GfsEnvelopeSender(gfs_service=service, gfs_repo=repo)
    with pytest.raises(EnvelopeRelayUnavailable):
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url=conn.inbox_url,
        )


# ── Resolution failures ───────────────────────────────────────────────────


async def test_refuses_when_not_connected_to_the_issuing_server(gfs, http_session):
    sender, _conn = _wire(gfs, http_session)
    with pytest.raises(EnvelopeRelayUnavailable) as exc:
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url="https://someone-elses-server.example",
        )
    assert "isn't connected" in str(exc.value)


async def test_refuses_with_no_connections_at_all(http_session):
    repo = _FakeRepo([])
    service = GfsConnectionService(repo, http_client=http_session)
    sender = GfsEnvelopeSender(gfs_service=service, gfs_repo=repo)
    with pytest.raises(EnvelopeRelayUnavailable) as exc:
        await sender.send_sealed_envelope(to_instance_id="b" * 32, envelope=ENVELOPE)
    assert "connection server" in str(exc.value)


async def test_skips_a_suspended_connection(gfs, http_session):
    suspended = _conn("gfs-1", gfs.url, gfs.public_key_hex, status="suspended")
    sender, _ = _wire(gfs, http_session, conns=[suspended])
    with pytest.raises(EnvelopeRelayUnavailable):
        await sender.send_sealed_envelope(
            to_instance_id="b" * 32,
            envelope=ENVELOPE,
            gfs_url=suspended.inbox_url,
        )


async def test_no_gfs_url_falls_back_to_the_single_connection(gfs, http_session):
    sender, _conn_ = _wire(gfs, http_session)
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
    )
    assert ok is True


# ── Transport failures ────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 413, 429, 500])
async def test_non_2xx_is_a_transport_failure_not_an_exception(
    gfs,
    http_session,
    status,
):
    gfs.envelope_status = status
    sender, conn = _wire(gfs, http_session)
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
        gfs_url=conn.inbox_url,
    )
    assert ok is False


async def test_unreachable_server_is_false_not_an_exception(gfs, http_session):
    """A capability we already verified, then a dead socket: the redeem
    reports a transport failure rather than blowing up."""
    conn = _conn("gfs-1", gfs.url, gfs.public_key_hex)
    repo = _FakeRepo([conn])
    service = GfsConnectionService(repo, http_client=http_session)
    sender = GfsEnvelopeSender(gfs_service=service, gfs_repo=repo)
    assert await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
        gfs_url=conn.inbox_url,
    )
    # Same connection id (so the verified capability stays cached), but the
    # URL now points at a closed port.
    dead = _conn("gfs-1", "http://127.0.0.1:1", gfs.public_key_hex)
    repo._conns = [dead]
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope=ENVELOPE,
        gfs_url=dead.inbox_url,
    )
    assert ok is False


async def test_an_envelope_with_no_seal_is_refused(gfs, http_session):
    sender, conn = _wire(gfs, http_session)
    ok = await sender.send_sealed_envelope(
        to_instance_id="b" * 32,
        envelope={"to_instance": "b" * 32},
        gfs_url=conn.inbox_url,
    )
    assert ok is False
    assert gfs.received == []
