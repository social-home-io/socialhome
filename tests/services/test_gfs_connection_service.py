"""Tests for GfsConnectionService + SqliteGfsConnectionRepo."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
import pytest

from socialhome.domain.errors import SpaceNotPublishableError
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
)
from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
    verify_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import gfs_fed_repo_key, gfs_ws_registry_key
from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    sign_capabilities,
)
from socialhome.global_server.domain import ClientInstance
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services import gfs_connection_service
from socialhome.services.gfs_connection_service import (
    GFS_INFO_NEGATIVE_TTL_S,
    MAX_REMOTE_DETAIL_CHARS,
    GfsConnectionError,
    GfsConnectionService,
    GfsSignupError,
    _remote_detail,
)


# ─── Helpers ────────────────────────────────────────────────────────────


class _Content:
    """Minimal stand-in for ``aiohttp``'s streaming body reader."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        # Consumes, like aiohttp's StreamReader (the reader loops to EOF).
        size = len(self._raw) if n < 0 else n
        out, self._raw = self._raw[:size], self._raw[size:]
        return out


class _StubResp:
    __slots__ = ("status", "_body", "_text", "content", "content_length", "headers")

    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body or {}
        self._text = text
        self.content = _Content(text.encode())
        self.content_length = len(text.encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _StubSession:
    """Stub aiohttp session.

    Per-method overrides land in ``method_responses`` (keyed by ``"GET"``
    / ``"POST"`` / etc.); fall back to the default ``status`` / ``body``
    when the method has no override.
    """

    __slots__ = ("_status", "_body", "method_responses", "calls", "_last_body")

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        method_responses: dict[str, tuple[int, dict]] | None = None,
    ):
        self._status = status
        self._body = body or {}
        self.method_responses = method_responses or {}
        self.calls: list[tuple[str, str]] = []
        # Last JSON body the caller passed via ``json=`` — exposed for
        # tests that need to assert what got serialized on the wire
        # (e.g. publish-body signature verification).
        self._last_body: dict | None = None

    def _resp(self, method: str) -> _StubResp:
        override = self.method_responses.get(method)
        if override is not None:
            status, body = override
            return _StubResp(status, body)
        return _StubResp(self._status, self._body)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._resp("GET")

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "json" in kw:
            self._last_body = kw["json"]
        return self._resp("POST")

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return self._resp("DELETE")


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    repo = SqliteGfsConnectionRepo(db)
    yield db, repo
    await db.shutdown()


#: The GFS identity keypair the anonymous-publish tests pretend a household
#: pinned at pair time. The capability block on ``/gfs/info`` is signed with
#: it, and the household trusts the capability only through that signature.
_GFS_KP = generate_identity_keypair()


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
    public_key: str = "pubkey-hex",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key=public_key,
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


async def _publishable_svc(
    env, session, gfs_id: str, *, space_id: str, space_type: str = "global"
):
    """Build a GfsConnectionService with the publish context wired + a real
    local space row so ``publish_space`` can compose a signed body.

    The GFS now mandates a signature on every publish, so the service must
    always have a signing identity + a space to describe; this helper sets
    both up for the publish-path tests.
    """
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn(gfs_id))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id=space_id,
            name="Publishable",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType(space_type),
            join_mode=JoinMode.OPEN,
        )
    )
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc


def _signing_svc(repo, session) -> tuple[GfsConnectionService, bytes]:
    """A service with only the signing identity wired (no space repo).

    ``unpublish_space`` signs its body with the household identity key, so
    every unpublish test needs an identity; the space metadata is irrelevant
    there (the GFS looks the row up by id). Returns the service plus the
    public key so a test can verify the signature it produced.
    """
    kp = generate_identity_keypair()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, kp.public_key


# ─── Repo tests ─────────────────────────────────────────────────────────


async def test_save_and_get(env):
    _, repo = env
    conn = _make_conn("gfs-1")
    await repo.save(conn)
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.id == "gfs-1"
    assert got.gfs_instance_id == "inst-gfs-1"


async def test_get_nonexistent_returns_none(env):
    _, repo = env
    assert await repo.get("nope") is None


async def test_list_active_filters_status(env):
    _, repo = env
    await repo.save(_make_conn("a1", status="active"))
    await repo.save(_make_conn("a2", status="suspended"))
    await repo.save(_make_conn("a3", status="pending"))
    active = await repo.list_active()
    assert len(active) == 1
    assert active[0].id == "a1"


# ── Service: refresh_connection_metadata ───────────────────────────────


async def test_refresh_connection_metadata_updates_changed_name(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={"server_name": "New Name"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "New Name"
    # Hit GET /gfs/info on the connection's inbox URL.
    assert session.calls and session.calls[0][0] == "GET"
    assert session.calls[0][1].endswith("/gfs/info")


async def test_refresh_connection_metadata_noop_when_unchanged(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    # The fake GFS returns the SAME name the row already holds.
    session = _StubSession(status=200, body={"server_name": "GFS gfs-1"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "GFS gfs-1"


async def test_refresh_connection_metadata_swallows_transport_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))

    class _BoomSession:
        def get(self, url, **kw):
            raise aiohttp.ClientError("boom")

    svc = GfsConnectionService(repo, http_client=_BoomSession())  # type: ignore[arg-type]
    # Best-effort: never raises.
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    # Name untouched on error.
    assert got.display_name == "GFS gfs-1"


async def test_refresh_connection_metadata_noop_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200, body={"server_name": "Whatever"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    # No connection row → returns without touching the network.
    await svc.refresh_connection_metadata("nope")
    assert session.calls == []


async def test_refresh_connection_metadata_ignores_missing_server_name(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "GFS gfs-1"


# ── Service: report_fraud ──────────────────────────────────────────────


async def test_report_fraud_signs_and_posts(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={"status": "recorded"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-1",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes="bad",
        reporter_instance_id="me.home",
        reporter_user_id="u-1",
        signing_key=b"\x01" * 32,
    )
    assert ok is True
    assert session.calls and session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/gfs/report")


async def test_report_fraud_returns_false_on_http_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-2", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(
        status=500,
        body={},
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-2",
        target_type="instance",
        target_id="peer.home",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x02" * 32,
    )
    assert ok is False


async def test_report_fraud_returns_false_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "nope",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x03" * 32,
    )
    assert ok is False
    assert session.calls == []


async def test_disconnect_deletes_connection(env):
    _, repo = env
    await repo.save(_make_conn("rm-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    await svc.disconnect("rm-1")
    assert await repo.get("rm-1") is None


async def test_disconnect_unknown_raises(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    with pytest.raises(GfsConnectionError):
        await svc.disconnect("nope")


async def test_publish_space_records_local(env):
    session = _StubSession(status=200)
    svc = await _publishable_svc(env, session, "pub-1", space_id="space-x")
    pub = await svc.publish_space("space-x", "pub-1")
    # Post to publish endpoint happened.
    assert session.calls
    assert session.calls[0][0] == "POST"
    assert "/gfs/spaces/space-x/publish" in session.calls[0][1]
    # Returns the persisted publication; default status when the GFS
    # body carries none.
    assert pub.space_id == "space-x"
    assert pub.gfs_connection_id == "pub-1"
    assert pub.status == "active"


async def test_publish_space_returns_gfs_status(env):
    """The status the GFS returns in the publish body flows back into
    the returned publication AND the persisted row (e.g. ``pending``
    when the GFS requires moderator approval)."""
    session = _StubSession(status=200, body={"status": "pending"})
    svc = await _publishable_svc(env, session, "pub-2", space_id="space-p")
    _, repo = env
    pub = await svc.publish_space("space-p", "pub-2")
    assert pub.status == "pending"
    rows = await repo.list_publications_for_space("space-p")
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_publish_space_raises_on_non_2xx_and_skips_local_row(env):
    """A rejecting GFS surfaces as ``GfsConnectionError`` and does NOT
    write a local publication row — a failed publish is no longer
    indistinguishable from success."""
    session = _StubSession(status=500)
    svc = await _publishable_svc(env, session, "pub-3", space_id="space-e")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="HTTP 500"):
        await svc.publish_space("space-e", "pub-3")
    assert await repo.list_publications_for_space("space-e") == []


async def test_publish_space_raises_on_network_error_and_skips_local_row(env):
    class _RaisingSession:
        def post(self, *a, **kw):
            import aiohttp

            raise aiohttp.ClientError("unreachable")

    svc = await _publishable_svc(env, _RaisingSession(), "pub-4", space_id="space-n")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.publish_space("space-n", "pub-4")
    assert await repo.list_publications_for_space("space-n") == []


class _TimeoutSession:
    """Stub session whose request raises a *bare* ``asyncio.TimeoutError``.

    Mirrors what aiohttp's ``ClientTimeout(total=...)`` raises when the
    request hangs — NOT an ``aiohttp.ClientError`` subclass. The transport
    guard must still map it to ``GfsConnectionError``.
    """

    def post(self, *a, **kw):
        raise asyncio.TimeoutError

    def delete(self, *a, **kw):
        raise asyncio.TimeoutError


async def test_publish_space_maps_timeout_to_gfs_error(env):
    """A hung GFS raises a bare ``asyncio.TimeoutError`` from the total
    timeout; ``publish_space`` must map it to ``GfsConnectionError`` (→ 422)
    and leave no local row, not leak the raw timeout (→ 500)."""
    svc = await _publishable_svc(env, _TimeoutSession(), "pub-to", space_id="space-to")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.publish_space("space-to", "pub-to")
    assert await repo.list_publications_for_space("space-to") == []


async def test_unpublish_space_maps_timeout_to_gfs_error(env):
    """Symmetric with publish: a bare ``asyncio.TimeoutError`` on unpublish
    maps to ``GfsConnectionError`` and keeps the local row."""
    _, repo = env
    await repo.save(_make_conn("up-to"))
    await repo.publish_space("space-uto", "up-to")
    svc, _pk = _signing_svc(repo, _TimeoutSession())
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.unpublish_space("space-uto", "up-to")
    rows = await repo.list_publications_for_space("space-uto")
    assert len(rows) == 1


async def test_publish_space_to_all_skips_timed_out_gfs(env):
    """One GFS that times out must NOT abort the whole fan-out — the bare
    ``asyncio.TimeoutError`` is caught as ``GfsConnectionError`` and skipped,
    so the healthy GFS is still published to."""

    class _MixedSession:
        """``post`` times out for the slow GFS, succeeds for the healthy one."""

        def post(self, url, **kw):
            if "slow.example" in url:
                raise asyncio.TimeoutError
            return _StubResp(200, {"status": "active"})

    svc = await _publishable_svc(env, _MixedSession(), "ok-gfs", space_id="space-fan")
    _, repo = env
    # Re-home the first conn's URL + add a second (slow) GFS.
    await repo.save(_make_conn("ok-gfs", inbox_url="https://ok.example"))
    await repo.save(_make_conn("slow-gfs", inbox_url="https://slow.example"))
    published = await svc.publish_space_to_all("space-fan")
    assert published == 1
    # Only the healthy GFS got a local publication row.
    rows = await repo.list_publications_for_space("space-fan")
    assert len(rows) == 1
    assert rows[0].gfs_connection_id == "ok-gfs"


async def test_publish_space_handles_non_dict_json_body(env):
    """A GFS that returns valid-but-non-object JSON (e.g. ``[]``) on a 200
    must not crash the status parse — coerce to ``{}`` and default to
    ``active``."""

    class _NonDictResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return []  # valid JSON, but not an object

        async def text(self):
            return ""

    class _NonDictSession:
        def post(self, *a, **kw):
            return _NonDictResp()

    svc = await _publishable_svc(env, _NonDictSession(), "pub-nd", space_id="space-nd")
    _, repo = env
    pub = await svc.publish_space("space-nd", "pub-nd")
    assert pub.status == "active"
    rows = await repo.list_publications_for_space("space-nd")
    assert len(rows) == 1
    assert rows[0].status == "active"


async def test_unpublish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("up-1"))
    session = _StubSession(status=200)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("space-y", "up-1")
    # POST, not DELETE: the GFS accepts both, and some proxies strip a
    # DELETE body — which would turn the now-signed unpublish into a
    # permanent 400.
    assert session.calls and session.calls[0][0] == "POST"


async def test_unpublish_space_404_treated_as_success(env):
    """The space is already absent on the GFS — idempotent delete, so
    a 404 is success and the local row is removed without raising."""
    _, repo = env
    await repo.save(_make_conn("up-404"))
    await repo.publish_space("space-z", "up-404")
    session = _StubSession(status=404)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("space-z", "up-404")
    assert await repo.list_publications_for_space("space-z") == []


async def test_unpublish_space_raises_on_500_and_keeps_local_row(env):
    _, repo = env
    await repo.save(_make_conn("up-500"))
    await repo.publish_space("space-k", "up-500")
    session = _StubSession(status=500)
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError, match="HTTP 500"):
        await svc.unpublish_space("space-k", "up-500")
    rows = await repo.list_publications_for_space("space-k")
    assert len(rows) == 1


async def test_update_status(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.update_status("gfs-1", "suspended")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.status == "suspended"


async def test_delete_removes_connection_and_publications(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.delete("gfs-1")
    assert await repo.get("gfs-1") is None
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_and_unpublish_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"

    await repo.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_space_idempotent(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1


async def test_list_gfs_for_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-2")
    conns = await repo.list_gfs_for_space("sp-1")
    assert {c.id for c in conns} == {"gfs-1", "gfs-2"}


async def test_count_published_spaces(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    assert await repo.count_published_spaces("gfs-1") == 0
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-2", "gfs-1")
    assert await repo.count_published_spaces("gfs-1") == 2


# ─── Service tests ──────────────────────────────────────────────────────


_OWN_PAIR_KW = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_display_name": "Alpha House",
}


async def test_pair_success(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote-gfs-id",
                    "public_key": "bb" * 32,
                    "server_name": "Test GFS",
                    "base_url": "https://gfs.example.com",
                },
            ),
            "POST": (200, {"status": "registered", "instance_id": "alpha.home"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok-123"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "active"
    assert conn.gfs_instance_id == "remote-gfs-id"
    # The display name comes from the GFS's own ``server_name`` (its
    # branding), not anything the SH made up locally.
    assert conn.display_name == "Test GFS"
    # The pinned public key is the GFS's, fetched via ``/gfs/info`` —
    # this is the trust anchor for every subsequent relay.
    assert conn.public_key == "bb" * 32
    # Two calls: GET /gfs/info, then POST /gfs/register.
    assert session.calls == [
        ("GET", "https://gfs.example.com/gfs/info"),
        ("POST", "https://gfs.example.com/gfs/register"),
    ]
    # Saved to repo.
    saved = await repo.get(conn.id)
    assert saved is not None


async def test_pair_ships_keywrap_pubkey_and_kem_suite(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote-gfs-id",
                    "public_key": "bb" * 32,
                    "server_name": "Test GFS",
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok"},
        **_OWN_PAIR_KW,
        own_keywrap_public_key_hex="ee" * 32,
        own_keywrap_sig="c2ln",
    )
    body = session._last_body
    assert body is not None
    assert body["keywrap_public_key"] == "ee" * 32
    assert body["kem_suite"] == "x25519"
    assert body["keywrap_sig"] == "c2ln"


async def test_pair_omits_keywrap_when_unavailable(env):
    """A caller that passes no key-wrap pubkey → empty field, no kem_suite
    claim (an older/unprovisioned HFS can't seal yet — graceful)."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "bb" * 32,
                    "server_name": "GFS",
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    await svc.pair(
        {"gfs_url": "https://gfs", "token": "tok"},
        **_OWN_PAIR_KW,
    )
    body = session._last_body
    assert body is not None
    assert body.get("keywrap_public_key", "") == ""
    assert body.get("kem_suite", "") == ""


async def test_pair_pending_status(env):
    """A GFS with auto-accept disabled returns ``status="pending"``;
    the local connection lands as ``pending`` (not ``active``) so the
    UI can render the "awaiting GFS admin review" state."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (200, {"status": "pending"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs", "token": "tok"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "pending"


async def test_pair_missing_qr_fields(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="QR payload"):
        await svc.pair({"gfs_url": "https://x.com"}, **_OWN_PAIR_KW)


async def test_pair_missing_own_identity(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="own_instance_id"):
        await svc.pair(
            {"gfs_url": "https://x.com", "token": "tok"},
            own_instance_id="",
            own_public_key_hex="ab",
        )


@pytest.mark.parametrize(
    "gfs_url",
    [
        "http://gfs.example.com",
        "HTTP://Gfs.Example.Com",
        "http://8.8.8.8:9000",
        "http://gfs.example.com:8080/base",
        "ftp://gfs.example.com",
        "gfs.example.com",
    ],
)
async def test_pair_rejects_a_public_gfs_url_without_tls(env, gfs_url):
    """A GFS reachable over plain ``http://`` on the public internet is an
    on-path attacker's dream: ``/gfs/info`` is unauthenticated at the
    transport layer, so stripping the signed capability block (or swapping the
    pinned key on the very first TOFU fetch) is trivial. Refuse at pair time —
    before a single byte is sent — rather than pinning a downgradeable peer."""
    _, repo = env
    session = _StubSession(method_responses={"GET": (200, {})})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError, match="https"):
        await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    # Nothing left the household — the URL never reached the network.
    assert session.calls == []
    assert await repo.list_all() == []


@pytest.mark.parametrize(
    "gfs_url",
    [
        "http://127.0.0.1:8081",
        "http://localhost:9000",
        "http://[::1]:9000",
        "http://192.168.1.5",
        "http://10.0.0.7:8080",
        "http://172.16.4.2",
        "http://[fe80::1]:9000",
        "http://[fd00::5]",
    ],
)
async def test_pair_allows_plain_http_on_loopback_or_a_private_network(env, gfs_url):
    """A GFS on the LAN (or on the developer's loopback, which is how the
    federation demo harness pairs) has no public path to attack and often no
    certificate to serve, so plain ``http://`` stays allowed there."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {"gfs_instance_id": "lan-gfs", "public_key": "bb" * 32},
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    conn = await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    assert conn.gfs_instance_id == "lan-gfs"


@pytest.mark.parametrize(
    "gfs_url",
    [
        "https://user:pw@gfs.example.com",
        "https://user@gfs.example.com",
        "http://gfs.example.com@127.0.0.1:8081",
        "https://",
        "https://gfs.example.com/\r\nX: y",
    ],
)
async def test_pair_rejects_a_malformed_gfs_url(env, gfs_url):
    """The shared household-address rules apply to the GFS URL too: no
    credentials in the URL, a host is required, no control characters."""
    _, repo = env
    session = _StubSession(method_responses={"GET": (200, {})})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    assert session.calls == []
    assert await repo.list_all() == []


async def test_pair_registration_body_has_no_inbox_url(env):
    """The GFS never needed the household's address (its relay runs over the
    household-opened WebSocket), and on the Home Assistant add-on the address
    doesn't exist at onboarding time. The register body is exactly the
    identity + display name — no ``inbox_url`` key at all."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "bb" * 32,
                    "server_name": "GFS",
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok"}, **_OWN_PAIR_KW
    )
    body = session._last_body
    assert body == {
        "token": "tok",
        "instance_id": "alpha.home",
        "public_key": "aa" * 32,
        "display_name": "Alpha House",
    }


async def test_pair_gfs_info_unreachable(env):
    """A GFS that doesn't expose ``/gfs/info`` cannot be pinned —
    surface the failure cleanly instead of saving a half-trusted
    connection."""
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (404, {})},
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 404"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_register_rejects(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (401, {}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 401"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "stale-tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_no_public_key_in_info(env):
    """``/gfs/info`` must return both ``gfs_instance_id`` and
    ``public_key`` — without the key there's no anchor to verify
    later reports against, so refuse to register."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (200, {"gfs_instance_id": "remote", "public_key": ""}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError, match="gfs_instance_id and public_key"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


class _UnreachableSession:
    """A GFS that never answers: every call raises at the transport."""

    def get(self, *a, **kw):
        raise aiohttp.ClientConnectionError("connection refused")

    def post(self, *a, **kw):
        raise aiohttp.ClientConnectionError("connection refused")


_PAIR_INFO = {
    "gfs_instance_id": "remote",
    "public_key": "cc" * 32,
    "server_name": "GFS",
}


async def _pair_reason(
    repo, session, *, gfs_url="https://gfs.example.com"
) -> GfsSignupError:
    """Run :meth:`pair` and hand back the classified failure."""
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsSignupError) as excinfo:
        await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    return excinfo.value


# ``pair`` classifies its failures exactly as ``pair_open_signup`` does, so
# the route can answer the same codes for the same causes on both paths.


async def test_pair_classifies_an_insecure_url(env):
    _, repo = env
    session = _StubSession(method_responses={"GET": (200, _PAIR_INFO)})
    exc = await _pair_reason(repo, session, gfs_url="http://gfs.example.com")
    assert exc.reason == "invalid_url"
    assert session.calls == []


async def test_pair_classifies_an_already_connected_gfs(env):
    """Nothing leaves the household for a GFS it already has."""
    _, repo = env
    await repo.save(_make_conn("gfs-1", inbox_url="https://gfs.example.com"))
    session = _StubSession(method_responses={"GET": (200, _PAIR_INFO)})
    exc = await _pair_reason(repo, session, gfs_url="https://gfs.example.com/")
    assert exc.reason == "already_connected"
    assert session.calls == []


async def test_pair_classifies_a_gfs_that_never_answers_as_unreachable(env):
    _, repo = env
    exc = await _pair_reason(repo, _UnreachableSession())
    assert exc.reason == "unreachable"
    assert exc.status is None


async def test_pair_classifies_a_5xx_descriptor_as_unreachable(env):
    _, repo = env
    session = _StubSession(method_responses={"GET": (503, {})})
    exc = await _pair_reason(repo, session)
    assert exc.reason == "unreachable"
    assert exc.status == 503


@pytest.mark.parametrize(
    "answer",
    [
        (200, {"hello": "world"}),
        (200, {"gfs_instance_id": "remote", "public_key": ""}),
        (404, {}),
    ],
    ids=["not-a-descriptor", "no-key", "no-such-page"],
)
async def test_pair_classifies_a_non_gfs_answer_as_identity_mismatch(env, answer):
    """The address answered, but not as a GFS — retrying won't help, so
    this is not ``unreachable``."""
    _, repo = env
    session = _StubSession(method_responses={"GET": answer})
    exc = await _pair_reason(repo, session)
    assert exc.reason == "identity_mismatch"
    assert exc.status == answer[0]
    assert [m for m, _ in session.calls] == ["GET"]


class _HtmlSession:
    """A plain web server at the pairing code's address: 200, text/html."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append("GET")
        return _HtmlResp()

    def post(self, url, **kw):
        self.calls.append("POST")
        return _HtmlResp()


class _HtmlResp(_StubResp):
    def __init__(self):
        super().__init__(200, {})

    async def json(self):
        url = URL("https://gfs.example.com/gfs/info")
        raise aiohttp.ContentTypeError(
            aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url),
            (),
            message="Attempt to decode JSON with unexpected mimetype: text/html",
        )


async def test_pair_classifies_a_non_json_answer_as_identity_mismatch(env):
    """A web page where ``/gfs/info`` should be is not a GFS either — and
    not "unreachable": the address answered."""
    _, repo = env
    session = _HtmlSession()
    exc = await _pair_reason(repo, session)
    assert exc.reason == "identity_mismatch"
    assert exc.status == 200
    assert session.calls == ["GET"]


async def test_pair_classifies_a_refused_token(env):
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (200, _PAIR_INFO), "POST": (401, {})}
    )
    exc = await _pair_reason(repo, session)
    assert exc.reason == "refused"
    assert exc.status == 401


async def test_pair_classifies_a_5xx_registration_as_unreachable(env):
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (200, _PAIR_INFO), "POST": (502, {})}
    )
    exc = await _pair_reason(repo, session)
    assert exc.reason == "unreachable"
    assert exc.status == 502


