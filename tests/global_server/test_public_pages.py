"""Tests for the public SSR pages (§24.7 / §24.8)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import DEFAULT_TRUSTED_PROXIES, GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.public import (
    PUBLISH_MAX_PER_MINUTE,
    RATE_LIMIT_MAX_TRACKED_IPS,
    ClientIpResolver,
    SlidingWindowCounter,
)
from socialhome.global_server.server import create_gfs_app


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


# ─── Landing page ────────────────────────────────────────────────────


async def test_landing_renders_server_name(client):
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "My Global Server" in text
    # QR img tag in Connect section.
    assert 'src="data:image/png;base64,' in text


async def test_landing_renders_updated_server_name_from_db(client):
    app = client._app
    await app[gfs_admin_repo_key].set_config(
        "server_name",
        "Pascal's GFS",
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Pascal&#x27;s GFS" in text or "Pascal's GFS" in text


async def test_landing_lists_active_spaces_only(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-active",
            owning_instance="o.home",
            name="Active Space",
            description="hello",
            accent_color="#ff0000",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending Space",
            status="pending",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Active Space" in text
    assert "Pending Space" not in text


async def test_landing_search_filters(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-makers",
            owning_instance="o.home",
            name="Makers Space",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-garden",
            owning_instance="o.home",
            name="Garden Club",
            status="active",
        )
    )
    resp = await client.get("/?search=makers")
    text = await resp.text()
    assert "Makers Space" in text
    assert "Garden Club" not in text


async def test_landing_category_filter(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="gaming-sp",
            owning_instance="o.home",
            name="Gaming Guild",
            status="active",
            category="gaming",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="tech-sp",
            owning_instance="o.home",
            name="Tech Talk",
            status="active",
            category="tech",
        )
    )
    # ?category=gaming keeps only the gaming space.
    resp = await client.get("/?category=gaming")
    text = await resp.text()
    assert "Gaming Guild" in text
    assert "Tech Talk" not in text


async def test_landing_shows_category_label(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="outdoors-sp",
            owning_instance="o.home",
            name="Trail Crew",
            status="active",
            category="sports_outdoors",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Sports &amp; outdoors" in text or "Sports & outdoors" in text


async def test_landing_no_filter_shows_all_active(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="gaming-sp",
            owning_instance="o.home",
            name="Gaming Guild",
            status="active",
            category="gaming",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="tech-sp",
            owning_instance="o.home",
            name="Tech Talk",
            status="active",
            category="tech",
        )
    )
    resp = await client.get("/")
    text = await resp.text()
    assert "Gaming Guild" in text
    assert "Tech Talk" in text


async def test_landing_renders_category_tabs(client):
    """The filter row renders an All tab + a per-category tab, and the
    ``.filters`` row uses ``flex-wrap`` so 10 tabs wrap on narrow screens."""
    resp = await client.get("/")
    text = await resp.text()
    assert 'href="/?category=gaming"' in text
    # Assert a tab *label* that isn't also a plausible space name — the
    # HTML-escaped "Hobby & crafts" only appears as a filter tab.
    assert "Hobby &amp; crafts" in text
    assert "flex-wrap" in text


async def test_landing_unknown_category_shows_all_with_all_tab_active(client):
    """An unknown ``?category=`` value falls back to All: 200, every active
    space shown, and the All tab carries ``active`` (not a category tab)."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-game",
            owning_instance="o.home",
            name="Gaming Guild",
            status="active",
            category="gaming",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-tech",
            owning_instance="o.home",
            name="Tech Talk",
            status="active",
            category="tech",
        )
    )
    resp = await client.get("/?category=bogus")
    assert resp.status == 200
    text = await resp.text()
    # All spaces remain visible regardless of the bogus filter value.
    assert "Gaming Guild" in text
    assert "Tech Talk" in text
    # The All tab is active; no category tab matched the unknown value.
    assert '<a href="/" class="active">All</a>' in text


async def test_landing_listing_rate_limit(client):
    """Spec §24.7.3: 30 GETs/min/IP on the public listing."""
    for _ in range(30):
        resp = await client.get("/")
        assert resp.status == 200
    resp = await client.get("/")
    assert resp.status == 429


# ─── Space page ───────────────────────────────────────────────────────


async def test_space_page_renders_deep_link(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-deep",
            owning_instance="o.home",
            name="Deep Link Space",
            description="about",
            accent_color="#112233",
            status="active",
        )
    )
    resp = await client.get("/spaces/sp-deep")
    assert resp.status == 200
    text = await resp.text()
    assert "sh://join-space/http://gfs.test/spaces/sp-deep" in text
    assert 'property="og:title"' in text


