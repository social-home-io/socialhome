"""GFS runtime configuration (spec §24.5).

Loaded with layered precedence:

1. **Runtime overrides in ``server_config`` table** — the admin portal
   writes here; wins over everything for the keys it owns (server_name,
   landing_markdown, header_image_file, auto_accept_clients,
   auto_accept_spaces, fraud_threshold, admin_password_hash).
2. **Environment variables** (``GFS_HOST``, ``GFS_PORT``, ``GFS_BASE_URL``,
   ``GFS_DATA_DIR``, ``GFS_DB_PATH``, ``GFS_INSTANCE_ID``,
   ``GFS_SIGNING_SEED`` — 64 hex chars, the Ed25519 identity seed;
   ``GFS_TRUSTED_PROXIES`` — comma-separated IPs/CIDRs, empty to clear;
   ``GFS_WRITE_BATCH_WINDOW_MS`` — the DB write-coalescing window;
   ``GFS_OPEN_SIGNUP`` — ``true``/``false``, the ``[policy] open_signup``
   switch; ``GFS_PUBLIC_VIEWER_BASE_URL`` — the pinned viewer the public
   pages redirect to, empty for the pages this server renders itself;
   ``GFS_LANDING_SPACE_LIST`` — ``true``/``false``, whether ``GET /``
   lists the active spaces; ``GFS_SPACE_MEDIA_MAX_BYTES`` /
   ``_TTL_SECONDS`` / ``_SPACE_MAX_BYTES`` / ``_SPACE_UPLOADS_PER_HOUR`` —
   the ``[space_media]`` cache bounds)
   — override the matching ``[server]`` key when set, so an orchestrator can retarget a
   single value (e.g. a per-instance port) without rewriting the file.
   This mirrors :class:`socialhome.config.Config` (env > file > defaults).
   Only ``[server]`` scalars (plus ``open_signup``) have env bindings;
   the rest of branding/policy/webrtc/cluster is file- and DB-owned. Unset vars leave the file value intact.
3. **TOML file** at the path passed via ``--config``, or
   ``$SOCIAL_HOME_GFS_CONFIG``, or ``$SOCIAL_HOME_GFS_DATA/global_server.toml``,
   or ``./global_server.toml``.
4. **Dataclass defaults** — safe values for a fresh node.

Note: the shipped image (``Dockerfile.gfs``) deliberately does NOT bake
``GFS_HOST``/``GFS_PORT`` — that would pin them at layer 2 and silently
shadow the ``[server]`` keys of any ``--config`` file (see issue #563).
The env vars are an opt-in override, not an image default.

Separate from :class:`socialhome.config.Config` because the GFS is a
different deploy artifact with its own sections. Import-safe: does not
pull in any core services.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from socialhome.config import PROJECT_PINNED_VIEWER_URL
from socialhome.db.database import DEFAULT_WRITE_BATCH_WINDOW_MS

from .node_route import valid_route_tag
from .peer_url import normalized_peer_url

log = logging.getLogger(__name__)


DEFAULT_DATA_DIR = "/var/lib/sh-gfs"

#: Lifetime of a TURN credential minted for the public-highlight viewer. A
#: viewer uses it within seconds (it fetches one per connection, and a
#: highlight download is short); coturn refuses every request — a refresh of
#: an existing allocation included — once it has passed, so a scraped one is
#: worthless after 10 minutes.
DEFAULT_TURN_TTL_SECONDS: int = 600

#: Bounds on ``[webrtc] turn_ttl_seconds``: below a minute the viewer may not
#: finish gathering candidates; above a day "short-lived" means nothing.
MIN_TURN_TTL_SECONDS: int = 60
MAX_TURN_TTL_SECONDS: int = 86_400
DEFAULT_CONFIG_FILENAME = "global_server.toml"

#: ``[space_media]`` — the encrypted space media cache
#: (:mod:`.space_media`). A cache, not storage: the operator sizes it to the
#: disk they have. All ciphertext this server holds for spaces, in bytes;
#: ``0`` turns the cache off (it is then not advertised).
DEFAULT_SPACE_MEDIA_MAX_BYTES: int = 5 * 1024**3
#: How long a blob is kept; an uploader's longer expiry is clamped to it.
DEFAULT_SPACE_MEDIA_TTL_SECONDS: int = 3 * 86_400
#: Bounds on ``ttl_seconds``: under 10 minutes a reader that comes back
#: after a short sleep finds nothing; over a week it is storage, not a cache.
MIN_SPACE_MEDIA_TTL_SECONDS: int = 600
MAX_SPACE_MEDIA_TTL_SECONDS: int = 7 * 86_400
#: Live ciphertext one space (or private space's channel) may hold: past
#: it, that scope's own least recently fetched blobs are evicted.
DEFAULT_SPACE_MEDIA_SPACE_MAX_BYTES: int = 256 * 1024**2
#: Blobs one space (or channel) may upload per hour (429 past it).
DEFAULT_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR: int = 300
#: Bytes one client network prefix (an IPv6 /48, an IPv4 /24) may upload per hour, whatever
#: scopes it uses — the backstop behind fair eviction (429 past it).
DEFAULT_SPACE_MEDIA_IP_BYTES_PER_HOUR: int = 1024**3


@dataclass(slots=True, frozen=True)
class SpaceMediaOnDemandConfig:
    """``[space_media]`` on-demand keys (v_83,
    :mod:`.space_media_on_demand`): when a reader misses a blob, whether this
    server asks a holder household to re-upload it into the cache or to
    stream it straight through, and what bounds that.

    * ``enabled`` (``on_demand``) — answer ``wanted`` requests at all.
    * Cache, via re-upload: a blob asked ``cache_after_requests`` times
      within ``request_window_seconds``; or one this server stored no
      longer than ``fresh_seconds`` ago, while the cache is under
      ``full_percent`` of ``max_bytes``. Everything else — old content, a
      one-off view, anything while the cache is that full — streams.
    * ``coalesce_seconds`` — after a blob was asked of a holder, a repeat
      on the same node within it joins or does nothing; one re-upload per
      blob per window cluster-wide.
    * ``stream_wait_seconds`` — the longest a reader waits for a holder.
    * ``max_streams`` / ``scope_max_streams`` — holder sessions open at once
      per node, and per scope per node (each buffers ≤ one 8 MiB blob).
    * ``scope_stream_bytes_per_hour`` — bytes one scope may stream an hour
      (cluster-wide, shared DB).
    * ``prefix_wanted_per_hour`` — holder requests one reader network
      prefix (IPv6 /48, IPv4 /24) may trigger an hour (cluster-wide)."""

    enabled: bool = True
    fresh_seconds: int = 7 * 86_400
    cache_after_requests: int = 3
    request_window_seconds: int = 3600
    coalesce_seconds: int = 60
    full_percent: int = 95
    stream_wait_seconds: int = 25
    max_streams: int = 16
    scope_max_streams: int = 4
    scope_stream_bytes_per_hour: int = 512 * 1024**2
    prefix_wanted_per_hour: int = 240

    @classmethod
    def parse(
        cls, table: dict, env: dict[str, str] | None = None, *, base=None
    ) -> SpaceMediaOnDemandConfig:
        """From a ``[space_media]`` table (keys absent keep *base*'s values,
        else the defaults), then the ``GFS_SPACE_MEDIA_<KEY>`` env vars.
        :class:`ValueError` names the bad key."""
        out = base if base is not None else cls()
        values: dict[str, object] = {}
        for key, (lo, hi) in _ON_DEMAND_BOUNDS.items():
            env_key = f"GFS_SPACE_MEDIA_{key.upper()}"
            if env is not None and env_key in env:
                raw: object = env[env_key]
            elif env is None and key in table:
                raw = table[key]
            else:
                continue
            values[key] = _on_demand_int(key, raw, lo, hi)
        enabled = out.enabled
        if env is not None and "GFS_SPACE_MEDIA_ON_DEMAND" in env:
            enabled = _on_demand_bool(env["GFS_SPACE_MEDIA_ON_DEMAND"])
        elif env is None and "on_demand" in table:
            enabled = _on_demand_bool(table["on_demand"])
        return replace(out, enabled=enabled, **values)


#: The integer on-demand keys and their inclusive bounds.
_ON_DEMAND_BOUNDS: dict[str, tuple[int, int]] = {
    "fresh_seconds": (0, 365 * 86_400),
    "cache_after_requests": (1, 1000),
    "request_window_seconds": (60, 7 * 86_400),
    "coalesce_seconds": (1, 3600),
    "full_percent": (1, 100),
    "stream_wait_seconds": (1, 60),
    "max_streams": (1, 256),
    "scope_max_streams": (1, 64),
    "scope_stream_bytes_per_hour": (1, 1 << 40),
    "prefix_wanted_per_hour": (1, 1_000_000),
}


def _on_demand_int(key: str, raw: object, lo: int, hi: int) -> int:
    value: int | None = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        value = int(raw)
    if value is None:
        raise ValueError(f"[space_media] {key} must be an integer")
    if not lo <= value <= hi:
        raise ValueError(f"[space_media] {key} must be {lo}..{hi}, got {value}")
    return value


def _on_demand_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"[space_media] on_demand must be true or false, got {raw!r}")


@dataclass(slots=True, frozen=True)
class EnvelopeMediaConfig:
    """``[envelope_relay]`` media keys (v_87, :mod:`.envelope_relay`): the
    binary relay media frames (``\\x00SHR``) ``POST /gfs/envelope`` carries
    for households reached only through this server. Their budgets are
    separate from the JSON envelopes', so media can never crowd a household's
    control envelopes out.

    * ``media`` — carry media frames at all (and advertise ``envelope_media``).
    * ``media_ip_bytes_per_minute`` — bytes one client address may post as
      media frames a minute (per node; 429 past it).
    * ``media_queue_bytes_per_recipient`` — media bytes queued for one
      offline household (tail-drop past it).
    * ``media_queue_ttl_seconds`` — how long a queued media frame waits.
    * ``media_queue_max_total_bytes`` — media bytes queued for everyone.
    * ``media_max_inflight_bytes`` — media bytes this node holds in memory
      between the answer and the push / enqueue (dropped past it)."""

    enabled: bool = True
    media_ip_bytes_per_minute: int = 64 * 1024**2
    media_queue_bytes_per_recipient: int = 16 * 1024**2
    media_queue_ttl_seconds: int = 3600
    media_queue_max_total_bytes: int = 1024**3
    media_max_inflight_bytes: int = 64 * 1024**2

    @classmethod
    def parse(
        cls, table: dict, env: dict[str, str] | None = None, *, base=None
    ) -> EnvelopeMediaConfig:
        """From an ``[envelope_relay]`` table (keys absent keep *base*'s
        values, else the defaults), then the ``GFS_ENVELOPE_RELAY_<KEY>`` env
        vars. :class:`ValueError` names the bad key."""
        out = base if base is not None else cls()
        values: dict[str, object] = {}
        for key, (lo, hi) in _ENVELOPE_MEDIA_BOUNDS.items():
            env_key = f"GFS_ENVELOPE_RELAY_{key.upper()}"
            if env is not None and env_key in env:
                raw: object = env[env_key]
            elif env is None and key in table:
                raw = table[key]
            else:
                continue
            values[key] = _bounded_int("envelope_relay", key, raw, lo, hi)
        enabled = out.enabled
        if env is not None and "GFS_ENVELOPE_RELAY_MEDIA" in env:
            enabled = _bool("envelope_relay", "media", env["GFS_ENVELOPE_RELAY_MEDIA"])
        elif env is None and "media" in table:
            enabled = _bool("envelope_relay", "media", table["media"])
        return replace(out, enabled=enabled, **values)


@dataclass(slots=True, frozen=True)
class EnvelopeMailboxConfig:
    """``[envelope_relay]`` mailbox keys (GFS redesign PR 1,
    :mod:`.mailboxes`): anonymous per-space mailboxes a household registers
    with proof of possession and claims on ``/gfs/mailbox/ws``.

    * ``mailboxes`` — offer mailboxes at all (and advertise
      ``envelope_mailbox``).
    * ``mailbox_max_per_ip`` — NEW mailboxes one client address may register
      per 24 h (per node). Counted in memory only: the address is never
      stored with, or next to, a mailbox.
    * ``mailbox_idle_days`` — a mailbox whose last claim (or, never
      claimed, its registration) is this many days old is dropped by the
      maintenance loop, with its queued frames. Counted in whole days; a
      claim socket held open refreshes it once a day.
    * ``mailbox_tickets_per_day`` — registration tickets one registered
      household may fetch per UTC day (cluster-wide, shared table). Every
      NEW mailbox spends one (:mod:`.mailbox_tickets`), so this — not a
      server-wide cap anyone could burn — bounds how many mailboxes exist.
    * ``mailbox_ticket_epoch_days`` — how long one ticket issuer key signs;
      a key's tickets stay redeemable for one more epoch after it.
    * ``mailbox_max_sockets_per_ip`` — claim sockets one client address may
      hold open at once (per node share; ×16 per /56, ×256 per /48).
    * ``mailbox_queue_max_per_recipient`` / ``mailbox_queue_bytes_per_recipient``
      — queued envelopes for ONE offline mailbox (rows / bytes, tail-drop);
      far below a household's (2000 / 64 MiB): ids are free to mint.
    * ``mailbox_queue_max_total_bytes`` — envelope bytes queued for ALL
      mailboxes; past it a new envelope is dropped (never anything evicted)
      unless its mailbox is under its reserve (20 envelopes / 64 KiB), which
      is admitted up to ``mailbox_queue_ceiling_bytes``. Households' own
      pool (1 GiB, ceiling 2 GiB) is never touched.
    * ``mailbox_media_queue_bytes_per_recipient`` /
      ``mailbox_media_queue_max_total_bytes`` — the same for binary media
      frames (tail-drop), apart from households' media room."""

    enabled: bool = True
    mailbox_max_per_ip: int = 200
    mailbox_idle_days: int = 30
    mailbox_tickets_per_day: int = 50
    mailbox_ticket_epoch_days: int = 30
    mailbox_max_sockets_per_ip: int = 512
    mailbox_queue_max_per_recipient: int = 200
    mailbox_queue_bytes_per_recipient: int = 4 * 1024**2
    mailbox_queue_max_total_bytes: int = 256 * 1024**2
    mailbox_queue_ceiling_bytes: int = 1024**3
    mailbox_media_queue_bytes_per_recipient: int = 8 * 1024**2
    mailbox_media_queue_max_total_bytes: int = 256 * 1024**2

    @classmethod
    def parse(
        cls, table: dict, env: dict[str, str] | None = None, *, base=None
    ) -> EnvelopeMailboxConfig:
        """From an ``[envelope_relay]`` table (absent keys keep *base*'s
        values, else the defaults), then the ``GFS_ENVELOPE_RELAY_<KEY>`` env
        vars. :class:`ValueError` names the bad key."""
        out = base if base is not None else cls()
        values: dict[str, object] = {}
        for key, (lo, hi) in _ENVELOPE_MAILBOX_BOUNDS.items():
            env_key = f"GFS_ENVELOPE_RELAY_{key.upper()}"
            if env is not None and env_key in env:
                raw: object = env[env_key]
            elif env is None and key in table:
                raw = table[key]
            else:
                continue
            values[key] = _bounded_int("envelope_relay", key, raw, lo, hi)
        enabled = out.enabled
        if env is not None and "GFS_ENVELOPE_RELAY_MAILBOXES" in env:
            enabled = _bool(
                "envelope_relay", "mailboxes", env["GFS_ENVELOPE_RELAY_MAILBOXES"]
            )
        elif env is None and "mailboxes" in table:
            enabled = _bool("envelope_relay", "mailboxes", table["mailboxes"])
        return replace(out, enabled=enabled, **values)


