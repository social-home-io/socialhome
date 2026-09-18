"""Global Federation Server (GFS) entry point (§24).

aiohttp application factory for the GFS relay process. Wires together:

* Config loader (:mod:`.config`) — TOML + env fallback.
* Data layer — :class:`SqliteGfsFederationRepo` + :class:`SqliteGfsAdminRepo` +
  :class:`SqliteClusterRepo`.
* Federation service — instance registration, publish, subscribe.
* Admin service — accept / reject / ban clients + spaces, policy,
  branding, fraud reports, audit log.
* Admin auth — bcrypt password + session cookie + middleware
  (gates every ``/admin/api/*`` route).
* Public HTTP routes — mounted via :func:`.routes.register_routes`.

Console-script entry point: ``socialhome-global-server``. Sub-commands:

* ``--init [--config PATH]`` — write a fresh ``global_server.toml``.
* ``--set-password [--config PATH]`` — bcrypt-hash stdin + persist.
* ``--config PATH`` — explicit config path to load.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives import serialization as _ser
from cryptography.hazmat.primitives.asymmetric import ed25519

from ..db import AsyncDatabase
from . import app_keys as K
from .admin import AdminAuth, build_admin_middleware, hash_password
from .admin_service import GfsAdminService
from .cluster import ClusterService
from .config import (
    DEFAULT_CONFIG_FILENAME,
    GfsConfig,
    set_password_in_toml,
    write_example_config,
)
from .federation import GfsFederationService
from .maintenance import GfsMaintenanceScheduler
from .public import (
    ClientIpResolver,
    PairingTokenService,
    build_listing_rate_limit,
    build_public_rtc_rate_limit,
    build_publish_rate_limit,
)
from .repositories import (
    SqliteClusterRepo,
    SqliteGfsAdminRepo,
    SqliteGfsFederationRepo,
    SqliteGfsHighlightPublicationRepo,
    SqliteGfsHighlightTokenRepo,
    SqliteGfsMomentFollowRepo,
    SqliteGfsUserPictureRepo,
    SqliteGfsUserRegistrationRepo,
)
from .routes import register_routes
from .rtc_transport import GfsRtcSession
from .relay_bridge import RelayBridge
from .highlight_publications import HighlightPublicationRegistry
from .moment_public_registry import MomentPublicRegistry
from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_ADMIN_UI_DIR = Path(__file__).resolve().parent / "admin_ui"
_PUBLIC_STATIC_DIR = Path(__file__).resolve().parent / "static"


# ─── Application factory ───────────────────────────────────────────────


class GfsApp:
    """Owns the GFS runtime — repos, services, middleware, and the
    :class:`aiohttp.web.Application`.

    Mirrors the structure of :func:`socialhome.app.create_app` but in
    class form: each ``_build_*`` method lives as a method on the class
    so tests can subclass + override one piece without reimplementing
    the whole factory. The public entry point stays
    :func:`create_gfs_app` — a thin wrapper that instantiates
    :class:`GfsApp` and returns its aiohttp application.
    """

    __slots__ = (
        "config",
        "db",
        "repos",
        "services",
        "client_ip",
        "app",
    )

    def __init__(
        self,
        config: GfsConfig,
        *,
        db_path_override: str | Path | None = None,
    ) -> None:
        self.config = config
        self.db = self._build_db(db_path_override)
        self.repos = self._build_repos(self.db)
        self.services = self._build_services(config, self.repos)
        # ONE resolver for the whole server: the trusted-proxy CIDRs are parsed
        # here, never per request, and every limiter plus every handler agrees
        # on what "the client" is.
        self.client_ip = ClientIpResolver(config.trusted_proxies)
        self.app = self._build_app()
        self._wire_app_keys()
        self._register_routes()
        self._register_lifecycle()

    # ─── Factories (overridable) ────────────────────────────────────

    def _build_db(self, db_path_override: str | Path | None) -> AsyncDatabase:
        """Prepare data_dir + return an :class:`AsyncDatabase`."""
        resolved_db = (
            Path(db_path_override) if db_path_override else Path(self.config.db_path)
        )
        try:
            resolved_db.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        if db_path_override is None:
            try:
                Path(self.config.media_dir).mkdir(parents=True, exist_ok=True)
            except PermissionError:
                pass
        return AsyncDatabase(resolved_db, migrations_dir=_MIGRATIONS_DIR)

    def _build_repos(self, db: AsyncDatabase) -> SimpleNamespace:
        """Instantiate the GFS repositories."""
        return SimpleNamespace(
            federation=SqliteGfsFederationRepo(db),
            admin=SqliteGfsAdminRepo(db),
            cluster=SqliteClusterRepo(db),
            highlight_pubs=SqliteGfsHighlightPublicationRepo(db),
            highlight_tokens=SqliteGfsHighlightTokenRepo(db),
            moment_public_users=SqliteGfsUserRegistrationRepo(db),
            moment_public_follows=SqliteGfsMomentFollowRepo(db),
            moment_public_pictures=SqliteGfsUserPictureRepo(db),
        )

    def _build_services(
        self,
        config: GfsConfig,
        repos: SimpleNamespace,
    ) -> SimpleNamespace:
        """Instantiate federation / admin / cluster services + auth + tokens."""
        ws_registry = GfsWebSocketRegistry()
        federation = GfsFederationService(
            repos.federation,
            ws_registry=ws_registry,
        )
        admin = GfsAdminService(
            fed_repo=repos.federation,
            admin_repo=repos.admin,
            federation=federation,
            fraud_threshold=config.fraud_threshold,
            ws_registry=ws_registry,
        )
        # Cluster mode (spec §24.10). Signing key derived deterministically
        # from the config's instance_id hash so every start is stable
        # without touching disk. Operators who need a persistent key
        # across hostname changes can override via [cluster] signing_key_hex
        # (future — not in this pass).
        seed = hashlib.sha256(
            f"gfs-cluster-{config.instance_id}".encode("utf-8"),
        ).digest()
        signing_key = seed  # Ed25519 private key is 32 bytes
        pk_obj = ed25519.Ed25519PrivateKey.from_private_bytes(signing_key).public_key()
        own_pk_hex = pk_obj.public_bytes(
            encoding=_ser.Encoding.Raw,
            format=_ser.PublicFormat.Raw,
        ).hex()
        cluster = ClusterService(
            repos.cluster,
            admin_repo=repos.admin,
            fed_repo=repos.federation,
            node_id=config.cluster_node_id or config.instance_id,
            self_url=config.base_url,
            peers=config.cluster_peers,
            signing_key=signing_key,
            own_public_key_hex=own_pk_hex,
            enabled=config.cluster_enabled,
            ws_registry=ws_registry,
        )
        admin.attach_cluster(cluster)
        highlight_pubs = HighlightPublicationRegistry(
            repos.highlight_pubs,
            repos.highlight_tokens,
            ws_registry,
            base_url=config.base_url,
        )
        moment_public = MomentPublicRegistry(
            repos.moment_public_users,
            repos.moment_public_follows,
            ws_registry,
        )
        # Periodic retention sweep — purges expired admin sessions, expired
        # highlight publications, and aged pair tokens (the GFS otherwise has
        # no recurring cleanup loop; these tables would grow without bound).
        maintenance = GfsMaintenanceScheduler(
            admin_repo=repos.admin,
            highlight_repo=repos.highlight_pubs,
        )
        return SimpleNamespace(
            federation=federation,
            cluster=cluster,
            maintenance=maintenance,
            admin_auth=AdminAuth(repos.admin),
            admin=admin,
            tokens=PairingTokenService(repos.admin),
            rtc=GfsRtcSession(),
            relay_bridge=RelayBridge(),
            ws_registry=ws_registry,
            highlight_pubs=highlight_pubs,
            moment_public=moment_public,
        )

    def _build_app(self) -> web.Application:
        middlewares = [
            build_admin_middleware(self.services.admin_auth),
            build_listing_rate_limit(self.client_ip),
            build_public_rtc_rate_limit(self.client_ip),
            build_publish_rate_limit(self.client_ip),
        ]
        return web.Application(middlewares=middlewares)

    # ─── Wiring ────────────────────────────────────────────────────

    def _wire_app_keys(self) -> None:
        a = self.app
        a[K.gfs_db_key] = self.db
        a[K.gfs_config_key] = self.config
        a[K.gfs_client_ip_key] = self.client_ip
        a[K.gfs_fed_repo_key] = self.repos.federation
        a[K.gfs_admin_repo_key] = self.repos.admin
        a[K.gfs_cluster_repo_key] = self.repos.cluster
        a[K.gfs_federation_key] = self.services.federation
        a[K.gfs_cluster_key] = self.services.cluster
        a[K.gfs_admin_auth_key] = self.services.admin_auth
        a[K.gfs_admin_service_key] = self.services.admin
        a[K.gfs_rtc_key] = self.services.rtc
        a[K.gfs_relay_bridge_key] = self.services.relay_bridge
        a[K.gfs_ws_registry_key] = self.services.ws_registry
        a[K.gfs_highlight_pub_repo_key] = self.repos.highlight_pubs
        a[K.gfs_highlight_token_repo_key] = self.repos.highlight_tokens
        a[K.gfs_highlight_pub_service_key] = self.services.highlight_pubs
        a[K.gfs_moment_public_user_repo_key] = self.repos.moment_public_users
        a[K.gfs_moment_public_follow_repo_key] = self.repos.moment_public_follows
        a[K.gfs_moment_public_registry_key] = self.services.moment_public
        a[K.gfs_user_picture_repo_key] = self.repos.moment_public_pictures
        # Non-typed helpers the admin module reads directly.
        a["admin_auth"] = self.services.admin_auth
        a["gfs_token_service"] = self.services.tokens

    def _register_routes(self) -> None:
        """Mount relay / cluster / rtc / admin / public / static routes."""
        register_routes(
            self.app,
            admin_ui_dir=_ADMIN_UI_DIR,
            media_dir=self.config.media_dir,
            public_static_dir=_PUBLIC_STATIC_DIR,
        )

    def _register_lifecycle(self) -> None:
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)

    # ─── Lifecycle hooks ────────────────────────────────────────────

    async def _on_startup(self, app: web.Application) -> None:
        log.info(
            "GFS: starting up (db=%s, base_url=%s)",
            self.db._path if hasattr(self.db, "_path") else "—",
            self.config.base_url,
        )
        await self.db.startup()
        http_session = aiohttp.ClientSession()
        app[K.gfs_http_session_key] = http_session

        # Mirror TOML's admin_password_hash into the DB on first run.
        if self.config.admin_password_hash:
            existing = await self.repos.admin.get_config("admin_password_hash")
            if not existing:
                await self.repos.admin.set_config(
                    "admin_password_hash",
                    self.config.admin_password_hash,
                )

        await self.repos.admin.purge_expired_sessions(int(time.time()))
        await self.services.cluster.start()
        # Recurring retention sweep — runs the purge again on its first tick
        # then hourly (the boot purge above stays for an immediate clean).
        await self.services.maintenance.start()

    async def _on_cleanup(self, app: web.Application) -> None:
        log.info("GFS: shutting down")
        await self.services.maintenance.stop()
        await self.services.cluster.stop()
        await self.services.ws_registry.close_all()
        session = app.get(K.gfs_http_session_key)
        if session is not None:
            await session.close()
        await self.db.shutdown()


def create_gfs_app(
    config: GfsConfig | None = None,
    *,
    db_path: str | Path | None = None,
) -> web.Application:
    """Build and return the configured GFS :class:`aiohttp.web.Application`.

    Thin wrapper over :class:`GfsApp` for compatibility with every
    existing caller + ``aiohttp.web.run_app``. ``db_path`` is a legacy
    test knob; new code should pass a fully-populated :class:`GfsConfig`.
    """
    if config is None:
        config = GfsConfig.load()
    return GfsApp(config, db_path_override=db_path).app


# ─── Console-script CLI ─────────────────────────────────────────────────


def _cli_init(config_path: Path | None) -> int:
    target = config_path or Path(DEFAULT_CONFIG_FILENAME)
    try:
        write_example_config(target)
    except FileExistsError:
        print(f"{target} already exists — refusing to overwrite.", file=sys.stderr)
        return 2
    print(f"Wrote example config to {target}")
    print("Edit [server] base_url + [admin] password_hash before starting the GFS.")
    return 0


def _cli_set_password(config_path: Path | None) -> int:
    target = config_path or Path(DEFAULT_CONFIG_FILENAME)
    if not target.is_file():
        print(f"No config at {target}; run --init first.", file=sys.stderr)
        return 2
    pw = getpass.getpass("New GFS admin password: ")
    confirm = getpass.getpass("Confirm: ")
    if pw != confirm or len(pw) < 8:
        print("Passwords do not match or too short (min 8 chars).", file=sys.stderr)
        return 2
    hashed = hash_password(pw)
    set_password_in_toml(target, hashed)
    print(f"Admin password hash written to {target}")
    return 0


def main() -> None:
    """Entry point for the ``socialhome-global-server`` console script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    args = sys.argv[1:]
    config_path: Path | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--config" and i + 1 < len(args):
            config_path = Path(args[i + 1])
            i += 2
            continue
        if arg == "--init":
            sys.exit(_cli_init(config_path))
        if arg == "--set-password":
            sys.exit(_cli_set_password(config_path))
        i += 1

    # ``load`` already layers GFS_* env vars over the file (env > file >
    # defaults), so the bind address comes straight from the resolved
    # config — no second env lookup here (that asymmetry was issue #563:
    # a baked-in GFS_HOST/GFS_PORT shadowed the --config file's values).
    config = GfsConfig.load(config_path)
    log.info(
        "Starting GFS on %s:%s (base_url=%s)",
        config.host,
        config.port,
        config.base_url,
    )
    web.run_app(create_gfs_app(config), host=config.host, port=config.port)
