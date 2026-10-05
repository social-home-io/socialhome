"""Integration tests for /api/apps/{app_id}/runtime and
/api/apps/{app_id}/bundle/{tail:.*}.

These tests exercise:
- AppRuntimeView (bearer-authed, returns signed entry URL)
- AppBundleView  (self-authorizing via query sig or path-scoped cookie)

Bundle seeding: we write real files to the app's apps_path so the file-serve
path exercises real aiofiles I/O.  The signer is read directly from the running
app via ``client.app[media_signer_key]`` (the TestClient exposes ``.app``).

Cookie persistence: aiohttp's TestClient keeps a ``CookieJar`` on the
underlying session, so cookies set by one response are automatically sent on
subsequent requests **to the same URL prefix** — we verify this in the
sub-resource test and also test manual extraction for explicitness.
"""

from __future__ import annotations

import pathlib
import urllib.parse

from unittest.mock import AsyncMock

import pytest

from socialhome.app_keys import config_key, media_signer_key, platform_adapter_key
from socialhome.domain.apps import AppAgeRestrictedError, AppManifest, InstalledApp
from socialhome.platform.adapter import Capability
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.routes.app_bundle import BUNDLE_COOKIE_PREFIX, BUNDLE_TTL_SECONDS
from socialhome.services.app_service import AppService

from .conftest import _auth

# ── Constants for test app ────────────────────────────────────────────────

_APP_ID = "com.example.chess"
_APP_VERSION = "1.0.0"
_BUNDLE_REL = f"{_APP_ID}/{_APP_VERSION}"
_INDEX_CONTENT = b"<html>chess app</html>"
_JS_CONTENT = b"console.log('chess');"


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_app(
    app_id: str = _APP_ID,
    enabled: bool = True,
    min_age: int = 0,
) -> InstalledApp:
    """Build an InstalledApp domain object for seeding."""
    manifest = AppManifest(
        entry="index.html",
        icon=None,
        capabilities=("read",),
    )
    return InstalledApp(
        app_id=app_id,
        name="Chess App",
        version=_APP_VERSION,
        enabled=enabled,
        manifest=manifest,
        bundle_path=_BUNDLE_REL,
        bundle_sha256="deadbeef",
        source_url="https://example.com/chess.tgz",
        installed_by=None,
        installed_at="2026-06-01T00:00:00+00:00",
        min_age=min_age,
    )


async def _seed_app(db, *, app_id: str = _APP_ID, enabled: bool = True) -> InstalledApp:
    """Insert the app row via the real SqliteAppRepo."""
    repo = SqliteAppRepo(db)
    app = _make_app(app_id=app_id, enabled=enabled)
    await repo.install(app)
    return app


def _write_bundle(apps_path: str, app_id: str = _APP_ID) -> None:
    """Write index.html + app.js into <apps_path>/<id>/<ver>/."""
    bundle_dir = pathlib.Path(apps_path) / app_id / _APP_VERSION
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "index.html").write_bytes(_INDEX_CONTENT)
    (bundle_dir / "app.js").write_bytes(_JS_CONTENT)


def _parse_sig_from_url(url: str) -> tuple[str, str]:
    """Extract (exp, sig) from a signed URL's query string."""
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return qs["exp"][0], qs["sig"][0]


# ── AppRuntimeView tests ──────────────────────────────────────────────────


async def test_runtime_returns_signed_entry_url(client):
    """GET /runtime with a bearer token returns 200 with a signed entry_url."""
    config = client.app[media_signer_key]  # just checking the signer exists
    assert config is not None

    apps_path = client.app.get(
        __import__("socialhome.app_keys", fromlist=["config_key"]).config_key
    ).apps_path
    _write_bundle(apps_path)
    await _seed_app(client._db)

    r = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["app_id"] == _APP_ID
    assert body["name"] == "Chess App"
    assert "entry_url" in body
    entry_url = body["entry_url"]
    assert "exp=" in entry_url
    assert "sig=" in entry_url
    assert "index.html" in entry_url
    assert body["capabilities"] == ["read"]
    assert "self_user_id" in body


async def test_runtime_404_uninstalled(client):
    """GET /runtime for an unknown app_id → 404 NOT_FOUND."""
    r = await client.get(
        "/api/apps/com.example.notinstalled/runtime",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_runtime_403_disabled(client):
    """GET /runtime for a disabled app → 403 FORBIDDEN."""
    config_obj = client.app[
        __import__("socialhome.app_keys", fromlist=["config_key"]).config_key
    ]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db, enabled=False)

    r = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_runtime_requires_auth(client):
    """GET /runtime without a bearer token → 401."""
    r = await client.get(f"/api/apps/{_APP_ID}/runtime")
    assert r.status == 401