async def test_pair_saves_nothing_when_the_gfs_refuses(env):
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (200, _PAIR_INFO), "POST": (401, {})}
    )
    await _pair_reason(repo, session)
    assert await repo.list_all() == []


async def test_disconnect_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    await svc.disconnect("gfs-1")
    assert await repo.get("gfs-1") is None


async def test_disconnect_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.disconnect("nonexistent")


async def test_list_connections(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active"))
    await repo.save(_make_conn("gfs-2", status="suspended"))
    await repo.save(_make_conn("gfs-3", status="pending"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.list_connections()
    # The UI list surfaces every status — a pending/suspended connection
    # must not be invisible just because the GFS hasn't approved yet.
    assert {c.id for c in result} == {"gfs-1", "gfs-2", "gfs-3"}
    assert {c.status for c in result} == {"active", "suspended", "pending"}


async def test_publish_space_success(env):
    session = _StubSession(status=200)
    svc = await _publishable_svc(env, session, "gfs-1", space_id="sp-1")
    _, repo = env
    pub = await svc.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"
    assert pub.space_id == "sp-1"
    assert len(session.calls) == 1


async def test_publish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.publish_space("sp-1", "nonexistent")


async def test_unpublish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    session = _StubSession(status=204)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_unpublish_space_not_found(env):
    _, repo = env
    svc, _pk = _signing_svc(repo, _StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.unpublish_space("sp-1", "nonexistent")


# ── publish-space context (attach_publish_context + _build_publish_body) ──


async def test_publish_body_raises_when_context_unset(env):
    """Without ``attach_publish_context`` there's no signing key, so the
    GFS publish is fail-closed: ``publish_space`` raises rather than send
    an unsigned body (the GFS now rejects unsigned publishes)."""
    _, repo = env
    await repo.save(_make_conn("gfs-1", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    # No attach_publish_context call → no signing key → must refuse.
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp-bare", "gfs-1")
    # And no HTTP request was made.
    assert session.calls == []


async def test_publish_body_carries_metadata_and_signature(env):
    """With ``attach_publish_context`` wired, the publish body includes
    the local space's name + description + signed canonical JSON the
    GFS verifies against ``ClientInstance.public_key``."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-2", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp-rich",
        name="Local Birds",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
        description="everyday birds in the neighbourhood",
    )
    await space_repo.save(space)

    kp = generate_identity_keypair()
    session = _StubSession(
        method_responses={"POST": (200, {"status": "registered"})},
    )
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-rich", "gfs-2")
    # Capture the body the stub session forwarded.
    body = session._last_body  # type: ignore[attr-defined]
    assert body["space_id"] == "sp-rich"
    assert body["owning_instance"] == "alpha.home"
    assert body["name"] == "Local Birds"
    assert body["description"] == "everyday birds in the neighbourhood"
    # Phase 5a: the publish body ships the space's Ed25519 authority verify key
    # so the GFS can TOFU-pin it for space-authority-signed relays.
    assert body["identity_public_key"] == "aa" * 32
    # The membership gate travels so the directory can label the listing…
    assert body["join_mode"] == "open"
    # …and the readability opt-in travels separately, because it — not the
    # join mode — is what the GFS enforces on /gfs/subscribe. Off by default.
    assert body["allow_subscribers"] is False
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_allow_subscribers(env):
    """The readability opt-in travels INSIDE the signed canonical body — the
    GFS refuses a subscribe without it. Deliberately paired with
    ``invite_only`` here: the two dials are independent, and this broadcast
    shape (invited people post, anyone may follow) is exactly what the old
    join-mode-as-readability model could not express."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-rd", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-rd",
            name="Broadcast",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-rd", "gfs-rd")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["allow_subscribers"] is True
    assert body["join_mode"] == "invite_only"
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_invite_only_join_mode(env):
    """A global space whose owner keeps it invite-only says so on the wire:
    the GFS lists it for discovery but must refuse to seat a subscriber, so
    the value has to travel (it used to be absent entirely)."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-jm", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-jm",
            name="Quiet",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-jm", "gfs-jm")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["join_mode"] == "invite_only"
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_fresh_signed_ts(env):
    """The publish body ships a tz-aware ``ts`` INSIDE the signed canonical
    bytes, so the GFS can replay-guard it — only such a fresh publish may
    restore a listing the owner previously withdrew."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-ts", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-ts",
            name="Timestamped",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-ts", "gfs-ts")

    body = session._last_body  # type: ignore[attr-defined]
    parsed = datetime.fromisoformat(body["ts"])
    assert parsed.tzinfo is not None, "ts must be tz-aware (a naive one is rejected)"
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60
    # ``ts`` is covered by the signature, not bolted on beside it.
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_brand_colors_and_image_data_uris(env):
    """With theme/cover/icon repos wired, the publish body ships the real
    theme colours + the cover and icon as self-contained data URIs (so the
    GFS public page renders the space's brand on its own origin)."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_cover_repo import SqliteSpaceCoverRepo
    from socialhome.repositories.space_icon_repo import SqliteSpaceIconRepo
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.theme_repo import SqliteThemeRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-b", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp-brand",
        name="Brandy",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    await space_repo.save(space)
    await space_repo.set_cover_hash("sp-brand", "ch")
    await space_repo.set_icon_hash("sp-brand", "ih")
    theme_repo = SqliteThemeRepo(db)
    await theme_repo.upsert_space(
        space_id="sp-brand", primary_color="#112233", accent_color="#445566"
    )
    cover_repo = SqliteSpaceCoverRepo(db)
    await cover_repo.set(
        "sp-brand", bytes_webp=b"RIFFcover", hash="ch", width=8, height=8
    )
    icon_repo = SqliteSpaceIconRepo(db)
    await icon_repo.set(
        "sp-brand", bytes_webp=b"RIFFicon", hash="ih", width=8, height=8
    )

    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "ok"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
        theme_repo=theme_repo,
        cover_repo=cover_repo,
        icon_repo=icon_repo,
    )
    await svc.publish_space("sp-brand", "gfs-b")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["primary_color"] == "#112233"
    assert body["accent_color"] == "#445566"
    assert body["cover_url"].startswith("data:image/webp;base64,")
    assert body["icon_url"].startswith("data:image/webp;base64,")


# ── update_display_name_to_all ──────────────────────────────────────────


async def test_update_display_name_signs_and_posts_to_each_gfs(env):
    """The household rename is signed over the canonical
    ``{instance_id, display_name, ts}`` JSON and POSTed to every active
    GFS's ``/gfs/instance``; the return value is the number of 200s and
    the signature verifies byte-for-byte against the GFS contract."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    await repo.save(_make_conn("g2", inbox_url="https://b.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "ok"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("Casa Vizeli")
    assert n == 2
    # POSTed to each GFS's /gfs/instance.
    assert ("POST", "https://a.example/gfs/instance") in session.calls
    assert ("POST", "https://b.example/gfs/instance") in session.calls
    body = session._last_body  # type: ignore[attr-defined]
    assert body["instance_id"] == "alpha.home"
    assert body["display_name"] == "Casa Vizeli"
    assert body["ts"]
    sig = body["signature"]
    assert sig
    canonical = json.dumps(
        {
            "instance_id": body["instance_id"],
            "display_name": body["display_name"],
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_update_display_name_skips_404_old_gfs(env):
    """A GFS too old to have ``/gfs/instance`` returns 404 — skipped,
    counted as 0, never raising."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://old.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=404)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("New Name")
    assert n == 0


async def test_update_display_name_skips_transport_errors(env):
    """A GFS raising ClientError or a bare TimeoutError is skipped; the
    healthy GFS is still updated, and the method never raises."""
    _, repo = env
    await repo.save(_make_conn("ok", inbox_url="https://ok.example"))
    await repo.save(_make_conn("err", inbox_url="https://err.example"))
    await repo.save(_make_conn("slow", inbox_url="https://slow.example"))
    kp = generate_identity_keypair()

    class _MixedSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post(self, url, **kw):
            self.calls.append(("POST", url))
            if "err.example" in url:
                raise aiohttp.ClientError("boom")
            if "slow.example" in url:
                raise asyncio.TimeoutError
            return _StubResp(200, {"status": "ok"})

    session = _MixedSession()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("New Name")
    assert n == 1
    assert len(session.calls) == 3


async def test_update_display_name_no_context_returns_zero(env):
    """Without publish context wired (no instance id / signing key),
    the method is a no-op returning 0 — never crashes early boot."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    n = await svc.update_display_name_to_all("New Name")
    assert n == 0
    assert session.calls == []


# ── push_display_name (single GFS, reconnect self-heal) ─────────────────


async def test_push_display_name_signs_and_posts_to_one_gfs(env):
    """``push_display_name`` POSTs the signed canonical
    ``{instance_id, display_name, ts}`` body to exactly the named GFS's
    ``/gfs/instance`` and returns True on 200. The signature verifies
    byte-for-byte against the GFS contract — same shape as the
    fan-out variant, but to one connection."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    await repo.save(_make_conn("g2", inbox_url="https://b.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "ok"})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "Casa Vizeli")
    assert ok is True
    # POSTed only to g1's /gfs/instance, not g2's.
    assert ("POST", "https://a.example/gfs/instance") in session.calls
    assert ("POST", "https://b.example/gfs/instance") not in session.calls
    body = session._last_body  # type: ignore[attr-defined]
    assert body["instance_id"] == "alpha.home"
    assert body["display_name"] == "Casa Vizeli"
    assert body["ts"]
    sig = body["signature"]
    assert sig
    canonical = json.dumps(
        {
            "instance_id": body["instance_id"],
            "display_name": body["display_name"],
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_push_display_name_returns_false_on_http_error(env):
    """A non-200 (e.g. 404 from an old GFS, or a 5xx) yields False and
    never raises."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://old.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=404)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False


async def test_push_display_name_returns_false_on_transport_error(env):
    """A GFS raising ClientError/TimeoutError is swallowed — best-effort
    push returns False, never propagates."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://err.example"))
    kp = generate_identity_keypair()

    class _ErrSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post(self, url, **kw):
            self.calls.append(("POST", url))
            raise aiohttp.ClientError("boom")

    session = _ErrSession()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False
    assert len(session.calls) == 1


async def test_push_display_name_returns_false_for_unknown_gfs(env):
    """A gfs_id with no active connection row → False, no POST."""
    _, repo = env
    kp = generate_identity_keypair()
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("nope", "New Name")
    assert ok is False
    assert session.calls == []


async def test_push_display_name_no_context_returns_false(env):
    """Without publish context wired (no instance id / signing key),
    the push is a no-op returning False — never crashes early boot."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False
    assert session.calls == []


async def test_publish_body_raises_when_space_missing(env):
    """``attach_publish_context`` is wired but the local space row is
    gone — a publish with no space metadata can't be signed into a valid
    body, so fail closed (raise) rather than send a partial / unsigned
    publish the GFS will reject anyway."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-3", inbox_url="https://gfs.example"))
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=SqliteSpaceRepo(db),
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp-missing", "gfs-3")
    assert session.calls == []


async def test_subscribe_to_gfs_space_signs_body(env):
    """``subscribe_to_gfs_space`` signs the canonical
    ``{action, instance_id, space_id, ts}`` body with the household
    identity key and POSTs it to the GFS ``/gfs/subscribe`` endpoint.
    The ``action`` rides inside the signed bytes (domain separation), so
    the GFS can't have this signature replayed as an unsubscribe."""
    _, repo = env
    await repo.save(_make_conn("gfs-sub", inbox_url="https://gfs.example"))
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "subscribed"})})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.subscribe_to_gfs_space("sp-join", "gfs-sub")
    assert session.calls == [
        ("POST", "https://gfs.example/gfs/subscribe"),
    ]
    body = session._last_body  # type: ignore[attr-defined]
    assert body["action"] == "subscribe"
    assert body["instance_id"] == "alpha.home"
    assert body["space_id"] == "sp-join"
    assert body["ts"]
    sig = body["signature"]
    canonical = json.dumps(
        {
            "action": "subscribe",
            "instance_id": "alpha.home",
            "space_id": "sp-join",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_unpublish_space_signs_canonical_body(env):
    """SECURITY: ``unpublish`` is owner-authenticated on the GFS, so the HFS
    must ship a signed ``{owning_instance, ts, signature}`` body — the
    signature covering the canonical ``{action: "unpublish", owning_instance,
    space_id, ts}`` bytes (``action`` inside, so a captured subscribe
    signature can't be replayed as a delisting)."""
    _, repo = env
    await repo.save(_make_conn("gfs-un", inbox_url="https://gfs.example"))
    await repo.publish_space("sp-un", "gfs-un")
    session = _StubSession(status=200)
    svc, pubkey = _signing_svc(repo, session)
    await svc.unpublish_space("sp-un", "gfs-un")

    assert session.calls == [
        ("POST", "https://gfs.example/gfs/spaces/sp-un/unpublish"),
    ]
    body = session._last_body  # type: ignore[attr-defined]
    assert body is not None
    assert body["owning_instance"] == "alpha.home"
    assert body["ts"]
    canonical = json.dumps(
        {
            "action": "unpublish",
            "owning_instance": "alpha.home",
            "space_id": "sp-un",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(pubkey, canonical, b64url_decode(body["signature"]))
    # Local publication row is cleared on the successful round-trip.
    assert await repo.list_publications_for_space("sp-un") == []


async def test_unpublish_space_raises_without_signing_key(env):
    """No identity wired → fail closed rather than send a body the GFS
    rejects (mirrors ``subscribe_to_gfs_space``); the local row survives."""
    _, repo = env
    await repo.save(_make_conn("gfs-un2", inbox_url="https://gfs.example"))
    await repo.publish_space("sp-un2", "gfs-un2")
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.unpublish_space("sp-un2", "gfs-un2")
    assert session.calls == []
    assert len(await repo.list_publications_for_space("sp-un2")) == 1


async def test_subscribe_to_gfs_space_raises_without_signing_key(env):
    """No identity wired → fail closed; the GFS rejects unsigned subscribes."""
    _, repo = env
    await repo.save(_make_conn("gfs-sub2", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "subscribed"})})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.subscribe_to_gfs_space("sp-join", "gfs-sub2")
    assert session.calls == []


# ─── publish_space_event (Phase 5a2 — relay a space event to the GFS) ──────


class _RecordingSession:
    """aiohttp-session stub that records EVERY POST body + url.

    The shared :class:`_StubSession` keeps only the last body; the
    fan-out tests need every (url, body) pair to assert the relay hit
    each published GFS.
    """

    def __init__(self, status: int = 200) -> None:
        self._status = status
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def get(self, url, **_kw):
        # ``/gfs/info`` with no ``anonymous_publish`` — i.e. a GFS predating
        # the anonymous relay, so a test pins ``_anon_publish`` itself.
        self.gets.append(url)
        return _StubResp(200, {"server_name": "Legacy GFS"})

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, {"status": "published"})


async def _publish_event_svc(env, session, *, space_id: str, gfs_ids: list[str]):
    """Service wired for publish_space_event, with the space published to
    each of *gfs_ids* (so ``list_gfs_for_space`` returns them)."""
    db, conn_repo = env
    for gid in gfs_ids:
        await conn_repo.save(
            _make_conn(
                gid,
                inbox_url=f"https://{gid}.example",
                # The GFS identity key this household pinned at pair time —
                # the only key a capability block may be verified against.
                public_key=_GFS_KP.public_key.hex(),
            )
        )
        await conn_repo.publish_space(space_id, gid)
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, kp


async def test_publish_space_event_fans_identity_free_to_each_published_gfs(env):
    """A relay event is POSTed to ``/gfs/publish`` on EVERY GFS the space
    is published to, carrying the verbatim envelope as ``payload`` — and
    nothing else: no ``from_instance``, no household signature."""
    session = _RecordingSession()
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-relay", gfs_ids=["g1", "g2"]
    )
    # Both servers proved ``anonymous_publish`` (signed block, pair time).
    svc._anon_publish.update({"g1": True, "g2": True})
    envelope = {"space_id": "sp-relay", "epoch": 0, "encrypted_payload": "ct"}
    delivered = await svc.publish_space_event(
        space_id="sp-relay",
        event_type="space_post_public",
        payload=envelope,
    )
    assert delivered == 2
    urls = sorted(u for u, _ in session.posts)
    assert urls == ["https://g1.example/gfs/publish", "https://g2.example/gfs/publish"]
    for _url, body in session.posts:
        assert body == {
            "space_id": "sp-relay",
            "event_type": "space_post_public",
            "payload": envelope,
        }


async def test_publish_space_event_returns_zero_without_signing_key(env):
    """No identity wired → fail closed, no POST."""
    _, repo = env
    await repo.save(_make_conn("g-x", inbox_url="https://gx.example"))
    await repo.publish_space("sp-x", "g-x")
    session = _RecordingSession()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    delivered = await svc.publish_space_event(
        space_id="sp-x",
        event_type="space_post_public",
        payload={"space_id": "sp-x"},
    )
    assert delivered == 0
    assert session.posts == []


async def test_publish_space_event_skips_unpublished_space(env):
    """A space published to no GFS → nothing sent."""
    session = _RecordingSession()
    svc, _ = await _publish_event_svc(env, session, space_id="sp-pub", gfs_ids=[])
    delivered = await svc.publish_space_event(
        space_id="sp-none",
        event_type="space_post_public",
        payload={"space_id": "sp-none"},
    )
    assert delivered == 0
    assert session.posts == []


# ─── unsubscribe_from_gfs_space ──────────────────────────────────────────


async def test_unsubscribe_from_gfs_space_signs_body(env):
    """``unsubscribe_from_gfs_space`` signs the canonical
    ``{action, instance_id, space_id, ts}`` body with the household identity
    key and POSTs it to ``/gfs/subscribe``. The GFS requires the signature
    (an unsigned unsubscribe is a 403) and the ``action`` rides inside the
    signed bytes, so the signature can't be replayed as a subscribe."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "removed"})})
    svc, pubkey = _signing_svc(repo, session)
    status = await svc.unsubscribe_from_gfs_space("sp-leave", "gfs-unsub")
    assert status == "removed"
    assert session.calls == [("POST", "https://gfs.example/gfs/subscribe")]
    body = session._last_body  # type: ignore[attr-defined]
    assert body["action"] == "unsubscribe"
    assert body["instance_id"] == "alpha.home"
    assert body["space_id"] == "sp-leave"
    assert body["ts"]
    canonical = json.dumps(
        {
            "action": "unsubscribe",
            "instance_id": "alpha.home",
            "space_id": "sp-leave",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(pubkey, canonical, b64url_decode(body["signature"]))


async def test_unsubscribe_from_gfs_space_404_is_success(env):
    """Idempotent: already-absent on the GFS is not an error."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (404, {})})
    svc, _pk = _signing_svc(repo, session)
    assert await svc.unsubscribe_from_gfs_space("sp-gone", "gfs-unsub")


async def test_unsubscribe_from_gfs_space_raises_on_server_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (500, {})})
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "gfs-unsub")


async def test_unsubscribe_from_gfs_space_unknown_gfs_raises(env):
    _, repo = env
    session = _StubSession(status=200)
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "nope")
    assert session.calls == []


