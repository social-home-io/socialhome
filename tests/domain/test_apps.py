"""Tests for app-registry domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.apps import (
    APP_PERMISSIONS,
    CATALOG_CHANGELOG_MAX,
    CATALOG_CHANGELOG_NOTE_MAX,
    CATALOG_DESCRIPTION_MAX,
    CATALOG_LOCALES_MAX,
    CATALOG_NAME_MAX,
    CATALOG_PUBLISHER_MAX,
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    PERMISSION_STORAGE,
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppCatalogEntry,
    AppCatalogStatus,
    AppCatalogText,
    AppCatalogUnreachableError,
    AppChangelogEntry,
    AppContactNotFoundError,
    AppError,
    AppIntegrityError,
    AppKvEntry,
    AppManifest,
    AppNeedsNewerCoreError,
    AppNotEnabledError,
    AppNotFoundError,
    AppPermissionDeniedError,
    AppQuotaExceededError,
    AppRequestNotAllowedError,
    InstalledApp,
    app_age_allows,
    app_permissions,
    core_version_satisfies,
    is_newer_version,
    parse_app_version,
    unknown_capabilities,
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


@pytest.mark.parametrize(
    ("min_age", "protected", "declared_age", "allowed"),
    [
        (0, True, 5, True),  # no gate
        (13, False, None, True),  # unprotected account
        (13, True, 13, True),  # at the gate
        (13, True, 16, True),
        (13, True, 12, False),
        (13, True, None, False),  # protected, no age: fails closed
    ],
)
def test_app_age_allows(min_age, protected, declared_age, allowed):
    assert (
        app_age_allows(min_age, protected=protected, declared_age=declared_age)
        is allowed
    )


# ─── Catalog schema v2 (receiver bounds, r2-proposal §7) ─────────────────────

_V1 = {
    "app_id": "chess",
    "name": "Chess",
    "latest_version": "2026.10.29",
    "description": "Play chess",
    "capabilities": ["storage", "federation"],
    "bundle_url": "https://example/chess.tgz",
    "bundle_sha256": "cd" * 32,
}


def _v2(**over: object) -> AppCatalogEntry:
    return AppCatalogEntry.from_dict({**_V1, "schema": 2, **over})


def test_catalog_v2_reads_the_published_chess_entry():
    """The fields socialhome-apps 2026.10.29.1 publishes, as published."""
    entry = _v2(
        bundle_size=16198,
        permissions=["storage", "people", "federation"],
        min_age=0,
        languages=["en"],
        publisher="Social Home",
        i18n={"de": {"name": "Schach", "description": "Spiel eine Partie Schach."}},
        homepage="https://github.com/social-home-io/socialhome-apps/tree/main/apps/chess",
        min_core_version="2026.10.29",
        changelog=[
            {
                "version": "2026.10.29",
                "date": "2026-10-29",
                "notes": {
                    "en": "Play with the keyboard.",
                    "de": "Spiel mit der Tastatur.",
                },
            }
        ],
    )
    assert entry.bundle_size == 16198
    assert entry.permissions == ("storage", "people", "federation")
    assert entry.unknown_permissions == ()
    assert entry.min_age == 0
    assert entry.languages == ("en",)
    assert entry.publisher == "Social Home"
    assert entry.i18n == (AppCatalogText("de", "Schach", "Spiel eine Partie Schach."),)
    assert entry.homepage is not None and entry.homepage.endswith("/apps/chess")
    assert entry.min_core_version == "2026.10.29"
    assert entry.changelog == (
        AppChangelogEntry(
            version="2026.10.29",
            date="2026-10-29",
            notes=(
                ("en", "Play with the keyboard."),
                ("de", "Spiel mit der Tastatur."),
            ),
        ),
    )


def test_catalog_v1_entry_still_reads_and_implies_people_from_federation():
    entry = AppCatalogEntry.from_dict(dict(_V1))
    assert entry.permissions == ("storage", "people", "federation")
    assert entry.i18n == ()
    assert entry.bundle_size is None
    assert entry.min_age is None
    assert entry.min_core_version is None
    assert entry.changelog == ()


def test_catalog_v2_caps_text_at_the_receiver_bounds():
    entry = _v2(
        name="N" * 100,
        description="D" * 1000,
        publisher="P" * 200,
        i18n={"de": {"name": "S" * 100, "description": "B" * 1000}},
        changelog=[{"version": "1.0", "notes": {"en": "x" * 2000}}],
    )
    assert len(entry.name) == CATALOG_NAME_MAX
    assert entry.name.endswith("…")
    assert len(entry.description) == CATALOG_DESCRIPTION_MAX
    assert entry.publisher is not None
    assert len(entry.publisher) == CATALOG_PUBLISHER_MAX
    assert len(entry.i18n[0].name or "") == CATALOG_NAME_MAX
    assert len(entry.i18n[0].description or "") == CATALOG_DESCRIPTION_MAX
    assert len(entry.changelog[0].notes[0][1]) == CATALOG_CHANGELOG_NOTE_MAX


def test_catalog_v2_bounds_counts():
    tags = [f"l{chr(97 + i)}" for i in range(20)]
    entry = _v2(
        i18n={tag: {"name": "x"} for tag in tags},
        languages=[*tags, "la"],
        changelog=[{"version": f"1.{i}", "notes": {}} for i in range(9)],
    )
    assert len(entry.i18n) == CATALOG_LOCALES_MAX
    assert len(entry.languages) == CATALOG_LOCALES_MAX
    assert len(entry.changelog) == CATALOG_CHANGELOG_MAX


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("bundle_size", -1, None),
        ("bundle_size", "212 KB", None),
        ("bundle_size", True, None),
        ("bundle_size", 10**12, None),
        ("min_age", 12, None),
        ("min_age", "13", None),
        ("min_age", True, None),
        ("min_age", 16, 16),
        ("languages", "en", ()),
        ("languages", ["en", "en", "not a tag", 3, "pt-BR"], ("en", "pt-BR")),
        ("homepage", "http://example.org", None),
        ("homepage", "https://x.org/a b", None),
        ("homepage", "https://x.org/" + "a" * 400, None),
        ("min_core_version", "next", None),
        ("min_core_version", 2026, None),
        ("publisher", {"name": "Social Home"}, None),
        ("publisher", "   ", None),
        ("i18n", ["de"], ()),
        ("i18n", {"DE!": {"name": "x"}, "de": "Schach", "nl": {}}, ()),
        ("changelog", {"version": "1"}, ()),
        ("changelog", [{"version": "v2"}, "x"], ()),
        ("icon_url", "data:image/svg+xml," + "a" * 20000, None),
        ("icon_url", 42, None),
    ],
)
def test_catalog_v2_ignores_bad_values(field, value, expected):
    """A bad v2 value reads as unknown — the entry itself stays."""
    entry = _v2(**{field: value})
    assert getattr(entry, field) == expected
    assert entry.app_id == "chess"


def test_catalog_v2_changelog_drops_a_bad_date_and_note_but_keeps_the_entry():
    entry = _v2(
        changelog=[
            {
                "version": "2026.10.29",
                "date": "yesterday",
                "notes": {"en": "", "EN": "x", "de": "Neu"},
            }
        ]
    )
    assert entry.changelog == (AppChangelogEntry("2026.10.29", None, (("de", "Neu"),)),)


def test_catalog_v2_unknown_permission_is_kept_apart():
    """An unknown permission is never silently granted: the entry says
    which ones this Social Home doesn't know."""
    entry = _v2(permissions=["storage", "camera"])
    assert entry.permissions == ("storage",)
    assert entry.unknown_permissions == ("camera",)