#: The integer ``[envelope_relay]`` mailbox keys and their inclusive bounds.
_ENVELOPE_MAILBOX_BOUNDS: dict[str, tuple[int, int]] = {
    "mailbox_max_per_ip": (1, 1_000_000),
    "mailbox_idle_days": (1, 3650),
    "mailbox_tickets_per_day": (1, 10_000),
    "mailbox_ticket_epoch_days": (1, 365),
    "mailbox_max_sockets_per_ip": (1, 1_000_000),
    "mailbox_queue_max_per_recipient": (1, 100_000),
    "mailbox_queue_bytes_per_recipient": (320 * 1024, 1 << 30),
    "mailbox_queue_max_total_bytes": (320 * 1024, 1 << 40),
    "mailbox_queue_ceiling_bytes": (320 * 1024, 1 << 40),
    "mailbox_media_queue_bytes_per_recipient": (528 * 1024, 1 << 30),
    "mailbox_media_queue_max_total_bytes": (528 * 1024, 1 << 40),
}


#: The integer ``[envelope_relay]`` media keys and their inclusive bounds. The
#: per-recipient queue holds at least one frame (528 KiB) and at most 1 GiB.
_ENVELOPE_MEDIA_BOUNDS: dict[str, tuple[int, int]] = {
    "media_ip_bytes_per_minute": (528 * 1024, 1 << 40),
    "media_queue_bytes_per_recipient": (528 * 1024, 1 << 30),
    "media_queue_ttl_seconds": (60, 86_400),
    "media_queue_max_total_bytes": (528 * 1024, 1 << 40),
    "media_max_inflight_bytes": (528 * 1024, 1 << 34),
}