async def test_unsubscribe_from_gfs_space_without_signing_key_raises(env):
    """Fail-closed: no signing identity → never send an unsigned body."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "gfs-unsub")
    assert session.calls == []


# ─── remote-authored error text (FIX 6) ──────────────────────────────────


async def test_remote_error_detail_is_truncated(caplog):
    """``GfsConnectionError`` text reaches the SPA as the 502
    ``GFS_UNAVAILABLE`` message (``routes/base.py``), so a hostile GFS must
    not be able to author arbitrarily long copy in the household's own error
    toast. The full body goes to the log instead."""
    resp = _StubResp(500, text="X" * 5000)
    with caplog.at_level(logging.WARNING):
        detail = await _remote_detail(resp, context="publish")

    assert len(detail) <= MAX_REMOTE_DETAIL_CHARS + len("… (truncated)")
    assert detail.endswith("… (truncated)")
    assert "X" * 5000 in caplog.text


async def test_short_remote_error_detail_passes_through():
    resp = _StubResp(400, text="space not published")
    assert await _remote_detail(resp, context="publish") == "space not published"


async def test_remote_error_detail_bounds_the_read():
    """Even the logged body is bounded — a multi-gigabyte error body must
    never be buffered whole."""
    resp = _StubResp(500, text="Y" * (1024 * 1024))
    detail = await _remote_detail(resp, context="subscribe")
    assert len(detail) <= MAX_REMOTE_DETAIL_CHARS + len("… (truncated)")


# ─── Anonymous publish (the GFS must not learn WHICH household relayed) ───


def _signed_info(
    *,
    gfs_instance_id: str = "inst-g1",
    capabilities: dict | None = None,
    kp=None,
    server_name: str = "GFS g1",
    suite: str = CAPS_SIG_SUITE_ED25519,
) -> dict:
    """The ``/gfs/info`` body a CURRENT connection server serves.

    The capability map travels with a signature made by the GFS identity key
    the household pinned at pair time — that signature is the ONLY thing the
    household may act on. The top-level ``anonymous_publish`` mirror stays for
    readability and is deliberately ignored by the client.
    """
    caps = {"anonymous_publish": True} if capabilities is None else capabilities
    sig, _real_suite = sign_capabilities(
        (kp or _GFS_KP).private_key, gfs_instance_id, caps
    )
    return {
        "server_name": server_name,
        "anonymous_publish": bool(caps.get("anonymous_publish")),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }


def _stripped_info(server_name: str = "GFS g1") -> dict:
    """What an on-path attacker leaves behind: the unauthenticated flag still
    says ``true`` but the signed block is gone. Indistinguishable on the wire
    from an older GFS — and treated the same way (no publish + a warning)."""
    return {"server_name": server_name, "anonymous_publish": True}


class _AnonSession:
    """aiohttp-session stub with a scriptable ``GET /gfs/info``.

    ``info`` is the body ``/gfs/info`` returns; ``raise_on_get`` makes the GET
    raise a transport error (the "GFS unreachable at publish time" case).
    Every GET and POST is recorded so a test can assert the info endpoint was
    hit exactly once across N publishes.
    """

    def __init__(
        self,
        *,
        info: dict | None = None,
        info_status: int = 200,
        raise_on_get: bool = False,
        status: int = 200,
    ) -> None:
        self.info = info if info is not None else {}
        self.info_status = info_status
        self.raise_on_get = raise_on_get
        self._status = status
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, **_kw):
        self.gets.append(url)
        if self.raise_on_get:
            raise aiohttp.ClientError("boom")
        return _StubResp(self.info_status, self.info)

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, {"status": "published"})


def _freeze_clock(monkeypatch, clock: list[float]) -> None:
    """Drive the service's negative-TTL clock from *clock[0]*.

    Only the service module's own ``time`` binding is swapped — patching the
    real :func:`time.monotonic` would also freeze asyncio's event-loop clock
    and hang every timeout in the process.
    """
    monkeypatch.setattr(
        gfs_connection_service,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )


def test_the_service_does_not_import_the_gfs_server_package():
    """The household verifies capability blocks with
    :mod:`socialhome.capabilities_sig`, which lives at the package top level
    exactly so importing this service does NOT drag the GFS server package
    into every HFS process."""
    code = (
        "import sys; import socialhome.services.gfs_connection_service; "
        "print('socialhome.global_server' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout


def _info_probes(session) -> int:
    return len([u for u in session.gets if u.endswith("/gfs/info")])


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "socialhome.services.gfs_connection_service"
    ]


async def test_publish_space_event_omits_identity_for_anonymous_gfs(env):
    """A GFS whose SIGNED capability block advertises ``anonymous_publish``
    gets the identity-free body: exactly ``{space_id, event_type, payload}`` —
    no ``from_instance``, no household transport ``signature``, no ``ts``, and
    this household's own instance id nowhere in the serialized bytes."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-anon", gfs_ids=["g1"]
    )
    envelope = {"space_id": "sp-anon", "epoch": 0, "encrypted_payload": "ct"}
    delivered = await svc.publish_space_event(
        space_id="sp-anon",
        event_type="space_post_public",
        payload=envelope,
    )
    assert delivered == 1
    _url, body = session.posts[0]
    assert set(body) == {"space_id", "event_type", "payload"}
    assert body["payload"] == envelope
    # The household identity must not survive anywhere in the wire bytes.
    assert "alpha.home" not in json.dumps(body)
    assert "from_instance" not in json.dumps(body)