def test_catalog_v2_bad_permissions_list_falls_back_to_capabilities():
    assert _v2(permissions="storage").permissions == (
        "storage",
        "people",
        "federation",
    )


def test_app_permissions_order_and_people_implied():
    assert app_permissions(["federation"]) == ("people", "federation")
    assert app_permissions(("storage", "nope")) == ("storage",)
    assert app_permissions([]) == ()


@pytest.mark.parametrize(
    ("need", "have", "ok"),
    [
        (None, "2026.10.27", True),
        ("2026.10.29", "2026.10.29", True),
        ("2026.10.29", "2026.11.1", True),
        ("2026.10.29", "2026.10.27", False),
        ("2026.10.29", "2026.10.28.3", False),
        # Development builds run the newest code: never held back.
        ("2026.10.29", "2026.10.12.dev2", True),
        ("2026.10.29", "0.0.0+unknown", True),
        ("2026.10.29", "0.0.0", True),
        ("2026.10.29", "", True),
    ],
)
def test_core_version_satisfies(need, have, ok):
    assert core_version_satisfies(need, have) is ok


def test_catalog_entry_must_be_an_object():
    with pytest.raises(TypeError):
        AppCatalogEntry.from_dict(["chess"])  # type: ignore[arg-type]


def test_needs_newer_core_and_request_errors_are_coded():
    assert (AppNeedsNewerCoreError.status, AppNeedsNewerCoreError.code) == (
        409,
        "APP_NEEDS_NEWER_SOCIAL_HOME",
    )
    assert AppRequestNotAllowedError.code == "APP_REQUEST_NOT_NEEDED"


