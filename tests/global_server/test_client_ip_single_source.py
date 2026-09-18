"""One reader for ``X-Forwarded-For`` across the whole GFS process.

The header is client-supplied.
:class:`~socialhome.global_server.public.ClientIpResolver` is the single
place that decides whether to believe it (trusted TCP peer, last entry
only). A second reader anywhere else silently re-opens the spoof: a fresh
rate-limit / throttle bucket per request, or a forged ``admin_ip`` in the
audit log. This module pins both properties — the guard grep, and the
delegation of the two readers that used to parse the header themselves.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from socialhome.global_server import app_keys as K
from socialhome.global_server.public import ClientIpResolver
from socialhome.global_server.routes.base import GfsBaseView

_GFS_DIR = Path(__file__).resolve().parents[2] / "socialhome" / "global_server"

#: Matches an actual header READ (``headers.get("X-Forwarded-For")`` /
#: ``headers["X-Forwarded-For"]``) — prose mentioning the header in a
#: docstring or comment is fine and deliberately not flagged.
_HEADER_READ = re.compile(
    r"""headers\s*(?:\.get\(|\[)\s*["']X-Forwarded-For["']""",
)


def test_client_ip_resolver_is_the_only_forwarded_for_reader():
    """A fourth copy of the first-entry parse must not creep back in."""
    readers = {
        path.relative_to(_GFS_DIR).as_posix()
        for path in _GFS_DIR.rglob("*.py")
        if _HEADER_READ.search(path.read_text(encoding="utf-8"))
    }
    assert readers == {"public.py"}, (
        "X-Forwarded-For must only be read by ClientIpResolver in "
        f"global_server/public.py; also read by: {sorted(readers - {'public.py'})}"
    )


def _view(peer: str, xff: str | None, resolver: ClientIpResolver) -> GfsBaseView:
    headers: dict[str, str] = {} if xff is None else {"X-Forwarded-For": xff}
    request = SimpleNamespace(
        headers=headers,
        transport=SimpleNamespace(get_extra_info=lambda _k: (peer, 40000)),
        remote=peer,
        app={K.gfs_client_ip_key: resolver},
    )
    return GfsBaseView(request)  # type: ignore[arg-type]


def test_base_view_client_ip_ignores_forwarded_for_from_untrusted_peer():
    """A direct internet peer cannot name itself in the audit log."""
    view = _view("203.0.113.9", "10.0.0.1", ClientIpResolver())
    assert view.client_ip() == "203.0.113.9"


def test_base_view_client_ip_uses_last_entry_from_trusted_peer():
    """Behind a trusted proxy the hop the proxy appended wins — never the
    client-supplied prefix."""
    view = _view("127.0.0.1", "10.0.0.1, 10.0.0.2", ClientIpResolver())
    assert view.client_ip() == "10.0.0.2"


def test_base_view_client_ip_uses_the_app_wide_resolver():
    """The view must not build its own resolver — it borrows the server's, so
    ``trusted_proxies`` configuration reaches every reader."""
    view = _view("203.0.113.9", "10.0.0.1", ClientIpResolver(("203.0.113.0/24",)))
    assert view.client_ip() == "10.0.0.1"
