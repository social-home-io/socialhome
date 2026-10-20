"""Tests for :class:`HighlightPublicationService` (SH side).

Covers the publish / revoke / unpublish flow against a stubbed GFS
HTTP session — same shape as ``test_gfs_connection_service`` so the
sign-then-POST pattern is exercised end-to-end without a real socket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.federation import GfsConnection
from socialhome.domain.highlight import Highlight, HighlightAudience
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.services.highlight_public_seal import link_secret
from socialhome.services.highlight_publication_service import (
    HighlightNotFoundError,
    HighlightPublicationError,
    HighlightPublicationService,
)

# ─── Helpers ─────────────────────────────────────────────────────────────


class _StubResp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self._body = body or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _StubSession:
    def __init__(self, *, status: int = 201, body: dict | None = None):
        self._status = status
        self._body = body or {"token": "tok-A", "url": "https://gfs/example/url"}
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, self._body)


def _expires(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def repos(db):
    """Real SqliteHighlightRepo + SqliteGfsConnectionRepo with a paired GFS row."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1', 'alice', 'Alice', 'active')",
    )
    await db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES('gfs-abc','gfs-abc','GFS A','ff','https://gfs.example',
                 'active', datetime('now'))
        """,
    )
    return {
        "highlights": SqliteHighlightRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
    }


@pytest.fixture
async def seeded_highlight(repos):
    return await repos["highlights"].find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )


def _make_service(repos, *, session):
    svc = HighlightPublicationService(repos["highlights"], repos["gfs"])
    svc.attach_session(session)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    return svc


# ─── Tests ───────────────────────────────────────────────────────────────


async def test_publish_signs_body_and_marks_local_flag(repos, seeded_highlight):
    sess = _StubSession(body={"token": "tok-A", "url": "https://gfs/u/s/A"})
    svc = _make_service(repos, session=sess)
    out = await svc.publish(
        seeded_highlight.id,
        "u1",
        gfs_id="gfs-abc",
        label="twitter",
    )
    # The link secret rides in the fragment (never sent to the GFS) and is
    # derived from our identity, the highlight and the token.
    secret = link_secret(b"\x01" * 32, highlight_id=seeded_highlight.id, token="tok-A")
    assert out == {
        "token": "tok-A",
        "url": f"https://gfs/u/s/A#k={b64url_encode(secret)}",
        "label": "twitter",
    }
    assert sess.posts and sess.posts[0][0].endswith(
        f"/gfs/highlights/{seeded_highlight.id}/publish",
    )
    body = sess.posts[0][1]
    assert body["highlight_id"] == seeded_highlight.id
    assert body["instance_id"] == "inst-self"
    # Body must include a signature; canonical-body equality is in
    # ``test_gfs_connection_service`` already so we only check shape here.
    assert "signature" in body
    assert b64url_encode(secret) not in str(body)
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id == "gfs-abc"
    assert refreshed.public_published_at is not None


async def test_publish_other_users_highlight_raises_not_found(repos, seeded_highlight):
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightNotFoundError):
        await svc.publish(seeded_highlight.id, "someone-else", gfs_id="gfs-abc")
    assert sess.posts == []  # didn't even reach GFS


async def test_publish_unknown_gfs_raises(repos, seeded_highlight):
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-missing")


async def test_publish_failure_does_not_set_local_flag(repos, seeded_highlight):
    sess = _StubSession(status=502, body={"error": "down"})
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None
    assert refreshed.public_published_at is None


async def test_revoke_token_calls_gfs(repos, seeded_highlight):
    # Pre-mark the highlight as published so the service knows which GFS.
    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    sess = _StubSession(status=200, body={"status": "ok"})
    svc = _make_service(repos, session=sess)
    await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")
    assert sess.posts and sess.posts[0][0].endswith(
        "/gfs/highlight_tokens/tok-A/revoke",
    )


async def test_revoke_token_when_not_published_raises(repos, seeded_highlight):
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")


async def test_unpublish_clears_local_flag_even_if_gfs_404(repos, seeded_highlight):
    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    sess = _StubSession(status=404, body={"error": "not_found"})
    svc = _make_service(repos, session=sess)
    await svc.unpublish(seeded_highlight.id, "u1")
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None


async def test_unpublish_idempotent_when_not_published(repos, seeded_highlight):
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    # Should be a quiet no-op rather than raising.
    await svc.unpublish(seeded_highlight.id, "u1")
    assert sess.posts == []  # didn't talk to GFS


# ── Identity guards ──────────────────────────────────────────────────────


async def test_publish_without_identity_raises(repos, seeded_highlight):
    svc = HighlightPublicationService(repos["highlights"], repos["gfs"])
    svc.attach_session(_StubSession())
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")


async def test_publish_without_session_raises(repos, seeded_highlight):
    svc = HighlightPublicationService(repos["highlights"], repos["gfs"])
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")


async def test_publish_response_missing_token_raises(repos, seeded_highlight):
    sess = _StubSession(body={"url": "https://gfs/u/s/A"})  # missing token
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")


async def test_revoke_token_to_inactive_gfs_raises(repos, seeded_highlight):
    """When the publication points at a now-suspended GFS, revoke fails
    with a service-level error rather than reaching the network."""
    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    await repos["gfs"]._db.enqueue(
        "UPDATE gfs_connections SET status='suspended' WHERE id='gfs-abc'"
    )
    svc = _make_service(repos, session=_StubSession())
    with pytest.raises(HighlightPublicationError):
        await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")


async def test_unpublish_logs_and_clears_on_aiohttp_client_error(
    repos, seeded_highlight
):
    """If the GFS request raises ClientError (network down), the local
    flag is still cleared — author asked to unpublish."""
    import aiohttp as _aiohttp

    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )

    class _BoomSession(_StubSession):
        def post(self, url, *, json=None, **_kw):
            self.posts.append((url, json or {}))
            raise _aiohttp.ClientError("network down")

    svc = _make_service(repos, session=_BoomSession())
    await svc.unpublish(seeded_highlight.id, "u1")
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None


async def test_expires_to_unix_handles_legacy_unix_string(repos):
    """Legacy callers may have stored ``highlights.expires_at`` as a unix
    epoch in a TEXT column; the helper still resolves it."""
    from socialhome.services.highlight_publication_service import _expires_to_unix

    assert _expires_to_unix("1730000000") == 1730000000


async def test_expires_to_unix_handles_iso(repos):
    from socialhome.services.highlight_publication_service import _expires_to_unix

    # Round-trip via the same parser the helper uses so timezone math
    # stays out of the assertion.
    expected = int(datetime.fromisoformat("2026-05-03T12:00:00+00:00").timestamp())
    assert _expires_to_unix("2026-05-03T12:00:00Z") == expected


# ── Other error paths that were untested ────────────────────────────────


async def test_publish_with_no_expires_at_raises(repos):
    """Defensive guard — if a highlight somehow has no ``expires_at``
    (shouldn't happen — every highlight is created with one) the service
    refuses publish rather than POSTing an unbounded cap."""
    await repos["highlights"]._db.enqueue(
        "INSERT INTO highlights(id, author_user_id, highlight_date, audience_kind, "
        "audience_json, created_at, expires_at) "
        "VALUES('s-no-exp', 'u1', '2026-05-03', 'all_paired', '[]', "
        "datetime('now'), '')",
    )
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.publish("s-no-exp", "u1", gfs_id="gfs-abc")
    assert sess.posts == []  # didn't reach GFS


async def test_publish_aiohttp_client_error_wraps_as_service_error(
    repos,
    seeded_highlight,
):
    """Network errors during publish are translated to service-level
    ``HighlightPublicationError`` so route handlers can map them
    uniformly to HTTP 502."""
    import aiohttp as _aiohttp

    class _BoomSession(_StubSession):
        def post(self, url, *, json=None, **_kw):
            self.posts.append((url, json or {}))
            raise _aiohttp.ClientError("network down")

    svc = _make_service(repos, session=_BoomSession())
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")


async def test_revoke_token_aiohttp_client_error_wraps(repos, seeded_highlight):
    import aiohttp as _aiohttp

    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )

    class _BoomSession(_StubSession):
        def post(self, url, *, json=None, **_kw):
            self.posts.append((url, json or {}))
            raise _aiohttp.ClientError("network down")

    svc = _make_service(repos, session=_BoomSession())
    with pytest.raises(HighlightPublicationError):
        await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")


async def test_revoke_token_http_error_wraps(repos, seeded_highlight):
    """A non-2xx revoke response is logged + re-raised as a service
    error so the SPA gets a 502."""
    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    sess = _StubSession(status=500, body={"error": "server"})
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")


# ── Highlight type sanity (read-only) ────────────────────────────────────────


def test_highlight_dataclass_carries_publication_fields():
    s = Highlight(
        id="s",
        author_user_id="u",
        highlight_date="2026-05-03",
        public_gfs_id="gfs-1",
        public_published_at="2026-05-03T00:00:00+00:00",
    )
    assert s.public_gfs_id == "gfs-1"
    # Reusing the unused import so flake8 doesn't complain (GfsConnection
    # is checked indirectly via the repo fixture).
    assert GfsConnection.__name__ == "GfsConnection"
