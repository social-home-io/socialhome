"""GFS public website — landing, space pages, invite links (§24.7 / §24.8).

Server-side-rendered HTML. No JS framework, no build step. Three routes:

* ``GET /``          — landing page (hero + pairing QR + spaces list).
* ``GET /spaces/{slug}`` — per-space public page with deep-link CTA.
* ``GET /join/{gfs_token}`` — invite link landing + deep-link CTA.

Shared QR-token service handles the single-use 10-minute pairing token
(spec §24.7.4) with a 1-new-token-per-30-s-per-IP rate limit.
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import io
import ipaddress
import logging
import secrets
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import qrcode  # type: ignore[import-untyped]
from aiohttp import web

from ..domain.space import SPACE_CATEGORIES
from . import app_keys as K
from .config import DEFAULT_TRUSTED_PROXIES
from .markdown_lite import render_markdown

if TYPE_CHECKING:
    from .repositories import AbstractGfsAdminRepo


log = logging.getLogger(__name__)


#: Pairing-token TTL (spec §24.7.4).
PAIR_TOKEN_TTL_SECONDS: int = 600
#: Minimum seconds between new-token requests per IP.
PAIR_TOKEN_MIN_INTERVAL: int = 30
#: Public-listing rate limit per IP (spec §24.7.3).
LISTING_MAX_PER_MINUTE: int = 30

#: Per-IP/minute cap on the anonymous public-content RTC entry points
#: (offer + relay). Each request fans a WS push to the author, so this
#: bounds that amplification while leaving headroom for a guest's normal
#: connect + a retry or two. The session-poll / ICE endpoints are NOT
#: gated here — the viewer polls them ~1/s by design.
PUBLIC_RTC_MAX_PER_MINUTE: int = 20

#: Per-IP/minute cap on ``POST /gfs/publish``. Since the relay is authorized by
#: the space-authority signature alone, the request carries no household
#: identity — the client IP is the only handle left for shedding a flood, so
#: this limiter replaces the per-instance accountability the old
#: ``from_instance`` check implied. 120/min = 2/s sustained per IP: orders of
#: magnitude above a real household (one publish per public post or subscriber
#: key handoff, per GFS) while still bounding how much unauthenticated
#: signature-verification + fan-out work one source can force.
PUBLISH_MAX_PER_MINUTE: int = 120

#: Hard cap on how many client IPs a rate-limit window tracks at once. The key
#: is attacker-influenced (one bucket per source address, and a botnet or an
#: IPv6 /64 supplies effectively unlimited distinct ones), so an unbounded dict
#: is a slow memory exhaustion. 10 000 buckets is far more concurrent clients
#: than a GFS sees in a 60-second window while costing well under a megabyte;
#: past the cap the least-recently-seen buckets are evicted, which is also the
#: correct eviction order (their windows are the closest to expiring).
#:
#: The cap is a MEMORY bound, not a defence: an attacker with at least this
#: many source addresses evicts every bucket each window, so no limiter here
#: ever fires against a distributed flood. These per-IP windows shed one noisy
#: source; volumetric defence belongs at the network edge. Stated in
#: ``docs/api.md`` so operators don't read the limits as DDoS protection.
RATE_LIMIT_MAX_TRACKED_IPS: int = 10_000

#: Sliding-window length for every per-IP limiter in this module, in seconds.
RATE_LIMIT_WINDOW_SECONDS: float = 60.0

#: Hard body cap on ``POST /gfs/publish`` — the endpoint is unauthenticated
#: until the authority signature inside the payload is verified, so the bytes
#: must be bounded BEFORE they are buffered or parsed. Sized from the largest
#: legitimate relay payload: a ``space_post_public`` envelope carries one
#: AES-GCM ciphertext over a single post (``MAX_POST_LENGTH`` is 10 000 chars
#: → ≤ 40 KiB UTF-8, plus author signature, media references and an optional
#: 4-decimal location; media bytes ride separate blobs, never this body) and a
#: ``space_subscriber_key_handoff`` is a few hundred bytes of sealed key. Even
#: base64-expanded that is ~55 KiB, so 256 KiB leaves ~5× headroom while
#: keeping a flood's per-request memory cost trivial.
PUBLISH_MAX_BODY_BYTES: int = 256 * 1024


# ─── Client IP resolution ───────────────────────────────────────────────


class ClientIpResolver:
    """Resolve the address a request should be rate-limited under.

    ``X-Forwarded-For`` is client-supplied data. Believing it unconditionally
    means an attacker rotates the header and mints a fresh rate-limit bucket
    per request, so no per-IP limiter on this server ever fires. It is honoured
    only when the TCP peer is itself a trusted proxy, and then only its LAST
    entry is used — that is the hop the trusted proxy appended; everything to
    its left was written by whoever was upstream of it (the client included).

    IPv4-mapped IPv6 addresses (``::ffff:1.2.3.4``) are unmapped to their IPv4
    form before the trusted-peer test AND before the key is returned. A
    dual-stack listener reports a v4 proxy as ``::ffff:127.0.0.1``, which
    matches none of the v4 trusted networks — the header would be ignored and
    every client behind that proxy would then collapse into the proxy's single
    bucket, 429-ing the whole deployment after one client's quota. Unmapping
    also stops one host doubling its quota by switching address family.

    The trusted set is parsed into :mod:`ipaddress` networks ONCE, at
    construction, so the middleware never re-parses CIDRs per request.
    Unparseable entries are dropped at construction (an operator typo must not
    turn into a per-request exception on the hot path).

    Two documented limits of this design:

    * **Single hop.** Only the last ``X-Forwarded-For`` entry is read, so a
      chain of two or more trusted proxies resolves to the address the
      INNERMOST proxy appended — i.e. the outer proxy, not the real client.
      Every client behind such a chain then shares one bucket. Operators
      running multi-hop ingress must collapse the chain (have the outermost
      proxy rewrite the header) before the GFS sees it.
    * **The proxy must OVERWRITE the header.** Trust here is "this peer's last
      entry is authoritative". An L4/TCP proxy (or any L7 proxy configured to
      APPEND rather than set) forwards the client's own ``X-Forwarded-For``
      untouched, so the "last entry" is once again attacker-chosen and a single
      source can mint an unlimited number of buckets. With such a front end the
      only safe configuration is ``trusted_proxies = []``.
    """

    __slots__ = ("_networks",)

    def __init__(self, trusted_proxies: Iterable[str] = DEFAULT_TRUSTED_PROXIES):
        networks = []
        for entry in trusted_proxies:
            try:
                networks.append(ipaddress.ip_network(entry.strip(), strict=False))
            except ValueError:
                log.warning(
                    "GFS: ignoring unparseable trusted_proxies entry %r",
                    entry,
                )
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
            tuple(networks)
        )

    def __call__(self, request: web.Request) -> str:
        peer = self._peer_ip(request)
        if peer is None:
            return "unknown"
        if not self._is_trusted(peer):
            return str(peer)
        forwarded = request.headers.get("X-Forwarded-For", "")
        last = self._parse_ip(forwarded.rsplit(",", 1)[-1])
        if last is None:
            # No header, or a malformed last hop — fall back to the real peer
            # rather than keying the limiter on an attacker-chosen string.
            return str(peer)
        return str(last)

    @staticmethod
    def _peer_ip(
        request: web.Request,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        transport = request.transport
        peername = transport.get_extra_info("peername") if transport else None
        raw = peername[0] if peername else request.remote
        if not raw:
            return None
        return ClientIpResolver._parse_ip(str(raw))

    @staticmethod
    def _parse_ip(
        raw: str,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Parse one address — peer or ``X-Forwarded-For`` entry — or ``None``.

        The SAME parse for both sources, deliberately: whatever normalisation
        one side does, the other must do too, or the two disagree about what
        "the client" is. It strips the IPv6 zone (``fe80::1%eth0``) — a local
        interface label, not part of the address, and ``ipaddress`` keeps it
        as a scope id, so leaving it on hands one host a fresh rate-limit
        bucket per zone spelling AND makes the bucket key as long as the
        attacker-supplied zone — then unmaps IPv4-mapped IPv6 (see
        :meth:`_unmap`).
        """
        try:
            return ClientIpResolver._unmap(
                ipaddress.ip_address(raw.strip().split("%", 1)[0])
            )
        except ValueError:
            return None

    @staticmethod
    def _unmap(
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        """Collapse an IPv4-mapped IPv6 address to its IPv4 form.

        ``::ffff:1.2.3.4`` and ``1.2.3.4`` are the same host; keeping them
        distinct both breaks trusted-proxy matching on a dual-stack listener
        and hands one source two rate-limit buckets.
        """
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        return addr

    def _is_trusted(
        self,
        peer: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return any(peer in net for net in self._networks)


# ─── Sliding-window rate-limit counter ──────────────────────────────────


class SlidingWindowCounter:
    """Per-key sliding-window hit counter with a bounded key space.

    Shared by every per-IP limiter in this module (listing, public RTC entry
    points, ``/gfs/publish``) — they differ only in which paths they gate and
    how many hits they allow.

    Two bounds keep an anonymous flood from growing the process: the hit list
    for a key is pruned to the current window on every touch (so an idle key
    holds nothing), and the key space itself is capped at *max_keys* with
    least-recently-seen eviction. ``dict`` preserves insertion order and each
    allowed hit re-inserts its key, so iteration order IS recency order.
    """

    __slots__ = ("_limit", "_max_keys", "_hits")

    def __init__(
        self,
        limit: int,
        *,
        max_keys: int = RATE_LIMIT_MAX_TRACKED_IPS,
    ) -> None:
        self._limit = limit
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record a hit for *key* and return whether it is within the window."""
        stamp = time.monotonic() if now is None else now
        previous = self._hits.pop(key, ())
        hits = [t for t in previous if stamp - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(hits) >= self._limit:
            self._hits[key] = hits
            return False
        hits.append(stamp)
        self._hits[key] = hits
        self._evict()
        return True

    def _evict(self) -> None:
        overflow = len(self._hits) - self._max_keys
        if overflow <= 0:
            return
        for key in list(self._hits)[:overflow]:
            del self._hits[key]

    def __contains__(self, key: str) -> bool:
        return key in self._hits

    def __len__(self) -> int:
        return len(self._hits)


# ─── Token service ──────────────────────────────────────────────────────


class PairingTokenService:
    """Issue + consume single-use pairing tokens."""

    __slots__ = ("_admin_repo",)

    def __init__(self, admin_repo: "AbstractGfsAdminRepo") -> None:
        self._admin_repo = admin_repo

    async def generate(self, client_ip: str) -> tuple[str | None, int]:
        """Return ``(token, remaining_wait)`` — if the caller is in the
        rate-limit window ``token`` is ``None`` and ``remaining_wait`` is
        the seconds until a new one can be issued.
        """
        since = int(time.time()) - PAIR_TOKEN_MIN_INTERVAL
        recent = await self._admin_repo.count_pair_tokens(client_ip, since=since)
        if recent > 0:
            return None, PAIR_TOKEN_MIN_INTERVAL
        token = secrets.token_urlsafe(32)
        await self._admin_repo.save_pair_token(token, client_ip)
        return token, 0

    async def consume(self, token: str) -> bool:
        """Single-use + 10-minute TTL. Returns ``True`` iff consumed."""
        return await self._admin_repo.consume_pair_token(token)


# ─── Per-IP rate-limit middlewares ─────────────────────────────────────


def _build_window_limiter(
    resolver: ClientIpResolver,
    limit: int,
    applies: Callable[[str], bool],
):
    """Build a middleware that sheds >*limit* hits/minute per client IP.

    One implementation for all three limiters below: they differ only in which
    paths they gate (*applies*) and the *limit*. The client address comes from
    *resolver*, which is the only component allowed to look at
    ``X-Forwarded-For`` (see :class:`ClientIpResolver`).
    """
    counter = SlidingWindowCounter(limit)

    @web.middleware
    async def _rate_limit(request: web.Request, handler):
        if not applies(request.rel_url.path):
            return await handler(request)
        if not counter.allow(resolver(request)):
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        return await handler(request)

    return _rate_limit


def _is_public_listing(path: str) -> bool:
    """The public HTML pages — the landing page and the per-space pages."""
    return path == "/" or path.startswith("/spaces/")


def _is_public_rtc_entry(path: str) -> bool:
    """The anonymous, WS-amplifying RTC entry points: the offers and the
    relay-stream GETs (not the signed author uploads at ``/relay-stream/``,
    not the high-frequency session/ICE polls)."""
    return (
        path in ("/gfs/highlight_rtc/offer", "/gfs/moment_rtc/offer")
        or path.startswith("/gfs/highlight_rtc/relay/")
        or path.startswith("/gfs/moment_rtc/relay/")
    )


def build_listing_rate_limit(resolver: ClientIpResolver):
    """Simple in-memory per-IP rate limiter for the public listing.

    Spec §24.7.3: 30 GETs per minute on ``/`` and ``/spaces/{id}``.
    """
    return _build_window_limiter(resolver, LISTING_MAX_PER_MINUTE, _is_public_listing)


def build_public_rtc_rate_limit(resolver: ClientIpResolver):
    """Per-IP rate limiter for the anonymous public-content RTC entry
    points. Each offer/relay request pushes a WS frame to the author, so a
    flood is an amplification vector; cap it per IP."""
    return _build_window_limiter(
        resolver,
        PUBLIC_RTC_MAX_PER_MINUTE,
        _is_public_rtc_entry,
    )


def build_publish_rate_limit(resolver: ClientIpResolver):
    """Per-IP rate limiter for ``POST /gfs/publish``.

    The relay is authorized by the space-authority signature alone, so the GFS
    cannot (and must not) identify the publishing household — per-IP is the
    only rate handle available."""
    return _build_window_limiter(
        resolver,
        PUBLISH_MAX_PER_MINUTE,
        lambda path: path == "/gfs/publish",
    )


# ─── Helpers ────────────────────────────────────────────────────────────


def _client_ip(request: web.Request) -> str:
    """The client address for this request, per the app's trusted-proxy policy.

    Handlers (unlike the middlewares, which close over the resolver) read it
    off the app so there is exactly ONE parsed trusted-proxy set per server.
    """
    resolver: ClientIpResolver = request.app[K.gfs_client_ip_key]
    return resolver(request)


def _escape(value: object | None) -> str:
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


async def _render_qr_png_data_uri(payload: str) -> str:
    """Return a data: URI with a PNG-encoded QR code of *payload*."""
    return await asyncio.to_thread(_render_qr_png_data_uri_sync, payload)


def _render_qr_png_data_uri_sync(payload: str) -> str:
    img = qrcode.make(payload, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


#: Discovery categories (§23.50) as ``(value, label)`` pairs. The values
#: mirror ``socialhome.domain.space.SPACE_CATEGORIES``; this ordered list
#: drives both the filter tabs and the per-space label on the SSR pages.
#: Kept local to the GFS layer (which must not import the HFS
#: ``services.space_service``) — a label tweak is a one-liner here.
CATEGORY_LABELS: list[tuple[str, str]] = [
    ("general", "General"),
    ("hobby_crafts", "Hobby & crafts"),
    ("sports_outdoors", "Sports & outdoors"),
    ("gaming", "Gaming"),
    ("music_arts", "Music & arts"),
    ("food_drink", "Food & drink"),
    ("tech", "Tech"),
    ("local", "Local / neighborhood"),
    ("family_parenting", "Family & parenting"),
    ("learning", "Learning"),
]
_CATEGORY_LABEL_MAP: dict[str, str] = dict(CATEGORY_LABELS)


def _category_label(value: str | None) -> str:
    """Human label for a stored category value (default ``General``)."""
    return _CATEGORY_LABEL_MAP.get(value or "", "General")


def _render_landing(
    *,
    server_name: str,
    landing_markdown: str,
    header_image_url: str,
    token: str,
    pair_code: str,
    pair_qr_data_uri: str,
    spaces: list[dict],
    search: str,
    category: str,
    base_url: str,
) -> str:
    rows = []
    for sp in spaces:
        accent = _escape(sp.get("accent_color") or "#ce5d3e")
        cat_label = _escape(_category_label(sp.get("category")))
        rows.append(f"""
          <li class="card" style="border-left:6px solid {accent}">
            <a href="/spaces/{_escape(sp["space_id"])}">
              <strong>{_escape(sp.get("name") or "—")}</strong>
            </a>
            <div class="muted">{sp.get("subscriber_count", 0)} members
              · {sp.get("posts_per_week", 0):.1f} posts/week
              · {cat_label}</div>
            <p>{_escape((sp.get("description") or "")[:120])}</p>
          </li>
        """)
    rows_html = "\n".join(rows) or ('<li class="muted">No active spaces yet.</li>')
    cat_tabs = [f'<a href="/" class="{"active" if not category else ""}">All</a>']
    for value, label in CATEGORY_LABELS:
        active = "active" if category == value else ""
        cat_tabs.append(
            f'<a href="/?category={value}" class="{active}">{_escape(label)}</a>'
        )
    cat_tabs_html = "\n        ".join(cat_tabs)
    header_html = (
        f'<img src="{_escape(header_image_url)}" alt="" class="hero-image" />'
        if header_image_url
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{_escape(server_name)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta http-equiv="refresh" content="600" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Fraunces:opsz,wght,SOFT,WONK@9..144,400..900,0..100,0..1&display=swap"
  />
  <style>
    /* GFS public theme — mirrors the SH SPA design tokens (see
     * ``client/src/styles/tokens.css``) so the public landing feels
     * like part of the same product family as the app. Pure CSS, no
     * JS, no build step — server-rendered. The token values below
     * are copied verbatim from ``--sh-*`` in the SPA so a brand
     * tweak in the SPA tokens file is a small ``s/old/new/`` here
     * too. (We don't ``@import`` the SPA file because that would
     * require shipping it from the GFS aiohttp app — keeping the
     * SSR pages self-contained avoids the round trip.) */
    :root {{
      --warm-bg:      #F4ECE0;       /* paper — primary surface */
      --paper:        #EFE3D2;       /* paper-tinted — cards */
      --ink:          #1A1814;       /* ink — warm-tinted brown */
      --ink-soft:     #807766;       /* ink-quiet — 5.4:1 on paper */
      --hair:         #D8CFC0;       /* warm-tinted hairline */
      --primary:      #D2542A;       /* hearth — terracotta */
      --primary-soft: #F1D9CA;       /* hearth-tint — for tinted surfaces */
    }}
    /* Dark mode — warm ember palette (see comment in
     * ``users_directory.css`` for the full design rationale).
     * ``--primary-soft`` is overridden to a saturated terracotta
     * overlay so the washi-tape accent on each card stays visible
     * against the deep ember background. */
    @media (prefers-color-scheme: dark) {{
      :root {{
        --warm-bg:      #1A1612;     /* deep ember */
        --paper:        #251E18;     /* coffee surface */
        --ink:          #F1E9DA;     /* ink-light — warm cream */
        --ink-soft:     #9A8E7D;     /* warm muted */
        --hair:         #3D332A;     /* burnt-sienna hairline */
        --primary:      #E96A3F;     /* hearth lifted for dark */
        --primary-soft: #4A2419;     /* hearth-on-dark — for washi accents */
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      /* Manrope geometric body — same family the SH SPA picked in
       * 2026.4 ("looks like a household, not every other SaaS app").
       * System fallbacks keep the first paint quick on a slow link. */
      font: 15px/1.55 'Manrope', -apple-system, BlinkMacSystemFont,
            'Segoe UI', Roboto, sans-serif;
      margin: 0; color: var(--ink); background: var(--warm-bg);
    }}
    .hero-image {{ display: block; width: 100%; max-height: 240px;
                    object-fit: cover; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
    /* Display copy — Fraunces serif with the SPA's hand-set tuning
     * (``salt`` + ``ss01`` features, soft + wonk axes) so the
     * wordmark + section titles read editorial, not stock. */
    h1, h2 {{
      font-family: 'Fraunces', 'Iowan Old Style', 'Palatino Linotype', serif;
      font-feature-settings: "ss01" on, "salt" on;
      font-variation-settings: "SOFT" 75, "WONK" 1, "opsz" 96;
      letter-spacing: -0.01em;
    }}
    h1 {{ font-size: 34px; margin: 16px 0 4px; }}
    h2 {{ font-size: 22px; margin: 0 0 12px; }}
    .muted {{ color: var(--ink-soft); font-size: 13px; }}
    section {{
      position: relative;
      border: 1px solid var(--hair); border-radius: 12px;
      padding: 22px; margin: 20px 0; background: var(--paper);
      box-shadow: 0 1px 0 var(--hair),
                  0 18px 36px -28px rgba(26, 24, 20, 0.25);
    }}
    /* Tiny washi-tape accent on the section corner — same vocabulary
     * as the SH SPA's pinned-card aesthetic, just more restrained. */
    section::before {{
      content: ""; position: absolute; top: -8px; right: 28px;
      width: 60px; height: 16px; transform: rotate(-3deg);
      background: var(--primary-soft); opacity: 0.85;
      border-radius: 2px;
    }}
    .pair {{ display: flex; gap: 22px; align-items: center; flex-wrap: wrap; }}
    .pair img {{ width: 200px; height: 200px;
                 background: #fff; padding: 6px;
                 border: 1px solid var(--hair); border-radius: 8px; }}
    .pair code {{ background: var(--warm-bg); border: 1px solid var(--hair);
                 padding: 4px 8px; border-radius: 6px;
                 display: inline-block; word-break: break-all;
                 font-size: 13px; max-width: 100%; }}
    .pair .copy-btn {{ margin-top: 6px; padding: 6px 14px;
                       background: var(--primary); color: #fff;
                       border: 0; border-radius: 999px; font: inherit;
                       font-size: 13px; font-weight: 600; cursor: pointer;
                       transition: filter 100ms; }}
    .pair .copy-btn:hover {{ filter: brightness(0.95); }}
    .pair .copy-btn.copied {{ background: #2D8F4E; }}
    .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }}
    .filters a {{ padding: 4px 12px; border-radius: 999px;
                  border: 1px solid var(--hair); color: var(--ink-soft);
                  text-decoration: none; background: transparent;
                  font-size: 13px; }}
    .filters a:hover {{ color: var(--ink); border-color: var(--primary); }}
    .filters a.active {{ background: var(--primary); color: #fff;
                         border-color: var(--primary); }}
    input[type=search] {{ padding: 8px 12px; border: 1px solid var(--hair);
                          border-radius: 8px; font: inherit; width: 100%;
                          max-width: 360px; background: var(--warm-bg);
                          color: var(--ink); }}
    input[type=search]:focus {{ outline: 2px solid var(--primary);
                                outline-offset: 1px; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    .card {{ background: var(--warm-bg); border: 1px solid var(--hair);
             border-radius: 10px;
             padding: 14px 16px; margin-bottom: 10px;
             transition: transform 100ms, box-shadow 100ms; }}
    .card:hover {{ transform: translateY(-1px);
                  box-shadow: 0 14px 28px -22px rgba(26, 24, 20, 0.4); }}
    .card a {{ color: var(--ink); text-decoration: none; }}
    .card a:hover strong {{ color: var(--primary); }}
    a {{ color: var(--primary); }}
    a:hover {{ color: var(--ink); }}
    .footer-brand {{
      text-align: center; padding: 14px 0 32px;
      color: var(--ink-soft); font-size: 12px;
    }}
  </style>
</head>
<body>
  {header_html}
  <main>
    <h1>{_escape(server_name)}</h1>
    <p class="muted">Community relay running the Social Home federation.</p>
    <div>{landing_markdown}</div>

    <section>
      <h2>Connect your Social Home</h2>
      <div class="pair">
        <img src="{pair_qr_data_uri}" alt="Pairing QR" />
        <div>
          <ol>
            <li>Open Social Home</li>
            <li>Spaces → Discover → ⬡ Global</li>
            <li>Scan the QR or copy the pairing code below</li>
          </ol>
          <p><code id="pair-code" data-pair-token="{_escape(token)}"
                   >{_escape(pair_code)}</code></p>
          <p>
            <button type="button" class="copy-btn" id="copy-pair-btn"
                    data-copy-target="pair-code"
                    data-copied-label="Copied ✓"
                    data-default-label="Copy code">Copy code</button>
          </p>
          <p class="muted">Valid for 10 minutes · single-use.</p>
        </div>
      </div>
    </section>
    <script>
      // Tiny inline copy-button shim — keeps the landing as a single
      // server-rendered HTML page (no SPA bundle to ship to first-time
      // visitors). Falls back to selecting the code text if the
      // clipboard API is unavailable (older Safari, file://).
      (function() {{
        var btn = document.getElementById("copy-pair-btn");
        var target = document.getElementById("pair-code");
        if (!btn || !target) return;
        btn.addEventListener("click", function() {{
          var text = target.textContent || "";
          var done = function() {{
            btn.textContent = btn.dataset.copiedLabel || "Copied";
            btn.classList.add("copied");
            setTimeout(function() {{
              btn.textContent = btn.dataset.defaultLabel || "Copy code";
              btn.classList.remove("copied");
            }}, 1800);
          }};
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(text).then(done).catch(function() {{
              var r = document.createRange(); r.selectNode(target);
              window.getSelection().removeAllRanges();
              window.getSelection().addRange(r);
            }});
          }} else {{
            var r = document.createRange(); r.selectNode(target);
            window.getSelection().removeAllRanges();
            window.getSelection().addRange(r);
          }}
        }});
      }})();
    </script>

    <section>
      <h2>Spaces</h2>
      <form method="get" action="/">
        <input name="search" type="search"
               placeholder="Search spaces…" value="{_escape(search)}" />
      </form>
      <div class="filters">
        {cat_tabs_html}
      </div>
      <ul>
        {rows_html}
      </ul>
    </section>

    <section>
      <h2>What is Social Home?</h2>
      <p>Social Home is a privacy-first, self-hosted social network.
      Global Federation Servers like {_escape(server_name)} stitch
      households together for public spaces. Learn more at
      <a href="https://social-home.io" rel="nofollow noopener">
      social-home.io</a>.</p>
    </section>
  </main>
  <p class="footer-brand">
    A Global Federation Server · powered by
    <a href="https://social-home.io" rel="nofollow noopener">Social Home</a>
  </p>
</body>
</html>
"""


def _render_space_page(
    *,
    space: dict,
    server_name: str,
    base_url: str,
) -> str:
    primary = _escape(
        space.get("primary_color") or space.get("accent_color") or "#D2542A"
    )
    accent = _escape(space.get("accent_color") or primary)
    icon_url = _escape(space.get("icon_url") or "")
    cover_uri = _escape(space.get("cover_url") or "")
    deep_link = f"sh://join-space/{base_url}/spaces/{_escape(space['space_id'])}"
    og_image = cover_uri or icon_url
    og_title = _escape(f"{space.get('name') or ''} — {server_name}")
    og_desc = _escape(space.get("description") or "")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{og_title}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta property="og:title"       content="{og_title}" />
  <meta property="og:description" content="{og_desc}" />
  <meta property="og:image"       content="{og_image}" />
  <meta property="og:url"         content="{_escape(base_url)}/spaces/{
        _escape(space["space_id"])
    }" />
  <meta name="twitter:card"       content="summary_large_image" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Fraunces:opsz,wght,SOFT,WONK@9..144,400..900,0..100,0..1&display=swap"
  />
  <style>
    /* GFS per-space public page — same SH design tokens as the
     * landing (see the matching ``:root`` block in
     * ``handle_landing``'s template). The CTA + accent-bar pick
     * up the per-space accent so the page feels keyed to that
     * community, not just the server. */
    :root {{
      --warm-bg:  #F4ECE0;
      --paper:    #EFE3D2;
      --ink:      #1A1814;
      --ink-soft: #807766;
      --hair:     #D8CFC0;
      --primary:  {primary};
      --accent:   {accent};
    }}
    /* Dark mode — warm ember (see ``users_directory.css`` rationale).
     * ``--primary`` keeps the per-space accent so the page reads as
     * "keyed to this community" even at night. */
    @media (prefers-color-scheme: dark) {{
      :root {{
        --warm-bg:  #1A1612;
        --paper:    #251E18;
        --ink:      #F1E9DA;
        --ink-soft: #9A8E7D;
        --hair:     #3D332A;
      }}
    }}
    body {{
      font: 15px/1.55 'Manrope', -apple-system, BlinkMacSystemFont,
            'Segoe UI', Roboto, sans-serif;
      margin: 0; color: var(--ink); background: var(--warm-bg);
    }}
    main {{ max-width: 780px; margin: 0 auto; padding: 24px; }}
    .accent-bar {{ height: 6px; background: var(--primary);
                   margin: 12px 0 22px; border-radius: 3px; }}
    .cover {{ width: 100%; max-height: 360px; object-fit: cover;
              display: block; }}
    /* Space icon (avatar) — overlaps the cover bottom-left, profile-style. */
    .space-avatar {{ width: 76px; height: 76px; border-radius: 50%;
                     object-fit: cover; border: 3px solid var(--warm-bg);
                     box-shadow: 0 2px 8px rgba(0,0,0,0.18);
                     margin: -52px 0 8px; background: var(--paper); }}
    .about-md {{ margin-top: 8px; }}
    .about-md h1, .about-md h2, .about-md h3 {{ font-size: 1.05rem; margin: 12px 0 4px; }}
    .about-md ul {{ margin: 6px 0; padding-left: 20px; }}
    .about-md blockquote {{ margin: 8px 0; padding-left: 12px;
                            border-left: 3px solid var(--hair); color: var(--ink-soft); }}
    .about-md code {{ background: var(--paper); padding: 1px 5px; border-radius: 4px;
                      font-size: 0.92em; }}
    .about-md a {{ color: var(--primary); }}
    h1, h2 {{
      font-family: 'Fraunces', 'Iowan Old Style', 'Palatino Linotype', serif;
      font-feature-settings: "ss01" on, "salt" on;
      font-variation-settings: "SOFT" 75, "WONK" 1, "opsz" 96;
      letter-spacing: -0.01em;
    }}
    h1 {{ margin-bottom: 4px; font-size: 30px; }}
    h2 {{ font-size: 20px; margin: 0 0 8px; }}
    .muted {{ color: var(--ink-soft); }}
    .cta {{ background: var(--primary); color: #fff; padding: 11px 22px;
            border-radius: 999px; display: inline-block;
            text-decoration: none; font-weight: 600; font-size: 15px;
            transition: transform 100ms, filter 100ms; }}
    .cta:hover {{ transform: translateY(-1px); filter: brightness(0.95); }}
    a.secondary {{ color: var(--ink-soft); text-decoration: none; }}
    a.secondary:hover {{ color: var(--primary); }}
    section {{
      margin: 22px 0;
      background: var(--paper);
      border: 1px solid var(--hair); border-radius: 12px;
      padding: 20px;
      box-shadow: 0 1px 0 var(--hair),
                  0 18px 36px -28px rgba(26, 24, 20, 0.22);
    }}
    .cta-section {{ background: transparent; border: none;
                    box-shadow: none; padding: 0; }}
    .footer-brand {{
      text-align: center; padding: 14px 0 32px;
      color: var(--ink-soft); font-size: 12px;
    }}
    .footer-brand a {{ color: var(--primary); }}
  </style>
</head>
<body>
  {'<img class="cover" src="' + cover_uri + '" alt="" />' if cover_uri else ""}
  <main>
    {'<img class="space-avatar" src="' + icon_url + '" alt="" />' if icon_url else ""}
    <div class="accent-bar"></div>
    <a href="/" class="secondary">← {_escape(server_name)}</a>
    <h1>{_escape(space.get("name") or "—")}</h1>
    <p class="muted">{space.get("subscriber_count", 0)} members
      · {_escape(_category_label(space.get("category")))}</p>

    <section class="cta-section">
      <a class="cta" href="{_escape(deep_link)}">Open in Social Home</a>
    </section>

    <section>
      <h2>About</h2>
      <p>{_escape(space.get("description") or "")}</p>
      {
        (
            '<div class="about-md">'
            + render_markdown(space.get("about_markdown"))
            + "</div>"
        )
        if space.get("about_markdown")
        else ""
    }
    </section>
  </main>
  <p class="footer-brand">
    Hosted on {_escape(server_name)} · powered by
    <a href="https://social-home.io" rel="nofollow noopener">Social Home</a>
  </p>
</body>
</html>
"""


def _render_invite_page(
    *,
    token: str,
    space: dict | None,
    server_name: str,
    base_url: str,
) -> str:
    if space is None:
        return (
            "<!doctype html>"
            "<style>body{font:15px/1.5 'Manrope',-apple-system,system-ui,sans-serif;"
            "margin:0;padding:48px 24px;color:#1A1814;background:#F4ECE0}</style>"
            "<p>This invite has expired or was revoked.</p>"
        )
    accent = _escape(space.get("accent_color") or "#D2542A")
    deep_link = f"sh://gfs-invite/{base_url}/join/{_escape(token)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Join {_escape(space.get("name") or "")} — {_escape(server_name)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Fraunces:opsz,wght,SOFT,WONK@9..144,400..900,0..100,0..1&display=swap"
  />
  <style>
    /* Same SH design tokens as the landing + per-space pages so
     * the invite-link handoff feels continuous with the rest of
     * the GFS surface and the SH SPA. */
    :root {{
      --warm-bg:  #F4ECE0;
      --ink:      #1A1814;
      --ink-soft: #807766;
    }}
    /* Dark mode — warm ember (see ``users_directory.css``
     * rationale). The CTA keeps the per-space accent for contrast
     * even at night. */
    @media (prefers-color-scheme: dark) {{
      :root {{
        --warm-bg:  #1A1612;
        --ink:      #F1E9DA;
        --ink-soft: #9A8E7D;
      }}
    }}
    body {{
      font: 15px/1.5 'Manrope', -apple-system, BlinkMacSystemFont,
            'Segoe UI', Roboto, sans-serif;
      margin: 0; color: var(--ink); background: var(--warm-bg);
    }}
    main {{ max-width: 640px; margin: 0 auto; padding: 24px; }}
    .accent-bar {{ height: 6px; background: {accent};
                   margin: 12px 0 22px; border-radius: 3px; }}
    h1 {{
      font-family: 'Fraunces', 'Iowan Old Style', 'Palatino Linotype', serif;
      font-feature-settings: "ss01" on, "salt" on;
      font-variation-settings: "SOFT" 75, "WONK" 1, "opsz" 96;
      font-size: 30px; margin-bottom: 4px; letter-spacing: -0.01em;
    }}
    .cta {{ background: {accent}; color: #fff; padding: 11px 22px;
            border-radius: 999px; display: inline-block;
            text-decoration: none; font-weight: 600; font-size: 15px;
            transition: transform 100ms, filter 100ms; }}
    .cta:hover {{ transform: translateY(-1px); filter: brightness(0.95); }}
    .muted {{ color: var(--ink-soft); font-size: 13px; }}
  </style>
</head>
<body>
  <main>
    <div class="accent-bar"></div>
    <h1>You're invited to {_escape(space.get("name") or "")}</h1>
    <p>on {_escape(server_name)}.</p>
    <p><a class="cta" href="{_escape(deep_link)}">Open in Social Home</a></p>
    <p class="muted">Opens the invite in your Social Home app — you join
    from your own household instance.</p>
  </main>
</body>
</html>
"""


# ─── Handlers ────────────────────────────────────────────────────────────


async def handle_landing(request: web.Request) -> web.Response:
    """GET / — public landing page."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]
    token_svc: PairingTokenService = request.app["gfs_token_service"]

    # Settings pulled fresh (admin portal may have changed them).
    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    landing_markdown = (
        await admin_repo.get_config("landing_markdown")
    ) or cfg.landing_markdown
    header_image_file = (
        await admin_repo.get_config("header_image_file")
    ) or cfg.header_image_file

    token, _wait = await token_svc.generate(_client_ip(request))
    if token is None:
        token = "please-wait"
    # The pairing code is a single ``socialhome://gfs-pair/{base_url}
    # ?token={token}`` URL — chat-safe, copy/paste friendly, and the
    # same string the SH SPA's GFS paste field consumes. When the GFS
    # was booted without an external ``base_url`` (admin still
    # configuring), fall back to the bare token so the QR isn't broken
    # — the SPA paste path won't accept it, which is the right
    # behaviour (no operator should pair against an unconfigured GFS).
    pair_code = (
        f"socialhome://gfs-pair/{cfg.base_url}?token={token}"
        if cfg.base_url
        else f"token:{token}"
    )
    qr_data = await _render_qr_png_data_uri(pair_code)

    search = (request.query.get("search") or "").strip()
    category = (request.query.get("category") or "").strip()
    # Normalize unknown categories to "" so garbage input lights up the
    # "All" tab and shows every space (consistent with the filter below).
    category = category if category in SPACE_CATEGORIES else ""
    active_spaces = await fed_repo.list_spaces(status="active")
    items: list[dict] = []
    for sp in active_spaces:
        if search:
            haystack = f"{sp.name} {sp.description or ''}".lower()
            if search.lower() not in haystack:
                continue
        if category in SPACE_CATEGORIES and sp.category != category:
            continue
        items.append(
            {
                "space_id": sp.space_id,
                "name": sp.name,
                "description": sp.description or "",
                "accent_color": sp.accent_color,
                "subscriber_count": sp.subscriber_count,
                "posts_per_week": sp.posts_per_week,
                "category": sp.category,
            }
        )

    header_image_url = (
        f"{cfg.base_url}/media/{header_image_file}" if header_image_file else ""
    )
    html = _render_landing(
        server_name=server_name,
        landing_markdown=_escape(landing_markdown),
        header_image_url=header_image_url,
        token=token,
        pair_code=pair_code,
        pair_qr_data_uri=qr_data,
        spaces=items,
        search=search,
        category=category,
        base_url=cfg.base_url,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_space_page(request: web.Request) -> web.Response:
    """GET /spaces/{slug} — per-space public page."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]

    slug = request.match_info["slug"]
    space = await fed_repo.get_space(slug)
    # Owner-withdrawn rows are hidden from the public page too — the same
    # discovery surface the listing + detail route filter (``hide_space``).
    if space is None or space.status != "active" or space.withdrawn:
        raise web.HTTPNotFound(reason="Space not found or not published")

    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    space_dict = {
        "space_id": space.space_id,
        "name": space.name,
        "description": space.description,
        "about_markdown": space.about_markdown,
        "cover_url": space.cover_url,
        "icon_url": space.icon_url,
        "accent_color": space.accent_color,
        "primary_color": space.primary_color,
        "subscriber_count": space.subscriber_count,
        "category": space.category,
    }
    html = _render_space_page(
        space=space_dict,
        server_name=server_name,
        base_url=cfg.base_url,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_invite_page(request: web.Request) -> web.Response:
    """GET /join/{gfs_token} — invite link landing."""
    cfg = request.app[K.gfs_config_key]
    admin_repo = request.app[K.gfs_admin_repo_key]
    fed_repo = request.app[K.gfs_fed_repo_key]

    token = request.match_info["gfs_token"]
    row = await admin_repo._db.fetchone(  # type: ignore[attr-defined]
        "SELECT space_id, expires_at FROM gfs_invite_tokens WHERE gfs_token=?",
        (token,),
    )
    space = None
    if row is not None:
        expires_at = row["expires_at"]
        if expires_at is None or int(expires_at) > int(time.time()):
            space_row = await fed_repo.get_space(row["space_id"])
            if (
                space_row is not None
                and space_row.status == "active"
                and not space_row.withdrawn
            ):
                space = {
                    "name": space_row.name,
                    "accent_color": space_row.accent_color,
                    "space_id": space_row.space_id,
                }
    server_name = (await admin_repo.get_config("server_name")) or cfg.server_name
    html = _render_invite_page(
        token=token,
        space=space,
        server_name=server_name,
        base_url=cfg.base_url,
    )
    return web.Response(
        text=html,
        content_type="text/html",
        status=200 if space else 404,
    )