# ── Permissions ───────────────────────────────────────────────────────────


def _app_with_caps(*caps: str) -> InstalledApp:
    return InstalledApp(
        app_id="chess",
        name="Chess",
        version="2026.10.29",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=caps),
        bundle_path="chess/2026.10.29",
        bundle_sha256="ab" * 32,
        source_url="https://example/chess.tgz",
        installed_by="u1",
        installed_at="2026-10-29T00:00:00+00:00",
    )


def test_permission_names_are_the_three_plain_words():
    assert APP_PERMISSIONS == ("storage", "people", "federation")
    assert (PERMISSION_STORAGE, PERMISSION_PEOPLE, PERMISSION_FEDERATION) == (
        APP_PERMISSIONS
    )


@pytest.mark.parametrize(
    ("caps", "perms"),
    [
        ((), ()),
        (("storage",), ("storage",)),
        (("people",), ("people",)),
        (("people", "storage"), ("storage", "people")),
        # Chess: federation implies people (grandfathered installs too).
        (("storage", "federation"), ("storage", "people", "federation")),
        (("federation",), ("people", "federation")),
        # An unknown capability grants nothing.
        (("storage", "camera"), ("storage",)),
    ],
)
def test_an_installed_app_has_its_manifests_permissions(caps, perms):
    """One mapping (``app_permissions``) for the catalog and the install."""
    assert _app_with_caps(*caps).permissions == perms


def test_unknown_capabilities_keeps_order_and_dedupes():
    assert unknown_capabilities(("storage", "camera", "gps", "camera")) == (
        "camera",
        "gps",
    )
    assert unknown_capabilities(("storage", "people", "federation")) == ()


def test_require_permission_allows_granted():
    _app_with_caps("storage", "federation").require_permission("people")


def test_require_permission_refuses_missing_with_code():
    with pytest.raises(AppPermissionDeniedError) as ei:
        _app_with_caps("storage").require_permission("federation")
    assert ei.value.status == 403
    assert ei.value.code == "APP_PERMISSION_DENIED"
    assert ei.value.params == {"permission": "federation"}


def test_needs_newer_social_home_names_the_capabilities():
    err = AppNeedsNewerCoreError(params={"capabilities": "camera, gps"})
    assert err.status == 409
    assert err.code == "APP_NEEDS_NEWER_SOCIAL_HOME"
    assert err.params == {"capabilities": "camera, gps"}