async def test_space_page_renders_icon_and_brand_colors(client):
    """The per-space page renders the icon avatar + the space's real theme
    colours (primary), so the GFS page reflects the space's brand."""
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o2.home",
            display_name="O2",
            public_key="bb" * 32,
            inbox_url="http://o2/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-brand",
            owning_instance="o2.home",
            name="Branded",
            description="about",
            cover_url="data:image/webp;base64,Y292ZXI=",
            icon_url="data:image/webp;base64,aWNvbg==",
            accent_color="#445566",
            primary_color="#112233",
            status="active",
        )
    )
    resp = await client.get("/spaces/sp-brand")
    assert resp.status == 200
    text = await resp.text()
    assert 'class="space-avatar"' in text
    assert "data:image/webp;base64,aWNvbg==" in text  # icon
    assert "data:image/webp;base64,Y292ZXI=" in text  # cover
    assert "#112233" in text  # primary colour applied to --primary


async def test_space_page_404_for_pending_or_banned(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="sp-pending",
            owning_instance="o.home",
            name="Pending",
            status="pending",
        )
    )
    resp = await client.get("/spaces/sp-pending")
    assert resp.status == 404


# ─── Invite page ──────────────────────────────────────────────────────


async def test_invite_page_known_token(client):
    app = client._app
    fed_repo = app[gfs_fed_repo_key]
    admin_repo = app[gfs_admin_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="o.home",
            display_name="O",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id="inv-sp",
            owning_instance="o.home",
            name="Invite Me",
            accent_color="#aabbcc",
            status="active",
        )
    )
    # Seed a valid invite-token row.
    await admin_repo._db.enqueue(
        "INSERT INTO gfs_invite_tokens(gfs_token, space_id, "
        "source_instance_id, max_uses) VALUES(?, ?, ?, ?)",
        ("invtok-1", "inv-sp", "o.home", 5),
    )
    resp = await client.get("/join/invtok-1")
    assert resp.status == 200
    text = await resp.text()
    assert "Invite Me" in text
    assert "sh://gfs-invite/http://gfs.test/join/invtok-1" in text


async def test_invite_page_unknown_token_404(client):
    resp = await client.get("/join/no-such-token")
    assert resp.status == 404


# ─── Pairing token rate-limit (spec §24.7.4) ──────────────────────────


async def test_pairing_token_rate_limit_per_ip(client):
    """A fresh token is issued on first visit; second visit within the
    30-second window gets a ``please-wait`` placeholder token instead.
    """
    resp = await client.get("/")
    assert resp.status == 200
    text_a = await resp.text()
    # Second immediate visit — rate-limited.
    resp = await client.get("/")
    text_b = await resp.text()
    assert "please-wait" in text_b
    # (The first token was real.)
    assert "please-wait" not in text_a


# ─── Pairing-code copy/paste fallback (socialhome:// scheme) ──────────


async def test_landing_renders_socialhome_pair_code(client):
    """The landing page renders a ``socialhome://gfs-pair/{base_url}
    ?token={token}`` pairing code in a ``<code>`` element next to the
    QR, plus a Copy-code button. Replaces the old ``sh://`` scheme
    that the SPA doesn't recognise."""
    resp = await client.get("/")
    assert resp.status == 200
    text = await resp.text()
    # New scheme present in the rendered HTML.
    assert "socialhome://gfs-pair/http://gfs.test?token=" in text
    # Old scheme is gone — operators who script against the landing
    # shouldn't get away with two parallel formats.
    assert "sh://gfs-pair" not in text
    # Copy button + the code id the inline JS shim wires to.
    assert 'id="pair-code"' in text
    assert 'id="copy-pair-btn"' in text


async def test_landing_pair_code_carries_token_in_data_attr(client):
    """The code element carries ``data-pair-token`` for tests + the
    inline JS shim — so screen-scraper tooling can extract the raw
    token without parsing the full URL."""
    resp = await client.get("/")
    text = await resp.text()
    assert "data-pair-token=" in text


# ─── Client-IP resolution / trusted proxies ──────────────────────────


def _fake_request(peer: str, xff: str | None = None):
    """Minimal stand-in for ``web.Request`` — headers + transport peername."""
    headers: dict[str, str] = {} if xff is None else {"X-Forwarded-For": xff}
    return SimpleNamespace(
        headers=headers,
        transport=SimpleNamespace(get_extra_info=lambda _k: (peer, 40000)),
        remote=peer,
    )