# ── AppBundleView tests ───────────────────────────────────────────────────


async def test_bundle_entry_served_with_valid_sig(client):
    """GET entry URL (from /runtime) with no Authorization header → 200.

    Asserts:
    - body == index.html bytes
    - Content-Security-Policy contains connect-src 'none'
    - X-Frame-Options: SAMEORIGIN (set explicitly on the bundle)
    - Set-Cookie with sh_app_bundle_{id} is present
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    # Get entry_url via /runtime
    r_runtime = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers=_auth(client._tok),
    )
    assert r_runtime.status == 200
    entry_url = (await r_runtime.json())["entry_url"]

    # Fetch the bundle entry WITHOUT an Authorization header
    r = await client.get(entry_url)
    assert r.status == 200
    body = await r.read()
    assert body == _INDEX_CONTENT

    # Security headers
    csp = r.headers.get("Content-Security-Policy", "")
    assert "connect-src 'none'" in csp
    assert "worker-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"

    # Cookie presence
    cookie_name = f"{BUNDLE_COOKIE_PREFIX}{_APP_ID}"
    set_cookie = r.headers.get("Set-Cookie", "")
    assert cookie_name in set_cookie


async def test_bundle_rejects_without_sig_or_cookie(client):
    """GET a bundle file with no query sig and no cookie → 403 FORBIDDEN."""

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    r = await client.get(f"/api/apps/{_APP_ID}/bundle/app.js")
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_bundle_subresource_via_cookie(client):
    """After the entry load sets a cookie, app.js is served via cookie auth.

    aiohttp's TestClient persists cookies in its session CookieJar, so the
    cookie from the entry response is automatically sent on subsequent requests
    to the same path prefix.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    # Step 1: get entry_url
    r_runtime = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers=_auth(client._tok),
    )
    entry_url = (await r_runtime.json())["entry_url"]

    # Step 2: load the entry (sets cookie)
    r_entry = await client.get(entry_url)
    assert r_entry.status == 200

    # Step 3: load app.js WITHOUT any query sig — cookie should authorize
    r_js = await client.get(f"/api/apps/{_APP_ID}/bundle/app.js")
    assert r_js.status == 200
    js_body = await r_js.read()
    assert js_body == _JS_CONTENT


