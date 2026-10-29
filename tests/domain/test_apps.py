"""Tests for app-registry domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.apps import (
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppCatalogEntry,
    AppCatalogStatus,
    AppCatalogUnreachableError,
    AppContactNotFoundError,
    AppError,
    AppIntegrityError,
    AppKvEntry,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    InstalledApp,
    is_newer_version,
    parse_app_version,
)


def test_installed_app_is_frozen():
    app = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess-1.0.0.tgz",
        installed_by="u1",
        installed_at="2026-06-02T00:00:00+00:00",
    )
    assert app.app_id == "chess"
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.app_id = "other"  # type: ignore[misc]


def test_catalog_entry_from_dict_validates_required_fields():
    entry = AppCatalogEntry.from_dict(
        {
            "app_id": "chess",
            "name": "Chess",
            "latest_version": "1.0.0",
            "description": "Play chess",
            "icon_url": None,
            "capabilities": ["storage", "federation"],
            "bundle_url": "https://example/chess-1.0.0.tgz",
            "bundle_sha256": "cd" * 32,
        }
    )
    assert entry.app_id == "chess"
    assert entry.bundle_sha256 == "cd" * 32


def test_catalog_entry_from_dict_missing_fields():
    with pytest.raises(ValueError):
        AppCatalogEntry.from_dict({"app_id": "x"})  # missing fields


def test_app_not_found_is_exception_subclass():
    assert issubclass(AppNotFoundError, Exception)
    assert issubclass(AppNotEnabledError, Exception)
    assert issubclass(AppAlreadyInstalledError, Exception)
    assert issubclass(AppIntegrityError, Exception)


def test_app_contact_not_found_is_app_error_subclass():
    assert issubclass(AppContactNotFoundError, AppError)
    assert issubclass(AppContactNotFoundError, Exception)


def test_app_manifest_from_dict_validates_entry():
    manifest = AppManifest.from_dict(
        {
            "entry": "index.html",
            "icon": "icon.svg",
            "capabilities": ["storage", "federation"],
        }
    )
    assert manifest.entry == "index.html"
    assert manifest.icon == "icon.svg"
    assert manifest.capabilities == ("storage", "federation")


def test_app_manifest_from_dict_rejects_absolute_path():
    with pytest.raises(ValueError, match="relative path"):
        AppManifest.from_dict({"entry": "/index.html"})


def test_app_manifest_from_dict_rejects_parent_traversal():
    with pytest.raises(ValueError, match="relative path"):
        AppManifest.from_dict({"entry": "../index.html"})


def test_app_manifest_from_dict_requires_entry():
    with pytest.raises(ValueError, match="entry must be"):
        AppManifest.from_dict({})


def test_app_kv_entry_is_frozen():
    e = AppKvEntry(
        app_id="chess",
        user_id="u1",
        key="game:1",
        value_json='{"turn":"w"}',
        updated_at="2026-06-02T00:00:00+00:00",
    )
    assert e.key == "game:1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.key = "x"  # type: ignore[misc]


def test_app_quota_exceeded_is_app_error():
    assert issubclass(AppQuotaExceededError, AppError)


def test_manifest_entry_rejects_dotdot_segment():
    for bad in ("a/../b", "../x", "foo/../../etc", "a\\b", "/abs"):
        with pytest.raises(ValueError):
            AppManifest.from_dict({"entry": bad, "capabilities": []})


def test_manifest_entry_accepts_nested_relative():
    m = AppManifest.from_dict({"entry": "sub/index.html", "capabilities": []})
    assert m.entry == "sub/index.html"


# ── min_age tests ─────────────────────────────────────────────────────────────


def test_manifest_min_age_default_zero():
    m = AppManifest.from_dict({"entry": "index.html", "capabilities": []})
    assert m.min_age == 0


def test_manifest_min_age_valid_values():
    for age in (0, 13, 16, 18):
        m = AppManifest.from_dict(
            {"entry": "index.html", "capabilities": [], "min_age": age}
        )
        assert m.min_age == age


def test_manifest_min_age_invalid_value_raises():
    for bad in (1, 12, 15, 17, 21, -1, 99):
        with pytest.raises(ValueError, match="min_age"):
            AppManifest.from_dict(
                {"entry": "index.html", "capabilities": [], "min_age": bad}
            )


def test_manifest_min_age_non_integer_raises():
    with pytest.raises(ValueError, match="min_age"):
        AppManifest.from_dict(
            {"entry": "index.html", "capabilities": [], "min_age": "adult"}
        )


def test_installed_app_min_age_field():
    app = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=("storage",)),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess-1.0.0.tgz",
        installed_by="u1",
        installed_at="2026-06-02T00:00:00+00:00",
        min_age=13,
    )
    assert app.min_age == 13


def test_installed_app_min_age_default_zero():
    app = InstalledApp(
        app_id="chess",
        name="Chess",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path="apps/chess/1.0.0",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess-1.0.0.tgz",
        installed_by="u1",
        installed_at="2026-06-02T00:00:00+00:00",
    )
    assert app.min_age == 0


def test_app_age_restricted_error_subclasses_app_error():
    assert issubclass(AppAgeRestrictedError, AppError)
    err = AppAgeRestrictedError("This app is restricted to ages 13+.")
    assert "13+" in str(err)


# ─── App versions compare as CalVer numbers, never downgrade ─────────────────


@pytest.mark.parametrize(
    ("version", "parsed"),
    [
        ("2026.6.4", (2026, 6, 4)),
        ("2026.10.28", (2026, 10, 28)),
        ("2026.10.28.1", (2026, 10, 28, 1)),
        ("1.0.0", (1, 0, 0)),
        ("", None),
        ("v2026.10.28", None),
        ("2026.10.28-beta", None),
        ("2026..28", None),
        ("2026.10.", None),
        ("٢٠٢٦.1.1", None),  # non-ASCII digits
    ],
)
def test_parse_app_version(version, parsed):
    assert parse_app_version(version) == parsed


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        # A string compare says "2026.6.4" > "2026.10.28": the tuple doesn't.
        ("2026.10.28", "2026.6.4", True),
        ("2026.6.4", "2026.10.28", False),  # a rolled-back catalog
        ("2026.10.28.1", "2026.10.28", True),  # a same-day fix
        ("2026.10.28", "2026.10.28.1", False),
        ("2026.10.28", "2026.10.28", False),
        ("2026.11.1", "2026.10.28.1", True),
        ("garbage", "2026.10.28", False),  # unparseable: fail closed
        ("2026.10.28", "garbage", False),
    ],
)
def test_is_newer_version(candidate, current, newer):
    assert is_newer_version(candidate, current) is newer


def test_catalog_unreachable_is_a_coded_503():
    err = AppCatalogUnreachableError()
    assert isinstance(err, AppError)
    assert (err.status, err.code) == (503, "APP_CATALOG_UNREACHABLE")


def test_catalog_status_defaults_to_never_checked():
    status = AppCatalogStatus()
    assert status.checked_at is None
    assert status.stale is False
