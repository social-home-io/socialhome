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
   switch)
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

from socialhome.db.database import DEFAULT_WRITE_BATCH_WINDOW_MS

from .peer_url import normalized_peer_url

log = logging.getLogger(__name__)


DEFAULT_DATA_DIR = "/var/lib/sh-gfs"
DEFAULT_CONFIG_FILENAME = "global_server.toml"

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
    turn_url: str = ""
    turn_secret: str = ""

    # [cluster]
    cluster_enabled: bool = False
    cluster_node_id: str = ""
    cluster_peers: tuple[str, ...] = ()
    cluster_advertise_url: str = ""

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
    def from_toml(cls, path: str | Path) -> "GfsConfig":
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
            cluster_enabled=bool(cluster.get("enabled", False)),
            cluster_node_id=str(cluster.get("node_id") or ""),
            cluster_peers=_normalized_peers(cluster.get("peers") or ()),
            cluster_advertise_url=_normalized_advertise_url(
                cluster.get("advertise_url"), f"GFS config at {p}"
            ),
            write_batch_window_ms=_window_ms(
                server.get("write_batch_window_ms", DEFAULT_WRITE_BATCH_WINDOW_MS)
            ),
            source_path=str(p),
        )
        if not cfg.base_url:
            raise ValueError(
                f"GFS config at {p} is missing [server] base_url — "
                "public URLs and pairing QRs cannot be generated without it",
            )
        return cfg._with_clean_aliases()

    def _with_clean_aliases(self) -> "GfsConfig":
        """Trim + de-duplicate ``instance_id_aliases``; drop empty entries and
        the ``instance_id`` itself (an alias equal to the id is no alias)."""
        seen: list[str] = []
        for raw in self.instance_id_aliases:
            alias = str(raw).strip()
            if alias and alias != self.instance_id and alias not in seen:
                seen.append(alias)
        return replace(self, instance_id_aliases=tuple(seen))

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

    @property
    def cluster_self_url(self) -> str:
        """The URL this node tells cluster peers to reach it at.

        It is the HELLO / heartbeat / fan-out target peers dial. Defaults
        to ``base_url`` for a single-node or directly reachable
        deployment; set ``[cluster] advertise_url`` when ``base_url`` is a
        load balancer in front of every node.
        """
        return self.cluster_advertise_url or self.base_url

    def _with_env_overrides(self) -> "GfsConfig":
        """Return a copy with ``GFS_*`` env vars layered on top.

        Env wins over the file/defaults this instance was built from
        (env > file > defaults), matching :class:`socialhome.config.Config`.
        Only the ``[server]`` scalars have env bindings, plus one
        exception: ``GFS_CLUSTER_ADVERTISE_URL`` (a per-alloc value is
        what env is for; an empty or whitespace-only value counts as
        unset). An unset var leaves its field untouched so a single
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
        return replace(
            self,
            cluster_advertise_url=advertise_url,
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
        )._with_clean_aliases()

    @classmethod
    def from_env_fallback(cls) -> "GfsConfig":
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
    def load(cls, config_path: str | Path | None = None) -> "GfsConfig":
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


def _window_ms(raw: int | str) -> int:
    """Parse ``write_batch_window_ms``; a negative window is a config error."""
    value = int(raw)
    if value < 0:
        raise ValueError(f"write_batch_window_ms must be >= 0, got {value}")
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
stun_urls   = ["stun:stun.l.google.com:19302"]
turn_url    = ""
turn_secret = ""

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
peers   = []
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