async def test_publish_space_event_old_gfs_warns_once_per_connection(env, caplog):
    """A GFS that did NOT advertise the flag gets no publish at all (there is
    no identified legacy body any more) — and the skip is logged as exactly
    ONE warning per connection per process, naming the connection (never the
    space), next to the one "no signed capability block" warning."""
    session = _AnonSession(info={"server_name": "Old GFS"})
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-legacy", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        for _ in range(3):
            assert (
                await svc.publish_space_event(
                    space_id="sp-legacy",
                    event_type="space_post_public",
                    payload={"space_id": "sp-legacy", "epoch": 0},
                )
                == 0
            )
    assert session.posts == []
    msgs = _warnings(caplog)
    assert len(msgs) == 2
    assert all("sp-legacy" not in m for m in msgs)
    assert all("g1" in m or "https://g1.example" in m for m in msgs)
    assert any("no signed capability block" in m for m in msgs)


async def test_stripped_capability_block_is_not_trusted(env, caplog):
    """THE downgrade attack: an on-path attacker strips the signed block from
    ``/gfs/info`` while leaving the bare ``anonymous_publish: true`` flag. The
    household must NOT act on the unauthenticated flag; only the signature
    decides. Stripping now buys the attacker a denied relay, never an
    identified body."""
    session = _AnonSession(info=_stripped_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-strip", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-strip",
            event_type="space_post_public",
            payload={"space_id": "sp-strip"},
        )
    assert session.posts == []
    assert any("no signed capability block" in m for m in _warnings(caplog))


async def test_capability_block_signed_by_the_wrong_key_is_rejected(env, caplog):
    """A block signed by ANY key but the pinned one is tampering, and the
    warning says so — an operator has to be able to tell "old GFS" (no block)
    from "someone is rewriting my /gfs/info" (a block that fails)."""
    attacker = generate_identity_keypair()
    session = _AnonSession(info=_signed_info(kp=attacker))
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-evil", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-evil",
            event_type="space_post_public",
            payload={"space_id": "sp-evil"},
        )
    assert session.posts == []
    msgs = _warnings(caplog)
    assert any("FAILED verification" in m for m in msgs)
    assert not any("no signed capability block" in m for m in msgs)


async def test_capability_block_bound_to_another_gfs_is_rejected(env, caplog):
    """The signature covers the GFS instance id, so a block lifted verbatim
    from another (legitimately signed) server does not authenticate this one —
    even when the attacker owns that other server's key."""
    session = _AnonSession(info=_signed_info(gfs_instance_id="inst-somewhere-else"))
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-lift", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-lift",
            event_type="space_post_public",
            payload={"space_id": "sp-lift"},
        )
    assert session.posts == []
    assert any("FAILED verification" in m for m in _warnings(caplog))


async def test_unknown_capability_suite_is_unverifiable_not_fatal(env, caplog):
    """A suite this build doesn't know is rejected (no default fallback) — but
    it must never raise out of a best-effort fetch: nothing is sent and the
    operator gets a warning naming the suite."""
    session = _AnonSession(info=_signed_info(suite="ed25519+mldsa65"))
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-pq", gfs_ids=["g1"])
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        delivered = await svc.publish_space_event(
            space_id="sp-pq",
            event_type="space_post_public",
            payload={"space_id": "sp-pq"},
        )
    assert delivered == 0
    assert session.posts == []
    assert any("ed25519+mldsa65" in m for m in _warnings(caplog))


async def test_signed_block_denying_the_capability_is_not_tamper_flagged(env, caplog):
    """A VALIDLY signed ``anonymous_publish: false`` is a GFS honestly saying
    it can't do the anonymous relay. No publish, yes — but no tampering /
    missing-block warning, or the honest answer would look like an attack."""
    session = _AnonSession(info=_signed_info(capabilities={"anonymous_publish": False}))
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-no", gfs_ids=["g1"])
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-no",
            event_type="space_post_public",
            payload={"space_id": "sp-no"},
        )
    assert session.posts == []
    msgs = _warnings(caplog)
    assert not any("FAILED verification" in m for m in msgs)
    assert not any("no signed capability block" in m for m in msgs)


async def test_verified_capability_cannot_be_downgraded_in_process(env, caplog):
    """In-process ratchet: once a connection has been seen advertising
    ``anonymous_publish`` under a VALID signature, a later fetch that lacks it
    does NOT flip the cache back. A GFS cannot legitimately lose the
    capability, so a mid-life downgrade is an attack (or a broken proxy) — the
    household keeps relaying identity-free and logs the attempt."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-rat", gfs_ids=["g1"])
    await svc.publish_space_event(
        space_id="sp-rat",
        event_type="space_post_public",
        payload={"space_id": "sp-rat"},
    )
    assert set(session.posts[0][1]) == {"space_id", "event_type", "payload"}
    # The block disappears (attacker on-path, or a proxy eating the field).
    session.info = _stripped_info()
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.refresh_connection_metadata("g1")
        await svc.publish_space_event(
            space_id="sp-rat",
            event_type="space_post_public",
            payload={"space_id": "sp-rat"},
        )
    assert set(session.posts[1][1]) == {"space_id", "event_type", "payload"}
    assert any("downgrade ignored" in m for m in _warnings(caplog))


async def test_the_ratchet_does_not_survive_a_restart(env):
    """The ratchet is RAM-only: a fresh process starts from "unknown" and has
    to learn the capability again from a signed block. (Persisting it would
    pin a remote server's build state in this household's database.)"""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-boot", gfs_ids=["g1"]
    )
    await svc.publish_space_event(
        space_id="sp-boot",
        event_type="space_post_public",
        payload={"space_id": "sp-boot"},
    )
    assert set(session.posts[0][1]) == {"space_id", "event_type", "payload"}

    _db, conn_repo = env
    session.info = _stripped_info()
    restarted = GfsConnectionService(
        conn_repo, http_client=session, publish_client=session
    )
    restarted.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    await restarted.publish_space_event(
        space_id="sp-boot",
        event_type="space_post_public",
        payload={"space_id": "sp-boot"},
    )
    # The restarted process has not seen a signed block: nothing goes out.
    assert len(session.posts) == 1


async def test_publish_space_event_fetches_info_once_on_cold_cache(env):
    """The flag is unknown on the first publish after boot → /gfs/info is
    fetched ONCE on demand and the answer cached for later publishes."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-cold", gfs_ids=["g1"]
    )
    for _ in range(3):
        await svc.publish_space_event(
            space_id="sp-cold",
            event_type="space_post_public",
            payload={"space_id": "sp-cold"},
        )
    assert _info_probes(session) == 1
    assert all(
        set(b) == {"space_id", "event_type", "payload"} for _u, b in session.posts
    )


async def test_publish_space_event_info_failure_waits_then_goes_identity_free(
    env, monkeypatch
):
    """An unreachable /gfs/info sends NOTHING (no legacy fallback): the
    publish waits in the retry queue. The failure is NOT cached as a
    capability — only as a short probe suppression — so once the TTL passes
    the retry probes again and goes out identity-free."""
    clock = [1000.0]
    _freeze_clock(monkeypatch, clock)
    session = _AnonSession(raise_on_get=True)
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-retry", gfs_ids=["g1"]
    )
    await svc.publish_space_event(
        space_id="sp-retry",
        event_type="space_post_public",
        payload={"space_id": "sp-retry"},
    )
    assert session.posts == []
    assert svc._publish_retry.pending("g1")
    # The GFS comes back; after the negative TTL the retry probes again.
    session.raise_on_get = False
    session.info = _signed_info()
    clock[0] += GFS_INFO_NEGATIVE_TTL_S + 0.1
    queue = svc._publish_retry
    await queue._drain_conn("g1", queue._queues["g1"])
    assert _info_probes(session) == 2
    assert [b for _u, b in session.posts] == [
        {
            "space_id": "sp-retry",
            "event_type": "space_post_public",
            "payload": {"space_id": "sp-retry"},
        }
    ]


async def test_failed_info_probe_is_negative_cached_for_the_ttl(env, monkeypatch):
    """A GFS whose ``/gfs/info`` is down used to cost a full 10 s connect
    timeout on EVERY capability check (the answer is deliberately never
    cached as ``False``). A short negative TTL collapses a burst onto one
    probe while it stays "unknown" — so publishes wait, never get skipped —
    and past the TTL the household probes again."""
    clock = [500.0]
    _freeze_clock(monkeypatch, clock)
    session = _AnonSession(raise_on_get=True)
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-ttl", gfs_ids=["g1"])
    (conn,) = await env[1].list_gfs_for_space("sp-ttl")

    assert await svc._publish_capability(conn) == "unknown"
    clock[0] += GFS_INFO_NEGATIVE_TTL_S - 0.1
    assert await svc._publish_capability(conn) == "unknown"
    assert await svc._publish_capability(conn) == "unknown"
    assert _info_probes(session) == 1
    clock[0] += 0.2
    assert await svc._publish_capability(conn) == "unknown"
    assert _info_probes(session) == 2
    assert session.posts == []


async def test_ws_reconnect_refresh_upgrades_the_publish_body(env):
    """``refresh_connection_metadata`` (run on every GFS-WS reconnect) is what
    keeps the cached flag fresh: an old GFS gets nothing, and once upgraded it
    starts receiving identity-free bodies on the next publish, with no extra
    probe."""
    session = _AnonSession(info={"server_name": "GFS g1"})
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-up", gfs_ids=["g1"])
    await svc.refresh_connection_metadata("g1")
    await svc.publish_space_event(
        space_id="sp-up",
        event_type="space_post_public",
        payload={"space_id": "sp-up"},
    )
    assert session.posts == []
    # Operator upgrades the GFS; the next reconnect refresh learns the flag.
    session.info = _signed_info()
    await svc.refresh_connection_metadata("g1")
    await svc.publish_space_event(
        space_id="sp-up",
        event_type="space_post_public",
        payload={"space_id": "sp-up"},
    )
    assert set(session.posts[0][1]) == {"space_id", "event_type", "payload"}
    # Only the two reconnect refreshes hit /gfs/info — no on-demand probe.
    assert _info_probes(session) == 2


