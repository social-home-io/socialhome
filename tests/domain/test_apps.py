"""Tests for app-registry domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.apps import (
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppCatalogEntry,
    AppContactNotFoundError,
    AppError,
    AppIntegrityError,
    AppKvEntry,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
    InstalledApp,
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
