"""Tests for AppService — install/uninstall/enable with sha256 verify + safe unpack.

Security invariants tested:
- SHA-256 mismatch → AppIntegrityError, nothing written to disk or repo.
- Path traversal in tar member → AppIntegrityError, no file escapes the dest dir.
- Symlink member → AppIntegrityError (non-regular-file/non-dir rejected).
- Absolute path member → AppIntegrityError.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from socialhome.domain.apps import (
    MAX_BUNDLE_DOWNLOAD_BYTES,
    MAX_BUNDLE_EXTRACTED_BYTES,
    MAX_BUNDLE_FILES,
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppBundleTooLargeError,
    AppCatalogEntry,
    AppCatalogStatus,
    AppIntegrityError,
    AppKvEntry,
    AppNeedsNewerCoreError,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    AppRequestNotAllowedError,
    InstalledApp,
)
from socialhome.domain.events import (
    AppEnabledChanged,
    AppInstalled,
    AppRequestDismissed,
    AppRequested,
    AppUninstalled,
    AppUpdated,
)
from socialhome.domain.space import SpacePermissionError
from socialhome.services import app_service as app_service_mod
from socialhome.services.app_service import AppService

# ─── Helpers ─────────────────────────────────────────────────────────────────


def chess_tarball() -> tuple[bytes, str]:
    """Build a minimal in-memory .tgz for the chess app.

    Returns ``(bytes, sha256_hex)``.  The archive contains:
    - ``manifest.json`` — valid manifest with entry=index.html
    - ``index.html``    — placeholder page body
    """
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": ["storage"]}).encode()
    html = b"<html><body>Chess</body></html>"

    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        for name, data in [("manifest.json", manifest), ("index.html", html)]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def make_tarball_with_member(name: str, data: bytes = b"evil") -> tuple[bytes, str]:
    """Build a .tgz containing ``manifest.json`` plus a single member ``name``."""
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": []}).encode()
    html = b"ok"

    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        # valid manifest
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        # valid index.html
        info2 = tarfile.TarInfo(name="index.html")
        info2.size = len(html)
        tar.addfile(info2, io.BytesIO(html))
        # the adversarial member
        info3 = tarfile.TarInfo(name=name)
        info3.size = len(data)
        tar.addfile(info3, io.BytesIO(data))

    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def make_symlink_tarball() -> tuple[bytes, str]:
    """Build a .tgz containing a symlink member (type SYMTYPE)."""
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": []}).encode()
    html = b"ok"

    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        info2 = tarfile.TarInfo(name="index.html")
        info2.size = len(html)
        tar.addfile(info2, io.BytesIO(html))
        # symlink member
        sym = tarfile.TarInfo(name="evil_link")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        sym.size = 0
        tar.addfile(sym, io.BytesIO(b""))

    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


# ─── Fakes ────────────────────────────────────────────────────────────────────


class _FakeAppRepo:
    """Dict-backed in-memory AppRepo."""

    def __init__(self) -> None:
        self._apps: dict[str, InstalledApp] = {}
        self._kv: dict[tuple[str, str, str], AppKvEntry] = {}

    async def list_installed(self) -> list[InstalledApp]:
        return list(self._apps.values())

    async def get(self, app_id: str) -> InstalledApp | None:
        return self._apps.get(app_id)

    async def install(self, app: InstalledApp) -> None:
        self._apps[app.app_id] = app

    async def set_enabled(self, app_id: str, *, enabled: bool) -> None:
        existing = self._apps[app_id]
        # InstalledApp is frozen — rebuild with new enabled flag
        self._apps[app_id] = InstalledApp(
            app_id=existing.app_id,
            name=existing.name,
            version=existing.version,
            enabled=enabled,
            manifest=existing.manifest,
            bundle_path=existing.bundle_path,
            bundle_sha256=existing.bundle_sha256,
            source_url=existing.source_url,
            installed_by=existing.installed_by,
            installed_at=existing.installed_at,
            min_age=existing.min_age,
        )

    async def set_min_age(self, app_id: str, min_age: int) -> None:
        existing = self._apps[app_id]
        self._apps[app_id] = InstalledApp(
            app_id=existing.app_id,
            name=existing.name,
            version=existing.version,
            enabled=existing.enabled,
            manifest=existing.manifest,
            bundle_path=existing.bundle_path,
            bundle_sha256=existing.bundle_sha256,
            source_url=existing.source_url,
            installed_by=existing.installed_by,
            installed_at=existing.installed_at,
            min_age=min_age,
        )

    async def update_installed(self, app: InstalledApp) -> None:
        existing = self._apps[app.app_id]
        self._apps[app.app_id] = InstalledApp(
            app_id=app.app_id,
            name=app.name,
            version=app.version,
            enabled=existing.enabled,
            manifest=app.manifest,
            bundle_path=app.bundle_path,
            bundle_sha256=app.bundle_sha256,
            source_url=app.source_url,
            installed_by=existing.installed_by,
            installed_at=existing.installed_at,
            # min_age is computed by the service (max of existing and manifest)
            # and stored on the incoming app — honour it rather than overwriting
            # with the existing value so the FIX 2 semantics are testable.
            min_age=app.min_age,
        )

    async def uninstall(self, app_id: str) -> None:
        self._apps.pop(app_id, None)

    # ─── KV store ────────────────────────────────────────────────────────────

    async def kv_get(self, app_id: str, user_id: str, key: str) -> AppKvEntry | None:
        return self._kv.get((app_id, user_id, key))

    async def kv_list(self, app_id: str, user_id: str) -> list[AppKvEntry]:
        return [
            entry
            for (a, u, _k), entry in self._kv.items()
            if a == app_id and u == user_id
        ]

    async def kv_set(
        self,
        app_id: str,
        user_id: str,
        key: str,
        value_json: str,
        updated_at: str,
    ) -> None:
        self._kv[(app_id, user_id, key)] = AppKvEntry(
            app_id=app_id,
            user_id=user_id,
            key=key,
            value_json=value_json,
            updated_at=updated_at,
        )

    async def kv_delete(self, app_id: str, user_id: str, key: str) -> None:
        self._kv.pop((app_id, user_id, key), None)

    async def kv_count(self, app_id: str, user_id: str) -> int:
        return sum(1 for (a, u, _k) in self._kv if a == app_id and u == user_id)


class _StubCatalog:
    """Minimal stub that wraps a list[AppCatalogEntry]."""

    def __init__(self, entries: list[AppCatalogEntry]) -> None:
        self._entries = entries
        self.fetch_call_count: int = 0
        self.status = AppCatalogStatus()

    async def fetch_catalog(self, *, force: bool = False) -> list[AppCatalogEntry]:
        self.fetch_call_count += 1
        return list(self._entries)


class _FakeCpRepo:
    """Minimal fake CpRepo that only supports get_user_protection."""

    def __init__(
        self,
        protection: dict | None = None,
    ) -> None:
        # Maps user_id → protection dict (or None if not registered)
        self._protection: dict[str, dict | None] = {}
        if protection is not None:
            # Default entry for any user_id
            self._default: dict | None = protection
        else:
            self._default = None

    def add(self, user_id: str, *, enabled: bool, declared_age: int) -> None:
        self._protection[user_id] = {
            "child_protection_enabled": 1 if enabled else 0,
            "declared_age": declared_age,
        }

    async def get_user_protection(self, user_id: str) -> dict | None:
        return self._protection.get(user_id, self._default)


class _RecordingBus:
    """Captures published events for assertion."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def chess_bundle() -> tuple[bytes, str]:
    return chess_tarball()


@pytest.fixture()
def catalog_entry(chess_bundle: tuple[bytes, str]) -> AppCatalogEntry:
    _, sha = chess_bundle
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="Play chess",
        icon_url=None,
        capabilities=("storage",),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )


def make_service(
    tmp_path: Path,
    entries: list[AppCatalogEntry],
    bundle_bytes: bytes,
    *,
    bus: _RecordingBus | None = None,
    cp_repo: _FakeCpRepo | None = None,
) -> tuple[AppService, _FakeAppRepo]:
    repo = _FakeAppRepo()
    stub_catalog = _StubCatalog(entries)

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc = AppService(
        repo=repo,
        catalog=stub_catalog,  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=downloader,
        bus=bus,
        cp_repo=cp_repo,  # type: ignore[arg-type]
    )
    return svc, repo


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(SpacePermissionError):
        await svc.install(
            "chess",
            actor_is_admin=False,
            actor_user_id="user1",
        )

    # Nothing written to repo
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_sha_mismatch(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() must raise AppIntegrityError if sha256 doesn't match.

    Assert that NOTHING is written to disk or repo — the integrity check must
    happen BEFORE any filesystem mutation.
    """
    bundle_bytes, _ = chess_bundle
    # Catalog says a DIFFERENT sha than the actual bundle
    bad_entry = AppCatalogEntry(
        app_id=catalog_entry.app_id,
        name=catalog_entry.name,
        latest_version=catalog_entry.latest_version,
        description=catalog_entry.description,
        icon_url=catalog_entry.icon_url,
        capabilities=catalog_entry.capabilities,
        bundle_url=catalog_entry.bundle_url,
        bundle_sha256="a" * 64,  # wrong hash
    )
    svc, repo = make_service(tmp_path, [bad_entry], bundle_bytes)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    # Nothing in repo
    assert await repo.list_installed() == []
    # Nothing on disk
    assert not (tmp_path / "chess").exists()


@pytest.mark.asyncio
async def test_install_unpacks_and_records(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Happy path: valid bundle → file on disk + repo row with verified sha."""
    bundle_bytes, expected_sha = chess_bundle
    bus = _RecordingBus()
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes, bus=bus)

    app = await svc.install(
        "chess",
        actor_is_admin=True,
        actor_user_id="admin1",
    )

    # File on disk
    index_path = tmp_path / "chess" / "1.0.0" / "index.html"
    assert index_path.exists(), f"expected {index_path} to exist"

    # Repo row
    installed = await repo.get("chess")
    assert installed is not None
    assert installed.bundle_sha256 == expected_sha
    assert installed.enabled is True
    assert installed.app_id == "chess"
    assert installed.bundle_path == "chess/1.0.0"

    # Return value
    assert app.app_id == "chess"

    # Event published
    assert len(bus.published) == 1
    evt = bus.published[0]
    assert isinstance(evt, AppInstalled)
    assert evt.app_id == "chess"
    assert evt.name == "Chess"
    # Who installed it ("Anna added Chess") and its age gate (who may hear).
    assert evt.installed_by == "admin1"
    assert evt.min_age == 0


@pytest.mark.asyncio
async def test_install_rejects_path_traversal(
    tmp_path: Path,
) -> None:
    """A tarball member named ``../evil.txt`` must raise AppIntegrityError.

    The evil file must NOT be created outside the destination directory.
    """
    raw, sha = make_tarball_with_member("../evil.txt")
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    # The evil file must NOT exist outside the dest dir
    evil_path = tmp_path / "evil.txt"
    assert not evil_path.exists(), (
        f"path traversal succeeded — {evil_path} was created!"
    )

    # Nothing in repo
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_absolute_path_member(
    tmp_path: Path,
) -> None:
    """A tarball member with an absolute path must raise AppIntegrityError."""
    raw, sha = make_tarball_with_member("/etc/evil.txt")
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_rejects_symlink_member(
    tmp_path: Path,
) -> None:
    """A tarball containing a symlink member must raise AppIntegrityError."""
    raw, sha = make_symlink_tarball()
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install(
            "chess",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_uninstall_removes_row_and_bundle(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """After install, uninstall deletes the repo row and the bundle dir."""
    bundle_bytes, _ = chess_bundle
    bus = _RecordingBus()
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes, bus=bus)

    # First install
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    bundle_dir = tmp_path / "chess" / "1.0.0"
    assert bundle_dir.exists()

    # Now uninstall
    await svc.uninstall("chess", actor_is_admin=True)

    # Row gone
    assert await repo.get("chess") is None

    # Bundle dir gone
    assert not bundle_dir.exists()

    # AppUninstalled event published (it's the second event in the bus)
    uninstall_events = [e for e in bus.published if isinstance(e, AppUninstalled)]
    assert len(uninstall_events) == 1
    assert uninstall_events[0].app_id == "chess"
    assert uninstall_events[0].min_age == 0


@pytest.mark.asyncio
async def test_uninstall_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """uninstall() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    # Install first as admin
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    with pytest.raises(SpacePermissionError):
        await svc.uninstall("chess", actor_is_admin=False)

    # Still installed
    assert await repo.get("chess") is not None


@pytest.mark.asyncio
async def test_uninstall_missing_raises_not_found(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Uninstalling a non-existent app raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.uninstall("nonexistent", actor_is_admin=True)


@pytest.mark.asyncio
async def test_set_enabled_toggles(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled can disable then re-enable an installed app."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Disable
    result = await svc.set_enabled("chess", enabled=False, actor_is_admin=True)
    assert result.enabled is False
    assert (await repo.get("chess")).enabled is False  # type: ignore[union-attr]

    # Re-enable
    result = await svc.set_enabled("chess", enabled=True, actor_is_admin=True)
    assert result.enabled is True
    assert (await repo.get("chess")).enabled is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_set_enabled_publishes_only_when_the_switch_moves(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Turning an app off / on tells open pages and tabs; setting the same
    state again tells nobody."""
    bundle_bytes, _ = chess_bundle
    bus = _RecordingBus()
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes, bus=bus)
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    await svc.set_enabled("chess", enabled=True, actor_is_admin=True)  # no-op
    await svc.set_enabled("chess", enabled=False, actor_is_admin=True)
    await svc.set_enabled("chess", enabled=False, actor_is_admin=True)  # no-op
    await svc.set_enabled("chess", enabled=True, actor_is_admin=True)

    changes = [e for e in bus.published if isinstance(e, AppEnabledChanged)]
    assert [(e.app_id, e.enabled) for e in changes] == [
        ("chess", False),
        ("chess", True),
    ]


@pytest.mark.asyncio
async def test_set_enabled_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    with pytest.raises(SpacePermissionError):
        await svc.set_enabled("chess", enabled=False, actor_is_admin=False)


