"""GFS runtime configuration (spec §24.5).

Loaded with layered precedence:

1. **Runtime overrides in ``server_config`` table** — the admin portal
   writes here; wins over everything for the keys it owns (server_name,
   landing_markdown, header_image_file, auto_accept_clients,
   auto_accept_spaces, fraud_threshold, admin_password_hash).
2. **Environment variables** (``GFS_HOST``, ``GFS_PORT``, ``GFS_BASE_URL``,
   ``GFS_DATA_DIR``, ``GFS_DB_PATH``, ``GFS_INSTANCE_ID``,
   ``GFS_SIGNING_SEED`` — 64 hex chars, the Ed25519 identity seed;
   ``GFS_TRUSTED_PROXIES`` — comma-separated IPs/CIDRs, empty to clear)
   — override the matching ``[server]`` key when set, so an orchestrator can retarget a
   single value (e.g. a per-instance port) without rewriting the file.
   This mirrors :class:`socialhome.config.Config` (env > file > defaults).
   Only ``[server]`` scalars have env bindings; branding/policy/webrtc/
   cluster are file- and DB-owned. Unset vars leave the file value intact.
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

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path


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
    instance_id: str = "gfs-node-0"
    #: Optional 64-hex-char (32-byte) override for this GFS's Ed25519 identity
    #: seed, for operators who inject secrets from a vault instead of letting
    #: the data dir own the key. Empty (the default) means "use the random seed
    #: persisted in the data dir". Never logged — it IS the private key.
    signing_seed_hex: str = ""
    # IPs / CIDRs of reverse proxies whose ``X-Forwarded-For`` is believed.
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES

    # [branding] — start values; admin portal overrides via DB.
    server_name: str = "My Global Server"
    landing_markdown: str = ""
    header_image_file: str = ""

    # [policy] — start values; admin portal overrides via DB.
    auto_accept_clients: bool = True
    auto_accept_spaces: bool = False
    fraud_threshold: int = 5

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
            admin_password_hash=str(admin.get("password_hash") or ""),
            stun_urls=tuple(webrtc.get("stun_urls") or default_stun),
            turn_url=str(webrtc.get("turn_url") or ""),
            turn_secret=str(webrtc.get("turn_secret") or ""),
            cluster_enabled=bool(cluster.get("enabled", False)),
            cluster_node_id=str(cluster.get("node_id") or ""),
            cluster_peers=tuple(cluster.get("peers") or ()),
            source_path=str(p),
        )
        if not cfg.base_url:
            raise ValueError(
                f"GFS config at {p} is missing [server] base_url — "
                "public URLs and pairing QRs cannot be generated without it",
            )
        return cfg

    def _with_env_overrides(self) -> "GfsConfig":
        """Return a copy with ``GFS_*`` env vars layered on top.

        Env wins over the file/defaults this instance was built from
        (env > file > defaults), matching :class:`socialhome.config.Config`.
        Only the ``[server]`` scalars have env bindings; an unset var
        leaves its field untouched so a single override (e.g. a
        per-instance ``GFS_PORT``) doesn't disturb the rest of the file.
        """
        env = os.environ
        data_dir = env.get("GFS_DATA_DIR", self.data_dir)
        # An explicit DB path pins data_dir to its parent (the filename
        # itself is always gfs.db via the ``db_path`` property). Wins
        # over GFS_DATA_DIR, mirroring the historical fallback order.
        if "GFS_DB_PATH" in env:
            data_dir = str(Path(env["GFS_DB_PATH"]).resolve().parent)
        trusted_proxies = self.trusted_proxies
        if "GFS_TRUSTED_PROXIES" in env:
            # Comma-separated IPs / CIDRs; an empty value clears the list.
            trusted_proxies = tuple(
                part.strip()
                for part in env["GFS_TRUSTED_PROXIES"].split(",")
                if part.strip()
            )
        return replace(
            self,
            host=env.get("GFS_HOST", self.host),
            port=int(env["GFS_PORT"]) if "GFS_PORT" in env else self.port,
            base_url=env.get("GFS_BASE_URL", self.base_url),
            data_dir=data_dir,
            instance_id=env.get("GFS_INSTANCE_ID", self.instance_id),
            signing_seed_hex=env.get("GFS_SIGNING_SEED", self.signing_seed_hex),
            trusted_proxies=trusted_proxies,
        )

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


# ─── TOML example (written by --init) ───────────────────────────────────

EXAMPLE_TOML: str = """\
[server]
# These apply as written. To retarget a single instance without editing
# the file, set the matching env var (env > file): GFS_HOST, GFS_PORT,
# GFS_BASE_URL, GFS_DATA_DIR, GFS_INSTANCE_ID, GFS_SIGNING_SEED.
host     = "0.0.0.0"
port     = 8765
base_url = "https://gfs.example.com"
data_dir = "/var/lib/sh-gfs"
instance_id = "gfs-node-0"
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

[branding]
server_name       = "My Global Server"
landing_markdown  = ""
header_image_file = ""

[policy]
auto_accept_clients = true
auto_accept_spaces  = false
fraud_threshold     = 5

[admin]
# bcrypt hash — do NOT edit by hand. Use:
#   socialhome-global-server --set-password --config /path/to/global_server.toml
password_hash = ""

[webrtc]
stun_urls   = ["stun:stun.l.google.com:19302"]
turn_url    = ""
turn_secret = ""

[cluster]
enabled = false
node_id = ""
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