def _bounded_int(section: str, key: str, raw: object, lo: int, hi: int) -> int:
    value: int | None = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        value = int(raw)
    if value is None:
        raise ValueError(f"[{section}] {key} must be an integer")
    if not lo <= value <= hi:
        raise ValueError(f"[{section}] {key} must be {lo}..{hi}, got {value}")
    return value


def _bool(section: str, key: str, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"[{section}] {key} must be true or false, got {raw!r}")


#: Peer networks whose ``X-Forwarded-For`` header the GFS believes by default:
#: loopback, the RFC1918 private ranges and the IPv6 unique-local block. A GFS
#: is almost always reached through a reverse proxy / ingress container that
#: sits on the same host or private network, so this default keeps per-client
#: rate limiting working with no configuration — while a peer connecting
#: straight from the internet can never spoof its own client IP. Operators who
#: expose the GFS directly (or whose proxy lives on a public address) set
#: ``[server] trusted_proxies`` / ``GFS_TRUSTED_PROXIES`` explicitly; an empty
#: list means "never believe the header".
#:
#: Two limits this default does NOT cover, both documented in
#: :class:`~socialhome.global_server.public.ClientIpResolver` and
#: ``docs/api.md``:
#:
#: * The proxy must OVERWRITE ``X-Forwarded-For``. An L4/TCP proxy (or an L7
#:   one configured to append) passes the client's own header through, so the
#:   last entry is attacker-chosen again and a single source mints unlimited
#:   rate-limit buckets. With such a front end the only safe value is ``[]``.
#: * Only the LAST entry is read (single-hop assumption). A chain of two or
#:   more trusted proxies resolves to the inner hop — the outer proxy, not the
#:   client — so every client behind the chain shares one bucket.
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
)