async def test_pair_seeds_the_capability_only_from_a_verified_block(env):
    """At pair time the household verifies the block against the ``public_key``
    in the SAME response — the one it is about to pin (TOFU). A verified block
    seeds the cache, so the first relay to a freshly-paired GFS is already
    identity-free without a second round-trip."""
    _db, repo = env
    gfs_kp = generate_identity_keypair()
    info = {
        "gfs_instance_id": "fresh-gfs",
        "public_key": gfs_kp.public_key.hex(),
        "server_name": "Fresh GFS",
        **_signed_info(gfs_instance_id="fresh-gfs", kp=gfs_kp),
    }
    session = _StubSession(
        method_responses={
            "GET": (200, info),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    conn = await svc.pair(
        {"gfs_url": "https://fresh.example", "token": "tok"}, **_OWN_PAIR_KW
    )
    await repo.publish_space("sp-fresh", conn.id)
    await svc.publish_space_event(
        space_id="sp-fresh",
        event_type="space_post_public",
        payload={"space_id": "sp-fresh"},
    )
    # No extra /gfs/info probe, and the very first relay is identity-free.
    assert [m for m, _u in session.calls].count("GET") == 1
    assert set(session._last_body or {}) == {"space_id", "event_type", "payload"}


async def test_pair_does_not_seed_the_capability_from_an_unsigned_flag(env):
    """Pairing over a stripped (or simply older) ``/gfs/info`` seeds the cache
    with ``False`` — the bare flag proves nothing, so nothing is relayed to it
    until a signed block shows up (never an identified body)."""
    _db, repo = env
    gfs_kp = generate_identity_keypair()
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "fresh-gfs",
                    "public_key": gfs_kp.public_key.hex(),
                    "server_name": "Fresh GFS",
                    "anonymous_publish": True,
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    conn = await svc.pair(
        {"gfs_url": "https://fresh.example", "token": "tok"}, **_OWN_PAIR_KW
    )
    await repo.publish_space("sp-fresh", conn.id)
    await svc.publish_space_event(
        space_id="sp-fresh",
        event_type="space_post_public",
        payload={"space_id": "sp-fresh"},
    )
    # The only POST was the pairing registration — no publish went out.
    assert "payload" not in (session._last_body or {})


# ─── heal_space_pins (self-heal a NULL authority pin on WS connect) ────────


class _FakeSpaceRepo:
    """Minimal space repo for the pin self-heal: a space table + seeds.

    ``kek=False`` reproduces a household with no key manager wired, where
    ``get_space_seed`` raises ``RuntimeError`` — the heal must skip, not blow
    up the connect hook.
    """

    def __init__(self, spaces: dict, seeds: dict, *, kek: bool = True) -> None:
        self._spaces = spaces
        self._seeds = seeds
        self._kek = kek

    async def get(self, space_id: str):
        return self._spaces.get(space_id)

    async def get_space_seed(self, space_id: str) -> bytes | None:
        if not self._kek:
            raise RuntimeError("space seed access requires a key_manager")
        return self._seeds.get(space_id)


def _fake_space(space_id: str):
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType

    return Space(
        id=space_id,
        name=f"Space {space_id}",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )


async def _heal_svc(env, session, *, gfs_id: str, spaces: list[str], space_repo):
    _db, conn_repo = env
    await conn_repo.save(_make_conn(gfs_id, inbox_url=f"https://{gfs_id}.example"))
    for sid in spaces:
        await conn_repo.publish_space(sid, gfs_id)
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc


async def test_heal_space_pins_republishes_each_published_space(env):
    """Every space this household published to the reconnecting GFS is
    re-published once, so a GFS row with a NULL authority pin heals."""
    session = _AnonSession()
    repo = _FakeSpaceRepo(
        {"sp-a": _fake_space("sp-a"), "sp-b": _fake_space("sp-b")},
        {"sp-a": b"\x01" * 32, "sp-b": b"\x02" * 32},
    )
    svc = await _heal_svc(
        env, session, gfs_id="g1", spaces=["sp-a", "sp-b"], space_repo=repo
    )
    healed = await svc.heal_space_pins("g1")
    assert healed == 2
    urls = sorted(u for u, _b in session.posts)
    assert urls == [
        "https://g1.example/gfs/spaces/sp-a/publish",
        "https://g1.example/gfs/spaces/sp-b/publish",
    ]
    assert all("identity_public_key" in b for _u, b in session.posts)


async def test_heal_space_pins_skips_seedless_and_missing_spaces(env):
    """A space this household holds no seed for (pure subscriber) and one whose
    local row is gone are both skipped — only the seed-held space republishes."""
    session = _AnonSession()
    repo = _FakeSpaceRepo(
        {"sp-a": _fake_space("sp-a"), "sp-noseed": _fake_space("sp-noseed")},
        {"sp-a": b"\x01" * 32},
    )
    svc = await _heal_svc(
        env,
        session,
        gfs_id="g1",
        spaces=["sp-a", "sp-noseed", "sp-gone"],
        space_repo=repo,
    )
    assert await svc.heal_space_pins("g1") == 1
    assert [u for u, _b in session.posts] == [
        "https://g1.example/gfs/spaces/sp-a/publish"
    ]


async def test_heal_space_pins_skips_everything_without_a_kek(env):
    """No household key manager wired → ``get_space_seed`` raises; the heal
    swallows it and publishes nothing rather than breaking the connect."""
    session = _AnonSession()
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {}, kek=False)
    svc = await _heal_svc(env, session, gfs_id="g1", spaces=["sp-a"], space_repo=repo)
    assert await svc.heal_space_pins("g1") == 0
    assert session.posts == []


async def test_heal_space_pins_never_raises_on_a_failing_gfs(env):
    """A GFS rejecting the re-publish is logged and skipped — the connect hook
    must never see an exception."""
    session = _AnonSession(status=503)
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {"sp-a": b"\x01" * 32})
    svc = await _heal_svc(env, session, gfs_id="g1", spaces=["sp-a"], space_repo=repo)
    assert await svc.heal_space_pins("g1") == 0


async def test_heal_space_pins_noops_for_unknown_or_inactive_gfs(env):
    """An unknown or non-active connection heals nothing and sends nothing."""
    session = _AnonSession()
    _db, conn_repo = env
    await conn_repo.save(_make_conn("g-sus", status="suspended"))
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {"sp-a": b"\x01" * 32})
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    assert await svc.heal_space_pins("nope") == 0
    assert await svc.heal_space_pins("g-sus") == 0
    assert session.posts == []


async def test_heal_space_pins_noops_without_publish_context(env):
    """No space repo wired → nothing to publish, no network."""
    session = _AnonSession()
    _db, conn_repo = env
    await conn_repo.save(_make_conn("g1"))
    await conn_repo.publish_space("sp-a", "g1")
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    assert await svc.heal_space_pins("g1") == 0
    assert session.posts == []


# ─── End-to-end against a REAL GFS (both-ways compatibility) ──────────────
#
# These boot an actual ``create_gfs_app`` behind an aiohttp ``TestServer``
# plus a recording "subscriber inbox" server, and drive the real household
# service against them: a current household + current GFS relay an
# identity-free frame, and the LEGACY body this same sender still emits for a
# GFS that never advertised ``anonymous_publish`` is accepted by the new GFS
# too — so the fallback can never strand a household.

_E2E_OWN_INSTANCE = "alpha.home"
_E2E_SUB_INSTANCE = "sub.home"
_E2E_SPACE_ID = "sp-e2e"


class _SeedSpaceRepo:
    """Space repo stand-in holding one publishable space + its authority seed."""

    def __init__(self, space, seed: bytes) -> None:
        self._space = space
        self._seed = seed

    async def get(self, space_id: str):
        return self._space if space_id == self._space.id else None

    async def get_space_seed(self, space_id: str) -> bytes | None:
        return self._seed if space_id == self._space.id else None


def _ws_hello(instance_id: str, seed: bytes) -> dict:
    """The signed ``hello`` a household opens its GFS push socket with."""
    ts = int(time.time())
    msg = f"{instance_id}|{ts}".encode("utf-8")
    return {
        "type": "hello",
        "instance_id": instance_id,
        "ts": ts,
        "sig": b64url_encode(sign_ed25519(seed, msg)),
    }


@pytest.fixture
async def real_gfs(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs-e2e.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


@pytest.fixture
async def e2e_sender(tmp_dir, real_gfs):
    """A real :class:`GfsConnectionService` wired against the real GFS.

    Registers the household + one subscriber on the GFS, publishes the space
    metadata through the REAL publish path (TOFU-pinning the space's authority
    key), subscribes the subscriber and holds its GFS push WebSocket open —
    the GFS stores no household address, so that socket is the only way a
    relay reaches it. Yields ``(svc, space_seed, subscriber_ws)``.
    """
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType

    fed_repo = real_gfs.server.app[gfs_fed_repo_key]

    household_kp = generate_identity_keypair()
    subscriber_kp = generate_identity_keypair()
    space_kp = generate_identity_keypair()

    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=_E2E_OWN_INSTANCE,
            display_name="Alpha",
            public_key=household_kp.public_key.hex(),
            status="active",
            auto_accept=True,
        )
    )
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=_E2E_SUB_INSTANCE,
            display_name="Sub",
            public_key=subscriber_kp.public_key.hex(),
            status="active",
            auto_accept=True,
        )
    )

    db = AsyncDatabase(tmp_dir / "hfs-e2e.db", batch_timeout_ms=10)
    await db.startup()
    conn_repo = SqliteGfsConnectionRepo(db)
    gfs_base = str(real_gfs.make_url("")).rstrip("/")
    await conn_repo.save(
        GfsConnection(
            id="gfs-e2e",
            gfs_instance_id="gfs-inst",
            display_name="E2E GFS",
            public_key="ab" * 32,
            inbox_url=gfs_base,
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        )
    )

    space = Space(
        id=_E2E_SPACE_ID,
        name="E2E Space",
        owner_instance_id=_E2E_OWN_INSTANCE,
        owner_username="alice",
        identity_public_key=space_kp.public_key.hex(),
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    async with aiohttp.ClientSession() as session:
        svc = GfsConnectionService(
            conn_repo, http_client=session, publish_client=session
        )
        svc.attach_publish_context(
            space_repo=_SeedSpaceRepo(space, space_kp.private_key),
            own_instance_id=_E2E_OWN_INSTANCE,
            own_signing_key=household_kp.private_key,
        )
        # Real metadata publish → the GFS TOFU-pins the space authority key,
        # which is the ONLY thing authorizing the anonymous relay below.
        await svc.publish_space(_E2E_SPACE_ID, "gfs-e2e")
        await fed_repo.add_subscriber(
            space_id=_E2E_SPACE_ID, instance_id=_E2E_SUB_INSTANCE
        )
        async with real_gfs.ws_connect("/gfs/ws") as sub_ws:
            await sub_ws.send_json(
                _ws_hello(_E2E_SUB_INSTANCE, subscriber_kp.private_key)
            )
            # The hello verifies asynchronously; wait until the GFS has the
            # socket registered or the relay below has nowhere to go.
            registry = real_gfs.server.app[gfs_ws_registry_key]
            for _ in range(100):
                if registry.is_connected(_E2E_SUB_INSTANCE):
                    break
                await asyncio.sleep(0.01)
            assert registry.is_connected(_E2E_SUB_INSTANCE)
            yield svc, space_kp.private_key, sub_ws
    await db.shutdown()


def _authority_payload(seed: bytes, *, post_id: str) -> dict:
    payload = {
        "space_id": _E2E_SPACE_ID,
        "epoch": 0,
        "post_id": post_id,
        "encrypted_payload": "Y2lwaGVy",
    }
    payload.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=_E2E_SPACE_ID,
            payload=payload,
            space_seed=seed,
        )
    )
    return payload


async def test_e2e_new_household_relays_identity_free_through_a_real_gfs(e2e_sender):
    """Current sender + current GFS: the publish carries no household identity,
    the GFS accepts it on the space-authority signature alone, and the frame it
    fans out to subscribers is identity-free too."""
    svc, seed, sub_ws = e2e_sender
    svc._anon_publish["gfs-e2e"] = True  # noqa: SLF001 — pin the capability
    delivered = await svc.publish_space_event(
        space_id=_E2E_SPACE_ID,
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        payload=_authority_payload(seed, post_id="p-anon"),
    )
    assert delivered == 1
    frame = await sub_ws.receive_json(timeout=5)
    assert set(frame) == {"type", "space_id", "event_type", "payload"}
    assert frame["type"] == "relay"
    assert frame["payload"]["post_id"] == "p-anon"
    assert _E2E_OWN_INSTANCE not in json.dumps(frame)


async def test_e2e_a_gfs_without_the_capability_gets_nothing_from_this_sender(
    e2e_sender,
):
    """With the capability cached as absent this sender sends NOTHING — it
    has no identified body to fall back to — so no subscriber is reached
    through that GFS until it proves ``anonymous_publish``. (The GFS still
    accepts, verifies and discards the legacy fields from OLDER households;
    that is covered in ``tests/global_server/``.)"""
    svc, seed, sub_ws = e2e_sender
    svc._anon_publish["gfs-e2e"] = False  # noqa: SLF001 — pin the capability
    delivered = await svc.publish_space_event(
        space_id=_E2E_SPACE_ID,
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        payload=_authority_payload(seed, post_id="p-legacy"),
    )
    assert delivered == 0
    with pytest.raises(TimeoutError):
        await sub_ws.receive_json(timeout=0.3)


async def test_e2e_pair_learns_anonymous_publish_from_a_real_signed_block(
    tmp_dir, real_gfs
):
    """Pairing against a REAL GFS learns ``anonymous_publish`` through the
    signed capability block, verified against the key pinned in that same
    response — and a later ``/gfs/info`` refresh keeps it.

    This is the only test that exercises ``sign_capabilities`` (GFS side) and
    ``verify_capabilities`` (household side) against each other over the wire,
    so it is what catches canonicalisation drift between the two packages.
    """
    token, _wait = await real_gfs.server.app["gfs_token_service"].generate("127.0.0.55")
    assert token is not None
    gfs_base = str(real_gfs.make_url("")).rstrip("/")

    db = AsyncDatabase(tmp_dir / "hfs-pair-e2e.db", batch_timeout_ms=10)
    await db.startup()
    try:
        conn_repo = SqliteGfsConnectionRepo(db)
        async with aiohttp.ClientSession() as session:
            svc = GfsConnectionService(
                conn_repo, http_client=session, publish_client=session
            )
            conn = await svc.pair(
                {"gfs_url": gfs_base, "token": token},
                own_instance_id=_E2E_OWN_INSTANCE,
                own_public_key_hex=generate_identity_keypair().public_key.hex(),
                own_display_name="Alpha House",
            )
            # Learned from the signed block, not the bare flag: the pinned key
            # verified the signature over {gfs_instance_id, capabilities}.
            assert svc._anon_publish[conn.id] is True  # noqa: SLF001
            # A refresh re-verifies the same block and must not downgrade.
            await svc.refresh_connection_metadata(conn.id)
            assert svc._anon_publish[conn.id] is True  # noqa: SLF001
    finally:
        await db.shutdown()


# ── envelope_relay capability (§D2b invite bootstrap) ──────────────────────


async def test_envelope_relay_supported_reads_the_signed_block(env):
    """Only the SIGNED capability block grants the relay — the same rule
    ``anonymous_publish`` follows, for the same reason."""
    _db, repo = env
    conn = _make_conn("er-1", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True, "envelope_relay": True},
        ),
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.envelope_relay_supported(conn) is True
    # Cached after the first probe — a redeem must not re-fetch /gfs/info.
    assert await svc.envelope_relay_supported(conn) is True
    assert len(session.gets) == 1


async def test_envelope_relay_absent_from_the_block_is_false(env):
    _db, repo = env
    conn = _make_conn("er-2", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True},
        ),
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.envelope_relay_supported(conn) is False


async def test_envelope_relay_from_a_stripped_block_is_false(env):
    """An unsigned flag is an on-path attacker's, not a capability."""
    _db, repo = env
    conn = _make_conn("er-3", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info={"server_name": "x", "envelope_relay": True})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.envelope_relay_supported(conn) is False