def test_client_ip_ignores_forwarded_for_from_untrusted_peer():
    """A direct internet peer can rotate ``X-Forwarded-For`` freely — believing
    it would let one attacker mint an unlimited number of rate-limit buckets."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    assert resolve(_fake_request("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_client_ip_uses_last_forwarded_entry_from_trusted_peer():
    """Behind a trusted proxy the LAST entry is the one that proxy appended —
    every entry to its left was supplied by the client and is forgeable."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    req = _fake_request("127.0.0.1", "9.9.9.9, 198.51.100.7")
    assert resolve(req) == "198.51.100.7"


def test_client_ip_ignores_client_supplied_forwarded_prefix():
    """A client that pre-seeds its own ``X-Forwarded-For`` before hitting a
    trusted proxy gains nothing: the proxy appends the real peer, and the last
    entry wins."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    a = resolve(_fake_request("127.0.0.1", "evil, 198.51.100.7"))
    b = resolve(_fake_request("127.0.0.1", "other, other2, 198.51.100.7"))
    assert a == b == "198.51.100.7"


def test_client_ip_default_trusts_private_and_loopback_peers():
    """Docker / reverse-proxy deployments (proxy on the same host or private
    network) keep per-client limiting with no configuration."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    for peer in ("127.0.0.1", "10.1.2.3", "172.16.4.5", "192.168.1.1", "::1"):
        assert resolve(_fake_request(peer, "1.2.3.4")) == "1.2.3.4", peer
    assert resolve(_fake_request("fd00::1", "1.2.3.4")) == "1.2.3.4"


def test_client_ip_empty_trusted_list_never_honours_forwarded_for():
    """``trusted_proxies = []`` is the internet-facing posture."""
    resolve = ClientIpResolver(())
    assert resolve(_fake_request("127.0.0.1", "1.2.3.4")) == "127.0.0.1"


def test_client_ip_honours_an_explicitly_configured_proxy():
    resolve = ClientIpResolver(("198.51.100.0/24",))
    assert resolve(_fake_request("198.51.100.1", "1.2.3.4")) == "1.2.3.4"
    assert resolve(_fake_request("203.0.113.1", "1.2.3.4")) == "203.0.113.1"


def test_client_ip_falls_back_to_peer_on_garbage_forwarded_for():
    """A malformed last entry is not an IP — fall back to the real peer rather
    than tracking an attacker-chosen string."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    assert resolve(_fake_request("127.0.0.1", "not-an-ip")) == "127.0.0.1"
    assert resolve(_fake_request("127.0.0.1", "")) == "127.0.0.1"


def test_client_ip_without_transport_is_unknown():
    req = SimpleNamespace(headers={}, transport=None, remote=None)
    assert ClientIpResolver(DEFAULT_TRUSTED_PROXIES)(req) == "unknown"


def test_client_ip_unmaps_ipv4_mapped_loopback_peer():
    """A dual-stack listener reports a local proxy as ``::ffff:127.0.0.1``.
    Without unmapping that is an unrecognised IPv6 address, the proxy is not
    trusted, ``X-Forwarded-For`` is ignored — and EVERY client behind that
    proxy shares one bucket, so the whole deployment 429s after one client's
    quota."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    assert resolve(_fake_request("::ffff:127.0.0.1", "1.2.3.4")) == "1.2.3.4"
    assert resolve(_fake_request("::ffff:10.0.0.5", "1.2.3.4")) == "1.2.3.4"


def test_client_ip_unmaps_ipv4_mapped_untrusted_peer_to_one_bucket():
    """``::ffff:203.0.113.9`` and ``203.0.113.9`` are the SAME host — they must
    share a rate-limit bucket, or an attacker doubles its quota by switching
    address family."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    assert resolve(_fake_request("::ffff:203.0.113.9")) == "203.0.113.9"
    assert resolve(_fake_request("203.0.113.9")) == "203.0.113.9"


def test_client_ip_unmaps_ipv4_mapped_forwarded_entry():
    """A proxy that appends ``::ffff:198.51.100.7`` keys the same bucket as one
    that appends the dotted-quad form."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    mapped = resolve(_fake_request("127.0.0.1", "::ffff:198.51.100.7"))
    plain = resolve(_fake_request("127.0.0.1", "198.51.100.7"))
    assert mapped == plain == "198.51.100.7"


