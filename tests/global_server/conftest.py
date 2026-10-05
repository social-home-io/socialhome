"""Shared fixtures for GFS tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from socialhome.csp import style_hash
from socialhome.db.database import AsyncDatabase

_GFS_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / (
    "socialhome/global_server/migrations"
)


# Some dev environments transitively pull in
# ``pytest-homeassistant-custom-component``, which globally disables
# sockets via ``pytest-socket``. GFS tests use real ``aiohttp.TestServer``
# loopback connections, so re-enable sockets when both plugins are
# present. CI does not install either plugin; the fixture simply does
# not register and tests run normally.
try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


@pytest.fixture
async def gfs_db(tmp_dir):
    """AsyncDatabase pointed at a temp GFS database with migrations applied."""
    db = AsyncDatabase(
        tmp_dir / "gfs.db",
        migrations_dir=_GFS_MIGRATIONS,
        batch_timeout_ms=10,
    )
    await db.startup()
    yield db
    await db.shutdown()


# ── Strict-CSP guard for the server-rendered public pages ──────────────

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=", re.I)
_STYLE_ATTR_RE = re.compile(r"\sstyle\s*=", re.I)


def assert_strict_public_page(resp, text: str) -> None:
    """The page carries the strict public-page CSP and nothing in its HTML
    needs a relaxation: no inline executable ``<script>`` (JSON data blocks
    are inert and allowed), no ``on*=`` handlers, no ``style=`` attributes,
    and every ``<style>`` element is admitted by its own hash."""
    header = resp.headers.get("Content-Security-Policy")
    assert header, "public page is missing its Content-Security-Policy"
    directives: dict[str, list[str]] = {}
    for part in header.split(";"):
        name, *sources = part.split()
        directives[name] = sources
    assert directives["script-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert "'unsafe-inline'" not in header
    assert "'unsafe-eval'" not in header
    for attrs, body in _SCRIPT_RE.findall(text):
        if "src=" in attrs:
            assert not body.strip(), f"script with src also has a body: {attrs}"
        else:
            assert re.search(r"type=['\"]application/json['\"]", attrs), (
                f"inline executable <script{attrs}> on a public page"
            )
    for tag in _TAG_RE.findall(text):
        assert not _EVENT_ATTR_RE.search(tag), f"inline event handler: {tag}"
        assert not _STYLE_ATTR_RE.search(tag), f"style attribute: {tag}"
    for css in _STYLE_RE.findall(text):
        assert style_hash(css) in directives["style-src"], (
            "an inline <style> is not admitted by a hash"
        )