async def test_envelope_relay_unreachable_gfs_is_false_and_suppressed(env):
    """An unreachable probe answers ``False`` for this attempt and is not
    re-tried until the negative TTL lapses."""
    _db, repo = env
    conn = _make_conn("er-4", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(raise_on_get=True)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.envelope_relay_supported(conn) is False
    assert await svc.envelope_relay_supported(conn) is False
    assert len(session.gets) == 1


async def test_envelope_relay_known_is_warmed_by_an_info_fetch_without_probing(env):
    """``envelope_relay_known`` is cache-only: cold → False with no GET; any
    ``/gfs/info`` fetch (a WS reconnect's metadata refresh) that carries a
    SIGNED ``envelope_relay`` warms it."""
    _db, repo = env
    conn = _make_conn("er-5", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True, "envelope_relay": True},
        ),
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert svc.envelope_relay_known(conn) is False
    assert session.gets == []
    await svc.refresh_connection_metadata(conn.id)
    assert svc.envelope_relay_known(conn) is True
    # And the gate answers from the warm cache — no second fetch.
    assert await svc.envelope_relay_supported(conn) is True
    assert len(session.gets) == 1


def _relay_info(conn) -> dict:
    return _signed_info(
        gfs_instance_id=conn.gfs_instance_id,
        capabilities={"anonymous_publish": True, "envelope_relay": True},
    )


async def test_warm_capabilities_warms_a_cold_connection_after_a_restart(env):
    """After a restart the cache is cold; one startup pass warms it without
    waiting for the GFS socket, so the list reads ``envelope_relay`` true."""
    _db, repo = env
    conn = _make_conn("wc-1", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info=_relay_info(conn))
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert svc.envelope_relay_known(conn) is False

    assert await svc.warm_capabilities() == 1

    assert svc.envelope_relay_known(conn) is True
    assert len(session.gets) == 1
    # A second pass skips the warm connection: no network.
    assert await svc.warm_capabilities() == 1
    assert len(session.gets) == 1


async def test_warm_capabilities_skips_inactive_connections(env):
    _db, repo = env
    conn = _make_conn("wc-2", status="pending", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info=_relay_info(conn))
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)

    assert await svc.warm_capabilities() == 0
    assert session.gets == []


async def test_warm_capabilities_is_fail_soft_on_an_unreachable_gfs(env):
    _db, repo = env
    conn = _make_conn("wc-3", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(raise_on_get=True)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)

    assert await svc.warm_capabilities() == 0
    assert svc.envelope_relay_known(conn) is False


async def test_warm_capabilities_stops_before_the_next_fetch(env):
    _db, repo = env
    conn = _make_conn("wc-4", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info=_relay_info(conn))
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)

    assert await svc.warm_capabilities(should_stop=lambda: True) == 0
    assert session.gets == []


async def test_envelope_relay_known_ignores_an_unsigned_flag(env):
    _db, repo = env
    conn = _make_conn("er-6", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info={"server_name": "x", "envelope_relay": True})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    await svc.refresh_connection_metadata(conn.id)
    assert svc.envelope_relay_known(conn) is False


# ── invite links (§24.8.5) ────────────────────────────────────────────────


#: A plausible opaque invite blob. The household composes it; the connection
#: server never parses it.
_INVITE_BLOB = "eyJ0b2tlbiI6ICJhYmMxMjMifQ"


class _InviteSession(_AnonSession):
    """``_AnonSession`` whose POST answers like the invite mint/revoke routes."""

    def __init__(self, *, post_status: int = 201, post_body=None, **kw) -> None:
        super().__init__(**kw)
        self.post_status = post_status
        self.post_body = (
            {"gfs_token": "tok-1", "url": "https://gfs.example.com/join/tok-1"}
            if post_body is None
            else post_body
        )

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self.post_status, self.post_body)


def _invite_capable_info(conn):
    return _signed_info(
        gfs_instance_id=conn.gfs_instance_id,
        capabilities={"anonymous_publish": True, "invite_links": True},
    )


async def _invite_svc(repo, session, gfs_id: str):
    """A service with a signing identity + a saved, invite-capable GFS."""
    conn = _make_conn(gfs_id, public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    kp = generate_identity_keypair()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, conn, kp.public_key


async def test_publish_invite_signs_the_canonical_mint_body(env):
    """SECURITY: the mint is owner-authenticated on the GFS, so the body
    carries an Ed25519 signature over ``{action: "mint_invite",
    owning_instance, space_id, ts}`` — the ``action`` inside the signed bytes
    so it can never be replayed as a revoke."""
    _db, repo = env
    conn = _make_conn("inv-1", public_key=_GFS_KP.public_key.hex())
    session = _InviteSession(info=_invite_capable_info(conn))
    svc, conn, own_pub = await _invite_svc(repo, session, "inv-1")

    token, url = await svc.publish_invite("sp-i", "inv-1", _INVITE_BLOB, 1234567890)
    assert token == "tok-1"
    assert url == "https://gfs.example.com/join/tok-1"

    posted_url, body = session.posts[-1]
    assert posted_url == "https://gfs.example.com/gfs/spaces/sp-i/invite"
    assert body["blob"] == _INVITE_BLOB
    assert body["expires_at"] == 1234567890
    canonical = json.dumps(
        {
            "action": "mint_invite",
            "owning_instance": "alpha.home",
            "space_id": "sp-i",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert verify_ed25519(own_pub, canonical, b64url_decode(body["signature"]))


async def test_publish_invite_refuses_a_server_without_the_capability(env):
    """An older connection server has no ``/invite`` route and no ``/join``
    page — minting there would hand the owner a link that 404s for everyone
    they send it to."""
    _db, repo = env
    conn = _make_conn("inv-2", public_key=_GFS_KP.public_key.hex())
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True},
        ),
    )
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-2")
    with pytest.raises(GfsConnectionError, match="can't host invite links"):
        await svc.publish_invite("sp-i", "inv-2", _INVITE_BLOB, 1234567890)
    assert session.posts == []


async def test_publish_invite_refuses_an_unsigned_capability_block(env):
    """An unsigned flag is an on-path attacker's, not a capability."""
    _db, repo = env
    session = _InviteSession(info={"server_name": "x", "invite_links": True})
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-3")
    with pytest.raises(GfsConnectionError):
        await svc.publish_invite("sp-i", "inv-3", _INVITE_BLOB, 1234567890)


async def test_publish_invite_raises_without_a_signing_identity(env):
    _db, repo = env
    await repo.save(_make_conn("inv-4"))
    svc = GfsConnectionService(repo, http_client=_InviteSession())
    with pytest.raises(GfsConnectionError, match="signing identity"):
        await svc.publish_invite("sp-i", "inv-4", _INVITE_BLOB, 1234567890)


async def test_publish_invite_unknown_connection(env):
    _db, repo = env
    svc = GfsConnectionService(repo, http_client=_InviteSession())
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.publish_invite("sp-i", "nope", _INVITE_BLOB, 1234567890)


async def test_publish_invite_raises_on_a_rejection(env):
    _db, repo = env
    conn = _make_conn("inv-5", public_key=_GFS_KP.public_key.hex())
    session = _InviteSession(
        info=_invite_capable_info(conn),
        post_status=403,
        post_body={"error": "not the owner of this space"},
    )
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-5")
    with pytest.raises(GfsConnectionError, match="HTTP 403"):
        await svc.publish_invite("sp-i", "inv-5", _INVITE_BLOB, 1234567890)


async def test_publish_invite_raises_when_the_gfs_returns_no_token(env):
    """Without both fields there is nothing to share and nothing to revoke
    later — fail loudly rather than hand back a half-answer."""
    _db, repo = env
    conn = _make_conn("inv-6", public_key=_GFS_KP.public_key.hex())
    session = _InviteSession(info=_invite_capable_info(conn), post_body={})
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-6")
    with pytest.raises(GfsConnectionError, match="no invite token"):
        await svc.publish_invite("sp-i", "inv-6", _INVITE_BLOB, 1234567890)


async def test_publish_invite_maps_a_transport_error(env):
    _db, repo = env
    conn = _make_conn("inv-7", public_key=_GFS_KP.public_key.hex())

    class _Boom(_InviteSession):
        def post(self, url, **kw):
            raise aiohttp.ClientError("down")

    session = _Boom(info=_invite_capable_info(conn))
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-7")
    with pytest.raises(GfsConnectionError, match="Could not reach GFS"):
        await svc.publish_invite("sp-i", "inv-7", _INVITE_BLOB, 1234567890)