def test_client_ip_strips_an_ipv6_zone_from_a_forwarded_entry():
    """``2001:db8::1%eth0`` is the same host as ``2001:db8::1`` — the zone is a
    local interface label, not part of the address. ``_peer_ip`` already
    stripped it; the forwarded branch did not, so every zone spelling minted a
    fresh rate-limit bucket for one source."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    plain = resolve(_fake_request("127.0.0.1", "2001:db8::1"))
    assert plain == "2001:db8::1"
    for zone in ("eth0", "1", "A" * 4096):
        assert resolve(_fake_request("127.0.0.1", f"2001:db8::1%{zone}")) == plain


def test_client_ip_key_length_is_bounded_by_the_address_not_the_zone():
    """An unbounded zone string must not grow the bucket KEY — otherwise one
    source both multiplies buckets and inflates each one's memory cost."""
    resolve = ClientIpResolver(DEFAULT_TRUSTED_PROXIES)
    key = resolve(_fake_request("127.0.0.1", "2001:db8::1%" + "z" * 10_000))
    assert len(key) < 64


def test_client_ip_resolver_parses_cidrs_once():
    """The middleware must not re-parse CIDRs per request."""
    resolve = ClientIpResolver(("10.0.0.0/8", "bogus-entry"))
    # The bogus entry is dropped at construction, not re-evaluated per call.
    assert resolve(_fake_request("10.0.0.5", "1.2.3.4")) == "1.2.3.4"
    assert resolve(_fake_request("203.0.113.5", "1.2.3.4")) == "203.0.113.5"


# ─── Rate-limit counter bounds ───────────────────────────────────────


def test_rate_limit_counter_is_bounded():
    """The counter key is attacker-influenced (one bucket per source IP), so
    an unbounded dict is a slow memory exhaustion."""
    counter = SlidingWindowCounter(limit=5)
    for i in range(20_000):
        counter.allow(f"198.51.{i // 256 % 256}.{i % 256}:{i}")
    assert len(counter) <= RATE_LIMIT_MAX_TRACKED_IPS


def test_rate_limit_counter_drops_expired_hits():
    """A key whose window has rolled over starts fresh rather than growing."""
    counter = SlidingWindowCounter(limit=2)
    assert counter.allow("a", now=1000.0)
    assert counter.allow("a", now=1000.1)
    assert not counter.allow("a", now=1000.2)
    # 61 s later the window has rolled over.
    assert counter.allow("a", now=1061.0)


def test_rate_limit_counter_evicts_least_recent_first():
    counter = SlidingWindowCounter(limit=5, max_keys=3)
    for key in ("a", "b", "c"):
        counter.allow(key, now=1000.0)
    counter.allow("a", now=1000.5)  # refresh recency of "a"
    counter.allow("d", now=1001.0)
    assert len(counter) == 3
    assert "b" not in counter


async def test_publish_limiter_cannot_be_bypassed_by_spoofed_forwarded_for(
    tmp_dir,
):
    """The end-to-end bypass: an internet-facing GFS (no trusted proxies) must
    shed a flood even when every request carries a fresh ``X-Forwarded-For``."""
    cfg = replace(_config(tmp_dir), trusted_proxies=())
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        body = {
            "space_id": "sp-spoof",
            "event_type": "space_post_public",
            "payload": {"authority_sig": "x"},
        }
        statuses = []
        for i in range(PUBLISH_MAX_PER_MINUTE + 5):
            resp = await tc.post(
                "/gfs/publish",
                json=body,
                headers={"X-Forwarded-For": f"10.9.{i // 256}.{i % 256}"},
            )
            statuses.append(resp.status)
        assert 429 in statuses
        assert statuses[-1] == 429


async def test_publish_limiter_buckets_per_client_behind_a_trusted_proxy(
    tmp_dir,
):
    """With the loopback TestServer peer trusted, each distinct LAST entry is
    its own bucket — a real reverse-proxy deployment keeps per-client limits."""
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        body = {
            "space_id": "sp-proxy",
            "event_type": "space_post_public",
            "payload": {"authority_sig": "x"},
        }
        for _ in range(PUBLISH_MAX_PER_MINUTE):
            resp = await tc.post(
                "/gfs/publish",
                json=body,
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
            assert resp.status != 429
        # Same client → shed.
        resp = await tc.post(
            "/gfs/publish",
            json=body,
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert resp.status == 429
        # A DIFFERENT client behind the same proxy is unaffected.
        resp = await tc.post(
            "/gfs/publish",
            json=body,
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        assert resp.status != 429
        # A client-supplied prefix does not buy a fresh bucket — the proxy's
        # appended last entry is what counts.
        resp = await tc.post(
            "/gfs/publish",
            json=body,
            headers={"X-Forwarded-For": "1.1.1.1, 198.51.100.1"},
        )
        assert resp.status == 429
