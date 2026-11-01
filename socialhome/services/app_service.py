"""AppService — install, uninstall, and enable/disable Social Home Apps.

Security invariants:
- SHA-256 is verified **before** any filesystem write.
- Tarball extraction rejects path traversal (``../`` or absolute paths),
  symlinks, and device/FIFO/block-device members.  Any violation raises
  :class:`AppIntegrityError` and the partially-extracted directory (if any)
  is cleaned up.
- Bundles are bounded: the download (``MAX_BUNDLE_DOWNLOAD_BYTES``), the
  unpacked total (``MAX_BUNDLE_EXTRACTED_BYTES``) and the member count
  (``MAX_BUNDLE_FILES``) — checked before hashing / per member before
  writing, so a zip bomb never inflates past the cap.
- Destination directory is checked to be inside ``apps_path`` before
  creation (catalog-supplied ``app_id`` / ``version`` path components are
  untrusted and could contain ``../../`` sequences).
- CPU-bound work (sha256, tarfile extraction, shutil.rmtree) runs via
  ``asyncio.to_thread`` so the event loop is never blocked.
- Filesystem mutations use ``aiofiles.os`` for async makedirs; the tar
  extraction and rmtree happen inside the thread helper (plain pathlib there
  is intentional — we're already off the event loop).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import logging
import shutil
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles.os
import orjson

from ..domain.apps import (
    _VALID_MIN_AGES,
    MAX_BUNDLE_DOWNLOAD_BYTES,
    MAX_BUNDLE_EXTRACTED_BYTES,
    MAX_BUNDLE_FILES,
    PERMISSION_STORAGE,
    UNINSTALL_GAME_RECENT_SECONDS,
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppBundleTooLargeError,
    AppCatalogDisabledError,
    AppCatalogEntry,
    AppCatalogStatus,
    AppIntegrityError,
    AppManifest,
    AppNeedsNewerCoreError,
    AppNewPermissionsError,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    AppRequestNotAllowedError,
    AppUninstallPreview,
    InstalledApp,
    app_age_allows,
    changelog_since,
    core_version_satisfies,
    is_newer_version,
    is_quiet_update,
    new_permissions,
    unknown_capabilities,
)
from ..domain.events import (
    AppEnabledChanged,
    AppInstalled,
    AppRequestDismissed,
    AppRequested,
    AppUninstalled,
    AppUpdateAvailable,
    AppUpdated,
    AppUpdateGone,
)
from ..domain.space import SpacePermissionError
from ..repositories.app_group_repo import AbstractAppGroupRepo
from ..repositories.app_repo import AbstractAppRepo
from ..repositories.cp_repo import AbstractCpRepo
from ..repositories.user_repo import AbstractUserRepo
from .app_catalog_service import AppCatalogService
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from .app_federation_service import AppFederationService

log = logging.getLogger(__name__)

APP_KV_MAX_KEYS = 500
APP_KV_MAX_VALUE_BYTES = 64 * 1024
APP_KV_MAX_KEY_LEN = 256


class AppService(BusPublisherMixin):
    """Install, uninstall, and toggle Social Home Apps.

    Parameters
    ----------
    repo:
        Persistent storage for installed-app rows.
    catalog:
        The catalog service used to browse available apps.  ``None`` means
        no catalog is configured — ``install`` and ``browse_catalog`` will
        signal the absence appropriately.
    apps_path:
        Dedicated root directory for app bundles.  Bundles are unpacked
        under ``<apps_path>/<app_id>/<version>/`` (e.g.
        ``<apps_path>/chess/2.0.0``).  The stored ``bundle_path`` value is
        relative to ``apps_path`` (e.g. ``chess/2.0.0``).
    downloader:
        Callable ``(url: str) -> bytes | Awaitable[bytes]`` that fetches the
        raw bundle bytes.  Injected so unit tests never hit the network.
    bus:
        Optional event bus for publishing :class:`AppInstalled` /
        :class:`AppUpdated` / :class:`AppUninstalled` /
        :class:`AppEnabledChanged` / :class:`AppRequested` /
        :class:`AppRequestDismissed`.  ``None`` silently skips publication.
    core_version:
        This Social Home's version — a catalog entry whose
        ``min_core_version`` is past it can't be installed
        (:func:`~socialhome.domain.apps.core_version_satisfies`).
    users:
        The household's people: who added an app ("Added by Anna") and
        who an "Ask an admin" request goes to.
    group_repo:
        Group tables (v_76) — counted into a person's "games going".
    """

    # NOTE: SpacePermissionError is intentionally used for admin-gate failures
    # in PR1 (it maps correctly to HTTP 403 via BaseView._map_exc and matches
    # the PR1 plan).  A dedicated AppPermissionError is deferred to PR2.

    __slots__ = (
        "_apps_path",
        "_bus",
        "_catalog",
        "_core_version",
        "_cp_repo",
        "_downloader",
        "_group_repo",
        "_repo",
        "_sessions",
        "_users",
    )

    def __init__(
        self,
        *,
        repo: AbstractAppRepo,
        catalog: AppCatalogService | None,
        apps_path: Path,
        downloader: Callable[[str], bytes | Awaitable[bytes]],
        bus: EventBus | None = None,
        cp_repo: AbstractCpRepo | None = None,
        core_version: str = "",
        users: AbstractUserRepo | None = None,
        group_repo: AbstractAppGroupRepo | None = None,
    ) -> None:
        self._repo = repo
        self._group_repo = group_repo
        self._core_version = core_version
        self._users = users
        self._catalog = catalog
        self._apps_path = Path(apps_path)
        self._downloader = downloader
        self._bus = bus
        self._cp_repo = cp_repo
        self._sessions: AppFederationService | None = None

    def attach_sessions(self, sessions: AppFederationService) -> None:
        """Wire the games side (D9): an uninstall reads which games and
        invites it ends with other households, and tells them."""
        self._sessions = sessions

    # ─── Read-side passthroughs ──────────────────────────────────────────────

    async def list_installed(self) -> list[InstalledApp]:
        """Return all installed apps."""
        return await self._repo.list_installed()

    async def get(self, app_id: str) -> InstalledApp | None:
        """Return a single installed app by id, or ``None``."""
        return await self._repo.get(app_id)

    async def heal_manifest_names(self) -> int:
        """Keep each installed bundle's own ``i18n`` names with its row.

        A row installed before the names were kept has none, so a game row
        named the app in English until the catalog first answered (after
        every restart, or never with the catalog unreachable). Once at
        startup: read each such bundle's ``manifest.json`` (off the event
        loop) and keep its names — the existing ``manifest_json`` column,
        no migration. Returns how many rows gained names."""
        healed = 0
        for app in await self._repo.list_installed():
            if app.manifest.names:
                continue
            names = await asyncio.to_thread(
                self._bundle_names_sync, self._apps_path, app.bundle_path
            )
            if not names:
                continue
            await self._repo.update_installed(
                replace(app, manifest=replace(app.manifest, names=names))
            )
            healed += 1
        return healed

    @staticmethod
    def _bundle_names_sync(
        apps_path: Path, bundle_path: str
    ) -> tuple[tuple[str, str], ...]:
        """The ``i18n`` names in an installed bundle's ``manifest.json``;
        ``()`` for a bundle outside *apps_path*, missing or unreadable."""
        root = apps_path.resolve()
        path = (apps_path / bundle_path / "manifest.json").resolve()
        if not path.is_relative_to(root):
            return ()
        try:
            return AppManifest.from_dict(orjson.loads(path.read_bytes())).names
        except OSError, ValueError, TypeError, KeyError:
            return ()

    @property
    def catalog_enabled(self) -> bool:
        """Whether an app catalog is configured (``apps_catalog_url`` is
        not empty)."""
        return self._catalog is not None

    async def browse_catalog(self, *, force: bool = False) -> list[AppCatalogEntry]:
        """Return the remote catalog entries, or an empty list when no catalog
        is configured. ``force`` re-fetches past the cache (an admin's
        "Retry")."""
        if self._catalog is None:
            return []
        return await self._catalog.fetch_catalog(force=force)

    @property
    def catalog_status(self) -> AppCatalogStatus:
        """When the catalog last answered and whether the latest try failed
        (``stale``: what :meth:`browse_catalog` / :meth:`list_updates`
        returned is that earlier answer)."""
        if self._catalog is None:
            return AppCatalogStatus()
        return self._catalog.status

    @property
    def catalog_source_host(self) -> str | None:
        """Where the catalog comes from (D6): ``None`` for Social Home's
        own app list, else the host an operator pointed
        ``apps_catalog_url`` at — the SPA says "From <host>, set by your
        server operator" instead of "From Social Home"."""
        if self._catalog is None:
            return None
        return self._catalog.source_host

    def is_compatible(self, entry: AppCatalogEntry) -> bool:
        """Can this Social Home install *entry*? Not when it needs a newer
        one (``min_core_version``) or asks for a permission this one
        doesn't know."""
        return not entry.unknown_permissions and core_version_satisfies(
            entry.min_core_version,
            self._core_version,
            today=datetime.now(UTC).date(),
        )

    @staticmethod
    def default_min_age(entry: AppCatalogEntry, manifest: AppManifest) -> int:
        """The age gate a fresh install starts with: the stricter of the
        catalog's rating and the bundle manifest's. An admin can change it
        afterwards (:meth:`set_min_age`)."""
        return max(entry.min_age or 0, manifest.min_age)

    async def browse_catalog_for(
        self, *, user_id: str, is_admin: bool, force: bool = False
    ) -> list[AppCatalogEntry]:
        """The catalog as *user_id* may see it: everything for an admin;
        for anyone else, without the apps the age gate keeps them from
        (D5: a protected account doesn't learn of an app rated above its
        age — the catalog's rating, as on install)."""
        entries = await self.browse_catalog(force=force and is_admin)
        if is_admin:
            return entries
        protected, age = await self._protection(user_id)
        return [
            e
            for e in entries
            if app_age_allows(e.min_age or 0, protected=protected, declared_age=age)
        ]

    async def _protection(self, user_id: str) -> tuple[bool, int | None]:
        """(protected, declared age) for *user_id*; unprotected when the
        child-protection data isn't wired or there is no record."""
        if self._cp_repo is None:
            return False, None
        p = await self._cp_repo.get_user_protection(user_id)
        if p is None:
            return False, None
        return bool(p.get("child_protection_enabled")), p.get("declared_age")

    async def user_names(self, user_ids: set[str]) -> dict[str, str]:
        """Display names for *user_ids* (who added an app); unknown or
        gone ids are left out."""
        if self._users is None or not user_ids:
            return {}
        return {
            u.user_id: u.display_name for u in await self._users.list_by_ids(user_ids)
        }

    async def request_app(self, app_id: str, *, user_id: str, is_admin: bool) -> None:
        """ "Ask an admin" (D4): a member asks for a catalog app.

        The request goes to the household's admins — or, for a protected
        account, to its guardians (admins when it has none) — as one
        "Needs you" row per decider, grouped per app
        (:class:`AppRequested`; the notification service writes it). No
        table: adding the app or "Not now" resolves the rows. Asking
        again only joins the open row.

        An app the household has but an admin turned off can be asked for
        too ("Ask to turn it on", D10 — an invite for it waits); turning it
        back on answers the ask.

        Raises :class:`AppRequestNotAllowedError` for an admin (they add
        it) or an app already in the household and turned on, and
        :class:`AppNotFoundError` for an app the catalog doesn't have or
        the age gate keeps from the caller (never confirms it exists)."""
        if is_admin:
            raise AppRequestNotAllowedError("Admins add apps themselves.")
        installed = await self._repo.get(app_id)
        if installed is not None and installed.enabled:
            raise AppRequestNotAllowedError("This app is already in the household.")
        entries = await self.browse_catalog_for(user_id=user_id, is_admin=False)
        entry = next((e for e in entries if e.app_id == app_id), None)
        if entry is None or (
            # A turned-off app: the household's own age gate holds too.
            installed is not None and not await self._age_allowed(installed, user_id)
        ):
            raise AppNotFoundError(f"App {app_id!r} not found in catalog")
        recipients: list[str] = []
        protected, _ = await self._protection(user_id)
        if protected and self._cp_repo is not None:
            recipients = list(await self._cp_repo.list_guardians(user_id))
        if not recipients and self._users is not None:
            recipients = [
                u.user_id for u in await self._users.list_active() if u.is_admin
            ]
        await self._emit(
            AppRequested(
                app_id=app_id,
                name=entry.name,
                requested_by=user_id,
                recipients=tuple(r for r in recipients if r != user_id),
            )
        )

    async def dismiss_requests(
        self, app_id: str, *, actor_is_admin: bool, actor_user_id: str
    ) -> None:
        """ "Not now": an admin answers every open request for *app_id*
        (:class:`AppRequestDismissed` resolves the rows for all)."""
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may answer app requests")
        await self._emit(AppRequestDismissed(app_id=app_id, dismissed_by=actor_user_id))

    async def list_updates(self, *, force: bool = False) -> list[dict]:
        """Return a list of installed apps that have a newer version in the catalog.

        Each entry is a dict with keys ``app_id``, ``name``,
        ``current_version``, ``latest_version``, ``compatible`` (this
        Social Home can install it, :meth:`is_compatible`),
        ``new_permissions`` (what the new version asks for beyond what the
        household agreed to — updating needs a yes for those, D7),
        ``changelog`` ("What's new": the catalog's entries newer than the
        installed version, newest first) and ``quiet`` (a maintenance build
        nobody notices — every entry since the installed version is quiet
        and nothing new is asked for, :func:`is_quiet_update`: no "Needs
        you" row, only the Apps page's Updates). Only a strictly newer
        version counts (:func:`~socialhome.domain.apps.is_newer_version`,
        CalVer compared as numbers): a catalog that went back to an older
        release offers no "update".

        If no catalog is configured, returns an empty list.

        Args:
            force: Passed through to :meth:`AppCatalogService.fetch_catalog`
                to bypass the 24-hour cache when ``True``.
        """
        if self._catalog is None:
            return []

        installed = await self._repo.list_installed()
        if not installed:
            return []

        entries = await self._catalog.fetch_catalog(force=force)
        entry_map: dict[str, AppCatalogEntry] = {e.app_id: e for e in entries}

        updates: list[dict] = []
        for app in installed:
            entry = entry_map.get(app.app_id)
            if entry is not None and is_newer_version(
                entry.latest_version, app.version
            ):
                asks = list(
                    new_permissions(app.manifest.capabilities, entry.permissions)
                )
                updates.append(
                    {
                        "app_id": app.app_id,
                        "name": app.name,
                        "current_version": app.version,
                        "latest_version": entry.latest_version,
                        # ``False``: the new version needs a newer Social
                        # Home — the SPA says so instead of offering it.
                        "compatible": self.is_compatible(entry),
                        "new_permissions": asks,
                        # A maintenance build nobody notices: listed under
                        # Updates, but no "Needs you" row (check_for_updates).
                        "quiet": not asks
                        and is_quiet_update(
                            entry.changelog, app.version, entry.latest_version
                        ),
                        "changelog": [
                            {
                                "version": c.version,
                                "date": c.date,
                                "notes": dict(c.notes),
                            }
                            for c in changelog_since(
                                entry.changelog, app.version, entry.latest_version
                            )
                        ],
                    }
                )
        return updates

    async def check_for_updates(self) -> list[dict]:
        """The daily update check (D7): :meth:`list_updates` against a
        fresh catalog, then one :class:`AppUpdateAvailable` per app with a
        newer compatible version that isn't ``quiet`` (to the household's
        admins — the notification service keeps one "Needs you" row per
        app) and one :class:`AppUpdateGone` per installed app without one,
        or with only a quiet one (its rows resolve). Nothing is installed:
        an admin updates with one tap.

        A check that couldn't reach the catalog (``stale``: the held copy
        was served) announces and withdraws nothing — it doesn't know.
        Returns the updates found."""
        if self._catalog is None:
            return []
        updates = await self.list_updates(force=True)
        if self.catalog_status.stale:
            log.info("app-update check: catalog unreachable, nothing announced")
            return updates
        admins: tuple[str, ...] = ()
        if self._users is not None:
            admins = tuple(
                u.user_id for u in await self._users.list_active() if u.is_admin
            )
        # A quiet update (a maintenance build, :func:`is_quiet_update`)
        # stays on the Apps page's Updates only: no row, and one left over
        # for a version now marked quiet goes away. Still never installed
        # on its own (D7).
        ready = {u["app_id"]: u for u in updates if u["compatible"] and not u["quiet"]}
        for app in await self._repo.list_installed():
            update = ready.get(app.app_id)
            if update is None:
                await self._emit(AppUpdateGone(app_id=app.app_id))
                continue
            await self._emit(
                AppUpdateAvailable(
                    app_id=app.app_id,
                    name=update["name"],
                    version=update["latest_version"],
                    recipients=admins,
                )
            )
        return updates

    # ─── Mutations ───────────────────────────────────────────────────────────

    async def _download_verify_unpack(
        self,
        app_id: str,
        entry: AppCatalogEntry,
    ) -> tuple[AppManifest, str, str]:
        """Download, SHA-256-verify, and unpack a catalog bundle.

        Shared by :meth:`install` and :meth:`update_app` so the full security
        pipeline (sha256 + apps-root containment + path-traversal guard) runs
        on both code paths.

        Returns ``(manifest, bundle_rel_path, actual_sha256)`` where
        ``bundle_rel_path`` is the relative path under ``apps_path``
        (e.g. ``"chess/2.0.0"``) and ``actual_sha256`` is the verified
        hex digest.

        Raises:
            :class:`AppIntegrityError` on sha mismatch, path traversal, or
            invalid manifest.
            :class:`OSError` on unrecoverable I/O errors (callers should
            wrap or re-raise with context).
        """
        # Download — the production downloader streams under the cap
        # (``download_bundle``); re-checked here for any downloader.
        result = self._downloader(entry.bundle_url)
        if inspect.isawaitable(result):
            data: bytes = await result
        else:
            data = result
        if len(data) > MAX_BUNDLE_DOWNLOAD_BYTES:
            raise AppBundleTooLargeError(
                f"The app bundle is larger than "
                f"{MAX_BUNDLE_DOWNLOAD_BYTES // (1024 * 1024)} MiB."
            )

        # SHA-256 verify (CPU-bound) — must happen BEFORE any disk write
        actual_sha = await asyncio.to_thread(self._compute_sha256, data)
        if actual_sha != entry.bundle_sha256:
            raise AppIntegrityError(
                f"SHA-256 mismatch for {app_id!r}: "
                f"expected {entry.bundle_sha256!r}, got {actual_sha!r}"
            )

        # Destination directory — validate containment BEFORE creating it so a
        # malicious catalog with app_id="../../etc" can't escape apps_path.
        dest = self._apps_path / app_id / entry.latest_version
        apps_root = self._apps_path.resolve()
        if not dest.resolve().is_relative_to(apps_root):
            raise AppIntegrityError(
                f"catalog path escapes apps root: "
                f"app_id={app_id!r}, version={entry.latest_version!r}"
            )

        await aiofiles.os.makedirs(str(dest), exist_ok=True)

        # Safe extraction (CPU + I/O-bound; runs in thread).
        # _extract_bundle_sync also reads and returns the parsed manifest.json
        # dict so no blocking filesystem call is needed on the event loop.
        try:
            manifest_dict = await asyncio.to_thread(
                self._extract_bundle_sync, data, dest
            )
        except (AppIntegrityError, OSError) as exc:
            # Clean up the partially-extracted directory before re-raising
            await asyncio.to_thread(self._rmtree_sync, dest)
            if isinstance(exc, AppIntegrityError):
                raise
            raise AppIntegrityError(
                f"Bundle extraction failed for {app_id!r}: {exc}"
            ) from exc

        try:
            manifest = AppManifest.from_dict(manifest_dict)
        except (ValueError, KeyError, TypeError) as exc:
            await asyncio.to_thread(self._rmtree_sync, dest)
            raise AppIntegrityError(
                f"Invalid manifest.json in bundle for {app_id!r}: {exc}"
            ) from exc

        # The installed manifest is what the household agrees to: a
        # capability this Social Home can't name or enforce is refused
        # outright — never granted blindly, never dropped silently.
        unknown = unknown_capabilities(manifest.capabilities)
        if unknown:
            await asyncio.to_thread(self._rmtree_sync, dest)
            raise AppNeedsNewerCoreError(params={"capabilities": ", ".join(unknown)})

        bundle_rel = f"{app_id}/{entry.latest_version}"
        return manifest, bundle_rel, actual_sha

    async def install(
        self,
        app_id: str,
        *,
        actor_is_admin: bool,
        actor_user_id: str,
    ) -> InstalledApp:
        """Download, verify, unpack, and register an app.

        Steps:
        1. Admin gate.
        2. Double-install guard → :class:`AppAlreadyInstalledError` if already
           installed.
        3. Catalog lookup → :class:`AppNotFoundError` if absent.
        4. Download + SHA-256 verify + safe extraction via
           :meth:`_download_verify_unpack`.
        5. Build :class:`InstalledApp`, persist, publish :class:`AppInstalled`.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may install apps")

        # Guard against double-install
        if await self._repo.get(app_id) is not None:
            raise AppAlreadyInstalledError(
                f"App {app_id!r} is already installed; uninstall first to reinstall"
            )

        if self._catalog is None:
            raise AppCatalogDisabledError()

        # Catalog lookup
        entries = await self.browse_catalog()
        entry = next((e for e in entries if e.app_id == app_id), None)
        if entry is None:
            raise AppNotFoundError(f"App {app_id!r} not found in catalog")
        if not self.is_compatible(entry):
            raise AppNeedsNewerCoreError(
                params={"min_core_version": entry.min_core_version or ""}
            )

        manifest, bundle_rel, actual_sha = await self._download_verify_unpack(
            app_id, entry
        )

        app = InstalledApp(
            app_id=app_id,
            name=entry.name,
            version=entry.latest_version,
            enabled=True,
            manifest=manifest,
            bundle_path=bundle_rel,
            bundle_sha256=actual_sha,
            source_url=entry.bundle_url,
            installed_by=actor_user_id,
            installed_at=datetime.now(UTC).isoformat(),
            # The catalog's rating is the default (D5); an admin can
            # change it afterwards.
            min_age=self.default_min_age(entry, manifest),
        )
        await self._repo.install(app)
        await self._emit(
            AppInstalled(
                app_id=app_id,
                name=entry.name,
                installed_by=actor_user_id,
                min_age=app.min_age,
            )
        )

        return app

    async def update_app(
        self,
        app_id: str,
        *,
        actor_is_admin: bool,
        actor_user_id: str | None = None,
        accept_permissions: frozenset[str] = frozenset(),
    ) -> InstalledApp:
        """Update an installed app to the latest version from the catalog.

        Steps:
        1. Admin gate.
        2. Lookup installed app → :class:`AppNotFoundError` if absent.
        3. Fetch the catalog (force refresh so the check is current).
        4. If already at the latest version, return the existing row unchanged.
        5. New permissions (D7): when the catalog's ``permissions`` grow
           past the installed ones beyond *accept_permissions*, refuse with
           :class:`AppNewPermissionsError` naming only the new ones — before
           anything is downloaded.
        6. Download + SHA-256 verify + safe extraction via
           :meth:`_download_verify_unpack` (same security pipeline as install),
           then the same check against the downloaded manifest (the bundle,
           not the catalog, is what runs): growth past *accept_permissions*
           removes the new bundle and refuses.
        7. Update the repo row, preserving ``enabled`` and ``installed_by``.
        8. Remove the OLD bundle directory from disk.
        9. Publish :class:`AppUpdated`.

        Args:
            app_id: The app to update.
            actor_is_admin: Must be ``True``; raises
                :class:`SpacePermissionError` otherwise.
            actor_user_id: The admin updating it (named on the other
                admins' resolved update rows).
            accept_permissions: The new permissions the admin said yes to.

        Returns:
            The updated :class:`InstalledApp` (or the unchanged existing row
            if already at the latest version).

        Raises:
            :class:`SpacePermissionError` — caller is not an admin.
            :class:`AppNotFoundError` — app is not installed.
            :class:`AppIntegrityError` — sha mismatch, path traversal,
                or invalid manifest in the new bundle.
            :class:`AppNewPermissionsError` — the new version asks for
                permissions *accept_permissions* doesn't cover.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may update apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

        if self._catalog is None:
            log.warning("update_app: no catalog configured, cannot check for updates")
            return existing

        # Force-fetch so we always use a fresh catalog for an explicit update
        entries = await self._catalog.fetch_catalog(force=True)
        entry = next((e for e in entries if e.app_id == app_id), None)
        if entry is None:
            raise AppNotFoundError(f"App {app_id!r} not found in catalog")

        if not is_newer_version(entry.latest_version, existing.version):
            # Equal, older (a catalog rolled back) or unparseable: never a
            # downgrade.
            log.info(
                "update_app: %r stays at %r (catalog has %r)",
                app_id,
                existing.version,
                entry.latest_version,
            )
            return existing
        if not self.is_compatible(entry):
            raise AppNeedsNewerCoreError(
                params={"min_core_version": entry.min_core_version or ""}
            )
        agreed = existing.manifest.capabilities
        self._require_consent(
            new_permissions(agreed, entry.permissions), accept_permissions
        )

        manifest, bundle_rel, actual_sha = await self._download_verify_unpack(
            app_id, entry
        )
        try:
            # Verified again from what was downloaded: the bundle runs,
            # not the catalog's description of it. (Unknown capabilities
            # were already refused while unpacking; this is the growth
            # among the known ones.)
            self._require_consent(
                new_permissions(agreed, manifest.capabilities), accept_permissions
            )
        except AppNewPermissionsError:
            await self._drop_unpacked(existing, bundle_rel)
            raise

        # Preserve admin-configured age gate, but auto-raise when the new
        # manifest declares a higher minimum age (e.g. app upgrades its rating).
        # Never silently LOWER an admin's gate — take the stricter value.
        new_min_age = max(existing.min_age, self.default_min_age(entry, manifest))
        updated = InstalledApp(
            app_id=app_id,
            name=entry.name,
            version=entry.latest_version,
            enabled=existing.enabled,
            manifest=manifest,
            bundle_path=bundle_rel,
            bundle_sha256=actual_sha,
            source_url=entry.bundle_url,
            installed_by=existing.installed_by,
            installed_at=existing.installed_at,
            min_age=new_min_age,
        )
        await self._repo.update_installed(updated)

        # Remove old bundle directory if it differs from the new one
        old_bundle_dir = self._apps_path / existing.bundle_path
        new_bundle_dir = self._apps_path / bundle_rel
        apps_root = self._apps_path.resolve()
        if (
            old_bundle_dir.resolve() != new_bundle_dir.resolve()
            and old_bundle_dir.resolve().is_relative_to(apps_root)
            and await aiofiles.os.path.isdir(str(old_bundle_dir))
        ):
            await asyncio.to_thread(self._rmtree_sync, old_bundle_dir)

        await self._emit(
            AppUpdated(
                app_id=app_id,
                name=entry.name,
                old_version=existing.version,
                new_version=entry.latest_version,
                min_age=new_min_age,
                updated_by=actor_user_id,
            )
        )
        return updated

    @staticmethod
    def _require_consent(growth: tuple[str, ...], accepted: frozenset[str]) -> None:
        """Refuse an update whose permission *growth* isn't all in
        *accepted* (:class:`AppNewPermissionsError` names the new ones)."""
        if any(p not in accepted for p in growth):
            raise AppNewPermissionsError(params={"permissions": ",".join(growth)})

    async def _drop_unpacked(self, existing: InstalledApp, bundle_rel: str) -> None:
        """Remove a new version's unpacked bundle that won't be used —
        never the installed one."""
        new_dir = self._apps_path / bundle_rel
        if (
            bundle_rel != existing.bundle_path
            and new_dir.resolve().is_relative_to(self._apps_path.resolve())
            and await aiofiles.os.path.isdir(str(new_dir))
        ):
            await asyncio.to_thread(self._rmtree_sync, new_dir)

    async def uninstall(self, app_id: str, *, actor_is_admin: bool) -> None:
        """Remove an installed app from disk and the registry.

        Everyone's saved data and sessions go with it (FK cascade); the
        households a person here plays it with are told their games and
        invites ended (D9, :meth:`AppFederationService.tell_opponents`).

        Raises :class:`SpacePermissionError` for non-admins and
        :class:`AppNotFoundError` when the app is not installed.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may uninstall apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

        # Read what the uninstall ends before the rows go with the app (FK
        # cascade); tell the other households once it is gone.
        ending = (
            await self._sessions.sessions_ending(app_id)
            if self._sessions is not None
            else []
        )

        await self._repo.uninstall(app_id)

        # Remove the bundle dir from disk (CPU/I/O-bound → thread).
        # Validate containment before rmtree — stored bundle_path is trusted
        # (written by install()), but defence-in-depth against DB tampering.
        bundle_dir = self._apps_path / existing.bundle_path
        apps_root = self._apps_path.resolve()
        if not bundle_dir.resolve().is_relative_to(apps_root):
            log.warning(
                "uninstall: bundle_path %r escapes apps root — skipping rmtree",
                existing.bundle_path,
            )
        elif await aiofiles.os.path.isdir(str(bundle_dir)):
            await asyncio.to_thread(self._rmtree_sync, bundle_dir)

        await self._emit(AppUninstalled(app_id=app_id, min_age=existing.min_age))
        if ending and self._sessions is not None:
            try:
                await self._sessions.tell_opponents(app_id, ending)
            except Exception as exc:
                log.warning(
                    "uninstall: telling opponents about %s failed: %s",
                    app_id,
                    exc,
                    exc_info=True,
                )

    async def uninstall_preview(
        self, app_id: str, *, actor_is_admin: bool
    ) -> AppUninstallPreview:
        """What uninstalling *app_id* loses (D9): people with saved data,
        and the games and open invites with other households (with who
        will be told). Admins only."""
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may uninstall apps")
        if await self._repo.get(app_id) is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")
        if self._sessions is not None:
            return await self._sessions.preview_uninstall(app_id)
        return AppUninstallPreview(
            people=await self._repo.kv_people(app_id), games=0, invites=0
        )

    async def set_enabled(
        self,
        app_id: str,
        *,
        enabled: bool,
        actor_is_admin: bool,
    ) -> InstalledApp:
        """Enable or disable an installed app.

        Raises :class:`SpacePermissionError` for non-admins and
        :class:`AppNotFoundError` when the app is not installed.
        Returns the updated :class:`InstalledApp`.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may enable/disable apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

        await self._repo.set_enabled(app_id, enabled=enabled)

        updated = await self._repo.get(app_id)
        assert updated is not None  # just set it
        if existing.enabled != updated.enabled:
            await self._emit(
                AppEnabledChanged(
                    app_id=app_id, enabled=updated.enabled, min_age=updated.min_age
                )
            )
        return updated

    # ─── Child protection age gate ───────────────────────────────────────────

    async def assert_age_allowed(self, app: InstalledApp, user_id: str) -> None:
        """Raise :class:`AppAgeRestrictedError` if a protected minor is below
        the app's minimum age.

        Gate semantics (mirrors ``ChildProtectionService.check_space_age_gate``):
        - ``app.min_age <= 0`` → no restriction → always allowed.
        - ``cp_repo`` not configured → allowed (no protection data available).
        - User has no protection record (``get_user_protection`` returns ``None``)
          → unprotected account → allowed.
        - ``child_protection_enabled`` is falsy → unprotected → allowed.
        - ``declared_age < app.min_age`` → blocked → :class:`AppAgeRestrictedError`.
        """
        if app.min_age <= 0:
            return
        if self._cp_repo is None:
            return
        p = await self._cp_repo.get_user_protection(user_id)
        if p is None:
            return
        if not app_age_allows(
            app.min_age,
            protected=bool(p.get("child_protection_enabled")),
            declared_age=p.get("declared_age"),
        ):
            raise AppAgeRestrictedError(
                f"This app is restricted to ages {app.min_age}+."
            )

    async def _age_allowed(self, app: InstalledApp, user_id: str) -> bool:
        """Non-raising predicate for use in list filtering."""
        try:
            await self.assert_age_allowed(app, user_id)
            return True
        except AppAgeRestrictedError:
            return False

    # ─── Per-user KV store ───────────────────────────────────────────────────

    async def _require_enabled_app(self, app_id: str, user_id: str) -> InstalledApp:
        """Return the installed app or raise AppNotFoundError / AppNotEnabledError
        / AppAgeRestrictedError.

        The age check is the final step — only applied when the app exists and
        is enabled, so the gate always fails closed.
        """
        app = await self._repo.get(app_id)
        if app is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")
        if not app.enabled:
            raise AppNotEnabledError(f"App {app_id!r} is disabled")
        await self.assert_age_allowed(app, user_id)
        return app

    async def require_app_permission(
        self, app_id: str, user_id: str, permission: str
    ) -> InstalledApp:
        """The installed, enabled app *user_id* may use — and only when its
        installed manifest grants *permission* (``storage`` / ``people`` /
        ``federation``, :func:`~socialhome.domain.apps.app_permissions`).

        The server half of the permission check (the SPA's bridge is the
        other): every ``/api/apps/{id}/…`` route an app reaches through the
        bridge calls this first. Raises :class:`AppNotFoundError` /
        :class:`AppNotEnabledError` / :class:`AppAgeRestrictedError` /
        :class:`~socialhome.domain.apps.AppPermissionDeniedError`.
        """
        app = await self._require_enabled_app(app_id, user_id)
        app.require_permission(permission)
        return app

    async def list_visible(self, *, user_id: str, is_admin: bool) -> list[InstalledApp]:
        """Return apps visible to the caller.

        Admins see all installed apps.  Non-admins see only *enabled* apps
        whose age restriction the caller passes.  A protected minor whose
        declared age is below an app's ``min_age`` does not see that app in
        the list — they must not discover gated content through enumeration.
        """
        apps = await self._repo.list_installed()
        if is_admin:
            return apps
        result: list[InstalledApp] = []
        for app in apps:
            if not app.enabled:
                continue
            if await self._age_allowed(app, user_id):
                result.append(app)
        return result

    async def games_going(self, user_id: str, app_ids: list[str]) -> dict[str, int]:
        """Per app of *app_ids*: how many games *user_id* has going — their
        1:1 games being played (:meth:`AppPendingSession.is_game_going`)
        and the group tables they have a place at that haven't ended. Only
        the caller's own games; apps with none are left out. One query per
        table, whatever the number of apps."""
        recent_cut = (
            datetime.now(UTC) - timedelta(seconds=UNINSTALL_GAME_RECENT_SECONDS)
        ).isoformat()
        out: dict[str, int] = {}
        for row in await self._repo.list_playing(user_id, app_ids):
            if row.is_game_going(recent_cut):
                out[row.app_id] = out.get(row.app_id, 0) + 1
        if self._group_repo is not None:
            seated = await self._group_repo.count_seated(user_id, app_ids)
            for app_id, n in seated.items():
                out[app_id] = out.get(app_id, 0) + n
        return out

    async def set_min_age(
        self,
        app_id: str,
        *,
        min_age: int,
        actor_is_admin: bool,
    ) -> InstalledApp:
        """Set the minimum age requirement for an installed app.

        Admin-only.  Validates that ``min_age`` is one of ``{0, 13, 16, 18}``.
        Returns the updated :class:`InstalledApp`.

        Raises:
            :class:`SpacePermissionError` — caller is not an admin.
            :class:`AppNotFoundError` — app is not installed.
            :class:`ValueError` — ``min_age`` is not a valid value.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may set the age restriction")
        if min_age not in _VALID_MIN_AGES:
            raise ValueError(
                f"min_age must be one of {sorted(_VALID_MIN_AGES)}, got {min_age!r}"
            )
        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")
        # Admin-authoritative: the household admin has full control of the
        # gate in both directions. The manifest's ``min_age`` is only the
        # install-time default (and an update-time ratchet) — not a floor.
        await self._repo.set_min_age(app_id, min_age)
        updated = await self._repo.get(app_id)
        assert updated is not None  # just set it
        return updated

    async def store_get(self, app_id: str, user_id: str, key: str) -> object:
        """Return the parsed value for ``key`` in the per-user store.

        Raises :class:`AppNotFoundError` / :class:`AppNotEnabledError` /
        :class:`AppAgeRestrictedError` if the app is absent, disabled, or
        age-restricted for this user; and :class:`KeyError` if the key has
        not been set.
        """
        await self.require_app_permission(app_id, user_id, PERMISSION_STORAGE)
        entry = await self._repo.kv_get(app_id, user_id, key)
        if entry is None:
            raise KeyError(key)
        return orjson.loads(entry.value_json)

    async def store_list(self, app_id: str, user_id: str) -> dict[str, object]:
        """Return all key→value pairs in the per-user store as a parsed dict.

        Returns an empty dict when no keys have been set.  Raises
        :class:`AppNotFoundError` / :class:`AppNotEnabledError` /
        :class:`AppAgeRestrictedError` if the app is absent, disabled, or
        age-restricted for this user.
        """
        await self.require_app_permission(app_id, user_id, PERMISSION_STORAGE)
        return {
            e.key: orjson.loads(e.value_json)
            for e in await self._repo.kv_list(app_id, user_id)
        }

    async def store_set(
        self, app_id: str, user_id: str, key: str, value: object
    ) -> None:
        """Persist ``value`` under ``key`` in the per-user store.

        Quota checks (enforced before any write):
        - ``len(key) <= APP_KV_MAX_KEY_LEN`` — else :class:`AppQuotaExceededError`
        - JSON-encoded ``value`` ≤ ``APP_KV_MAX_VALUE_BYTES`` bytes — else
          :class:`AppQuotaExceededError`
        - Adding a **new** key when the user already has ``APP_KV_MAX_KEYS``
          keys → :class:`AppQuotaExceededError`.  Updating an existing key at
          the cap is allowed.

        Raises :class:`AppNotFoundError` / :class:`AppNotEnabledError` /
        :class:`AppAgeRestrictedError` if the app is absent, disabled, or
        age-restricted for this user.
        """
        await self.require_app_permission(app_id, user_id, PERMISSION_STORAGE)

        if len(key) > APP_KV_MAX_KEY_LEN:
            raise AppQuotaExceededError(
                f"Key length {len(key)} exceeds maximum {APP_KV_MAX_KEY_LEN}"
            )

        value_json = orjson.dumps(value).decode()
        if len(value_json.encode("utf-8")) > APP_KV_MAX_VALUE_BYTES:
            raise AppQuotaExceededError(
                f"Serialised value size exceeds maximum {APP_KV_MAX_VALUE_BYTES} bytes"
            )

        # Only check the key count when inserting a NEW key
        if (
            await self._repo.kv_get(app_id, user_id, key) is None
            and await self._repo.kv_count(app_id, user_id) >= APP_KV_MAX_KEYS
        ):
            raise AppQuotaExceededError(
                f"Per-user key count limit {APP_KV_MAX_KEYS} reached for app {app_id!r}"
            )

        await self._repo.kv_set(
            app_id, user_id, key, value_json, datetime.now(UTC).isoformat()
        )

    async def store_delete(self, app_id: str, user_id: str, key: str) -> None:
        """Remove ``key`` from the per-user store (no-op if the key is absent).

        Raises :class:`AppNotFoundError` / :class:`AppNotEnabledError` /
        :class:`AppAgeRestrictedError` if the app is absent, disabled, or
        age-restricted for this user.
        """
        await self.require_app_permission(app_id, user_id, PERMISSION_STORAGE)
        await self._repo.kv_delete(app_id, user_id, key)

    # ─── Sync helpers (run inside asyncio.to_thread) ─────────────────────────

    @staticmethod
    def _compute_sha256(data: bytes) -> str:
        """Return the hex SHA-256 digest of ``data``.

        Runs inside ``asyncio.to_thread`` — blocking hashlib over potentially
        large buffers is too slow for the event loop.
        """
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _extract_bundle_sync(data: bytes, dest: Path) -> dict:
        """Extract a .tgz bundle into ``dest`` with path-traversal protection.

        Rejects:
        - Members with absolute paths (``name.startswith('/')``)
        - Members whose resolved path would escape ``dest`` (``../`` etc.)
        - Non-regular-file / non-directory members (symlinks, devices, FIFOs)
        - More than ``MAX_BUNDLE_FILES`` members, or member sizes adding up
          past ``MAX_BUNDLE_EXTRACTED_BYTES`` (:class:`AppBundleTooLargeError`)

        After extraction, reads and returns the parsed ``manifest.json`` dict
        so the caller never needs a blocking Path.read_text on the event loop.

        Raises :class:`AppIntegrityError` on any violation or a missing /
        invalid manifest.  Extraction is member-by-member so we validate
        *before* writing each file.
        """
        dest_resolved = dest.resolve()
        max_files = MAX_BUNDLE_FILES
        max_bytes = MAX_BUNDLE_EXTRACTED_BYTES
        members = 0
        unpacked = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                # Lazy iteration (not ``getmembers()``, which would inflate
                # the whole archive first): each header is checked against
                # the caps before its data is read or written.
                for member in tar:
                    members += 1
                    if members > max_files:
                        raise AppBundleTooLargeError(
                            f"The app bundle has more than {max_files} files."
                        )
                    unpacked += max(member.size, 0)
                    if unpacked > max_bytes:
                        raise AppBundleTooLargeError(
                            f"The app bundle unpacks to more than "
                            f"{max_bytes // (1024 * 1024)} MiB."
                        )
                    # Reject symlinks, hard links, device files, FIFOs, etc.
                    if not (member.isfile() or member.isdir()):
                        raise AppIntegrityError(
                            f"Bundle contains non-regular member: {member.name!r} "
                            f"(type={member.type!r})"
                        )

                    # Reject absolute paths
                    if member.name.startswith("/"):
                        raise AppIntegrityError(
                            f"Bundle contains absolute path: {member.name!r}"
                        )

                    # Reject path traversal — resolve against dest and check
                    # that the result stays inside dest.
                    member_path = (dest / member.name).resolve()
                    try:
                        member_path.relative_to(dest_resolved)
                    except ValueError:
                        raise AppIntegrityError(
                            f"Bundle path traversal detected: {member.name!r} "
                            f"would escape destination {dest}"
                        )

                    # Safe to extract this member
                    if member.isdir():
                        member_path.mkdir(parents=True, exist_ok=True)
                    else:
                        member_path.parent.mkdir(parents=True, exist_ok=True)
                        fobj = tar.extractfile(member)
                        if fobj is not None:
                            member_path.write_bytes(fobj.read())
        except (tarfile.TarError, EOFError) as exc:
            raise AppIntegrityError(f"Corrupt or unreadable bundle: {exc}") from exc

        # Read manifest.json here (we're already off the event loop) so the
        # caller never needs a blocking Path.read_text on the event loop.
        manifest_path = dest / "manifest.json"
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
            return orjson.loads(manifest_text)
        except (FileNotFoundError, OSError, orjson.JSONDecodeError) as exc:
            raise AppIntegrityError(
                f"Invalid or missing manifest.json in bundle: {exc}"
            ) from exc

    @staticmethod
    def _rmtree_sync(path: Path) -> None:
        """Remove ``path`` and all its contents (synchronous, run in thread).

        Logs a warning if the removal fails so a cleanup error is never
        silently swallowed.
        """
        if path.exists():

            def _on_error(
                func: object, failing_path: object, exc: BaseException
            ) -> None:
                log.warning(
                    "_rmtree_sync: failed to remove %s via %s: %s",
                    failing_path,
                    func,
                    exc,
                )

            shutil.rmtree(path, onexc=_on_error)