async def test_revoke_invite_signs_the_canonical_revoke_body(env):
    """SECURITY: the token is inside the signed bytes alongside the
    ``action``, so a revoke can be replayed neither as a mint nor against a
    different token."""
    _db, repo = env
    session = _InviteSession(post_status=204, post_body={})
    svc, _conn, own_pub = await _invite_svc(repo, session, "inv-8")

    await svc.revoke_invite("sp-i", "inv-8", "tok-9")

    posted_url, body = session.posts[-1]
    assert posted_url == "https://gfs.example.com/gfs/spaces/sp-i/invite/tok-9"
    canonical = json.dumps(
        {
            "action": "revoke_invite",
            "gfs_token": "tok-9",
            "owning_instance": "alpha.home",
            "space_id": "sp-i",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert verify_ed25519(own_pub, canonical, b64url_decode(body["signature"]))
    # The token never rides outside the signed bytes as a query parameter.
    assert "gfs_token" not in body


@pytest.mark.parametrize("status", [200, 204, 404])
async def test_revoke_invite_is_idempotent(env, status):
    """The link is gone either way — a revoke racing a sweep, or a second
    revoke, must not surface as an error."""
    _db, repo = env
    session = _InviteSession(post_status=status, post_body={})
    svc, _conn, _pub = await _invite_svc(repo, session, f"inv-r{status}")
    await svc.revoke_invite("sp-i", f"inv-r{status}", "tok-9")


async def test_revoke_invite_does_not_capability_gate(env):
    """A server that never had the route also never has the link; refusing
    here would strand an owner cleaning up after a downgrade."""
    _db, repo = env
    session = _InviteSession(
        info={"server_name": "old"},
        post_status=404,
        post_body={},
    )
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-9")
    await svc.revoke_invite("sp-i", "inv-9", "tok-9")
    assert session.gets == []  # no capability probe at all


async def test_revoke_invite_raises_on_a_rejection(env):
    _db, repo = env
    session = _InviteSession(post_status=403, post_body={"error": "nope"})
    svc, _conn, _pub = await _invite_svc(repo, session, "inv-10")
    with pytest.raises(GfsConnectionError, match="HTTP 403"):
        await svc.revoke_invite("sp-i", "inv-10", "tok-9")


async def test_revoke_invite_raises_without_a_signing_identity(env):
    _db, repo = env
    await repo.save(_make_conn("inv-11"))
    svc = GfsConnectionService(repo, http_client=_InviteSession())
    with pytest.raises(GfsConnectionError, match="signing identity"):
        await svc.revoke_invite("sp-i", "inv-11", "tok-9")


async def test_revoke_invite_unknown_connection(env):
    _db, repo = env
    svc = GfsConnectionService(repo, http_client=_InviteSession())
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.revoke_invite("sp-i", "nope", "tok-9")


async def test_revoke_invite_maps_a_transport_error(env):
    _db, repo = env

    class _Boom(_InviteSession):
        def post(self, url, **kw):
            raise aiohttp.ClientError("down")

    svc, _conn, _pub = await _invite_svc(repo, _Boom(), "inv-12")
    with pytest.raises(GfsConnectionError, match="Could not reach GFS"):
        await svc.revoke_invite("sp-i", "inv-12", "tok-9")


async def test_invite_links_capability_is_cached_after_one_probe(env):
    _db, repo = env
    conn = _make_conn("inv-13", public_key=_GFS_KP.public_key.hex())
    session = _InviteSession(info=_invite_capable_info(conn))
    svc, conn, _pub = await _invite_svc(repo, session, "inv-13")
    assert await svc.invite_links_supported(conn) is True
    assert await svc.invite_links_supported(conn) is True
    assert len(session.gets) == 1


async def test_invite_links_unreachable_gfs_is_false_and_suppressed(env):
    _db, repo = env
    session = _InviteSession(raise_on_get=True)
    svc, conn, _pub = await _invite_svc(repo, session, "inv-14")
    assert await svc.invite_links_supported(conn) is False
    assert await svc.invite_links_supported(conn) is False
    assert len(session.gets) == 1


async def test_client_exposes_the_shared_session(env):
    """Sibling GFS-facing services borrow this session rather than opening
    a second connection pool."""
    _db, repo = env
    session = _AnonSession()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert svc.client() is session


# ── v_44: the owner's publish carries its authority cert ─────────────────


async def _rotated_publish_svc(env, session):
    """An owner whose GLOBAL space's authority key rotated to epoch 1."""
    from socialhome.crypto import derive_instance_id
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
    from socialhome.infrastructure.key_manager import KeyManager
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("g1", public_key=_GFS_KP.public_key.hex()))
    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x05" * 32))
    await space_repo.save(
        Space(
            id="sp-rot",
            name="Rot",
            owner_instance_id=owner_id,
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    k2 = generate_identity_keypair()
    await space_repo.rotate_authority_key(
        "sp-rot", public_key_hex=k2.public_key.hex(), seed=k2.private_key, key_epoch=1
    )
    svc = GfsConnectionService(conn_repo, http_client=session, publish_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id=owner_id,
        own_signing_key=owner.private_key,
    )
    return svc, owner, owner_id, k2


async def test_publish_body_carries_the_owner_cert_after_a_rotation(env):
    """Once the space authority key rotated, the owner's publish to a GFS
    that advertises ``authority_rotation`` ships the cert (inside the signed
    body) so the GFS re-pins from it."""
    from socialhome.authority_cert import verify_authority_cert

    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id="inst-g1",
            capabilities={"anonymous_publish": True, "authority_rotation": True},
        )
    )
    svc, owner, owner_id, k2 = await _rotated_publish_svc(env, session)
    await svc.publish_space("sp-rot", "g1")
    body = dict(session.posts[-1][1])
    assert body["identity_public_key"] == k2.public_key.hex()
    got = verify_authority_cert(
        body["authority_cert"], space_id="sp-rot", owner_instance_id=owner_id
    )
    assert (got.authority_pk_hex, got.key_epoch) == (k2.public_key.hex(), 1)
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(owner.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_to_a_gfs_without_rotation_omits_the_cert_and_warns(env, caplog):
    """An older GFS signs only the fields it knows — a cert in the body would
    break its signature check and 403 every publish. So it gets the body
    without the cert (it keeps the old pin) and the household warns."""
    session = _AnonSession(info=_signed_info(gfs_instance_id="inst-g1"))
    svc, owner, _owner_id, k2 = await _rotated_publish_svc(env, session)
    with caplog.at_level(logging.WARNING):
        await svc.publish_space("sp-rot", "g1")
    body = dict(session.posts[-1][1])
    assert "authority_cert" not in body
    assert body["identity_public_key"] == k2.public_key.hex()
    assert any("authority_rotation" in m for m in _warnings(caplog))
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(owner.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_has_no_cert_before_any_rotation(env):
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = await _publishable_svc(env, session, "gfs-pub", space_id="sp-pub")
    await svc.publish_space("sp-pub", "gfs-pub")
    assert "authority_cert" not in session._last_body  # type: ignore[attr-defined]


async def test_republish_space_targets_only_the_gfs_it_is_published_to(env):
    """v_44 — after a rotation the owner re-publishes so each GFS re-pins,
    but ONLY where the space is already listed: a GFS that never heard of
    the space must not learn of it from a key rotation."""
    session = _AnonSession(info=_signed_info(gfs_instance_id="inst-g1"))
    svc, _owner, _owner_id, _k2 = await _rotated_publish_svc(env, session)
    _db, conn_repo = env
    await conn_repo.save(_make_conn("g2", inbox_url="https://other.example"))
    assert await svc.republish_space("sp-rot") == 0  # published nowhere yet
    assert not session.posts
    await svc.publish_space("sp-rot", "g1")
    session.posts.clear()
    assert await svc.republish_space("sp-rot") == 1
    assert [u for u, _b in session.posts] == [
        "https://gfs.example.com/gfs/spaces/sp-rot/publish"
    ]


# ─── publish_space_event retries (transient → retried, never identified) ──


class _ScriptedPublishSession:
    """aiohttp-session stub whose ``POST /gfs/publish`` answers from a
    script: an ``int`` status (with optional ``Retry-After``), or an
    exception to raise. ``GET /gfs/info`` returns ``info`` (or raises while
    ``info_down``). Every POST body is recorded as sent."""

    def __init__(self, script: list, *, info: dict | None, info_down=False) -> None:
        self.script = list(script)
        self.info = info
        self.info_down = info_down
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def get(self, url, **_kw):
        self.gets.append(url)
        if self.info_down:
            raise aiohttp.ClientError("info down")
        return _StubResp(200, self.info or {})

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json))
        nxt = self.script.pop(0) if self.script else 200
        if isinstance(nxt, Exception):
            raise nxt
        status, retry_after = nxt if isinstance(nxt, tuple) else (nxt, None)
        return _HeaderResp(status, retry_after)


class _HeaderResp(_StubResp):
    __slots__ = ()

    def __init__(self, status: int, retry_after: str | None) -> None:
        super().__init__(status, {"status": "published"})
        self.headers = {"Retry-After": retry_after} if retry_after else {}


@pytest.fixture
def fast_gfs_retry(monkeypatch):
    from socialhome.services import gfs_publish_retry

    monkeypatch.setattr(
        gfs_publish_retry, "GFS_PUBLISH_RETRY_BACKOFF_S", (0.0, 0.0, 0.0, 0.0)
    )


async def _publish_and_settle(svc, *, space_id: str, payload: dict) -> int:
    await svc.start()
    try:
        delivered = await svc.publish_space_event(
            space_id=space_id,
            event_type="space_post_public",
            payload=payload,
        )
        for _ in range(200):
            if not svc._publish_retry._queues:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()
    return delivered


@pytest.mark.parametrize(
    "first_failure",
    [aiohttp.ClientError("connection refused"), asyncio.TimeoutError(), 503, 408],
)
async def test_a_transient_gfs_publish_failure_is_retried_identically(
    env, fast_gfs_retry, first_failure
):
    """A network error, timeout, 5xx or 408 is not the end of the event: it
    is re-POSTed, and the retry body is byte-for-byte the first one —
    exactly ``{space_id, event_type, payload}``."""
    session = _ScriptedPublishSession([first_failure, 200], info=_signed_info())
    svc, _ = await _publish_event_svc(env, session, space_id="sp-r", gfs_ids=["g1"])
    envelope = {"space_id": "sp-r", "epoch": 0, "encrypted_payload": "ct"}

    await _publish_and_settle(svc, space_id="sp-r", payload=envelope)

    assert len(session.posts) == 2
    (url1, body1), (url2, body2) = session.posts
    assert url1 == url2 == "https://g1.example/gfs/publish"
    assert json.dumps(body1, sort_keys=True) == json.dumps(body2, sort_keys=True)
    assert set(body2) == {"space_id", "event_type", "payload"}
    assert body2["payload"] == envelope


@pytest.mark.parametrize("status", [400, 403, 404, 413, 422])
async def test_a_permanent_gfs_publish_failure_is_not_retried(
    env, fast_gfs_retry, status
):
    session = _ScriptedPublishSession([status], info=_signed_info())
    svc, _ = await _publish_event_svc(env, session, space_id="sp-p", gfs_ids=["g1"])

    await _publish_and_settle(svc, space_id="sp-p", payload={"space_id": "sp-p"})

    assert len(session.posts) == 1


async def test_a_429_gfs_publish_waits_its_retry_after(env, fast_gfs_retry):
    """The GFS's per-IP limiter answers 429 + ``Retry-After``: the retry is
    scheduled no earlier than that, not on the (zeroed) backoff."""
    session = _ScriptedPublishSession([(429, "60")], info=_signed_info())
    svc, _ = await _publish_event_svc(env, session, space_id="sp-t", gfs_ids=["g1"])
    before = time.monotonic()
    delivered = await svc.publish_space_event(
        space_id="sp-t",
        event_type="space_post_public",
        payload={"space_id": "sp-t"},
    )
    assert delivered == 0
    due_in = svc._publish_retry._queues["g1"].due_at - before
    assert 60.0 <= due_in < 61.0
    await svc.stop()


async def test_a_retry_to_a_gfs_without_anonymous_publish_is_dropped(
    env, fast_gfs_retry, caplog
):
    """The GFS was unreachable, so the publish waited; it comes back as an
    old build without ``anonymous_publish``. The retry is dropped — never
    re-sent as the identified body — and the GFS is named in a WARNING."""
    session = _ScriptedPublishSession(
        [], info={"server_name": "Old GFS"}, info_down=True
    )
    svc, _ = await _publish_event_svc(env, session, space_id="sp-o", gfs_ids=["g1"])
    await svc.publish_space_event(
        space_id="sp-o",
        event_type="space_post_public",
        payload={"space_id": "sp-o"},
    )
    session.info_down = False
    svc._info_failed_at.clear()
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await svc.start()
        try:
            for _ in range(200):
                if not svc._publish_retry._queues:
                    break
                await asyncio.sleep(0.01)
        finally:
            await svc.stop()
    assert session.posts == []
    assert "does not prove anonymous_publish" in caplog.text


async def test_a_later_publish_queues_behind_a_pending_retry(env, fast_gfs_retry):
    """Per GFS, events keep their order: while a retry is pending, a new
    publish to that GFS waits behind it instead of overtaking it."""
    session = _ScriptedPublishSession([503, 200, 200], info=_signed_info())
    svc, _ = await _publish_event_svc(env, session, space_id="sp-o", gfs_ids=["g1"])
    for n in (1, 2):
        await svc.publish_space_event(
            space_id="sp-o",
            event_type="space_post_public",
            payload={"space_id": "sp-o", "n": n},
        )
    assert len(session.posts) == 1  # the second one did not overtake
    await svc.start()
    try:
        for _ in range(200):
            if not svc._publish_retry._queues:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()
    assert [b["payload"]["n"] for _u, b in session.posts] == [1, 1, 2]


async def test_a_retry_skips_a_space_unpublished_meanwhile(env, fast_gfs_retry):
    session = _ScriptedPublishSession([503, 200], info=_signed_info())
    svc, _ = await _publish_event_svc(env, session, space_id="sp-u", gfs_ids=["g1"])
    await svc.publish_space_event(
        space_id="sp-u",
        event_type="space_post_public",
        payload={"space_id": "sp-u"},
    )
    _db, repo = env
    await repo.unpublish_space("sp-u", "g1")
    await svc.start()
    try:
        for _ in range(200):
            if not svc._publish_retry._queues:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()
    assert len(session.posts) == 1


# ─── No identified body, ever (the legacy fallback is gone) ──────────────


async def test_an_old_gfs_gets_no_publish_and_one_warning(env, caplog):
    """A reachable GFS without a signed ``anonymous_publish`` gets NOTHING —
    not the identified legacy body — and the skip is one WARNING per
    connection per process, naming the GFS, never the space."""
    session = _ScriptedPublishSession([], info={"server_name": "Old GFS"})
    svc, _ = await _publish_event_svc(env, session, space_id="sp-old", gfs_ids=["g1"])
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        for _ in range(3):
            assert (
                await svc.publish_space_event(
                    space_id="sp-old",
                    event_type="space_post_public",
                    payload={"space_id": "sp-old"},
                )
                == 0
            )
    assert session.posts == []
    skips = [m for m in _warnings(caplog) if "anonymous_publish" in m]
    assert len(skips) == 1
    assert "https://g1.example" in skips[0]
    assert "sp-old" not in skips[0]
    assert svc._publish_retry._queues == {}


async def test_a_cold_cache_with_the_gfs_down_never_yields_an_identified_body(
    env, fast_gfs_retry
):
    """Cold capability cache and ``/gfs/info`` unreachable: nothing is POSTed
    on the first attempt (no unknown → legacy fallback). The publish waits in
    the retry queue and goes out identity-free once the GFS proves the
    capability."""
    session = _ScriptedPublishSession([200], info=_signed_info(), info_down=True)
    svc, _ = await _publish_event_svc(env, session, space_id="sp-c", gfs_ids=["g1"])
    delivered = await svc.publish_space_event(
        space_id="sp-c",
        event_type="space_post_public",
        payload={"space_id": "sp-c"},
    )
    assert delivered == 0
    assert session.posts == []
    assert svc._publish_retry.pending("g1")
    session.info_down = False
    svc._info_failed_at.clear()
    await svc.start()
    try:
        for _ in range(200):
            if not svc._publish_retry._queues:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()
    assert len(session.posts) == 1
    body = session.posts[0][1]
    assert set(body) == {"space_id", "event_type", "payload"}
    assert "alpha.home" not in json.dumps(body)


def test_publish_space_event_takes_no_household_identity():
    """Structural: the relay API has no ``from_instance`` to leak."""
    params = inspect.signature(GfsConnectionService.publish_space_event).parameters
    assert "from_instance" not in params


async def test_a_stale_probe_failure_does_not_make_a_known_old_gfs_unknown(env):
    """A GFS already known to lack ``anonymous_publish`` whose later refresh
    failed once is still "unsupported" — not "unknown", which would park its
    publishes in the retry queue forever."""
    session = _ScriptedPublishSession([], info=_stripped_info())
    svc, _ = await _publish_event_svc(env, session, space_id="s1", gfs_ids=["g1"])
    (conn,) = await env[1].list_gfs_for_space("s1")
    await svc._fetch_gfs_info(conn)
    assert svc._anon_publish[conn.id] is False
    session.info_down = True
    await svc._fetch_gfs_info(conn)  # a blip on a reconnect-time refresh
    session.info_down = False
    assert await svc._publish_capability(conn) == "unsupported"
    await svc.publish_space_event(
        space_id="s1", event_type="space_post_public", payload={"x": 1}
    )
    assert not svc._publish_retry.pending(conn.id)
    assert session.posts == []


async def test_publishes_ride_only_the_publish_session(env):
    """The relay POST goes out on the cookie-less publish session, never on
    the shared one the household's authenticated GFS calls use — and with no
    publish session wired nothing is sent (no fallback)."""
    shared = _RecordingSession()
    publish = _RecordingSession()
    _db, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://g1.example"))
    await repo.publish_space("sp-s", "g1")
    for client in (publish, None):
        svc = GfsConnectionService(repo, http_client=shared, publish_client=client)
        svc.attach_publish_context(
            space_repo=None,
            own_instance_id="alpha.home",
            own_signing_key=generate_identity_keypair().private_key,
        )
        svc._anon_publish["g1"] = True
        await svc.publish_space_event(
            space_id="sp-s", event_type="space_post_public", payload={"x": 1}
        )
    assert len(publish.posts) == 1
    assert shared.posts == []


# ─── v_49: member publish capability + host dedupe skip ────────────────


async def test_member_publish_trusted_needs_the_signed_flag(env):
    _db, repo = env
    conn = _make_conn("mp-1", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True, "member_publish_trusted": True},
        )
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.member_publish_trusted_supported(conn)


async def test_member_publish_trusted_ignores_the_unsigned_flag(env):
    _db, repo = env
    conn = _make_conn("mp-2", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _InviteSession(info={"server_name": "x", "member_publish_trusted": True})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert not await svc.member_publish_trusted_supported(conn)


async def test_member_publish_strict_needs_the_signed_flag(env):
    _db, repo = env
    conn = _make_conn("mp-3", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={
                "member_publish_trusted": True,
                "member_publish_strict": True,
            },
        )
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.member_publish_strict_supported(conn)


async def test_member_publish_strict_ignores_the_unsigned_flag(env):
    _db, repo = env
    conn = _make_conn("mp-4", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _InviteSession(info={"server_name": "x", "member_publish_strict": True})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert not await svc.member_publish_strict_supported(conn)


async def test_member_publish_strict_is_absent_on_a_trusted_only_server(env):
    _db, repo = env
    conn = _make_conn("mp-5", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"member_publish_trusted": True},
        )
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.member_publish_trusted_supported(conn)
    assert not await svc.member_publish_strict_supported(conn)


async def test_private_channels_needs_the_signed_flag(env):
    """v_51: a private space's channel (and its members' identified seats)
    only against a server whose SIGNED block proves the contract."""
    _db, repo = env
    signed = _make_conn("pc-1", public_key=_GFS_KP.public_key.hex())
    unsigned = _make_conn("pc-2", public_key=_GFS_KP.public_key.hex())
    older = _make_conn("pc-3", public_key=_GFS_KP.public_key.hex())
    for conn in (signed, unsigned, older):
        await repo.save(conn)
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=signed.gfs_instance_id,
            capabilities={"private_channels": True},
        )
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert await svc.private_channels_supported(signed)
    session = _InviteSession(info={"server_name": "x", "private_channels": True})
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert not await svc.private_channels_supported(unsigned)
    session = _InviteSession(
        info=_signed_info(
            gfs_instance_id=older.gfs_instance_id,
            capabilities={"member_publish_strict": True},
        )
    )
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)
    assert not await svc.private_channels_supported(older)


async def _repin_svc(env, monkeypatch, *, with_cert: bool):
    _db, repo = env
    await repo.save(_make_conn("rp-1", inbox_url="https://rp.example"))
    session = _RecordingSession()
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)

    async def _body(self, space_id, *, with_cert=True):
        return {"name": "S", **({"authority_cert": {"k": 1}} if with_cert else {})}

    async def _supported(self, conn, name, cache):
        return True

    monkeypatch.setattr(GfsConnectionService, "_build_publish_body", _body)
    monkeypatch.setattr(
        GfsConnectionService, "_signed_capability_supported", _supported
    )
    calls: list[tuple[str, str]] = []

    async def _hook(space_id, gfs_id):
        calls.append((space_id, gfs_id))

    svc.attach_on_repinned(_hook)
    if not with_cert:

        async def _plain(self, space_id, *, with_cert=True):
            return {"name": "S"}

        monkeypatch.setattr(GfsConnectionService, "_build_publish_body", _plain)
    return svc, calls