async def test_bundle_path_traversal_blocked(client):
    """A traversal attempt is rejected — never 200 serving outside the bundle dir.

    Two traversal surfaces tested:

    1. URL-path ``..`` sequences — aiohttp normalises these at the router level,
       so ``/api/apps/{id}/bundle/../../secret`` resolves to ``/api/apps/secret``,
       which doesn't match the bundle public-path pattern and the auth middleware
       returns 401 before the handler is reached.  Safe: the outside file is never
       served.

    2. URL-percent-encoded traversal (``%2e%2e``) — the path is passed as-is to
       the handler; our ``pathlib.Path.resolve() + is_relative_to()`` guard blocks
       it with 403 FORBIDDEN before any file is opened.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    # Write a "secret" file above the bundle root to verify it's never served.
    secret_path = pathlib.Path(config_obj.apps_path) / "secret.txt"
    secret_path.write_bytes(b"secret data")
    await _seed_app(client._db)

    signer = client.app[media_signer_key]
    prefix = f"/api/apps/{_APP_ID}/bundle/"
    signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed)

    # Case 1: bare ``..`` in URL — aiohttp normalises it; we get anything != 200.
    r1 = await client.get(
        f"/api/apps/{_APP_ID}/bundle/../../secret?exp={exp}&sig={sig}"
    )
    assert r1.status != 200, (
        f"traversal via '..' must not serve a file (got {r1.status})"
    )

    # Case 2: percent-encoded traversal hits the handler's resolve() guard.
    # aiohttp passes the decoded path to match_info so the handler sees "../../secret.txt"
    # as the tail; pathlib.resolve() + is_relative_to() blocks it.
    r2 = await client.get(
        f"/api/apps/{_APP_ID}/bundle/%2e%2e%2fsecret.txt?exp={exp}&sig={sig}"
    )
    assert r2.status in (403, 404), (
        f"percent-encoded traversal must be blocked (got {r2.status})"
    )


async def test_bundle_expired_sig_rejected(client):
    """A sig with a TTL in the past is rejected with 403."""
    import time

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    signer = client.app[media_signer_key]
    prefix = f"/api/apps/{_APP_ID}/bundle/"
    # Sign with a negative TTL so the expiry is already in the past
    signed = signer.sign(prefix, ttl=-10, now=int(time.time()) - 100)
    exp, sig = _parse_sig_from_url(signed)

    entry_url = f"{prefix}index.html?exp={exp}&sig={sig}"
    r = await client.get(entry_url)
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


# ── Adversarial / security tests ─────────────────────────────────────────


async def test_bundle_403_when_app_disabled_even_with_sig(client):
    """A valid sig is rejected with 403 if the app is disabled after minting.

    Disabling an app must take effect immediately, not after the 300-second
    sig TTL expires.  This validates that AppBundleView re-checks ``enabled``
    after the auth check rather than trusting the signed URL alone.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    # Mint a valid prefix signature directly (bypasses /runtime auth).
    signer = client.app[media_signer_key]
    prefix = f"/api/apps/{_APP_ID}/bundle/"
    signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed)

    # Disable the app *after* minting the signature.
    repo = SqliteAppRepo(client._db)
    await repo.set_enabled(_APP_ID, enabled=False)

    # The signed URL must now be rejected.
    r = await client.get(f"{prefix}index.html?exp={exp}&sig={sig}")
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_bundle_cross_app_sig_rejected(client):
    """A valid sig minted for app A is rejected when used on app B's bundle URL.

    The HMAC covers the full prefix ``/api/apps/{app_id}/bundle/``, so a
    signature for one app must never authorize access to a different app's
    files.
    """

    _APP_B_ID = "com.example.checkers"

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    _write_bundle(config_obj.apps_path, app_id=_APP_B_ID)
    await _seed_app(client._db)
    await _seed_app(client._db, app_id=_APP_B_ID)

    # Mint a valid sig for app A's prefix.
    signer = client.app[media_signer_key]
    prefix_a = f"/api/apps/{_APP_ID}/bundle/"
    signed_a = signer.sign(prefix_a, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed_a)

    # Use that sig on app B's bundle URL — must be rejected.
    prefix_b = f"/api/apps/{_APP_B_ID}/bundle/"
    r = await client.get(f"{prefix_b}index.html?exp={exp}&sig={sig}")
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_bundle_absolute_path_tail_blocked(client):
    """A percent-encoded absolute or traversal tail is blocked (no escape).

    Complements test_bundle_path_traversal_blocked with a broader set of
    percent-encoded traversal patterns (%2F-separated absolute path,
    %2e%2e sequences) to confirm the pathlib.resolve() + is_relative_to()
    guard catches them all.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    signer = client.app[media_signer_key]
    prefix = f"/api/apps/{_APP_ID}/bundle/"
    signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed)

    traversal_tails = [
        # %2F-separated absolute path (decoded: /etc/passwd)
        "%2Fetc%2Fpasswd",
        # double-dot with %2F (decoded: ../secret.txt)
        "%2e%2e%2fsecret.txt",
        # mixed: decoded as ../../secret
        "%2e%2e%2F%2e%2e%2Fsecret",
    ]

    for tail in traversal_tails:
        r = await client.get(f"{prefix}{tail}?exp={exp}&sig={sig}")
        assert r.status in (403, 404), (
            f"Traversal tail {tail!r} must be blocked; got status {r.status}"
        )


# ── Ingress-prefix cookie path tests ─────────────────────────────────────


_INGRESS_PREFIX = "/api/hassio_ingress/TESTTOKEN123"


def _advertise_ingress(client, monkeypatch) -> None:
    """Make the running adapter advertise ``Capability.INGRESS`` (haos)."""
    adapter = client.app[platform_adapter_key]
    monkeypatch.setattr(
        type(adapter),
        "capabilities",
        property(lambda _self: frozenset({Capability.INGRESS})),
    )


async def _bundle_set_cookie(client, headers: dict[str, str] | None = None) -> str:
    """Request the bundle entry with a fresh sig; return its Set-Cookie."""
    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)
    signer = client.app[media_signer_key]
    signed = signer.sign(_BUNDLE_PREFIX, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed)
    r = await client.get(
        f"{_BUNDLE_PREFIX}index.html?exp={exp}&sig={sig}",
        headers=headers or {},
    )
    assert r.status == 200
    return r.headers.get("Set-Cookie", "")


_BUNDLE_PREFIX = f"/api/apps/{_APP_ID}/bundle/"


async def test_bundle_cookie_path_prefixed_under_ingress(client, monkeypatch):
    """Behind ``Capability.INGRESS`` a well-formed X-Ingress-Path prefixes
    the cookie Path, so the browser sends the cookie on the ingress-prefixed
    sub-resource URLs (multi-file bundles under haos)."""
    _advertise_ingress(client, monkeypatch)
    set_cookie = await _bundle_set_cookie(
        client, headers={"X-Ingress-Path": _INGRESS_PREFIX}
    )
    assert f"Path={_INGRESS_PREFIX}{_BUNDLE_PREFIX}" in set_cookie, set_cookie


async def test_bundle_cookie_path_standalone_ignores_ingress_header(client):
    """Without ``Capability.INGRESS`` any client can forge X-Ingress-Path —
    the cookie Path stays the bare bundle prefix."""
    set_cookie = await _bundle_set_cookie(
        client, headers={"X-Ingress-Path": _INGRESS_PREFIX}
    )
    assert f"Path={_BUNDLE_PREFIX}" in set_cookie, set_cookie
    assert "hassio_ingress" not in set_cookie


@pytest.mark.parametrize(
    "header",
    ["/evil", "/api/hassio_ingress/a;b", "/api/hassio_ingress/a/../../x", "/x/../y"],
)
async def test_bundle_cookie_path_ingress_ignores_invalid_header(
    client, monkeypatch, header
):
    """Under ingress a header not shaped like ``/api/hassio_ingress/<token>``
    is ignored — it can't inject cookie attributes or widen the Path."""
    _advertise_ingress(client, monkeypatch)
    set_cookie = await _bundle_set_cookie(client, headers={"X-Ingress-Path": header})
    assert f"Path={_BUNDLE_PREFIX}" in set_cookie, set_cookie
    assert "evil" not in set_cookie
    assert "hassio_ingress" not in set_cookie