@dataclass(slots=True, frozen=True)
class GfsConfig:
    """Top-level GFS configuration."""

    # [server]
    host: str = "0.0.0.0"
    port: int = 8765
    base_url: str = ""  # public URL, e.g. "https://gfs.example.com"
    data_dir: str = DEFAULT_DATA_DIR
    #: This GFS's PUBLIC identity. ``GET /gfs/info`` serves it and signs it
    #: into the capability block; households pin it at pairing and sign it
    #: into every member publish, epoch notice and channel request as the
    #: addressee. It MUST be identical on every node of a cluster (the nodes
    #: share one identity key and one ``base_url``, and a load balancer sends
    #: a household's requests to any of them) — nodes are told apart by
    #: ``[cluster] node_id``, never by this.
    instance_id: str = "gfs-node-0"
    #: Former ``instance_id`` values still accepted as the ADDRESSEE of a
    #: household-signed request — a migration bridge after a change of
    #: ``instance_id``, while households re-read ``/gfs/info`` and adopt the
    #: new id. Served only inside the SIGNED capability block, as
    #: ``replaces`` (what lets a household pinned to one of them move). Drop
    #: them once the INFO log of alias use goes quiet.
    instance_id_aliases: tuple[str, ...] = ()
    #: Optional 64-hex-char (32-byte) override for this GFS's Ed25519 identity
    #: seed, for operators who inject secrets from a vault instead of letting
    #: the data dir own the key. Empty (the default) means "use the random seed
    #: persisted in the data dir". Never logged — it IS the private key.
    signing_seed_hex: str = ""
    # IPs / CIDRs of reverse proxies whose ``X-Forwarded-For`` is believed.
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES
    #: How long the SQLite writer waits for companion statements before it
    #: commits a batch, in ms. It is a floor on every write that arrives
    #: alone, so each sequential write a request makes (publish, register,
    #: relay bookkeeping, invite mint) pays it; the GFS used to run the old
    #: 500 ms default with no knob. The wait happens before ``BEGIN
    #: IMMEDIATE``, so it does not hold the file lock the other cluster nodes
    #: share — it only adds latency. ``0`` commits each statement alone.
    write_batch_window_ms: int = DEFAULT_WRITE_BATCH_WINDOW_MS
    #: Where the PINNED viewer is published (``[server]
    #: public_viewer_base_url``, ``GFS_PUBLIC_VIEWER_BASE_URL``). The public
    #: pages ``/spaces/{id}``, ``/join/{token}`` and ``/moments/{user}``
    #: answer a link-preview page that refreshes to it
    #: (:mod:`.public_viewer`, :mod:`.link_preview`); empty — or not an
    #: ``https://`` base — keeps the pages this server renders itself.
    public_viewer_base_url: str = PROJECT_PINNED_VIEWER_URL
    #: Whether the landing page (``GET /``) lists the active spaces, with
    #: their search box and category filter (``[server]
    #: landing_space_list``, ``GFS_LANDING_SPACE_LIST``). Off keeps the
    #: page to the hero and the pairing QR. Each space's own page
    #: (``/spaces/{id}``) and the household directory (``GET /gfs/spaces``)
    #: are untouched — this only hides the public list.
    landing_space_list: bool = True

    # [branding] — start values; admin portal overrides via DB.
    server_name: str = "My Global Server"
    landing_markdown: str = ""
    header_image_file: str = ""

    # [policy] — start values; admin portal overrides via DB.
    auto_accept_clients: bool = True
    auto_accept_spaces: bool = False
    fraud_threshold: int = 5
    #: Open sign-up: ``POST /gfs/signup-token`` hands any household a fresh
    #: single-use pairing token, so it can connect without scanning the QR
    #: code (the one-click "Connect to the GFS" step in household
    #: onboarding). Off by default. File- and env-owned (``GFS_OPEN_SIGNUP``),
    #: advertised inside the SIGNED ``/gfs/info`` capability block. On a
    #: public server pair it with ``auto_accept_clients = false`` — approval
    #: is the operator's lever against a flood of self-registered households.
    open_signup: bool = False

    # [admin]
    admin_password_hash: str = ""

    # [webrtc]
    stun_urls: tuple[str, ...] = ("stun:stun.l.google.com:19302",)
    #: TURN server handed to the anonymous public-highlight viewer, e.g.
    #: ``turns:turn.example.org:5349``. Served ONLY together with
    #: ``turn_secret``: every answer carries a fresh short-lived credential
    #: (TURN REST API, coturn ``use-auth-secret``) — never a static one.
    turn_url: str = ""
    #: coturn's ``static-auth-secret``. Never leaves this server: only the
    #: HMAC credentials minted with it do.
    turn_secret: str = ""
    #: Lifetime of each minted TURN credential (seconds, at least 60).
    turn_ttl_seconds: int = DEFAULT_TURN_TTL_SECONDS

    # [cluster]
    cluster_enabled: bool = False
    cluster_node_id: str = ""
    cluster_peers: tuple[str, ...] = ()
    cluster_advertise_url: str = ""
    #: This node's label for session-sticky routing (``[cluster] route_tag``,
    #: ``GFS_CLUSTER_ROUTE_TAG``): stamped into every public-highlight session
    #: and relay id this node mints, so the load balancer can send the rest of
    #: the session here (:mod:`.node_route`). Empty: ids name no node (a single
    #: node, or a balancer that cannot route by tag). 1-64 of ``A-Za-z0-9-``.
    cluster_route_tag: str = ""

    # [space_media] — the encrypted space media cache (``GFS_SPACE_MEDIA_*``).
    space_media_max_bytes: int = DEFAULT_SPACE_MEDIA_MAX_BYTES
    space_media_ttl_seconds: int = DEFAULT_SPACE_MEDIA_TTL_SECONDS
    space_media_space_max_bytes: int = DEFAULT_SPACE_MEDIA_SPACE_MAX_BYTES
    space_media_space_uploads_per_hour: int = DEFAULT_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR
    space_media_ip_bytes_per_hour: int = DEFAULT_SPACE_MEDIA_IP_BYTES_PER_HOUR
    space_media_on_demand: SpaceMediaOnDemandConfig = SpaceMediaOnDemandConfig()

    # [envelope_relay] — binary relay media frames (``GFS_ENVELOPE_RELAY_*``).
    envelope_media: EnvelopeMediaConfig = EnvelopeMediaConfig()
    #: Anonymous per-space mailboxes (``GFS_ENVELOPE_RELAY_MAILBOX*``).
    envelope_mailbox: EnvelopeMailboxConfig = EnvelopeMailboxConfig()

    # Loaded-from path, for audit + --set-password write-back.
    source_path: str = ""

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "gfs.db")

    @property
    def media_dir(self) -> str:
        return str(Path(self.data_dir) / "media")

    # ─── Loaders ────────────────────────────────────────────────────────

    @classmethod
    def from_toml(cls, path: str | Path) -> GfsConfig:
        """Load a GFS config from a TOML file on disk.

        Raises :class:`FileNotFoundError` if the file doesn't exist and
        :class:`ValueError` if a required field (``base_url``) is unset —
        public URLs, QR tokens and admin cookies all depend on it.
        """
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"GFS config not found at {p}")
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        server = data.get("server", {})
        branding = data.get("branding", {})
        policy = data.get("policy", {})
        admin = data.get("admin", {})
        webrtc = data.get("webrtc", {})
        cluster = data.get("cluster", {})
        space_media = data.get("space_media", {})
        envelope_relay = data.get("envelope_relay", {})
        default_stun = ("stun:stun.l.google.com:19302",)
        cfg = cls(
            host=str(server.get("host") or "0.0.0.0"),
            port=int(server.get("port") or 8765),
            base_url=str(server.get("base_url") or ""),
            data_dir=str(server.get("data_dir") or DEFAULT_DATA_DIR),
            instance_id=str(server.get("instance_id") or "gfs-node-0"),
            instance_id_aliases=tuple(
                str(a) for a in (server.get("instance_id_aliases") or ())
            ),
            signing_seed_hex=str(server.get("signing_seed_hex") or ""),
            # An explicitly EMPTY list must stay empty (the internet-facing
            # posture) — only a missing key falls back to the default.
            trusted_proxies=(
                tuple(str(x) for x in server["trusted_proxies"])
                if "trusted_proxies" in server
                else DEFAULT_TRUSTED_PROXIES
            ),
            server_name=str(branding.get("server_name") or "My Global Server"),
            landing_markdown=str(branding.get("landing_markdown") or ""),
            header_image_file=str(branding.get("header_image_file") or ""),
            auto_accept_clients=bool(policy.get("auto_accept_clients", True)),
            auto_accept_spaces=bool(policy.get("auto_accept_spaces", False)),
            fraud_threshold=int(policy.get("fraud_threshold", 5)),
            open_signup=bool(policy.get("open_signup", False)),
            admin_password_hash=str(admin.get("password_hash") or ""),
            stun_urls=tuple(webrtc.get("stun_urls") or default_stun),
            turn_url=str(webrtc.get("turn_url") or ""),
            turn_secret=str(webrtc.get("turn_secret") or ""),
            turn_ttl_seconds=_turn_ttl(
                webrtc.get("turn_ttl_seconds", DEFAULT_TURN_TTL_SECONDS)
            ),
            cluster_enabled=bool(cluster.get("enabled", False)),
            cluster_node_id=str(cluster.get("node_id") or ""),
            cluster_peers=_normalized_peers(cluster.get("peers") or ()),
            cluster_advertise_url=_normalized_advertise_url(
                cluster.get("advertise_url"), f"GFS config at {p}"
            ),
            cluster_route_tag=_route_tag(
                cluster.get("route_tag"), f"GFS config at {p}"
            ),
            write_batch_window_ms=_window_ms(
                server.get("write_batch_window_ms", DEFAULT_WRITE_BATCH_WINDOW_MS)
            ),
            # A missing key keeps the project viewer; an explicit "" turns
            # the redirect off (the pages render here, as before).
            public_viewer_base_url=str(
                server.get("public_viewer_base_url", PROJECT_PINNED_VIEWER_URL)
            ).strip(),
            landing_space_list=bool(server.get("landing_space_list", True)),
            space_media_max_bytes=_space_media_max_bytes(
                space_media.get("max_bytes", DEFAULT_SPACE_MEDIA_MAX_BYTES)
            ),
            space_media_ttl_seconds=_space_media_ttl(
                space_media.get("ttl_seconds", DEFAULT_SPACE_MEDIA_TTL_SECONDS)
            ),
            space_media_space_max_bytes=_space_media_positive(
                "space_max_bytes",
                space_media.get("space_max_bytes", DEFAULT_SPACE_MEDIA_SPACE_MAX_BYTES),
            ),
            space_media_space_uploads_per_hour=_space_media_positive(
                "space_uploads_per_hour",
                space_media.get(
                    "space_uploads_per_hour",
                    DEFAULT_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR,
                ),
            ),
            space_media_ip_bytes_per_hour=_space_media_positive(
                "ip_bytes_per_hour",
                space_media.get(
                    "ip_bytes_per_hour", DEFAULT_SPACE_MEDIA_IP_BYTES_PER_HOUR
                ),
            ),
            space_media_on_demand=SpaceMediaOnDemandConfig.parse(space_media),
            envelope_media=EnvelopeMediaConfig.parse(envelope_relay),
            envelope_mailbox=EnvelopeMailboxConfig.parse(envelope_relay),
            source_path=str(p),
        )
        if not cfg.base_url:
            raise ValueError(
                f"GFS config at {p} is missing [server] base_url — "
                "public URLs and pairing QRs cannot be generated without it",
            )
        return cfg._with_clean_aliases()

    def _with_clean_aliases(self) -> GfsConfig:
        """Trim + de-duplicate ``instance_id_aliases``; drop empty entries and
        the ``instance_id`` itself (an alias equal to the id is no alias)."""
        seen: list[str] = []
        for raw in self.instance_id_aliases:
            alias = str(raw).strip()
            if alias and alias != self.instance_id and alias not in seen:
                seen.append(alias)
        return replace(self, instance_id_aliases=tuple(seen))

    def check_turn(self) -> None:
        """WARN when ``[webrtc] turn_url`` is set without ``turn_secret``.

        This server hands TURN to anonymous viewers only with a fresh
        short-lived credential, so a URL without the secret is never
        served (fail closed — a static or missing credential on a public
        endpoint is an open relay). Never raises."""
        if self.turn_url.strip() and not self.turn_secret:
            log.warning(
                "[webrtc] turn_url is set but turn_secret is empty, so no TURN "
                "server is offered to public-highlight viewers. Set turn_secret "
                "(GFS_TURN_SECRET) to coturn's static-auth-secret "
                "(use-auth-secret mode); see docs/operations/turn.md.",
            )

    def check_cluster_identity(self) -> None:
        """Log an ERROR for a cluster node whose ``[cluster] node_id`` is
        empty while ``instance_id_aliases`` is set.

        ``node_id`` falls back to ``instance_id`` (kept: refusing to start
        would break existing deployments). But ``instance_id`` is the public
        identity every node of a cluster must share, so on a cluster moving
        to one shared id (aliases set) the fallback gives every node the
        same ``node_id``. Duplicate ``node_id``s are also detected at
        runtime (:class:`ClusterService`). Never raises.
        """
        if (
            self.cluster_enabled
            and not self.cluster_node_id.strip()
            and self.instance_id_aliases
        ):
            log.error(
                "[cluster] node_id is empty, so it falls back to [server] "
                "instance_id %r — which every node of the cluster shares "
                "(instance_id_aliases is set). Nodes with the same node_id "
                "cannot be told apart: set a unique [cluster] node_id on "
                "every node.",
                self.instance_id,
            )

    def check_cluster_routing(self) -> None:
        """WARN when cluster mode is on without a ``[cluster] route_tag``.

        A public-highlight viewing session lives in one node's memory; behind
        a round-robin balancer its later requests reach the other nodes
        unless the session ids name this node and the balancer routes by
        that (:mod:`.node_route`). Never raises — a cluster whose balancer
        pins every client to one node works without it."""
        if self.cluster_enabled and not self.cluster_route_tag:
            log.warning(
                "[cluster] route_tag is empty: public-highlight sessions this "
                "node starts name no node, so behind a round-robin load "
                "balancer their later requests reach other nodes and viewers "
                "fall back to the relay or fail. Set a unique route_tag "
                "(GFS_CLUSTER_ROUTE_TAG) on every node and route by it; see "
                "docs/operations/gfs-cluster.md.",
            )

    @property
    def cluster_self_url(self) -> str:
        """The URL this node tells cluster peers to reach it at.

        It is the HELLO / heartbeat / fan-out target peers dial. Defaults
        to ``base_url`` for a single-node or directly reachable
        deployment; set ``[cluster] advertise_url`` when ``base_url`` is a
        load balancer in front of every node.
        """
        return self.cluster_advertise_url or self.base_url

    def _with_env_overrides(self) -> GfsConfig:
        """Return a copy with ``GFS_*`` env vars layered on top.

        Env wins over the file/defaults this instance was built from
        (env > file > defaults), matching :class:`socialhome.config.Config`.
        Only the ``[server]`` scalars have env bindings, plus two
        exceptions: ``GFS_CLUSTER_ADVERTISE_URL`` and
        ``GFS_CLUSTER_ROUTE_TAG`` (a per-alloc value is what env is for; an
        empty or whitespace-only value counts as unset). An unset var leaves its field untouched so a single
        override (e.g. a per-instance ``GFS_PORT``) doesn't disturb the
        rest of the file.
        """
        env = os.environ
        data_dir = env.get("GFS_DATA_DIR", self.data_dir)
        # An explicit DB path pins data_dir to its parent (the filename
        # itself is always gfs.db via the ``db_path`` property). Wins
        # over GFS_DATA_DIR, mirroring the historical fallback order.
        if "GFS_DB_PATH" in env:
            data_dir = str(Path(env["GFS_DB_PATH"]).resolve().parent)
        aliases = self.instance_id_aliases
        if "GFS_INSTANCE_ID_ALIASES" in env:
            # Comma-separated; an empty value clears the list.
            aliases = tuple(
                part.strip()
                for part in env["GFS_INSTANCE_ID_ALIASES"].split(",")
                if part.strip()
            )
        trusted_proxies = self.trusted_proxies
        if "GFS_TRUSTED_PROXIES" in env:
            # Comma-separated IPs / CIDRs; an empty value clears the list.
            trusted_proxies = tuple(
                part.strip()
                for part in env["GFS_TRUSTED_PROXIES"].split(",")
                if part.strip()
            )
        # An empty / whitespace-only value is "unset", not "clear": a
        # deploy template that rendered nothing must not drop the file's
        # advertise_url and fall back to base_url (the load balancer).
        advertise_url = self.cluster_advertise_url
        raw_advertise = env.get("GFS_CLUSTER_ADVERTISE_URL", "")
        if raw_advertise.strip():
            advertise_url = _normalized_advertise_url(
                raw_advertise, "GFS_CLUSTER_ADVERTISE_URL env"
            )
        # Same "empty is unset" rule as the advertise URL.
        route_tag = self.cluster_route_tag
        raw_route_tag = env.get("GFS_CLUSTER_ROUTE_TAG", "")
        if raw_route_tag.strip():
            route_tag = _route_tag(raw_route_tag.strip(), "GFS_CLUSTER_ROUTE_TAG env")
        return replace(
            self,
            cluster_advertise_url=advertise_url,
            cluster_route_tag=route_tag,
            host=env.get("GFS_HOST", self.host),
            port=int(env["GFS_PORT"]) if "GFS_PORT" in env else self.port,
            base_url=env.get("GFS_BASE_URL", self.base_url),
            data_dir=data_dir,
            instance_id=env.get("GFS_INSTANCE_ID", self.instance_id),
            instance_id_aliases=aliases,
            signing_seed_hex=env.get("GFS_SIGNING_SEED", self.signing_seed_hex),
            trusted_proxies=trusted_proxies,
            open_signup=(
                env["GFS_OPEN_SIGNUP"].strip().lower() in ("1", "true", "yes")
                if "GFS_OPEN_SIGNUP" in env
                else self.open_signup
            ),
            write_batch_window_ms=(
                _window_ms(env["GFS_WRITE_BATCH_WINDOW_MS"])
                if "GFS_WRITE_BATCH_WINDOW_MS" in env
                else self.write_batch_window_ms
            ),
            # The TURN secret is what a vault injects; the URL and TTL ride
            # along so a deploy template can set all three the same way.
            public_viewer_base_url=env.get(
                "GFS_PUBLIC_VIEWER_BASE_URL", self.public_viewer_base_url
            ).strip(),
            landing_space_list=(
                env["GFS_LANDING_SPACE_LIST"].strip().lower() in ("1", "true", "yes")
                if "GFS_LANDING_SPACE_LIST" in env
                else self.landing_space_list
            ),
            turn_url=env.get("GFS_TURN_URL", self.turn_url),
            turn_secret=env.get("GFS_TURN_SECRET", self.turn_secret),
            turn_ttl_seconds=(
                _turn_ttl(env["GFS_TURN_TTL_SECONDS"])
                if "GFS_TURN_TTL_SECONDS" in env
                else self.turn_ttl_seconds
            ),
            # The cache is sized to the disk an orchestrator gives the alloc.
            space_media_max_bytes=(
                _space_media_max_bytes(env["GFS_SPACE_MEDIA_MAX_BYTES"])
                if "GFS_SPACE_MEDIA_MAX_BYTES" in env
                else self.space_media_max_bytes
            ),
            space_media_ttl_seconds=(
                _space_media_ttl(env["GFS_SPACE_MEDIA_TTL_SECONDS"])
                if "GFS_SPACE_MEDIA_TTL_SECONDS" in env
                else self.space_media_ttl_seconds
            ),
            space_media_space_max_bytes=(
                _space_media_positive(
                    "space_max_bytes", env["GFS_SPACE_MEDIA_SPACE_MAX_BYTES"]
                )
                if "GFS_SPACE_MEDIA_SPACE_MAX_BYTES" in env
                else self.space_media_space_max_bytes
            ),
            space_media_space_uploads_per_hour=(
                _space_media_positive(
                    "space_uploads_per_hour",
                    env["GFS_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR"],
                )
                if "GFS_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR" in env
                else self.space_media_space_uploads_per_hour
            ),
            space_media_ip_bytes_per_hour=(
                _space_media_positive(
                    "ip_bytes_per_hour", env["GFS_SPACE_MEDIA_IP_BYTES_PER_HOUR"]
                )
                if "GFS_SPACE_MEDIA_IP_BYTES_PER_HOUR" in env
                else self.space_media_ip_bytes_per_hour
            ),
            space_media_on_demand=SpaceMediaOnDemandConfig.parse(
                {}, dict(env), base=self.space_media_on_demand
            ),
            envelope_media=EnvelopeMediaConfig.parse(
                {}, dict(env), base=self.envelope_media
            ),
            envelope_mailbox=EnvelopeMailboxConfig.parse(
                {}, dict(env), base=self.envelope_mailbox
            ),
        )._with_clean_aliases()

    @classmethod
    def from_env_fallback(cls) -> GfsConfig:
        """Build a GFS config from defaults + the ``GFS_*`` env vars.

        Used when no TOML is discoverable (mostly unit tests and the
        existing dev scripts). ``base_url`` is inferred from the resolved
        host + port as ``http://host:port`` when neither env nor a file
        supplies one — good enough for loopback; production should set it.
        """
        cfg = cls()._with_env_overrides()
        if not cfg.base_url:
            cfg = replace(cfg, base_url=f"http://{cfg.host}:{cfg.port}")
        return cfg

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> GfsConfig:
        """Discover + load the GFS config, then layer env overrides.

        Search order for the TOML:
          1. Explicit ``config_path`` argument.
          2. ``$SOCIAL_HOME_GFS_CONFIG``.
          3. ``$SOCIAL_HOME_GFS_DATA/global_server.toml``.
          4. ``./global_server.toml``.
          5. Env-var fallback (no file — dev / loopback path).

        Whichever base is chosen, ``GFS_*`` environment variables are
        applied on top (env > file > defaults). See the module docstring.
        """
        candidates: list[Path] = []
        if config_path:
            candidates.append(Path(config_path))
        env_cfg = os.environ.get("SOCIAL_HOME_GFS_CONFIG")
        if env_cfg:
            candidates.append(Path(env_cfg))
        env_data = os.environ.get("SOCIAL_HOME_GFS_DATA")
        if env_data:
            candidates.append(Path(env_data) / DEFAULT_CONFIG_FILENAME)
        candidates.append(Path(DEFAULT_CONFIG_FILENAME))
        for candidate in candidates:
            if candidate.is_file():
                return cls.from_toml(candidate)._with_env_overrides()
        return cls.from_env_fallback()