async def test_a_re_pinning_publish_re_announces_the_epoch(env, monkeypatch):
    """v_49: whichever path makes a re-pin land (rotation, a later retry,
    the reconnect heal), the epoch notice follows right after it."""
    svc, calls = await _repin_svc(env, monkeypatch, with_cert=True)
    await svc.publish_space("sp-r", "rp-1")
    assert calls == [("sp-r", "rp-1")]


async def test_a_plain_publish_does_not_re_announce(env, monkeypatch):
    svc, calls = await _repin_svc(env, monkeypatch, with_cert=False)
    await svc.publish_space("sp-r", "rp-1")
    assert calls == []


async def test_a_failing_re_announce_never_fails_the_publish(env, monkeypatch):
    svc, _calls = await _repin_svc(env, monkeypatch, with_cert=True)

    async def _boom(space_id, gfs_id):
        raise RuntimeError("down")

    svc.attach_on_repinned(_boom)
    pub = await svc.publish_space("sp-r", "rp-1")
    assert pub.space_id == "sp-r"


@pytest.mark.security
@pytest.mark.parametrize("space_type", ["private", "household"])
async def test_publish_refuses_a_space_outside_the_public_tiers(env, space_type):
    """A private or household space never reaches a GFS directory: refused
    before any request is built, and no local publication row is written."""
    session = _StubSession(status=200)
    svc = await _publishable_svc(
        env, session, "gfs-1", space_id="sp-priv", space_type=space_type
    )
    with pytest.raises(SpaceNotPublishableError) as exc_info:
        await svc.publish_space("sp-priv", "gfs-1")
    assert exc_info.value.status == 409
    assert exc_info.value.code == "SPACE_NOT_PUBLIC"
    assert session.calls == []
    _, repo = env
    assert await repo.list_publications("gfs-1") == []


async def test_publish_allows_a_public_space(env):
    session = _StubSession(status=200)
    svc = await _publishable_svc(
        env, session, "gfs-1", space_id="sp-pub", space_type="public"
    )
    pub = await svc.publish_space("sp-pub", "gfs-1")
    assert pub.space_id == "sp-pub"
    assert len(session.calls) == 1


async def test_publish_to_all_and_republish_skip_a_private_space(env):
    """The fan-out loops log and skip the refusal instead of raising, so a
    space that turned private can't break an authority-key rotation."""
    session = _StubSession(status=200)
    svc = await _publishable_svc(
        env, session, "gfs-1", space_id="sp-priv", space_type="private"
    )
    assert await svc.publish_space_to_all("sp-priv") == 0
    assert await svc.republish_space("sp-priv") == 0
    assert session.calls == []


async def test_unpublish_from_listed_only_contacts_listing_gfs(env):
    """A GFS that never listed the space must not learn its id from the
    withdrawal."""
    session = _StubSession(status=200)
    svc = await _publishable_svc(
        env, session, "gfs-1", space_id="sp-pub", space_type="public"
    )
    _, repo = env
    await repo.save(_make_conn("gfs-2", inbox_url="https://other.example.com"))
    await svc.publish_space("sp-pub", "gfs-1")
    session.calls.clear()
    assert await svc.unpublish_space_from_listed("sp-pub") == 1
    assert len(session.calls) == 1
    assert "gfs.example.com" in session.calls[0][1]
    assert all("other.example.com" not in c[1] for c in session.calls)


async def test_disconnect_sends_nothing(env):
    """H1 (round 3): unpairing is local only — the household's GFS seats
    stay (a re-pair of the same server re-takes them), and the DELETE
    route never waits on the network."""
    _, repo = env
    session = _StubSession()
    await repo.save(_make_conn("rm-2"))
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.disconnect("rm-2")
    assert await repo.get("rm-2") is None
    assert session.calls == []


async def test_the_same_server_at_a_differently_spelled_url_is_not_paired_twice(env):
    """L2: duplicate detection uses the seat binding's address
    normalization (case, default port, trailing slash)."""
    _, repo = env
    await repo.save(_make_conn("dup-1"))
    existing = (await repo.get("dup-1")).inbox_url
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    spelled = existing.upper().replace("HTTPS://", "https://") + ":443/"
    with pytest.raises(GfsSignupError) as exc:
        await svc._require_new_gfs_url(spelled)
    assert exc.value.reason == "already_connected"


# ─── Rebind: the pinned key is the trust anchor, the id is a label ───────


def _pinned_conn(conn_id: str = "g1", *, instance_id: str = "gfs-0") -> GfsConnection:
    """A connection that pinned ``_GFS_KP`` under an old per-node id."""
    return GfsConnection(
        id=conn_id,
        gfs_instance_id=instance_id,
        display_name="GFS g1",
        public_key=_GFS_KP.public_key.hex(),
        inbox_url="https://gfs.example.com",
        status="active",
        paired_at="2025-01-01T00:00:00+00:00",
    )


def _served(
    instance_id: str = "gfs-shared",
    *,
    kp=None,
    served_key: str | None = None,
    replaces: tuple[str, ...] | None = ("gfs-0", "gfs-1"),
) -> dict:
    """``/gfs/info`` serving *instance_id*, its block signed by *kp*'s key
    over that id (listing the former ids it *replaces*; ``None`` = an old
    per-node-id server that lists none), and *served_key* (default: *kp*'s)
    as ``public_key``."""
    signer = kp or _GFS_KP
    caps: dict = {"anonymous_publish": True, "private_channels": True}
    if replaces is not None:
        caps["replaces"] = list(replaces)
    info = _signed_info(gfs_instance_id=instance_id, kp=signer, capabilities=caps)
    info["gfs_instance_id"] = instance_id
    info["public_key"] = (
        served_key if served_key is not None else signer.public_key.hex()
    )
    return info


def _logs(caplog, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == level and r.name == "socialhome.services.gfs_connection_service"
    ]


def _rebind_svc(repo, info: dict) -> tuple[GfsConnectionService, list[str]]:
    session = _AnonSession(info=info)
    svc = GfsConnectionService(repo, http_client=session, publish_client=session)  # type: ignore[arg-type]
    rebound: list[str] = []

    async def hook(conn_id: str) -> None:
        rebound.append(conn_id)

    svc.attach_on_rebound(hook)
    return svc, rebound


async def test_a_new_id_under_the_pinned_key_is_adopted_on_reconnect(env, caplog):
    _, repo = env
    await repo.save(_pinned_conn())
    svc, rebound = _rebind_svc(repo, _served())
    with caplog.at_level(logging.INFO):
        await svc.refresh_connection_metadata("g1")
    got = await repo.get("g1")
    assert got is not None and got.gfs_instance_id == "gfs-shared"
    assert rebound == ["g1"]
    assert any("gfs-0" in m and "gfs-shared" in m for m in _logs(caplog, logging.INFO))
    # The capability block verified under (pinned key, new id): trusted.
    assert svc._anon_publish["g1"] is True
    assert _logs(caplog, logging.WARNING) == []
    # The ids the server signed that it replaces (grants issued before).
    assert svc.known_instance_ids(got) == frozenset({"gfs-shared", "gfs-0", "gfs-1"})


async def test_a_stale_connection_object_still_verifies_after_the_rebind(env):
    """A caller holding the row read before the rebind (old id) still gets
    the capability verified — the block is checked under the pinned key."""
    _, repo = env
    stale = _pinned_conn()
    await repo.save(stale)
    svc, _rebound = _rebind_svc(repo, _served())
    assert await svc.private_channels_supported(stale) is True
    assert (await repo.get("g1")).gfs_instance_id == "gfs-shared"


@pytest.mark.security
async def test_a_new_id_under_another_key_is_refused_with_a_warning(env, caplog):
    """The served key is not the pinned one: nothing is adopted, nothing
    from that block is trusted — re-pairing is the only way to a new key."""
    _, repo = env
    await repo.save(_pinned_conn())
    svc, rebound = _rebind_svc(repo, _served(kp=generate_identity_keypair()))
    with caplog.at_level(logging.WARNING):
        await svc.refresh_connection_metadata("g1")
        await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-0"
    assert rebound == []
    assert svc._anon_publish.get("g1") is False
    key_warnings = [
        m
        for m in _logs(caplog, logging.WARNING)
        if "pinned key" in m and "gfs-shared" in m
    ]
    # Once per connection, not per reconnect.
    assert len(key_warnings) == 1


@pytest.mark.security
async def test_the_pinned_key_served_beside_a_block_it_did_not_sign_is_refused(env):
    """The served ``public_key`` claims the pinned key, but the block over the
    new id is signed by another key: no rebind."""
    _, repo = env
    await repo.save(_pinned_conn())
    svc, rebound = _rebind_svc(
        repo,
        _served(kp=generate_identity_keypair(), served_key=_GFS_KP.public_key.hex()),
    )
    await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-0"
    assert rebound == []
    assert svc._anon_publish.get("g1") is False


async def test_an_unsigned_descriptor_with_a_new_id_is_not_adopted(env):
    _, repo = env
    await repo.save(_pinned_conn())
    svc, rebound = _rebind_svc(
        repo,
        {
            "server_name": "GFS g1",
            "gfs_instance_id": "gfs-shared",
            "public_key": _GFS_KP.public_key.hex(),
        },
    )
    await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-0"
    assert rebound == []


async def test_a_rebind_onto_an_id_another_connection_holds_keeps_both(env, caplog):
    """The same server paired twice (two rows, one key): the UNIQUE id
    cannot be given to both — both rows stay as they are, with a WARNING."""
    _, repo = env
    await repo.save(_pinned_conn("g1", instance_id="gfs-0"))
    await repo.save(_pinned_conn("g2", instance_id="gfs-shared"))
    svc, rebound = _rebind_svc(repo, _served())
    with caplog.at_level(logging.WARNING):
        await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-0"
    assert (await repo.get("g2")).gfs_instance_id == "gfs-shared"
    assert rebound == []
    assert any("paired twice" in m for m in _logs(caplog, logging.WARNING))


async def test_the_same_id_is_no_rebind(env):
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-shared"))
    svc, rebound = _rebind_svc(repo, _served())
    await svc.refresh_connection_metadata("g1")
    assert rebound == []
    assert svc.known_instance_ids(await repo.get("g1")) == frozenset(
        {"gfs-shared", "gfs-0", "gfs-1"}
    )


async def test_a_failing_rebind_hook_does_not_fail_the_refresh(env):
    _, repo = env
    await repo.save(_pinned_conn())
    svc, _rebound = _rebind_svc(repo, _served())

    async def hook(conn_id: str) -> None:
        raise RuntimeError("boom")

    svc.attach_on_rebound(hook)
    await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-shared"


# ── Rebind is gated on the server's signed ``replaces`` (no lateral moves) ──


async def test_a_lateral_id_under_the_pinned_key_never_moves_the_pin(env, caplog):
    """Today's per-node-id cluster: every node signs its OWN id under the
    shared key, none ``replaces`` the pinned one — a household must not
    flip between them on each reconnect."""
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-1"))
    svc, rebound = _rebind_svc(repo, _served("gfs-2", replaces=None))
    with caplog.at_level(logging.INFO):
        for node in ("gfs-2", "gfs-3", "gfs-0"):
            svc._http_client.info = _served(node, replaces=None)  # type: ignore[union-attr]
            await svc.refresh_connection_metadata("g1")
    assert (await repo.get("g1")).gfs_instance_id == "gfs-1"
    assert rebound == []
    assert _logs(caplog, logging.WARNING) == []
    # The capability block still verifies (same key): publishing works.
    assert svc._anon_publish["g1"] is True


async def test_a_mixed_cluster_never_flips_and_converges_on_the_shared_id(env):
    """Rolling deploy: old per-node nodes (gfs-k, no ``replaces``) beside
    new ones (gfs-shared, replacing gfs-0..3). The household moves exactly
    once — the first time it reaches a new node — and never back."""
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-1"))
    shared = _served("gfs-shared", replaces=("gfs-0", "gfs-1", "gfs-2", "gfs-3"))
    svc, rebound = _rebind_svc(repo, _served("gfs-2", replaces=None))
    seen: list[str] = []
    for info in (
        _served("gfs-2", replaces=None),
        _served("gfs-3", replaces=None),
        shared,
        _served("gfs-0", replaces=None),
        _served("gfs-2", replaces=None),
        shared,
    ):
        svc._http_client.info = info  # type: ignore[union-attr]
        await svc.refresh_connection_metadata("g1")
        seen.append((await repo.get("g1")).gfs_instance_id)
    assert seen == ["gfs-1", "gfs-1"] + ["gfs-shared"] * 4
    assert rebound == ["g1"]


async def test_grants_match_the_signed_replaces_list_after_a_restart(env):
    """L1: the former ids come from the server's signed ``replaces`` on each
    verified fetch — a fresh process (no memory of the rebind) still matches
    a grant naming the old id."""
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-shared"))
    svc, _rebound = _rebind_svc(repo, _served())
    conn = await repo.get("g1")
    assert svc.known_instance_ids(conn) == frozenset({"gfs-shared"})
    await svc.refresh_connection_metadata("g1")
    assert svc.known_instance_ids(conn) == frozenset({"gfs-shared", "gfs-0", "gfs-1"})


async def test_a_lateral_nodes_replaces_list_is_ignored(env):
    """A block served under ANOTHER id than the connection's never feeds
    its ``replaces`` into grant matching."""
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-shared"))
    svc, _rebound = _rebind_svc(repo, _served("gfs-other", replaces=("gfs-9",)))
    await svc.refresh_connection_metadata("g1")
    assert svc.known_instance_ids(await repo.get("g1")) == frozenset({"gfs-shared"})


async def test_refresh_all_metadata_visits_every_active_connection(env):
    _, repo = env
    await repo.save(_pinned_conn("g1", instance_id="gfs-0"))
    await repo.save(
        GfsConnection(
            id="g2",
            gfs_instance_id="other",
            display_name="x",
            public_key="ab",
            inbox_url="https://other.test",
            status="pending",
            paired_at="2025-01-01T00:00:00+00:00",
        )
    )
    svc, rebound = _rebind_svc(repo, _served())
    assert await svc.refresh_all_metadata() == 1
    assert rebound == ["g1"]
    assert await svc.refresh_all_metadata(should_stop=lambda: True) == 0


async def test_the_addressee_key_is_bound_only_once_the_server_proves_it(env):
    """L2: ``addressee_key_for`` answers the PINNED key only after a verified
    block says the server takes ``gfs_key`` — an older server would refuse
    the unknown field."""
    _, repo = env
    await repo.save(_pinned_conn(instance_id="gfs-shared"))
    conn = await repo.get("g1")
    svc, _rebound = _rebind_svc(repo, _served())
    await svc.refresh_connection_metadata("g1")
    assert svc.addressee_key_for(conn) is None
    info = _signed_info(
        gfs_instance_id="gfs-shared",
        capabilities={"anonymous_publish": True, "addressee_key": True},
    )
    info.update(gfs_instance_id="gfs-shared", public_key=_GFS_KP.public_key.hex())
    svc._http_client.info = info  # type: ignore[union-attr]
    await svc.refresh_connection_metadata("g1")
    assert svc.addressee_key_for(conn) == _GFS_KP.public_key.hex()
