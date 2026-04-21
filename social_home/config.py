"""Runtime configuration (§5.2).

:class:`Config` is loaded with layered precedence:

1. **Environment variables** (``SH_*``) — highest priority.
2. **TOML file** at ``$SH_CONFIG`` or
   ``~/.config/social-home/social_home.toml``.
3. **Dataclass defaults** — lowest priority.

The configuration object is frozen — the application never mutates it
at runtime.

Platform-specific sections (``[homeassistant]``, ``[standalone]``) are
passed through untouched in :attr:`Config.platform_options` so each
adapter can read its own settings without polluting the top-level config
namespace.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


# ── XDG-compliant default paths ──────────────────────────────────────────

_XDG_DATA_HOME = os.environ.get(
    "XDG_DATA_HOME",
    os.path.expanduser("~/.local/share"),
)
_XDG_CONFIG_HOME = os.environ.get(
    "XDG_CONFIG_HOME",
    os.path.expanduser("~/.config"),
)

DEFAULT_DATA_DIR = f"{_XDG_DATA_HOME}/social-home"
DEFAULT_CONFIG_DIR = f"{_XDG_CONFIG_HOME}/social-home"
DEFAULT_DB_FILENAME = "social_home.db"
DEFAULT_MEDIA_DIR = "media"
DEFAULT_TOML_FILE = f"{DEFAULT_CONFIG_DIR}/social_home.toml"

# TOML sections whose keys belong to Config (flattened with optional prefix).
# Anything NOT listed here is treated as a platform section and passed
# through via Config.platform_options.
_PREFIXED_SECTIONS: dict[str, str] = {
    "webrtc": "webrtc_",
}
_CORE_SECTIONS: frozenset[str] = frozenset(
    {
        "server",
        "storage",
        "federation",
        "webrtc",
    }
)


@dataclass(slots=True, frozen=True)
class Config:
    """Top-level runtime configuration."""

    # Storage paths
    data_dir: str = DEFAULT_DATA_DIR
    db_path: str = f"{DEFAULT_DATA_DIR}/{DEFAULT_DB_FILENAME}"
    media_path: str = f"{DEFAULT_DATA_DIR}/{DEFAULT_MEDIA_DIR}"

    # Display / identification
    instance_name: str = "My Home"

    # Server
    listen_host: str = "0.0.0.0"
    listen_port: int = 8099
    log_level: str = "INFO"

    # Write coalescing for the SQLite executor
    db_write_batch_max: int = 50
    db_write_batch_timeout_ms: int = 500

    # Storage quota — household-wide cap on file_meta byte total.
    # 0 disables the cap. Default matches spec §5.2
    # ``media_storage_max_gb: 5.0``.
    max_storage_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GB

    # CORS allowlist (§25.7). Empty tuple = deny every cross-origin
    # request. Set to ("https://example.com",) to allow that origin.
    cors_allowed_origins: tuple[str, ...] = ()

    # Deployment mode (affects PlatformAdapter selection)
    mode: str = "standalone"  # "ha" | "standalone"

    # Home Assistant credentials (HA mode, when not running as a Supervisor
    # add-on). In add-on mode these are ignored — the Supervisor injects
    # SUPERVISOR_TOKEN and the adapter routes through http://supervisor/core/api.
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""

    # WebRTC TURN / STUN config (§24.12, §26). Empty values disable TURN.
    webrtc_stun_url: str = "stun:stun.l.google.com:19302"
    webrtc_turn_url: str | None = None
    webrtc_turn_user: str | None = None
    webrtc_turn_cred: str | None = None
    webrtc_turn_secret: str = ""
    webrtc_turn_ttl_seconds: int = 3600

    #: Federation signature suite. ``"ed25519"`` (default) keeps the
    #: classical wire format; ``"ed25519+mldsa65"`` enables hybrid
    #: post-quantum signatures and requires the ``pq`` optional extra
    #: (``liboqs-python``) at runtime. See ``documentation/crypto.md``.
    federation_sig_suite: str = "ed25519"

    # Per-platform TOML sections — opaque to the core. Each adapter reads
    # its own section (e.g. config.platform_options["homeassistant"]).
    platform_options: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
    )

    @classmethod
    def from_env(cls) -> "Config":
        """Build a :class:`Config` from layered sources.

        Precedence (highest to lowest):

        1. Environment variables (``SH_*``).
        2. TOML file at ``$SH_CONFIG`` or the XDG default.
        3. Dataclass defaults.
        """
        # Layer 2: TOML file
        toml_options: dict[str, object] = {}
        platform_options: dict[str, Mapping[str, Any]] = {}
        toml_path_str = os.environ.get("SH_CONFIG")
        toml_path = Path(toml_path_str) if toml_path_str else Path(DEFAULT_TOML_FILE)
        if toml_path.exists():
            try:
                raw_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
                toml_options, platform_options = _split_toml(raw_toml)
            except OSError, ValueError, tomllib.TOMLDecodeError:
                toml_options = {}
                platform_options = {}

        # `url` / `token` inside [homeassistant] are HA *credentials*, not
        # adapter options — hoist them into the flat config scope and keep
        # the rest of the section as pure platform_options.
        ha_section = platform_options.get("homeassistant")
        if ha_section is not None and ("url" in ha_section or "token" in ha_section):
            remaining = {
                k: v for k, v in ha_section.items() if k not in ("url", "token")
            }
            platform_options["homeassistant"] = MappingProxyType(remaining)
            if "url" in ha_section:
                toml_options.setdefault("ha_url", ha_section["url"])
            if "token" in ha_section:
                toml_options.setdefault("ha_token", ha_section["token"])

        def _opt(key: str, env: str, default: object) -> object:
            # Layer 1: environment (highest)
            if env in os.environ:
                return os.environ[env]
            # Layer 2: TOML
            if key in toml_options:
                return toml_options[key]
            # Layer 3: dataclass default
            return default

        def _int_opt(key: str, env: str, default: int) -> int:
            v = _opt(key, env, default)
            return int(v) if isinstance(v, (int, str)) else default

        def _str_opt(key: str, env: str, default: str) -> str:
            v = _opt(key, env, default)
            return str(v) if v is not None else default

        data_dir = str(_opt("data_dir", "SH_DATA_DIR", DEFAULT_DATA_DIR))
        return cls(
            data_dir=data_dir,
            db_path=str(
                _opt(
                    "db_path",
                    "SH_DB_PATH",
                    f"{data_dir}/{DEFAULT_DB_FILENAME}",
                )
            ),
            media_path=str(
                _opt(
                    "media_path",
                    "SH_MEDIA_PATH",
                    f"{data_dir}/{DEFAULT_MEDIA_DIR}",
                )
            ),
            instance_name=_str_opt("instance_name", "SH_INSTANCE_NAME", "My Home"),
            listen_host=_str_opt("listen_host", "SH_LISTEN_HOST", "0.0.0.0"),
            listen_port=_int_opt("listen_port", "SH_LISTEN_PORT", 8099),
            log_level=_str_opt("log_level", "SH_LOG_LEVEL", "INFO").upper(),
            max_storage_bytes=_int_opt(
                "max_storage_bytes",
                "SH_MAX_STORAGE_BYTES",
                10 * 1024 * 1024 * 1024,
            ),
            cors_allowed_origins=tuple(
                v
                for v in str(
                    _opt("cors_allowed_origins", "SH_CORS_ALLOWED_ORIGINS", "")
                ).split(",")
                if v.strip()
            ),
            mode=_str_opt("mode", "SH_MODE", "standalone").lower(),
            ha_url=_str_opt(
                "ha_url",
                "SH_HA_URL",
                "http://homeassistant.local:8123",
            ),
            ha_token=_str_opt("ha_token", "SH_HA_TOKEN", ""),
            webrtc_stun_url=_str_opt(
                "webrtc_stun_url",
                "SH_WEBRTC_STUN_URL",
                "stun:stun.l.google.com:19302",
            ),
            webrtc_turn_url=(
                str(v)
                if (v := _opt("webrtc_turn_url", "SH_WEBRTC_TURN_URL", None))
                else None
            ),
            webrtc_turn_user=(
                str(v)
                if (v := _opt("webrtc_turn_user", "SH_WEBRTC_TURN_USER", None))
                else None
            ),
            webrtc_turn_cred=(
                str(v)
                if (v := _opt("webrtc_turn_cred", "SH_WEBRTC_TURN_CRED", None))
                else None
            ),
            webrtc_turn_secret=_str_opt(
                "webrtc_turn_secret",
                "SH_WEBRTC_TURN_SECRET",
                "",
            ),
            webrtc_turn_ttl_seconds=_int_opt(
                "webrtc_turn_ttl_seconds",
                "SH_WEBRTC_TURN_TTL_SECONDS",
                3600,
            ),
            federation_sig_suite=_str_opt(
                "federation_sig_suite",
                "SH_FEDERATION_SIG_SUITE",
                "ed25519",
            ),
            db_write_batch_max=_int_opt(
                "db_write_batch_max",
                "SH_DB_WRITE_BATCH_MAX",
                50,
            ),
            db_write_batch_timeout_ms=_int_opt(
                "db_write_batch_timeout_ms",
                "SH_DB_WRITE_BATCH_TIMEOUT_MS",
                500,
            ),
            platform_options=MappingProxyType(platform_options),
        )


# ── TOML flattening ──────────────────────────────────────────────────────


def _split_toml(
    raw: dict[str, object],
) -> tuple[dict[str, object], dict[str, Mapping[str, Any]]]:
    """Split a TOML dict into (flat core options, platform section map).

    Core sections listed in :data:`_CORE_SECTIONS` are flattened into
    keys on the returned core dict (with the optional ``webrtc_`` prefix
    applied via :data:`_PREFIXED_SECTIONS`). Any other top-level table is
    returned as a read-only platform section.
    """
    flat: dict[str, object] = {}
    platform: dict[str, Mapping[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            if key in _CORE_SECTIONS:
                prefix = _PREFIXED_SECTIONS.get(key, "")
                for sub_key, sub_value in value.items():
                    flat[f"{prefix}{sub_key}"] = sub_value
            else:
                platform[key] = MappingProxyType(dict(value))
        else:
            flat[key] = value
    return flat, platform
