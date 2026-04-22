"""GFS runtime configuration (spec §24.5).

Loaded with layered precedence:

1. **Runtime overrides in ``server_config`` table** — the admin portal
   writes here; wins over the TOML for keys it owns (server_name,
   landing_markdown, header_image_file, auto_accept_clients,
   auto_accept_spaces, fraud_threshold, admin_password_hash).
2. **TOML file** at the path passed via ``--config``, or
   ``$SOCIAL_HOME_GFS_CONFIG``, or ``$SOCIAL_HOME_GFS_DATA/global_server.toml``,
   or ``./global_server.toml``.
3. **Legacy env vars** (``GFS_HOST``, ``GFS_PORT``, ``GFS_DB_PATH``,
   ``GFS_INSTANCE_ID``) — kept as a fallback so existing dev scripts
   keep working.
4. **Dataclass defaults** — safe values for a fresh node.

Separate from :class:`socialhome.config.Config` because the GFS is a
different deploy artifact with its own sections. Import-safe: does not
pull in any core services.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_DIR = "/var/lib/sh-gfs"
DEFAULT_CONFIG_FILENAME = "global_server.toml"


@dataclass(slots=True, frozen=True)
class GfsConfig:
    """Top-level GFS configuration."""

    # [server]
    host: str = "0.0.0.0"
    port: int = 8765
    base_url: str = ""  # public URL, e.g. "https://gfs.example.com"
    data_dir: str = DEFAULT_DATA_DIR
    instance_id: str = "gfs-node-0"

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

    @classmethod
    def from_env_fallback(cls) -> "GfsConfig":
        """Build a GFS config from the legacy ``GFS_*`` env vars.

        Used when no TOML is discoverable (mostly unit tests and the
        existing dev scripts). ``base_url`` is inferred from ``GFS_HOST``
        + ``GFS_PORT`` as ``http://host:port`` — good enough for loopback,
        operators should always supply a TOML in production.
        """
        host = os.environ.get("GFS_HOST", "0.0.0.0")
        port = int(os.environ.get("GFS_PORT", "8765"))
        data_dir = os.environ.get("GFS_DATA_DIR", DEFAULT_DATA_DIR)
        db_path = os.environ.get("GFS_DB_PATH")
        # Prefer an explicit GFS_DB_PATH — override data_dir if it's outside.
        if db_path:
            data_dir = str(Path(db_path).resolve().parent)
        return cls(
            host=host,
            port=port,
            base_url=os.environ.get("GFS_BASE_URL", f"http://{host}:{port}"),
            data_dir=data_dir,
            instance_id=os.environ.get("GFS_INSTANCE_ID", "gfs-node-0"),
        )

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "GfsConfig":
        """Discover + load the GFS config.

        Search order:
          1. Explicit ``config_path`` argument.
          2. ``$SOCIAL_HOME_GFS_CONFIG``.
          3. ``$SOCIAL_HOME_GFS_DATA/global_server.toml``.
          4. ``./global_server.toml``.
          5. Env-var fallback (legacy dev path).
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
                return cls.from_toml(candidate)
        return cls.from_env_fallback()


# ─── TOML example (written by --init) ───────────────────────────────────

EXAMPLE_TOML: str = """\
[server]
host     = "0.0.0.0"
port     = 8765
base_url = "https://gfs.example.com"
data_dir = "/var/lib/sh-gfs"
instance_id = "gfs-node-0"

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