def _normalized_peers(raw: object) -> tuple[str, ...]:
    """``[cluster] peers``, each through the same normaliser as an admin
    add-peer — so a configured URL matches the stored row's URL and the
    announce HELLO can name its recipient. An unusable entry is dropped
    with a WARNING (logged with ``ascii()``, so no raw control character
    reaches the log)."""
    if not isinstance(raw, (list, tuple)):
        log.warning("GFS: [cluster] peers must be a list; ignoring %s", ascii(raw))
        return ()
    peers: list[str] = []
    for entry in raw:
        url = normalized_peer_url(entry)
        if not url:
            log.warning("GFS: ignoring unusable [cluster] peers entry %s", ascii(entry))
        elif url not in peers:
            peers.append(url)
    return tuple(peers)


def _normalized_advertise_url(raw: object, origin: str) -> str:
    """``[cluster] advertise_url`` through the peer-URL normaliser.

    Empty / unset means "use ``base_url``". A non-empty value that does
    not normalise is a config error (fail-safe): silently falling back
    would advertise the load balancer and reintroduce ``wrong_recipient``.
    """
    if raw is None or raw == "":
        return ""
    url = normalized_peer_url(raw)
    if not url:
        raise ValueError(
            f"{origin} has an unusable [cluster] advertise_url {raw!r} — it "
            "must be an http(s) base URL (scheme + host [+ port, path "
            "prefix]; no query, fragment or credentials)"
        )
    return url