@pytest.mark.asyncio
async def test_set_enabled_missing_raises_not_found(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """set_enabled on non-existent app raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.set_enabled("nonexistent", enabled=False, actor_is_admin=True)


@pytest.mark.asyncio
async def test_install_no_catalog_raises_integrity_error(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with catalog=None must raise AppIntegrityError."""
    bundle_bytes, _ = chess_bundle
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc = AppService(
        repo=repo,
        catalog=None,
        apps_path=tmp_path,
        downloader=downloader,
    )

    with pytest.raises(AppIntegrityError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")


@pytest.mark.asyncio
async def test_install_app_id_not_in_catalog(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() with unknown app_id raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.install("unknown_app", actor_is_admin=True, actor_user_id="admin1")


@pytest.mark.asyncio
async def test_list_installed_passthrough(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """list_installed() returns the repo's list."""
    bundle_bytes, _ = chess_bundle
    svc, _repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    assert await svc.list_installed() == []
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    result = await svc.list_installed()
    assert len(result) == 1
    assert result[0].app_id == "chess"


@pytest.mark.asyncio
async def test_browse_catalog_passthrough(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """browse_catalog() delegates to the catalog service."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    entries = await svc.browse_catalog()
    assert len(entries) == 1
    assert entries[0].app_id == "chess"


@pytest.mark.asyncio
async def test_browse_catalog_no_catalog_returns_empty(
    tmp_path: Path,
) -> None:
    """browse_catalog() with catalog=None returns empty list."""
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return b""

    svc = AppService(
        repo=repo,
        catalog=None,
        apps_path=tmp_path,
        downloader=downloader,
    )
    assert await svc.browse_catalog() == []


@pytest.mark.asyncio
async def test_get_passthrough(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """get() returns the installed app after install and None for unknown ids."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    # Before install — unknown id returns None
    assert await svc.get("chess") is None
    assert await svc.get("nonexistent") is None

    # After install — returns the installed app
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    result = await svc.get("chess")
    assert result is not None
    assert result.app_id == "chess"

    # A different id still returns None
    assert await svc.get("nonexistent") is None


@pytest.mark.asyncio
async def test_install_rejects_malicious_app_id(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() must raise AppIntegrityError when catalog app_id escapes apps_path.

    A catalog entry with app_id='../../etc' would compute a dest path that
    resolves outside apps_path.  The containment check must catch this BEFORE
    any directory is created outside the apps root.
    """
    bundle_bytes, sha = chess_bundle
    malicious_entry = AppCatalogEntry(
        app_id="../../etc",
        name="Evil",
        latest_version="1.0.0",
        description="escape attempt",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/evil.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [malicious_entry], bundle_bytes)

    with pytest.raises(AppIntegrityError, match="escapes apps root"):
        await svc.install(
            "../../etc",
            actor_is_admin=True,
            actor_user_id="admin1",
        )

    # Nothing written to repo
    assert await repo.list_installed() == []
    # Nothing created outside tmp_path (the resolved etc dir must not exist due
    # to us; we just verify the dest inside tmp_path was not created)
    assert not (tmp_path / "../../etc").exists()


@pytest.mark.asyncio
async def test_install_twice_raises(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """A second install() for the same app_id must raise AppAlreadyInstalledError."""
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    # First install succeeds
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert await repo.get("chess") is not None

    # Second install must fail immediately without touching disk again
    with pytest.raises(AppAlreadyInstalledError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Still exactly one row
    installed = await repo.list_installed()
    assert len(installed) == 1


@pytest.mark.asyncio
async def test_partial_dir_removed_after_failed_install(
    tmp_path: Path,
) -> None:
    """A failed extraction must not leave a partial bundle dir on disk.

    Uses a path-traversal tarball (raises AppIntegrityError mid-extraction)
    and asserts that dest is gone after the exception propagates.
    """
    raw, sha = make_tarball_with_member("../evil.txt")
    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], raw)

    with pytest.raises(AppIntegrityError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # The partial dest dir must have been cleaned up
    dest = tmp_path / "chess" / "1.0.0"
    assert not dest.exists(), f"partial bundle dir was not cleaned up: {dest}"
    # Nothing in repo
    assert await repo.list_installed() == []


# ─── Per-user KV store tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_set_get_roundtrip(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_set then store_get returns the original parsed value."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    await svc.store_set("chess", "user1", "prefs", {"turn": "w"})
    result = await svc.store_get("chess", "user1", "prefs")

    assert result == {"turn": "w"}


@pytest.mark.asyncio
async def test_store_requires_installed(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_set on an unknown app_id raises AppNotFoundError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    with pytest.raises(AppNotFoundError):
        await svc.store_set("unknown_app", "user1", "k", {"v": 1})


@pytest.mark.asyncio
async def test_store_requires_enabled(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_set and store_get raise AppNotEnabledError when the app is disabled."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    await svc.set_enabled("chess", enabled=False, actor_is_admin=True)

    with pytest.raises(AppNotEnabledError):
        await svc.store_set("chess", "user1", "k", {"v": 1})

    with pytest.raises(AppNotEnabledError):
        await svc.store_get("chess", "user1", "k")


@pytest.mark.asyncio
async def test_store_get_missing_key_raises_keyerror(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_get on a key that doesn't exist raises KeyError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    with pytest.raises(KeyError):
        await svc.store_get("chess", "user1", "nonexistent")


@pytest.mark.asyncio
async def test_store_value_too_large_raises_quota(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """A value whose JSON exceeds 64 KiB raises AppQuotaExceededError; nothing stored."""
    bundle_bytes, _ = chess_bundle
    svc, _repo = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Build a value whose json representation is > 64 KiB
    big_value = {"x": "a" * 70_000}

    with pytest.raises(AppQuotaExceededError):
        await svc.store_set("chess", "user1", "big", big_value)

    # Nothing should have been stored
    with pytest.raises(KeyError):
        await svc.store_get("chess", "user1", "big")


@pytest.mark.asyncio
async def test_store_too_many_keys_raises_quota(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After APP_KV_MAX_KEYS keys, adding a NEW key raises AppQuotaExceededError.

    Updating an existing key at the cap must still succeed.
    """
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Lower the cap to 2 for this test
    import socialhome.services.app_service as app_svc_mod

    monkeypatch.setattr(app_svc_mod, "APP_KV_MAX_KEYS", 2)

    await svc.store_set("chess", "user1", "k1", 1)
    await svc.store_set("chess", "user1", "k2", 2)

    # 3rd NEW key must fail
    with pytest.raises(AppQuotaExceededError):
        await svc.store_set("chess", "user1", "k3", 3)

    # Updating an existing key at the cap must still work
    await svc.store_set("chess", "user1", "k1", 999)
    assert await svc.store_get("chess", "user1", "k1") == 999


@pytest.mark.asyncio
async def test_store_list_returns_parsed_map(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_list returns a dict with all keys parsed from JSON."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    await svc.store_set("chess", "user1", "a", 1)
    await svc.store_set("chess", "user1", "b", [2, 3])
    await svc.store_set("chess", "user1", "c", "hello")

    result = await svc.store_list("chess", "user1")

    assert result == {"a": 1, "b": [2, 3], "c": "hello"}


@pytest.mark.asyncio
async def test_store_delete(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """store_delete removes the key; subsequent store_get raises KeyError."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)

    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    await svc.store_set("chess", "user1", "key_to_delete", {"data": True})
    # Confirm it's there
    assert await svc.store_get("chess", "user1", "key_to_delete") == {"data": True}

    # Delete it
    await svc.store_delete("chess", "user1", "key_to_delete")

    # Now it's gone
    with pytest.raises(KeyError):
        await svc.store_get("chess", "user1", "key_to_delete")

    # store_list should also not include it
    listing = await svc.store_list("chess", "user1")
    assert "key_to_delete" not in listing


# ─── list_updates + update_app tests ─────────────────────────────────────────


def chess_catalog_entry_v2(chess_bundle_v1: tuple[bytes, str]) -> AppCatalogEntry:
    """Return a catalog entry with version 2.0.0 pointing at the chess bundle."""
    _, sha = chess_bundle_v1
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="2.0.0",
        description="Play chess v2",
        icon_url=None,
        capabilities=("storage",),
        bundle_url="https://example.com/chess-2.0.0.tgz",
        bundle_sha256=sha,
    )


@pytest.mark.asyncio
async def test_list_updates_returns_updatable_app(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """list_updates() returns apps whose catalog version is newer than installed."""
    bundle_bytes, _sha = chess_bundle
    # Catalog has version 2.0.0 but we install 1.0.0
    entry_v2 = chess_catalog_entry_v2(chess_bundle)
    _svc, _ = make_service(tmp_path, [entry_v2], bundle_bytes)

    # Install via the v1 entry so the installed version is 1.0.0
    svc_v1, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Build a service whose repo already has chess@1.0.0 but catalog says 2.0.0
    repo = _FakeAppRepo()
    repo._apps["chess"] = await svc_v1._repo.get("chess")  # type: ignore[attr-defined]
    stub = _StubCatalog([entry_v2])

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc2 = AppService(
        repo=repo,
        catalog=stub,  # type: ignore[arg-type]
        apps_path=tmp_path / "svc2",
        downloader=downloader,
    )

    updates = await svc2.list_updates()
    assert len(updates) == 1
    assert updates[0]["app_id"] == "chess"
    assert updates[0]["current_version"] == "1.0.0"
    assert updates[0]["latest_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_list_updates_returns_empty_when_up_to_date(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """list_updates() returns [] when all installed apps are at the latest version."""
    bundle_bytes, _ = chess_bundle
    svc, _repo = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    updates = await svc.list_updates()
    assert updates == []


@pytest.mark.asyncio
async def test_list_updates_no_catalog_returns_empty(
    tmp_path: Path,
) -> None:
    """list_updates() returns [] when no catalog is configured."""
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return b""

    svc = AppService(
        repo=repo,
        catalog=None,
        apps_path=tmp_path,
        downloader=downloader,
    )
    assert await svc.list_updates() == []


@pytest.mark.asyncio
async def test_list_updates_force_passes_through(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """list_updates(force=True) passes force=True to the catalog."""
    bundle_bytes, _ = chess_bundle
    svc, _ = make_service(tmp_path, [catalog_entry], bundle_bytes)
    # Install an app so list_updates actually calls the catalog
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Reset the counter after install (which calls browse_catalog internally)
    stub: _StubCatalog = svc._catalog  # type: ignore[assignment]
    stub.fetch_call_count = 0

    await svc.list_updates(force=True)
    assert stub.fetch_call_count == 1


@pytest.mark.asyncio
async def test_update_app_requires_admin(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """update_app() with actor_is_admin=False must raise SpacePermissionError."""
    bundle_bytes, _ = chess_bundle
    entry_v2 = chess_catalog_entry_v2(chess_bundle)
    svc, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # Swap in a v2 catalog
    svc2 = AppService(
        repo=repo,
        catalog=_StubCatalog([entry_v2]),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=svc._downloader,
    )

    with pytest.raises(SpacePermissionError):
        await svc2.update_app("chess", actor_is_admin=False)


@pytest.mark.asyncio
async def test_update_app_raises_not_found_when_not_installed(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
) -> None:
    """update_app() on a non-installed app raises AppNotFoundError."""
    bundle_bytes, _sha = chess_bundle
    entry_v2 = chess_catalog_entry_v2(chess_bundle)
    repo = _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc = AppService(
        repo=repo,
        catalog=_StubCatalog([entry_v2]),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=downloader,
    )

    with pytest.raises(AppNotFoundError):
        await svc.update_app("chess", actor_is_admin=True)


@pytest.mark.asyncio
async def test_update_app_upgrades_to_new_version(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """Happy path: update_app installs new version, removes old dir, publishes event."""
    bundle_bytes, sha = chess_bundle
    bus = _RecordingBus()

    # Build a v2 entry — same bundle bytes/sha so the sha check passes
    entry_v2 = chess_catalog_entry_v2(chess_bundle)

    # Install v1 first using original entry
    svc_v1, repo = make_service(
        tmp_path / "install", [catalog_entry], bundle_bytes, bus=bus
    )
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")

    old_bundle_dir = tmp_path / "install" / "chess" / "1.0.0"
    assert old_bundle_dir.exists()

    # Now build a service with the v2 catalog, same repo, same apps_path
    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc_v2 = AppService(
        repo=repo,
        catalog=_StubCatalog([entry_v2]),  # type: ignore[arg-type]
        apps_path=tmp_path / "install",
        downloader=downloader,
        bus=bus,
    )

    updated = await svc_v2.update_app("chess", actor_is_admin=True)

    # Version updated in return value and repo
    assert updated.version == "2.0.0"
    assert updated.bundle_sha256 == sha
    assert updated.bundle_path == "chess/2.0.0"

    # New bundle dir unpacked
    new_bundle_dir = tmp_path / "install" / "chess" / "2.0.0"
    assert new_bundle_dir.exists()
    assert (new_bundle_dir / "index.html").exists()

    # Old bundle dir removed
    assert not old_bundle_dir.exists()

    # AppUpdated event published
    updated_events = [e for e in bus.published if isinstance(e, AppUpdated)]
    assert len(updated_events) == 1
    evt = updated_events[0]
    assert evt.app_id == "chess"
    assert evt.old_version == "1.0.0"
    assert evt.new_version == "2.0.0"
    assert evt.min_age == 0


@pytest.mark.asyncio
async def test_update_app_returns_existing_when_already_latest(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """update_app() returns existing app unchanged when already at latest version."""
    bundle_bytes, _ = chess_bundle
    svc, _repo = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")

    result = await svc.update_app("chess", actor_is_admin=True)
    assert result.version == "1.0.0"
    assert result.app_id == "chess"


# ─── Age gate tests (§CP) ─────────────────────────────────────────────────────


def _make_age_gated_app(app_id: str = "chess", min_age: int = 13) -> InstalledApp:
    from socialhome.domain.apps import AppManifest

    return InstalledApp(
        app_id=app_id,
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example.com/chess.tgz",
        installed_by="admin",
        installed_at="2026-06-02T00:00:00+00:00",
        min_age=min_age,
    )


@pytest.mark.asyncio
async def test_assert_age_allowed_min_age_zero_always_passes(tmp_path: Path) -> None:
    """min_age=0 means no restriction — always allowed."""
    cp = _FakeCpRepo()
    cp.add("user1", enabled=True, declared_age=0)
    svc, _repo = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=0)
    await svc.assert_age_allowed(app, "user1")  # must not raise


@pytest.mark.asyncio
async def test_assert_age_allowed_no_cp_repo_always_passes(tmp_path: Path) -> None:
    """When cp_repo is None the gate never blocks."""
    svc, _ = make_service(tmp_path, [], b"")  # no cp_repo
    app = _make_age_gated_app(min_age=18)
    await svc.assert_age_allowed(app, "user1")  # must not raise


@pytest.mark.asyncio
async def test_assert_age_allowed_unprotected_user_always_passes(
    tmp_path: Path,
) -> None:
    """A user without CP enabled is never blocked, even for a high min_age."""
    cp = _FakeCpRepo()
    cp.add("user1", enabled=False, declared_age=10)
    svc, _ = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=18)
    await svc.assert_age_allowed(app, "user1")  # unprotected → allowed


@pytest.mark.asyncio
async def test_assert_age_allowed_unknown_user_passes(tmp_path: Path) -> None:
    """A user not in the cp_repo is unprotected → allowed."""
    cp = _FakeCpRepo()  # no entries
    svc, _ = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=13)
    await svc.assert_age_allowed(app, "unknown-user")  # no protection record → allowed


@pytest.mark.asyncio
async def test_assert_age_allowed_minor_below_min_age_raises(tmp_path: Path) -> None:
    """Protected minor with declared_age < min_age → AppAgeRestrictedError."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, _ = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=13)
    with pytest.raises(AppAgeRestrictedError, match="13\\+"):
        await svc.assert_age_allowed(app, "minor1")


@pytest.mark.asyncio
async def test_assert_age_allowed_minor_at_min_age_passes(tmp_path: Path) -> None:
    """Protected minor with declared_age == min_age → allowed (boundary)."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=13)
    svc, _ = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=13)
    await svc.assert_age_allowed(app, "minor1")  # at threshold → allowed


@pytest.mark.asyncio
async def test_assert_age_allowed_minor_above_min_age_passes(tmp_path: Path) -> None:
    """Protected user with declared_age > min_age → allowed."""
    cp = _FakeCpRepo()
    cp.add("user1", enabled=True, declared_age=16)
    svc, _ = make_service(tmp_path, [], b"", cp_repo=cp)
    app = _make_age_gated_app(min_age=13)
    await svc.assert_age_allowed(app, "user1")  # 16 >= 13 → allowed


@pytest.mark.asyncio
async def test_set_min_age_admin_gate(tmp_path: Path) -> None:
    """set_min_age requires actor_is_admin=True."""
    svc, repo = make_service(tmp_path, [], b"")
    # Seed a row directly
    app = _make_age_gated_app(min_age=0)
    repo._apps["chess"] = app
    with pytest.raises(SpacePermissionError):
        await svc.set_min_age("chess", min_age=13, actor_is_admin=False)


@pytest.mark.asyncio
async def test_set_min_age_invalid_value_raises(tmp_path: Path) -> None:
    """set_min_age raises ValueError for values not in {0,13,16,18}."""
    svc, repo = make_service(tmp_path, [], b"")
    repo._apps["chess"] = _make_age_gated_app(min_age=0)
    with pytest.raises(ValueError, match="min_age"):
        await svc.set_min_age("chess", min_age=15, actor_is_admin=True)


@pytest.mark.asyncio
async def test_set_min_age_not_found_raises(tmp_path: Path) -> None:
    """set_min_age raises AppNotFoundError for uninstalled apps."""
    svc, _ = make_service(tmp_path, [], b"")
    with pytest.raises(AppNotFoundError):
        await svc.set_min_age("chess", min_age=13, actor_is_admin=True)


@pytest.mark.asyncio
async def test_set_min_age_valid_updates_and_returns(tmp_path: Path) -> None:
    """set_min_age updates the row and returns the updated app."""
    svc, repo = make_service(tmp_path, [], b"")
    repo._apps["chess"] = _make_age_gated_app(min_age=0)
    result = await svc.set_min_age("chess", min_age=13, actor_is_admin=True)
    assert result.min_age == 13
    assert (await repo.get("chess")).min_age == 13  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_set_min_age_admin_authoritative_can_lower(tmp_path: Path) -> None:
    """Admin-authoritative — the admin can set min_age to any valid value,
    including below the manifest's declared rating (the manifest is only the
    install-time default, not a floor)."""
    from socialhome.domain.apps import AppManifest, InstalledApp

    svc, repo = make_service(tmp_path, [], b"")
    # Publisher declared 13+ in the manifest, app currently at 13.
    repo._apps["chess"] = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(
            entry="index.html", icon=None, capabilities=(), min_age=13
        ),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example.com/chess.tgz",
        installed_by="admin",
        installed_at="2026-06-02T00:00:00+00:00",
        min_age=13,
    )
    # Admin may lower it below the manifest value.
    result = await svc.set_min_age("chess", min_age=0, actor_is_admin=True)
    assert result.min_age == 0


@pytest.mark.asyncio
async def test_list_visible_admin_sees_all(tmp_path: Path) -> None:
    """Admins see all installed apps regardless of enabled or age restriction."""
    cp = _FakeCpRepo()
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess"] = _make_age_gated_app("chess", min_age=18)
    repo._apps["puzzle"] = _make_age_gated_app("puzzle", min_age=0)
    result = await svc.list_visible(user_id="admin1", is_admin=True)
    assert {a.app_id for a in result} == {"chess", "puzzle"}


@pytest.mark.asyncio
async def test_list_visible_member_excludes_disabled(tmp_path: Path) -> None:
    """Non-admin does not see disabled apps."""
    svc, repo = make_service(tmp_path, [], b"")
    from socialhome.domain.apps import AppManifest

    repo._apps["chess"] = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=False,  # disabled
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example.com/chess.tgz",
        installed_by="admin",
        installed_at="2026-06-02T00:00:00+00:00",
    )
    result = await svc.list_visible(user_id="user1", is_admin=False)
    assert result == []


@pytest.mark.asyncio
async def test_list_visible_minor_does_not_see_age_restricted_app(
    tmp_path: Path,
) -> None:
    """A protected minor does not see apps whose min_age they fail."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess13"] = _make_age_gated_app("chess13", min_age=13)
    repo._apps["open"] = _make_age_gated_app("open", min_age=0)
    result = await svc.list_visible(user_id="minor1", is_admin=False)
    assert {a.app_id for a in result} == {"open"}
    assert "chess13" not in {a.app_id for a in result}


@pytest.mark.asyncio
async def test_list_visible_adult_sees_all_enabled(tmp_path: Path) -> None:
    """An unprotected adult sees all enabled apps."""
    cp = _FakeCpRepo()
    cp.add("adult1", enabled=False, declared_age=30)  # protection disabled
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess13"] = _make_age_gated_app("chess13", min_age=13)
    repo._apps["chess18"] = _make_age_gated_app("chess18", min_age=18)
    result = await svc.list_visible(user_id="adult1", is_admin=False)
    assert {a.app_id for a in result} == {"chess13", "chess18"}


@pytest.mark.asyncio
async def test_require_enabled_app_age_blocked_for_minor(tmp_path: Path) -> None:
    """_require_enabled_app raises AppAgeRestrictedError for under-age protected minor."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess"] = _make_age_gated_app("chess", min_age=13)
    with pytest.raises(AppAgeRestrictedError):
        await svc._require_enabled_app("chess", "minor1")


@pytest.mark.asyncio
async def test_store_get_blocked_for_minor(tmp_path: Path) -> None:
    """store_get raises AppAgeRestrictedError for a protected minor below min_age."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess"] = _make_age_gated_app("chess", min_age=13)
    with pytest.raises(AppAgeRestrictedError):
        await svc.store_get("chess", "minor1", "key")


@pytest.mark.asyncio
async def test_store_set_blocked_for_minor(tmp_path: Path) -> None:
    """store_set raises AppAgeRestrictedError for a protected minor below min_age."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess"] = _make_age_gated_app("chess", min_age=13)
    with pytest.raises(AppAgeRestrictedError):
        await svc.store_set("chess", "minor1", "key", {"v": 1})


@pytest.mark.asyncio
async def test_store_delete_blocked_for_minor(tmp_path: Path) -> None:
    """store_delete raises AppAgeRestrictedError for a protected minor below min_age."""
    cp = _FakeCpRepo()
    cp.add("minor1", enabled=True, declared_age=12)
    svc, repo = make_service(tmp_path, [], b"", cp_repo=cp)
    repo._apps["chess"] = _make_age_gated_app("chess", min_age=13)
    with pytest.raises(AppAgeRestrictedError):
        await svc.store_delete("chess", "minor1", "key")


@pytest.mark.asyncio
async def test_install_sets_min_age_from_manifest(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
) -> None:
    """install() sets min_age from the manifest's min_age field."""
    # Build a bundle with min_age=13 in manifest.json
    import gzip
    import io
    import json as _json
    import tarfile

    buf = io.BytesIO()
    manifest = _json.dumps(
        {"entry": "index.html", "capabilities": ["storage"], "min_age": 13}
    ).encode()
    html = b"<html><body>Chess</body></html>"
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        for name, data in [("manifest.json", manifest), ("index.html", html)]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    bundle_bytes = buf.getvalue()
    import hashlib

    sha = hashlib.sha256(bundle_bytes).hexdigest()

    entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="Play chess",
        icon_url=None,
        capabilities=("storage",),
        bundle_url="https://example.com/chess.tgz",
        bundle_sha256=sha,
    )
    svc, repo = make_service(tmp_path, [entry], bundle_bytes)
    app = await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert app.min_age == 13
    row = await repo.get("chess")
    assert row is not None
    assert row.min_age == 13


@pytest.mark.asyncio
async def test_update_app_preserves_enabled_and_installed_by(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """update_app() preserves the enabled flag and installed_by from the existing row."""
    bundle_bytes, _sha = chess_bundle
    entry_v2 = chess_catalog_entry_v2(chess_bundle)

    # Install v1 as admin1 and then disable the app
    svc_v1, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")
    await svc_v1.set_enabled("chess", enabled=False, actor_is_admin=True)

    assert (await repo.get("chess")).enabled is False  # type: ignore[union-attr]

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc_v2 = AppService(
        repo=repo,
        catalog=_StubCatalog([entry_v2]),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=downloader,
    )

    updated = await svc_v2.update_app("chess", actor_is_admin=True)

    # enabled and installed_by are preserved
    assert updated.enabled is False
    assert updated.installed_by == "admin1"


@pytest.mark.asyncio
async def test_update_app_sha_mismatch_raises_integrity_error(
    tmp_path: Path,
    catalog_entry: AppCatalogEntry,
    chess_bundle: tuple[bytes, str],
) -> None:
    """update_app() raises AppIntegrityError if the downloaded bundle sha256 doesn't match."""
    bundle_bytes, _ = chess_bundle

    # Install v1
    svc_v1, repo = make_service(tmp_path, [catalog_entry], bundle_bytes)
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")

    # v2 entry with wrong sha
    entry_v2_bad = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="2.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-2.0.0.tgz",
        bundle_sha256="a" * 64,  # wrong hash
    )

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    svc_v2 = AppService(
        repo=repo,
        catalog=_StubCatalog([entry_v2_bad]),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=downloader,
    )

    with pytest.raises(AppIntegrityError):
        await svc_v2.update_app("chess", actor_is_admin=True)

    # Repo row still has old version
    app = await repo.get("chess")
    assert app is not None
    assert app.version == "1.0.0"


# ─── FIX 2: update_app picks up raised min_age from manifest ─────────────────


def _make_bundle_with_min_age(min_age: int) -> tuple[bytes, str]:
    """Build a minimal .tgz with a manifest declaring the given min_age."""
    import gzip as _gzip
    import io as _io
    import json as _json
    import tarfile as _tarfile

    buf = _io.BytesIO()
    manifest = _json.dumps(
        {"entry": "index.html", "capabilities": [], "min_age": min_age}
    ).encode()
    html = b"<html><body>App</body></html>"
    with (
        _gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        _tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        for name, data in [("manifest.json", manifest), ("index.html", html)]:
            info = _tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, _io.BytesIO(data))
    raw = buf.getvalue()
    import hashlib as _hashlib

    sha = _hashlib.sha256(raw).hexdigest()
    return raw, sha


@pytest.mark.asyncio
async def test_update_app_raises_min_age_when_manifest_is_stricter(
    tmp_path: Path,
) -> None:
    """update_app auto-raises min_age when the new manifest declares a higher value.

    App installed at min_age 0 (no gate); manifest in v2 declares min_age 13
    → the updated row must have min_age 13.
    """
    # v1 bundle: manifest has min_age 0 (default / not declared)
    v1_raw, v1_sha = _make_bundle_with_min_age(0)
    v1_entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=v1_sha,
    )
    repo = _FakeAppRepo()

    async def downloader_v1(url: str) -> bytes:
        return v1_raw

    svc_v1 = AppService(
        repo=repo,
        catalog=_StubCatalog([v1_entry]),  # type: ignore[arg-type]
        apps_path=tmp_path / "v1",
        downloader=downloader_v1,
    )
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert (await repo.get("chess")).min_age == 0  # type: ignore[union-attr]

    # v2 bundle: manifest now declares min_age 13
    v2_raw, v2_sha = _make_bundle_with_min_age(13)
    v2_entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="2.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-2.0.0.tgz",
        bundle_sha256=v2_sha,
    )

    async def downloader_v2(url: str) -> bytes:
        return v2_raw

    svc_v2 = AppService(
        repo=repo,
        catalog=_StubCatalog([v2_entry]),  # type: ignore[arg-type]
        apps_path=tmp_path / "v2",
        downloader=downloader_v2,
    )
    updated = await svc_v2.update_app("chess", actor_is_admin=True)

    # The manifest raised the gate — the row must reflect the stricter value.
    assert updated.min_age == 13
    assert (await repo.get("chess")).min_age == 13  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_update_app_preserves_admin_min_age_when_manifest_is_laxer(
    tmp_path: Path,
) -> None:
    """update_app never lowers an admin-configured gate.

    App at admin-set min_age 16; new manifest declares only min_age 13
    → the updated row must still have min_age 16.
    """
    # v1 bundle: manifest has min_age 0
    v1_raw, v1_sha = _make_bundle_with_min_age(0)
    v1_entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=v1_sha,
    )
    repo = _FakeAppRepo()

    async def downloader_v1(url: str) -> bytes:
        return v1_raw

    svc_v1 = AppService(
        repo=repo,
        catalog=_StubCatalog([v1_entry]),  # type: ignore[arg-type]
        apps_path=tmp_path / "v1",
        downloader=downloader_v1,
    )
    await svc_v1.install("chess", actor_is_admin=True, actor_user_id="admin1")
    # Admin manually raises the gate to 16
    await svc_v1.set_min_age("chess", min_age=16, actor_is_admin=True)
    assert (await repo.get("chess")).min_age == 16  # type: ignore[union-attr]

    # v2 bundle: manifest declares min_age 13 (less strict than admin-set 16)
    v2_raw, v2_sha = _make_bundle_with_min_age(13)
    v2_entry = AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="2.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-2.0.0.tgz",
        bundle_sha256=v2_sha,
    )

    async def downloader_v2(url: str) -> bytes:
        return v2_raw

    svc_v2 = AppService(
        repo=repo,
        catalog=_StubCatalog([v2_entry]),  # type: ignore[arg-type]
        apps_path=tmp_path / "v2",
        downloader=downloader_v2,
    )
    updated = await svc_v2.update_app("chess", actor_is_admin=True)

    # The admin gate must NOT be lowered even though the new manifest is laxer.
    assert updated.min_age == 16
    assert (await repo.get("chess")).min_age == 16  # type: ignore[union-attr]


