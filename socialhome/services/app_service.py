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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles.os
import orjson

from ..domain.apps import (
    _VALID_MIN_AGES,
    MAX_BUNDLE_DOWNLOAD_BYTES,
    MAX_BUNDLE_EXTRACTED_BYTES,
    MAX_BUNDLE_FILES,
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppBundleTooLargeError,
    AppCatalogDisabledError,
    AppCatalogEntry,
    AppCatalogStatus,
    AppIntegrityError,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    InstalledApp,
    is_newer_version,
)
from ..domain.events import AppInstalled, AppUninstalled, AppUpdated
from ..domain.space import SpacePermissionError
from ..repositories.app_repo import AbstractAppRepo
from ..repositories.cp_repo import AbstractCpRepo
from .app_catalog_service import AppCatalogService
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

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
        :class:`AppUninstalled`.  ``None`` silently skips publication.
    """

    # NOTE: SpacePermissionError is intentionally used for admin-gate failures
    # in PR1 (it maps correctly to HTTP 403 via BaseView._map_exc and matches
    # the PR1 plan).  A dedicated AppPermissionError is deferred to PR2.

    __slots__ = ("_apps_path", "_bus", "_catalog", "_cp_repo", "_downloader", "_repo")

    def __init__(
        self,
        *,
        repo: AbstractAppRepo,
        catalog: AppCatalogService | None,
        apps_path: Path,
        downloader: Callable[[str], bytes | Awaitable[bytes]],
        bus: EventBus | None = None,
        cp_repo: AbstractCpRepo | None = None,
    ) -> None:
        self._repo = repo
        self._catalog = catalog
        self._apps_path = Path(apps_path)
        self._downloader = downloader
        self._bus = bus
        self._cp_repo = cp_repo

    # ─── Read-side passthroughs ──────────────────────────────────────────────

    async def list_installed(self) -> list[InstalledApp]:
        """Return all installed apps."""
        return await self._repo.list_installed()

    async def get(self, app_id: str) -> InstalledApp | None:
        """Return a single installed app by id, or ``None``."""
        return await self._repo.get(app_id)

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

    async def list_updates(self, *, force: bool = False) -> list[dict]:
        """Return a list of installed apps that have a newer version in the catalog.

        Each entry is a dict with keys ``app_id``, ``name``,
        ``current_version``, and ``latest_version``. Only a strictly newer
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
                updates.append(
                    {
                        "app_id": app.app_id,
                        "name": app.name,
                        "current_version": app.version,
                        "latest_version": entry.latest_version,
                    }
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
            min_age=manifest.min_age,
        )
        await self._repo.install(app)
        await self._emit(AppInstalled(app_id=app_id, name=entry.name))

        return app

    async def update_app(
        self,
        app_id: str,
        *,
        actor_is_admin: bool,
    ) -> InstalledApp:
        """Update an installed app to the latest version from the catalog.

        Steps:
        1. Admin gate.
        2. Lookup installed app → :class:`AppNotFoundError` if absent.
        3. Fetch the catalog (force refresh so the check is current).
        4. If already at the latest version, return the existing row unchanged.
        5. Download + SHA-256 verify + safe extraction via
           :meth:`_download_verify_unpack` (same security pipeline as install).
        6. Update the repo row, preserving ``enabled`` and ``installed_by``.
        7. Remove the OLD bundle directory from disk.
        8. Publish :class:`AppUpdated`.

        Args:
            app_id: The app to update.
            actor_is_admin: Must be ``True``; raises
                :class:`SpacePermissionError` otherwise.

        Returns:
            The updated :class:`InstalledApp` (or the unchanged existing row
            if already at the latest version).

        Raises:
            :class:`SpacePermissionError` — caller is not an admin.
            :class:`AppNotFoundError` — app is not installed.
            :class:`AppIntegrityError` — sha mismatch, path traversal,
                or invalid manifest in the new bundle.
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

        manifest, bundle_rel, actual_sha = await self._download_verify_unpack(
            app_id, entry
        )

        # Preserve admin-configured age gate, but auto-raise when the new
        # manifest declares a higher minimum age (e.g. app upgrades its rating).
        # Never silently LOWER an admin's gate — take the stricter value.
        new_min_age = max(existing.min_age, manifest.min_age)
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
            )
        )
        return updated

    async def uninstall(self, app_id: str, *, actor_is_admin: bool) -> None:
        """Remove an installed app from disk and the registry.

        Raises :class:`SpacePermissionError` for non-admins and
        :class:`AppNotFoundError` when the app is not installed.
        """
        if not actor_is_admin:
            raise SpacePermissionError("Only admins may uninstall apps")

        existing = await self._repo.get(app_id)
        if existing is None:
            raise AppNotFoundError(f"App {app_id!r} is not installed")

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

        await self._emit(AppUninstalled(app_id=app_id))

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
        if not p.get("child_protection_enabled"):
            return
        declared = int(p.get("declared_age") or 0)
        if declared < app.min_age:
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
        await self._require_enabled_app(app_id, user_id)
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
        await self._require_enabled_app(app_id, user_id)
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
        await self._require_enabled_app(app_id, user_id)

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
        await self._require_enabled_app(app_id, user_id)
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
