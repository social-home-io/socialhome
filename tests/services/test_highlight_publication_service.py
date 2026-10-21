"""Tests for :class:`HighlightPublicationService` (SH side).

Covers the publish / revoke / unpublish flow against a stubbed GFS
HTTP session — same shape as ``test_gfs_connection_service`` so the
sign-then-POST pattern is exercised end-to-end without a real socket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.errors import CodedError
from socialhome.domain.federation import GfsConnection
from socialhome.domain.highlight import Highlight, HighlightAudience
from socialhome.pinned_viewer import PinnedViewer, load_pinned_viewer
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.services.highlight_public_seal import (
    MAX_TOKEN_LEN,
    link_secret,
    token_digest,
)
from socialhome.services.highlight_publication_service import (
    GfsTooOldError,
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


class _Caps:
    """What :class:`GfsConnectionService` answers: did this GFS prove, under
    a valid signature, that its viewer page opens the sealed stream?"""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.asked: list[str] = []

    async def sealed_highlight_stream_supported(self, conn) -> bool:
        self.asked.append(conn.id)
        return self.supported


_PINNED = PinnedViewer(path="v/0123456789abcdef/", integrity="sha384-" + "A" * 64)


def _make_service(
    repos,
    *,
    session,
    caps: _Caps | None = None,
    viewer_base: str = "",
    pinned: PinnedViewer | None = None,
):
    svc = HighlightPublicationService(
        repos["highlights"],
        repos["gfs"],
        gfs_capabilities=caps or _Caps(),
        public_viewer_base_url=viewer_base,
        pinned_viewer=pinned,
    )
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


async def test_publish_mints_a_pinned_viewer_link(repos, seeded_highlight):
    """With a viewer base URL, the link opens the pinned page (no GFS serves
    it) and carries the GFS address, ids, token and secret in its fragment."""
    sess = _StubSession(body={"token": "tok-A", "url": "https://gfs/u/s/A"})
    svc = _make_service(
        repos,
        session=sess,
        viewer_base="https://viewer.example/socialhome/",
        pinned=_PINNED,
    )
    out = await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    secret = link_secret(b"\x01" * 32, highlight_id=seeded_highlight.id, token="tok-A")
    page, fragment = out["url"].split("#", 1)
    assert page == "https://viewer.example/socialhome/v/0123456789abcdef/"
    # ``g`` is the GFS address this household talks to (the pinned
    # connection's inbox_url), not whatever URL the GFS returned.
    assert dict(parse_qsl(fragment)) == {
        "g": "https://gfs.example",
        "i": "inst-self",
        "h": seeded_highlight.id,
        "t": "tok-A",
        "k": b64url_encode(secret),
    }
    assert b64url_encode(secret) not in str(sess.posts)


async def test_publish_falls_back_to_the_gfs_page_without_a_viewer_base(
    repos, seeded_highlight
):
    """``public_viewer_base_url = ""``: the GFS-served page with ``#k`` (the
    self-hosted, less safe mode)."""
    sess = _StubSession(body={"token": "tok-A", "url": "https://gfs/u/s/A"})
    svc = _make_service(repos, session=sess, viewer_base="", pinned=_PINNED)
    out = await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    secret = link_secret(b"\x01" * 32, highlight_id=seeded_highlight.id, token="tok-A")
    assert out["url"] == f"https://gfs/u/s/A#k={b64url_encode(secret)}"


async def test_publish_falls_back_without_a_manifest(repos, seeded_highlight):
    sess = _StubSession(body={"token": "tok-A", "url": "https://gfs/u/s/A"})
    svc = _make_service(
        repos, session=sess, viewer_base="https://v.example", pinned=None
    )
    out = await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    assert out["url"].startswith("https://gfs/u/s/A#k=")


async def test_publish_falls_back_when_the_viewer_would_refuse_the_gfs_url(
    db, repos, seeded_highlight
):
    """A GFS reached over plain http on a non-localhost host (a LAN or
    container address) can't be named in a pinned link: the viewer refuses
    it, so the GFS-served page is minted instead of a dead link."""
    await db.enqueue(
        "UPDATE gfs_connections SET inbox_url = ? WHERE id = ?",
        ("http://gfs:8765", "gfs-abc"),
    )
    sess = _StubSession(body={"token": "tok-A", "url": "http://gfs:8765/h/A"})
    svc = _make_service(
        repos, session=sess, viewer_base="https://v.example", pinned=_PINNED
    )
    out = await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    assert out["url"].startswith("http://gfs:8765/h/A#k=")


async def test_publish_remembers_the_minted_token(repos, seeded_highlight):
    """Only a token we minted is ever streamed — the household keeps its
    SHA-256, never the bearer token itself."""
    svc = _make_service(repos, session=_StubSession())
    await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    highlights = repos["highlights"]
    assert await highlights.is_live_public_token(
        seeded_highlight.id, token_digest("tok-A")
    )
    rows = await highlights._db.fetchall("SELECT * FROM highlight_public_tokens")
    assert "tok-A" not in str([dict(r) for r in rows])


async def test_publish_refuses_a_gfs_that_has_not_proved_the_sealed_viewer(
    repos, seeded_highlight
):
    """Rollout gate: an older GFS's viewer page can't open the sealed stream,
    so a link minted there would be dead for every viewer."""
    sess = _StubSession()
    caps = _Caps(supported=False)
    svc = _make_service(repos, session=sess, caps=caps)
    with pytest.raises(GfsTooOldError) as exc:
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    assert caps.asked == ["gfs-abc"]
    assert sess.posts == []  # nothing minted on that GFS
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None
    # A coded refusal the SPA translates.
    assert isinstance(exc.value, CodedError)
    assert (exc.value.status, exc.value.code) == (409, "GFS_TOO_OLD")


async def test_publish_without_the_capability_check_wired_refuses(
    repos, seeded_highlight
):
    sess = _StubSession()
    svc = HighlightPublicationService(repos["highlights"], repos["gfs"])
    svc.attach_session(sess)
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    assert sess.posts == []


async def test_publish_refuses_an_oversized_token_from_the_gfs(repos, seeded_highlight):
    sess = _StubSession(
        body={"token": "x" * (MAX_TOKEN_LEN + 1), "url": "https://gfs/u/s/A"}
    )
    svc = _make_service(repos, session=sess)
    with pytest.raises(HighlightPublicationError):
        await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None


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
    await repos["highlights"].add_public_token(
        seeded_highlight.id, gfs_id="gfs-abc", token_hash=token_digest("tok-A")
    )
    sess = _StubSession(status=200, body={"status": "ok"})
    svc = _make_service(repos, session=sess)
    await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")
    assert sess.posts and sess.posts[0][0].endswith(
        "/gfs/highlight_tokens/tok-A/revoke",
    )
    assert not await repos["highlights"].is_live_public_token(
        seeded_highlight.id, token_digest("tok-A")
    )


async def test_a_revoke_ends_the_link_here_even_when_the_gfs_fails(
    repos, seeded_highlight
):
    """Revocation is enforced by this household, not by the GFS."""
    await repos["highlights"].mark_published(
        seeded_highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    await repos["highlights"].add_public_token(
        seeded_highlight.id, gfs_id="gfs-abc", token_hash=token_digest("tok-A")
    )
    svc = _make_service(repos, session=_StubSession(status=500, body={}))
    with pytest.raises(HighlightPublicationError):
        await svc.revoke_token(seeded_highlight.id, "u1", token="tok-A")
    assert not await repos["highlights"].is_live_public_token(
        seeded_highlight.id, token_digest("tok-A")
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


async def test_unpublish_ends_the_tokens_even_when_the_gfs_is_inactive(
    repos, seeded_highlight
):
    """REGRESSION (#1087 review): a suspended GFS used to make unpublish
    raise before clearing anything, so the household's tokens stayed live
    and would stream again once that GFS came back."""
    svc = _make_service(repos, session=_StubSession())
    await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    await repos["gfs"]._db.enqueue(
        "UPDATE gfs_connections SET status='suspended' WHERE id='gfs-abc'"
    )
    with pytest.raises(HighlightPublicationError):
        await svc.unpublish(seeded_highlight.id, "u1")
    refreshed = await repos["highlights"].get_highlight(seeded_highlight.id)
    assert refreshed.public_gfs_id is None
    assert not await repos["highlights"].is_live_public_token(
        seeded_highlight.id, token_digest("tok-A")
    )


async def test_publishing_on_another_gfs_pulls_the_first_publication(
    repos, seeded_highlight
):
    """REGRESSION (#1087 review): one publication per highlight. Publishing
    on GFS B used to leave GFS A's links silently dead, and a later publish
    on A brought them back. Now A's publication is pulled first."""
    await repos["gfs"]._db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES('gfs-b','gfs-b','GFS B','ee','https://gfs-b.example',
                 'active', datetime('now'))
        """,
    )
    sess = _StubSession()
    svc = _make_service(repos, session=sess)
    await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    sess._body = {"token": "tok-B", "url": "https://gfs-b/u/s/B"}
    await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-b")
    urls = [u for u, _b in sess.posts]
    assert (
        urls[1] == f"https://gfs.example/gfs/highlights/{seeded_highlight.id}/unpublish"
    )
    highlights = repos["highlights"]
    assert not await highlights.is_live_public_token(
        seeded_highlight.id, token_digest("tok-A")
    )
    assert await highlights.is_live_public_token(
        seeded_highlight.id, token_digest("tok-B")
    )
    assert (
        await highlights.get_highlight(seeded_highlight.id)
    ).public_gfs_id == "gfs-b"


async def test_a_plain_http_viewer_base_falls_back_to_the_gfs_page(
    repos, seeded_highlight, caplog
):
    """Review: an ``http://`` viewer base would let the network swap the
    page that reads the secret; it is never used."""
    svc = HighlightPublicationService(
        repos["highlights"],
        repos["gfs"],
        gfs_capabilities=_Caps(),
        public_viewer_base_url="http://viewer.example",
        pinned_viewer=load_pinned_viewer(),
    )
    svc.attach_session(
        _StubSession(body={"token": "tok-A", "url": "https://gfs/u/s/A"})
    )
    svc.attach_identity(own_instance_id="inst-self", signing_key=b"\x01" * 32)
    with caplog.at_level("WARNING"):
        out = await svc.publish(seeded_highlight.id, "u1", gfs_id="gfs-abc")
    assert out["url"].startswith("https://gfs/u/s/A#k=")
    assert "viewer.example" in caplog.text