def _route_tag(raw: object, origin: str) -> str:
    """``[cluster] route_tag``: empty / unset means "no tag". Anything else
    must be a usable tag — a silently dropped one would leave every session
    of this node unroutable, so it is a config error (fail-safe)."""
    if raw is None or raw == "":
        return ""
    if not valid_route_tag(raw):
        raise ValueError(
            f"{origin} has an unusable [cluster] route_tag {raw!r} — it must "
            "be 1-64 characters of A-Z, a-z, 0-9 and '-'"
        )
    return str(raw)


def _turn_ttl(raw: int | str) -> int:
    """Parse ``turn_ttl_seconds``; outside 60 s – 1 day is a config error."""
    value = int(raw)
    if not MIN_TURN_TTL_SECONDS <= value <= MAX_TURN_TTL_SECONDS:
        raise ValueError(
            f"turn_ttl_seconds must be {MIN_TURN_TTL_SECONDS}..{MAX_TURN_TTL_SECONDS}"
            f", got {value}"
        )
    return value


def _window_ms(raw: int | str) -> int:
    """Parse ``write_batch_window_ms``; a negative window is a config error."""
    value = int(raw)
    if value < 0:
        raise ValueError(f"write_batch_window_ms must be >= 0, got {value}")
    return value


def _space_media_max_bytes(raw: int | str) -> int:
    """Parse ``[space_media] max_bytes``; ``0`` is off, negative an error."""
    value = int(raw)
    if value < 0:
        raise ValueError(f"[space_media] max_bytes must be >= 0, got {value}")
    return value