async def test_bundle_cookie_path_unprefixed_without_ingress_header(client):
    """Without X-Ingress-Path the cookie Path is the bare bundle prefix."""
    set_cookie = await _bundle_set_cookie(client)
    assert f"Path={_BUNDLE_PREFIX}" in set_cookie, set_cookie
    assert "hassio_ingress" not in set_cookie


async def test_bundle_tampered_bundle_path_returns_403(client):
    """A tampered DB ``bundle_path`` that escapes apps_root is blocked with 403.

    If a DB write (direct SQL injection, migration bug, etc.) stores
    ``bundle_path = "../../escape"`` for an installed app, the serve route
    must detect that the resolved ``base`` directory escapes ``apps_root``
    and return 403 before attempting any file I/O.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    app = await _seed_app(client._db)

    # Tamper the stored bundle_path so it escapes apps_root.
    repo = SqliteAppRepo(client._db)
    tampered = app.__class__(
        app_id=app.app_id,
        name=app.name,
        version=app.version,
        enabled=app.enabled,
        manifest=app.manifest,
        bundle_path="../../escape",
        bundle_sha256=app.bundle_sha256,
        source_url=app.source_url,
        installed_by=app.installed_by,
        installed_at=app.installed_at,
    )
    await repo.update_installed(tampered)

    # Mint a valid prefix signature.
    signer = client.app[media_signer_key]
    prefix = f"/api/apps/{_APP_ID}/bundle/"
    signed = signer.sign(prefix, ttl=BUNDLE_TTL_SECONDS)
    exp, sig = _parse_sig_from_url(signed)

    # The bundle GET must be rejected — the base dir escapes apps_root.
    r = await client.get(f"{prefix}index.html?exp={exp}&sig={sig}")
    assert r.status == 403, (
        f"tampered bundle_path escaping apps_root must return 403 (got {r.status})"
    )
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


# ── Age gate tests for /runtime ────────────────────────────────────────────


async def test_runtime_403_for_age_restricted_minor(client, monkeypatch):
    """/runtime returns 403 FORBIDDEN for a protected minor below the app's min_age.

    The age check in AppRuntimeView.get calls svc.assert_age_allowed(app,
    user.user_id) which raises AppAgeRestrictedError when the minor's
    declared_age < app.min_age.  BaseView._iter maps it to 403 FORBIDDEN.
    A blocked minor must never receive the signed entry URL.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    # Patch assert_age_allowed on the running AppService instance to raise.
    monkeypatch.setattr(
        AppService,
        "assert_age_allowed",
        AsyncMock(
            side_effect=AppAgeRestrictedError("This app is restricted to ages 13+.")
        ),
    )

    r = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers={"Authorization": f"Bearer {client._tok}"},
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert "13+" in body["error"]["detail"]


async def test_runtime_200_for_unprotected_user_with_age_gate(client, monkeypatch):
    """/runtime returns 200 for an unprotected user even when the app has min_age set.

    When assert_age_allowed does NOT raise (unprotected user), the runtime
    endpoint must proceed normally and return the signed entry URL.
    """

    config_obj = client.app[config_key]
    _write_bundle(config_obj.apps_path)
    await _seed_app(client._db)

    # assert_age_allowed returns None (no-op) for unprotected user.
    monkeypatch.setattr(
        AppService,
        "assert_age_allowed",
        AsyncMock(return_value=None),
    )

    r = await client.get(
        f"/api/apps/{_APP_ID}/runtime",
        headers={"Authorization": f"Bearer {client._tok}"},
    )
    assert r.status == 200
    body = await r.json()
    assert "entry_url" in body