# ─── Bundle size limits (zip bomb / OOM / disk fill) ─────────────────────────


def _tarball_of(members: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """A .tgz with ``manifest.json`` + ``index.html`` + *members*."""
    buf = io.BytesIO()
    manifest = json.dumps({"entry": "index.html", "capabilities": []}).encode()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):
        for name, data in [
            ("manifest.json", manifest),
            ("index.html", b"ok"),
            *members,
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def _entry_for(sha: str) -> AppCatalogEntry:
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version="1.0.0",
        description="x",
        icon_url=None,
        capabilities=(),
        bundle_url="https://example.com/chess-1.0.0.tgz",
        bundle_sha256=sha,
    )


def test_bundle_limits_fit_chess_with_headroom() -> None:
    """Chess 2026.10.28 is 13 169 bytes compressed, 42 444 bytes / 2 files
    extracted — the limits leave two orders of magnitude of room."""
    assert MAX_BUNDLE_DOWNLOAD_BYTES >= 100 * 13_169
    assert MAX_BUNDLE_EXTRACTED_BYTES >= 100 * 42_444
    assert MAX_BUNDLE_FILES >= 64
    assert MAX_BUNDLE_EXTRACTED_BYTES >= MAX_BUNDLE_DOWNLOAD_BYTES


@pytest.mark.asyncio
async def test_install_refuses_a_download_over_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the downloader returns, an oversized bundle is refused
    before hashing or any disk write."""
    raw, sha = chess_tarball()
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_DOWNLOAD_BYTES", len(raw) - 1)
    svc, repo = make_service(tmp_path, [_entry_for(sha)], raw)
    with pytest.raises(AppBundleTooLargeError) as info:
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert info.value.code == "APP_BUNDLE_TOO_LARGE"
    assert not (tmp_path / "chess").exists()
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_refuses_a_zip_bomb_over_the_extracted_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tiny .tgz that inflates past the cap is refused, and the partial
    directory is removed — nothing past the cap is written."""
    raw, sha = _tarball_of([("bomb.bin", b"\0" * 200_000)])
    assert len(raw) < 2_000  # compresses to almost nothing
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_EXTRACTED_BYTES", 100_000)
    svc, repo = make_service(tmp_path, [_entry_for(sha)], raw)
    with pytest.raises(AppBundleTooLargeError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert not (tmp_path / "chess" / "1.0.0").exists()
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_install_refuses_a_bundle_with_too_many_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, sha = _tarball_of([(f"f{i}.txt", b"x") for i in range(10)])
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_FILES", 5)
    svc, repo = make_service(tmp_path, [_entry_for(sha)], raw)
    with pytest.raises(AppBundleTooLargeError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert not (tmp_path / "chess" / "1.0.0").exists()
    assert await repo.list_installed() == []


@pytest.mark.asyncio
async def test_bundle_at_the_limits_still_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, sha = _tarball_of([("a.txt", b"x" * 10)])
    # manifest + index.html + a.txt: exactly at both caps.
    total = len(json.dumps({"entry": "index.html", "capabilities": []})) + 2 + 10
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_FILES", 3)
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_EXTRACTED_BYTES", total)
    monkeypatch.setattr(app_service_mod, "MAX_BUNDLE_DOWNLOAD_BYTES", len(raw))
    svc, _ = make_service(tmp_path, [_entry_for(sha)], raw)
    app = await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    assert app.app_id == "chess"


# ─── CalVer compare: no "update" to an older catalog version ─────────────────


def _entry_at(chess_bundle: tuple[bytes, str], version: str) -> AppCatalogEntry:
    _, sha = chess_bundle
    return AppCatalogEntry(
        app_id="chess",
        name="Chess",
        latest_version=version,
        description="Play chess",
        icon_url=None,
        capabilities=("storage",),
        bundle_url=f"https://example.com/chess-{version}.tgz",
        bundle_sha256=sha,
    )


async def _installed_at(
    tmp_path: Path, chess_bundle: tuple[bytes, str], version: str
) -> _FakeAppRepo:
    bundle_bytes, _ = chess_bundle
    svc, repo = make_service(
        tmp_path / "seed", [_entry_at(chess_bundle, version)], bundle_bytes
    )
    await svc.install("chess", actor_is_admin=True, actor_user_id="admin1")
    return repo


def _service_on(
    tmp_path: Path,
    repo: _FakeAppRepo,
    chess_bundle: tuple[bytes, str],
    catalog_version: str,
) -> AppService:
    bundle_bytes, _ = chess_bundle

    async def downloader(url: str) -> bytes:
        return bundle_bytes

    return AppService(
        repo=repo,
        catalog=_StubCatalog([_entry_at(chess_bundle, catalog_version)]),  # type: ignore[arg-type]
        apps_path=tmp_path / "seed",
        downloader=downloader,
    )


@pytest.mark.parametrize(
    ("installed", "catalog", "offered"),
    [
        ("2026.6.4", "2026.10.28", True),
        ("2026.10.28", "2026.10.28.1", True),
        # A string compare offered these; the catalog went back.
        ("2026.10.28", "2026.6.4", False),
        ("2026.10.28.1", "2026.10.28", False),
        ("2026.10.28", "2026.10.28", False),
    ],
)
async def test_list_updates_compares_calver_as_numbers(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
    installed: str,
    catalog: str,
    offered: bool,
) -> None:
    repo = await _installed_at(tmp_path, chess_bundle, installed)
    svc = _service_on(tmp_path, repo, chess_bundle, catalog)
    updates = await svc.list_updates()
    assert bool(updates) is offered
    if offered:
        assert updates[0]["latest_version"] == catalog


@pytest.mark.parametrize("catalog", ["2026.6.4", "2026.10.28"])
async def test_update_app_never_downgrades_to_an_older_catalog(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
    catalog: str,
) -> None:
    repo = await _installed_at(tmp_path, chess_bundle, "2026.10.28.1")
    svc = _service_on(tmp_path, repo, chess_bundle, catalog)
    result = await svc.update_app("chess", actor_is_admin=True)
    assert result.version == "2026.10.28.1"
    assert (await repo.get("chess")).version == "2026.10.28.1"  # type: ignore[union-attr]


async def test_catalog_status_comes_from_the_catalog(
    tmp_path: Path, catalog_entry: AppCatalogEntry, chess_bundle: tuple[bytes, str]
) -> None:
    svc, _ = make_service(tmp_path, [catalog_entry], chess_bundle[0])
    stub: _StubCatalog = svc._catalog  # type: ignore[assignment]
    stub.status = AppCatalogStatus(checked_at="2026-10-29T08:00:00+00:00", stale=True)
    assert svc.catalog_status == stub.status


async def test_catalog_status_without_a_catalog_is_never_checked(
    tmp_path: Path,
) -> None:
    async def downloader(url: str) -> bytes:
        return b""

    svc = AppService(
        repo=_FakeAppRepo(), catalog=None, apps_path=tmp_path, downloader=downloader
    )
    assert svc.catalog_status == AppCatalogStatus()


# ─── Catalog v2: compatibility, default age, "Ask an admin" ──────────────────


class _People:
    """The household's people (``AbstractUserRepo`` slice)."""

    def __init__(self, *users: tuple[str, str, bool]) -> None:
        self._users = [
            SimpleNamespace(user_id=uid, display_name=name, is_admin=admin)
            for uid, name, admin in users
        ]

    async def list_active(self) -> list[Any]:
        return list(self._users)

    async def list_by_ids(self, user_ids: set[str]) -> list[Any]:
        return [u for u in self._users if u.user_id in user_ids]


class _GuardedCp(_FakeCpRepo):
    def __init__(self) -> None:
        super().__init__()
        self.guardians: dict[str, list[str]] = {}

    async def list_guardians(self, minor_user_id: str) -> list[str]:
        return self.guardians.get(minor_user_id, [])


def _v2_entry(
    sha: str,
    *,
    app_id: str = "chess",
    min_age: int | None = None,
    min_core_version: str | None = None,
    unknown: tuple[str, ...] = (),
    version: str = "2026.10.29",
) -> AppCatalogEntry:
    return AppCatalogEntry(
        app_id=app_id,
        name=app_id.title(),
        latest_version=version,
        description="d",
        icon_url=None,
        capabilities=("storage",),
        bundle_url=f"https://example.com/{app_id}.tgz",
        bundle_sha256=sha,
        permissions=("storage",),
        unknown_permissions=unknown,
        min_age=min_age,
        min_core_version=min_core_version,
    )


def _v2_service(
    tmp_path: Path,
    entries: list[AppCatalogEntry],
    bundle: bytes = b"",
    *,
    core_version: str = "2026.10.29",
    users: _People | None = None,
    cp_repo: _FakeCpRepo | None = None,
    bus: _RecordingBus | None = None,
    repo: _FakeAppRepo | None = None,
) -> tuple[AppService, _FakeAppRepo]:
    repo = repo or _FakeAppRepo()

    async def downloader(url: str) -> bytes:
        return bundle

    svc = AppService(
        repo=repo,
        catalog=_StubCatalog(entries),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=downloader,
        bus=bus,  # type: ignore[arg-type]
        cp_repo=cp_repo,  # type: ignore[arg-type]
        core_version=core_version,
        users=users,  # type: ignore[arg-type]
    )
    return svc, repo


async def test_install_refuses_an_app_that_needs_a_newer_social_home(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    entry = _v2_entry(sha, min_core_version="2026.11.1")
    svc, repo = _v2_service(tmp_path, [entry], data, core_version="2026.10.29")
    assert svc.is_compatible(entry) is False
    with pytest.raises(AppNeedsNewerCoreError) as exc:
        await svc.install("chess", actor_is_admin=True, actor_user_id="a")
    assert exc.value.params == {"min_core_version": "2026.11.1"}
    assert await repo.get("chess") is None


async def test_install_refuses_an_app_asking_for_an_unknown_permission(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    svc, _ = _v2_service(tmp_path, [_v2_entry(sha, unknown=("camera",))], data)
    with pytest.raises(AppNeedsNewerCoreError):
        await svc.install("chess", actor_is_admin=True, actor_user_id="a")


async def test_a_development_build_installs_anything(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    svc, _ = _v2_service(
        tmp_path,
        [_v2_entry(sha, min_core_version="2099.1.1")],
        data,
        core_version="2026.10.30.dev3+gabc",
    )
    app = await svc.install("chess", actor_is_admin=True, actor_user_id="a")
    assert app.app_id == "chess"


@pytest.mark.parametrize(("catalog_age", "expected"), [(13, 13), (None, 0), (0, 0)])
async def test_install_takes_the_catalog_rating_as_default_min_age(
    tmp_path: Path,
    chess_bundle: tuple[bytes, str],
    catalog_age: int | None,
    expected: int,
) -> None:
    """D5: the catalog's age rating is the install default (the bundle's
    manifest declares 0 here); an admin can change it afterwards."""
    data, sha = chess_bundle
    bus = _RecordingBus()
    svc, _repo = _v2_service(
        tmp_path, [_v2_entry(sha, min_age=catalog_age)], data, bus=bus
    )
    app = await svc.install("chess", actor_is_admin=True, actor_user_id="a")
    assert app.min_age == expected
    installed = [e for e in bus.published if isinstance(e, AppInstalled)]
    assert installed[0].min_age == expected
    raised = await svc.set_min_age("chess", min_age=16, actor_is_admin=True)
    assert raised.min_age == 16


async def test_update_refuses_a_version_that_needs_a_newer_social_home(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    repo = await _installed_at(tmp_path, chess_bundle, "2026.10.28")
    newer = _v2_entry(sha, version="2026.11.2", min_core_version="2026.11.2")
    svc, _ = _v2_service(tmp_path / "seed", [newer], data, repo=repo)
    updates = await svc.list_updates()
    assert updates[0]["compatible"] is False
    with pytest.raises(AppNeedsNewerCoreError):
        await svc.update_app("chess", actor_is_admin=True)
    row = await repo.get("chess")
    assert row is not None and row.version == "2026.10.28"


async def test_update_raises_the_gate_to_the_catalog_rating(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    repo = await _installed_at(tmp_path, chess_bundle, "2026.10.28")
    newer = _v2_entry(sha, version="2026.11.2", min_age=13)
    svc, _ = _v2_service(tmp_path / "seed", [newer], data, repo=repo)
    updated = await svc.update_app("chess", actor_is_admin=True)
    assert updated.min_age == 13


async def test_browse_catalog_for_hides_apps_rated_above_a_protected_age(
    tmp_path: Path,
) -> None:
    """D5: a protected account doesn't learn of an app rated above its
    age; an adult member and an admin see the whole catalog."""
    cp = _GuardedCp()
    cp.add("kid", enabled=True, declared_age=10)
    entries = [
        _v2_entry("a" * 64, app_id="chess", min_age=0),
        _v2_entry("b" * 64, app_id="sketch", min_age=13),
        _v2_entry("c" * 64, app_id="quiz"),
    ]
    svc, _ = _v2_service(tmp_path, entries, cp_repo=cp)
    kid = await svc.browse_catalog_for(user_id="kid", is_admin=False)
    assert [e.app_id for e in kid] == ["chess", "quiz"]
    adult = await svc.browse_catalog_for(user_id="ben", is_admin=False)
    assert len(adult) == 3
    admin = await svc.browse_catalog_for(user_id="anna", is_admin=True)
    assert len(admin) == 3


async def test_a_member_cannot_force_a_catalog_refetch(tmp_path: Path) -> None:
    calls: list[bool] = []

    class _Catalog(_StubCatalog):
        async def fetch_catalog(self, *, force: bool = False) -> list[AppCatalogEntry]:
            calls.append(force)
            return []

    svc = AppService(
        repo=_FakeAppRepo(),
        catalog=_Catalog([]),  # type: ignore[arg-type]
        apps_path=tmp_path,
        downloader=lambda url: b"",
    )
    await svc.browse_catalog_for(user_id="ben", is_admin=False, force=True)
    await svc.browse_catalog_for(user_id="anna", is_admin=True, force=True)
    assert calls == [False, True]


async def test_user_names_for_added_by(tmp_path: Path) -> None:
    people = _People(("anna", "Anna", True), ("ben", "Ben", False))
    svc, _ = _v2_service(tmp_path, [], users=people)
    assert await svc.user_names({"anna", "gone"}) == {"anna": "Anna"}
    assert await svc.user_names(set()) == {}
    bare, _ = _v2_service(tmp_path, [])
    assert await bare.user_names({"anna"}) == {}


async def test_ask_an_admin_goes_to_every_admin(tmp_path: Path) -> None:
    bus = _RecordingBus()
    people = _People(
        ("anna", "Anna", True), ("ben", "Ben", True), ("milo", "Milo", False)
    )
    svc, _ = _v2_service(
        tmp_path, [_v2_entry("a" * 64)], users=people, bus=bus, cp_repo=_GuardedCp()
    )
    await svc.request_app("chess", user_id="milo", is_admin=False)
    (event,) = bus.published
    assert isinstance(event, AppRequested)
    assert (event.app_id, event.name, event.requested_by) == ("chess", "Chess", "milo")
    assert event.recipients == ("anna", "ben")


async def test_a_protected_account_asks_its_guardians(tmp_path: Path) -> None:
    bus = _RecordingBus()
    cp = _GuardedCp()
    cp.add("kiki", enabled=True, declared_age=9)
    cp.guardians["kiki"] = ["mum"]
    people = _People(("anna", "Anna", True), ("mum", "Mum", False))
    svc, _ = _v2_service(
        tmp_path, [_v2_entry("a" * 64)], users=people, bus=bus, cp_repo=cp
    )
    await svc.request_app("chess", user_id="kiki", is_admin=False)
    assert bus.published[0].recipients == ("mum",)
    # No guardian on record: the admins decide.
    cp.guardians["kiki"] = []
    await svc.request_app("chess", user_id="kiki", is_admin=False)
    assert bus.published[1].recipients == ("anna",)


async def test_ask_refuses_what_needs_no_request(
    tmp_path: Path, chess_bundle: tuple[bytes, str]
) -> None:
    data, sha = chess_bundle
    cp = _GuardedCp()
    cp.add("kiki", enabled=True, declared_age=9)
    entries = [_v2_entry(sha), _v2_entry("b" * 64, app_id="sketch", min_age=13)]
    svc, _ = _v2_service(tmp_path, entries, data, cp_repo=cp, users=_People())
    with pytest.raises(AppRequestNotAllowedError):
        await svc.request_app("chess", user_id="anna", is_admin=True)
    with pytest.raises(AppNotFoundError):
        await svc.request_app("nope", user_id="ben", is_admin=False)
    # Rated above the asker's age: as if it didn't exist.
    with pytest.raises(AppNotFoundError):
        await svc.request_app("sketch", user_id="kiki", is_admin=False)
    await svc.install("chess", actor_is_admin=True, actor_user_id="anna")
    with pytest.raises(AppRequestNotAllowedError):
        await svc.request_app("chess", user_id="ben", is_admin=False)


async def test_not_now_is_for_admins(tmp_path: Path) -> None:
    bus = _RecordingBus()
    svc, _ = _v2_service(tmp_path, [], bus=bus)
    with pytest.raises(SpacePermissionError):
        await svc.dismiss_requests("chess", actor_is_admin=False, actor_user_id="ben")
    await svc.dismiss_requests("chess", actor_is_admin=True, actor_user_id="anna")
    assert bus.published == [
        AppRequestDismissed(
            app_id="chess",
            dismissed_by="anna",
            occurred_at=bus.published[0].occurred_at,
        )
    ]


async def test_catalog_source_host(tmp_path: Path) -> None:
    svc, _ = _v2_service(tmp_path, [])
    svc._catalog.source_host = "apps.example.org"  # type: ignore[union-attr]
    assert svc.catalog_source_host == "apps.example.org"
    bare = AppService(
        repo=_FakeAppRepo(), catalog=None, apps_path=tmp_path, downloader=lambda u: b""
    )
    assert bare.catalog_source_host is None