def _space_media_ttl(raw: int | str) -> int:
    """Parse ``[space_media] ttl_seconds`` (10 minutes – 7 days)."""
    value = int(raw)
    if not MIN_SPACE_MEDIA_TTL_SECONDS <= value <= MAX_SPACE_MEDIA_TTL_SECONDS:
        raise ValueError(
            f"[space_media] ttl_seconds must be {MIN_SPACE_MEDIA_TTL_SECONDS}.."
            f"{MAX_SPACE_MEDIA_TTL_SECONDS}, got {value}"
        )
    return value


def _space_media_positive(key: str, raw: int | str) -> int:
    value = int(raw)
    if value < 1:
        raise ValueError(f"[space_media] {key} must be >= 1, got {value}")
    return value


# ─── TOML example (written by --init) ───────────────────────────────────

EXAMPLE_TOML: str = """\
[server]
# These apply as written. To retarget a single instance without editing
# the file, set the matching env var (env > file): GFS_HOST, GFS_PORT,
# GFS_BASE_URL, GFS_DATA_DIR, GFS_INSTANCE_ID, GFS_INSTANCE_ID_ALIASES,
# GFS_SIGNING_SEED, GFS_WRITE_BATCH_WINDOW_MS.
host     = "0.0.0.0"
port     = 8765
base_url = "https://gfs.example.com"
data_dir = "/var/lib/sh-gfs"
# instance_id is this GFS's public identity: /gfs/info serves it, the
# capability block is signed over it, and households pin it at pairing and
# sign it into their requests as the addressee. In a cluster it
# MUST be identical on every node (the nodes share one key and one base_url,
# and the load balancer sends a household to any of them); nodes are told
# apart by [cluster] node_id, never by this.
instance_id = "gfs-node-0"
# Former instance_id values still accepted as the addressee of a household's
# request — a migration bridge after changing instance_id, while households
# re-read /gfs/info and adopt the new id (on their next reconnect or hourly
# refresh, keyed on the unchanged identity key: /gfs/info signs these as
# "replaces", and a household moves only off an id listed there).
# Each use is logged at INFO; drop the aliases once those lines stop.
# Env override: GFS_INSTANCE_ID_ALIASES="gfs-0,gfs-1" (empty string = []).
instance_id_aliases = []
# This server's Ed25519 identity seed, 64 hex chars (32 bytes). Leave empty and
# the GFS mints a random seed on first boot and persists it as
# <data_dir>/gfs_identity.seed (0600) — that is the key every paired household
# pins, so keep the data dir with the deployment. Set it only if you inject
# secrets from a vault (env override: GFS_SIGNING_SEED). Treat it as a private
# key: anyone holding it can impersonate this connection server.
signing_seed_hex = ""
# Reverse proxies whose X-Forwarded-For header is believed when deciding the
# client IP for rate limiting. Defaults to loopback + the private ranges, which
# covers the usual "proxy container on the same host/network" deployment. Set
# to [] if the GFS is reachable directly from the internet, or list your
# proxy's public address(es) if it is not on a private network. Env override:
# GFS_TRUSTED_PROXIES="203.0.113.7,198.51.100.0/24" (empty string = []).
trusted_proxies = [
  "127.0.0.0/8", "::1/128", "10.0.0.0/8",
  "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
]
# How long (ms) the database writer waits for more writes before committing a
# batch. Every write that arrives alone waits this long, so keep it small; the
# wait holds no lock, so it is safe on a cluster sharing one database file.
# 0 commits every write on its own. Env override: GFS_WRITE_BATCH_WINDOW_MS.
write_batch_window_ms = 5
# The public pages of this server — a space's page (/spaces/<id>), an invite
# link (/join/<token>) and a person's Momentum profile (/moments/<user>) —
# open in the pinned Social Home viewer published here: a small page with the
# link-preview tags refreshes to it, everything it needs in the link's
# #fragment, and the viewer
# reads this server's JSON (/gfs/pages/...). https only (http only for
# localhost). Set "" to keep rendering those pages on this server instead.
# Env override: GFS_PUBLIC_VIEWER_BASE_URL.
public_viewer_base_url = "https://viewer.social-home.io"
# List the active spaces (with search and category filter) on the landing
# page. false keeps the page to the welcome text and the pairing QR code; each
# space's own page and the directory households read stay as they are.
# Env override: GFS_LANDING_SPACE_LIST=false.
landing_space_list = true

[branding]
server_name       = "My Global Server"
landing_markdown  = ""
header_image_file = ""

[policy]
# On a PUBLIC server (anyone can obtain a pairing token), set this to false
# and approve households in the admin console: every registered household
# can subscribe to spaces, and a flood of self-registered ones dilutes the
# offline-delivery share of member-published items (best effort, see
# docs/protocol/discovery.md "Operator notes").
auto_accept_clients = true
auto_accept_spaces  = false
fraud_threshold     = 5
# Open sign-up: hand out pairing codes automatically over
# POST /gfs/signup-token, so a household can connect from its onboarding
# with one click instead of scanning the QR code. Off by default. Rate
# limited per address and globally (still ~43k registrations a day at the
# global limit), so anyone can sign up: with open_signup on, set
# auto_accept_clients = false above and approve households in the admin
# console. Env override: GFS_OPEN_SIGNUP=true.
open_signup = false

[admin]
# bcrypt hash — do NOT edit by hand. Use:
#   socialhome-global-server --set-password --config /path/to/global_server.toml
password_hash = ""

[webrtc]
# STUN/TURN for the anonymous public-highlight viewer. TURN is offered only
# with BOTH turn_url and turn_secret, and then with a fresh short-lived
# credential per answer (coturn use-auth-secret: turn_secret = coturn's
# static-auth-secret). Never a static TURN user/password. Env overrides:
# GFS_TURN_URL, GFS_TURN_SECRET, GFS_TURN_TTL_SECONDS.
stun_urls        = ["stun:stun.l.google.com:19302"]
turn_url         = ""
turn_secret      = ""
turn_ttl_seconds = 600

[cluster]
# Several GFS nodes can gossip state over POST /cluster/sync. Membership is
# operator-approved — a node is a member only if its frames verify under a
# key this node already holds:
#   * the shared seed: nodes started with the same identity seed
#     (gfs_identity.seed / [server] signing_seed_hex) trust each other; or
#   * an admin-approved key: POST /admin/api/cluster/peers with the other
#     node's node_id, url and public_key (its own GET /admin/api/cluster
#     shows it). Do this on BOTH nodes.
# Anything else is refused; there is no trust-on-first-use. An approved key
# never changes in place — to rotate, remove the peer and add it again.
# Frames carry a timestamp that must be within 300 s of the receiver's
# clock: keep every node on NTP, or they stop syncing with each other.
# node_id MUST be unique per node — set it explicitly. Left empty it falls
# back to [server] instance_id, which is the public identity and MUST be
# identical on every node, so every node would get the same node_id (logged
# at ERROR at startup when instance_id_aliases is set, and at runtime when a
# node announces our node_id from another address; GET /admin/api/cluster
# lists it under duplicate_node_id_urls). Nodes sharing the identity seed
# compare their instance_id on HELLO / heartbeat: a sibling reporting an id
# linked through either side's instance_id_aliases is a rolling id change
# (WARNING, instance_id_transitional); any other difference is a
# misconfiguration (ERROR, instance_id_mismatches) — fix it by giving every
# node the same instance_id. Neither fails GET /healthz.
# Set [server] trusted_proxies EXPLICITLY on every cluster node.
# /cluster/sync budgets failed requests per client address (and failed
# verifies per node + address), and trusted_proxies decides that address. A
# peer whose address is flooded with junk still gets through (its frames
# verify), but forged frames from that same address can delay it, so keep
# the address hard to claim: list ONLY your real proxy in trusted_proxies
# (the default trusts every private range, so anything on a shared private
# network can claim any address through X-Forwarded-For), and make sure that
# proxy writes the client address into X-Forwarded-For itself — a TCP proxy
# that passes the client's header through needs trusted_proxies = [].
enabled = false
node_id = ""
# The URL the OTHER nodes reach THIS node at, for heartbeats and fan-out.
# Leave empty to use [server] base_url. Set it when base_url points at a
# load balancer in front of all nodes (e.g. several Nomad allocs behind one
# hostname): frames would then be routed to a random node and refused as
# wrong_recipient. Render each node's own address and port instead, e.g.
# "http://10.0.0.5:28467".
advertise_url = ""
# This node's label for session-sticky routing: every public-highlight
# session and relay id this node mints starts with "<route_tag>.", and the
# load balancer must send requests naming it here (path or JSON session_id;
# docs/operations/gfs-cluster.md has the HAProxy rules). Unique per node,
# 1-64 of A-Z a-z 0-9 '-' (e.g. the Nomad allocation id). Leave empty on a
# single node.
route_tag = ""
peers   = []

[space_media]
# A short-lived cache of encrypted space pictures and files (ciphertext this
# server can't open), so households reached only through this server see a
# space's pictures. A cache, not storage — size it to your disk:
# max_bytes is all space media held here (0 turns the cache off and stops
# advertising it); when an upload would pass it, the owner holding the most
# bytes — a household across all its spaces, or for channel blobs the
# uploader's network prefix — loses its own least recently fetched blobs first —
# never one global order and never per scope, so one household can't
# flush the others with many spaces. Blobs expire
# after ttl_seconds (600 – 604800) whatever the uploader asks. One space (or
# a private space's opaque channel) holds at most space_max_bytes live —
# past it, its own least recently fetched blobs are dropped for the new
# one — and uploads at most space_uploads_per_hour blobs an hour (429 past
# it). One client network prefix (IPv6 /48, IPv4 /24) uploads at most ip_bytes_per_hour
# bytes an hour across every scope (429 past it; counted in the shared
# database, so cluster-wide). A private space's channel takes media while
# a member household holds a seat in it. In a cluster every node shares
# data_dir, so any node serves a blob another took. Env overrides:
# GFS_SPACE_MEDIA_MAX_BYTES, GFS_SPACE_MEDIA_TTL_SECONDS,
# GFS_SPACE_MEDIA_SPACE_MAX_BYTES, GFS_SPACE_MEDIA_SPACE_UPLOADS_PER_HOUR,
# GFS_SPACE_MEDIA_IP_BYTES_PER_HOUR.
max_bytes              = 5368709120   # 5 GiB
ttl_seconds            = 259200       # 3 days
space_max_bytes        = 268435456    # 256 MiB
space_uploads_per_hour = 300
ip_bytes_per_hour      = 1073741824   # 1 GiB
# On demand (v_83): when a reader misses a blob that expired or was
# evicted, this server asks ONE household that holds the file (the space
# host first, else a connected key holder) to send the same ciphertext
# again — kept in the cache (a re-upload) when the blob was asked
# cache_after_requests times within request_window_seconds, or was stored
# here at most fresh_seconds ago while the cache is under full_percent of
# max_bytes; otherwise streamed straight to the waiting reader(s) and NOT
# kept (old content, one-off views, a cache that full). Concurrent readers
# of one blob share one request; a repeat within coalesce_seconds does
# nothing new. A reader waits at most stream_wait_seconds (1 – 60). Each node
# pipes at most max_streams at once (scope_max_streams per scope; each holds
# at most one 8 MiB blob in memory, never on disk); a scope streams at most
# scope_stream_bytes_per_hour and one reader network prefix triggers at most
# prefix_wanted_per_hour holder requests (both cluster-wide). Env overrides:
# GFS_SPACE_MEDIA_ON_DEMAND, GFS_SPACE_MEDIA_FRESH_SECONDS, … (the key in
# upper case after GFS_SPACE_MEDIA_).
on_demand                   = true
fresh_seconds               = 604800      # 7 days
cache_after_requests        = 3
request_window_seconds      = 3600
coalesce_seconds            = 60
full_percent                = 95
stream_wait_seconds         = 25
max_streams                 = 16
scope_max_streams           = 4
scope_stream_bytes_per_hour = 536870912   # 512 MiB
prefix_wanted_per_hour      = 240
"""


def write_example_config(path: str | Path) -> None:
    """Write the example TOML to *path*. Refuses to overwrite an existing file."""
    p = Path(path)
    if p.exists():
        raise FileExistsError(f"{p} already exists — refusing to overwrite")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(EXAMPLE_TOML, encoding="utf-8")


def set_password_in_toml(path: str | Path, bcrypt_hash: str) -> None:
    """Persist ``[admin] password_hash = …`` in the TOML at *path*.

    We rewrite the whole file rather than parse-and-modify to stay free
    of third-party TOML-writing libs. The existing content is read, the
    `[admin]` section's `password_hash = ""` line is replaced, and the
    result written back atomically.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.is_file() else EXAMPLE_TOML
    new_line = f'password_hash = "{bcrypt_hash}"'
    lines = text.splitlines()
    in_admin = False
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_admin = stripped == "[admin]"
            continue
        if in_admin and stripped.startswith("password_hash"):
            # Preserve any leading whitespace / commenting style.
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        # No [admin] section or no password_hash key — append both.
        lines.append("")
        lines.append("[admin]")
        lines.append(new_line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
