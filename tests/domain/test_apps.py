"""Tests for app-registry domain dataclasses."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from socialhome.domain.apps import (
    APP_PERMISSIONS,
    CATALOG_CHANGELOG_MAX,
    CATALOG_CHANGELOG_NOTE_MAX,
    CATALOG_DESCRIPTION_MAX,
    CATALOG_LOCALES_MAX,
    CATALOG_NAME_MAX,
    CATALOG_PUBLISHER_MAX,
    INVITES_OPEN_MAX,
    INVITES_PER_DAY_MAX,
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    PERMISSION_STORAGE,
    SESSION_MARKS,
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
    AppInviteLimitError,
    AppKvEntry,
    AppManifest,
    AppNeedsNewerCoreError,
    AppNewPermissionsError,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
    AppPermissionDeniedError,
    AppQuotaExceededError,
    AppRequestNotAllowedError,
    AppSessionUnknownError,
    InstalledApp,
    app_age_allows,
    app_permissions,
    app_view_key,
    changelog_since,
    core_version_satisfies,
    is_newer_version,
    new_permissions,
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


def _entry(**extra) -> AppCatalogEntry:
    return AppCatalogEntry.from_dict(
        {
            "app_id": "quiz",
            "name": "Quiz",
            "latest_version": "1.0.0",
            "description": "A quiz",
            "bundle_url": "https://example/quiz.tgz",
            "bundle_sha256": "cd" * 32,
            **extra,
        }
    )


def test_catalog_entry_players_make_it_a_group_game():
    """``players`` (SDK 1.9 catalogs) → the range, and the app can be
    opened as a game in a space; without it, it can't (unless ``tables``)."""
    e = _entry(players={"min": 3, "max": 30})
    assert e.players == (3, 30)
    assert e.tables is True
    plain = _entry()
    assert plain.players is None
    assert plain.tables is False
    assert _entry(tables=True).tables is True


@pytest.mark.parametrize(
    "raw",
    [
        "2-4",
        {"min": 1, "max": 4},
        {"min": 5, "max": 4},
        {"min": 2, "max": 31},
        {"min": True, "max": 4},
        {"min": 2.0, "max": 4},
        {"max": 4},
    ],
)
def test_a_bad_catalog_players_range_reads_as_unknown(raw):
    e = _entry(players=raw)
    assert e.players is None
    assert e.tables is False


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
        # Development builds run the newest code: never held back by a
        # release that is out (dated today or earlier)…
        ("2026.10.29", "2026.10.12.dev2", True),
        ("2026.10.29", "0.0.0+unknown", True),
        ("2026.10.29", "0.0.0", True),
        ("2026.10.29", "", True),
        ("2026.10.31.2", "0.0.0+unknown", True),
        ("1.2", "0.0.0+unknown", True),
        # …but a requirement dated after today needs a Social Home nobody
        # has: "Needs a newer Social Home" (#1265 showed Install).
        ("2026.11.1", "2026.10.30.dev3", False),
        ("2099.1.1", "0.0.0+unknown", False),
        ("2099.1.1", "", False),
        ("2099", "0.0.0", False),
        # A release still compares as a version.
        ("2099.1.1", "2026.10.29", False),
        ("not-a-version", "0.0.0+unknown", True),
    ],
)
def test_core_version_satisfies(need, have, ok):
    assert core_version_satisfies(need, have, today=date(2026, 10, 31)) is ok


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


# ─── Updates: new permissions and "What's new" (D7) ──────────────────────────


@pytest.mark.parametrize(
    ("installed", "candidate", "grown"),
    [
        (("storage",), ("storage",), ()),
        (("storage", "federation"), ("storage",), ()),  # fewer: no yes needed
        (("storage",), ("storage", "federation"), ("people", "federation")),
        # ``people`` was already implied by ``federation``.
        (("federation",), ("people", "federation"), ()),
        ((), ("storage",), ("storage",)),
        # An unknown id is never a "new permission" here (it reads as
        # "needs a newer Social Home" instead).
        (("storage",), ("storage", "camera"), ()),
    ],
)
def test_new_permissions(installed, candidate, grown):
    assert new_permissions(installed, candidate) == grown


def test_new_permissions_error_is_coded():
    err = AppNewPermissionsError(params={"permissions": "federation"})
    assert (err.status, err.code) == (409, "APP_NEW_PERMISSIONS")
    assert err.params == {"permissions": "federation"}


def _log(*versions: str) -> tuple[AppChangelogEntry, ...]:
    return tuple(AppChangelogEntry(v, None, (("en", v),)) for v in versions)


def test_changelog_since_keeps_only_what_is_new_newest_first():
    log = _log("2026.10.21", "2026.10.28", "2026.10.28.1", "2026.10.2", "2026.11.5")
    got = changelog_since(log, "2026.10.21", "2026.10.28.1")
    # Past the installed one, up to the offered one (not the later 11.5),
    # compared as numbers (10.2 < 10.21).
    assert [c.version for c in got] == ["2026.10.28.1", "2026.10.28"]


def test_changelog_since_nothing_new():
    assert changelog_since(_log("2026.10.21"), "2026.10.21", "2026.10.28") == ()


# ── D3: invites through Social Home (R3 PR 9) ─────────────────────────────


def test_invite_errors_are_coded():
    assert (INVITES_OPEN_MAX, INVITES_PER_DAY_MAX) == (10, 30)
    open_ = AppInviteLimitError(daily=False)
    daily = AppInviteLimitError(daily=True)
    assert (open_.status, open_.code, open_.params) == (
        429,
        "APP_INVITES_OPEN_LIMIT",
        {"limit": 10},
    )
    assert (daily.status, daily.code, daily.params) == (
        429,
        "APP_INVITES_DAILY_LIMIT",
        {"limit": 30},
    )
    unknown = AppSessionUnknownError()
    assert (unknown.status, unknown.code) == (403, "APP_SESSION_UNKNOWN")
    assert isinstance(unknown, AppError)


def test_session_marks_read_only_our_own_flags():
    assert SESSION_MARKS == {"sent", "answered", "withdrawn", "group"}
    base = {
        "app_id": "chess",
        "user_id": "u",
        "session_id": "s",
        "from_instance": "i",
        "from_user": "x",
        "created_at": "2026-10-29T00:00:00+00:00",
    }
    sent = AppPendingSession(
        **base, payload={"sent": True, "answered": True}, state="active"
    )
    assert sent.sent and sent.answered and not sent.withdrawn
    # Only an active row can be one this person sent; truthy isn't True.
    assert not AppPendingSession(**base, payload={"sent": True}).sent
    assert not AppPendingSession(**base, payload={"sent": 1}, state="active").sent
    assert AppPendingSession(**base, payload={"withdrawn": True}).withdrawn
    # A seat at a group table (v_76) is marked by Social Home alone.
    assert AppPendingSession(**base, payload={"group": True}).is_group
    assert not AppPendingSession(**base, payload={"group": 1}).is_group
    assert not AppPendingSession(**base, payload={}).is_group


def test_manifest_without_players_is_one_to_one():
    m = AppManifest.from_dict({"entry": "index.html"})
    assert (m.players_min, m.players_max, m.tables, m.late_join) == (
        2,
        2,
        False,
        False,
    )
    assert "players" not in m.to_dict()
    assert "tables" not in m.to_dict()


def test_manifest_group_fields_round_trip():
    m = AppManifest.from_dict(
        {
            "entry": "index.html",
            "players": {"min": 3, "max": 8},
            "tables": True,
            "late_join": True,
        }
    )
    assert (m.players_min, m.players_max, m.tables, m.late_join) == (3, 8, True, True)
    assert AppManifest.from_dict(m.to_dict()) == m


def test_manifest_players_max_is_cut_to_thirty():
    m = AppManifest.from_dict({"entry": "a.html", "players": {"min": 2, "max": 99}})
    assert m.players_max == 30


@pytest.mark.parametrize(
    "players",
    [
        {"min": 1, "max": 4},
        {"min": 5, "max": 4},
        {"min": "2", "max": 4},
        {"min": True, "max": 4},
        [2, 4],
    ],
)
def test_manifest_players_out_of_shape_is_refused(players):
    with pytest.raises(ValueError):
        AppManifest.from_dict({"entry": "a.html", "players": players})


def test_manifest_tables_must_be_a_boolean():
    with pytest.raises(ValueError):
        AppManifest.from_dict({"entry": "a.html", "tables": "yes"})


def test_catalog_name_in_the_viewers_language():
    """A row names the app as the viewer reads it: their language, else
    its base language (``de-AT`` → ``de``), else the catalog's own name."""
    entry = _v2(
        name="Battleships",
        i18n={"de": {"name": "Schiffe versenken"}, "fr": {"description": "Jeu"}},
    )
    assert entry.name_in("de") == "Schiffe versenken"
    assert entry.name_in("de-AT") == "Schiffe versenken"
    # A locale with only a description, an unknown one, or none at all.
    assert entry.name_in("fr") == "Battleships"
    assert entry.name_in("nl") == "Battleships"
    assert entry.name_in(None) == "Battleships"


def test_app_view_key_names_the_app_a_tab_shows():
    """The SPA's "I have this app open" claim (the ``dm.active`` frame)
    — never a conversation id, which has no ``app:`` prefix."""
    assert app_view_key("chess") == "app:chess"


# ── the bundle's own i18n names ───────────────────────────────────────────────


def test_manifest_keeps_the_bundles_i18n_names():
    m = AppManifest.from_dict(
        {
            "entry": "index.html",
            "i18n": {
                "de": {"name": "Schiffe versenken", "description": "x"},
                "nl": {"description": "only a description"},
                "not a locale!": {"name": "skipped"},
            },
        }
    )
    assert m.names == (("de", "Schiffe versenken"),)
    # What the household keeps reads back the same (``manifest_json``).
    assert AppManifest.from_dict(m.to_dict()) == m
    assert m.to_dict()["i18n"] == {"de": {"name": "Schiffe versenken"}}


def test_manifest_without_i18n_keeps_no_names():
    m = AppManifest.from_dict({"entry": "index.html", "i18n": "nope"})
    assert m.names == ()
    assert "i18n" not in m.to_dict()


def test_installed_app_name_in_the_readers_language():
    app = InstalledApp(
        app_id="battleships",
        name="Battleships",
        version="1",
        enabled=True,
        manifest=AppManifest(
            entry="index.html",
            icon=None,
            capabilities=(),
            names=(("de", "Schiffe versenken"),),
        ),
        bundle_path="battleships/1",
        bundle_sha256="ab" * 32,
        source_url="https://example/b.tgz",
        installed_by=None,
        installed_at="2026-10-30T00:00:00+00:00",
    )
    assert app.name_in("de") == "Schiffe versenken"
    assert app.name_in("de-AT") == "Schiffe versenken"
    assert app.name_in("fr") == "Battleships"
    assert app.name_in(None) == "Battleships"


@pytest.mark.parametrize(
    ("state", "payload", "held", "updated_at", "going"),
    [
        # An invite the person accepted: being played.
        ("active", {}, None, "2026-10-30T00:00:00+00:00", True),
        # One they sent that was answered.
        ("active", {"sent": True, "answered": True}, None, None, True),
        # Still waiting for an answer, either way: not a game yet.
        ("active", {"sent": True}, None, None, False),
        ("invited", {}, None, None, False),
        # Taken back, or closed by the other side.
        ("active", {"withdrawn": True}, None, None, False),
        (
            "active",
            {},
            {"kind": "session", "payload": {"verb": "close"}},
            None,
            False,
        ),
        # A move held for the app still means the game goes on.
        ("active", {}, {"kind": "message", "payload": {}}, None, True),
        # Quiet past the cut: most likely over.
        ("active", {}, None, "2026-09-01T00:00:00+00:00", False),
        # A seat at a group table is counted through its table.
        ("active", {"group": True}, None, None, False),
    ],
)
def test_is_game_going(state, payload, held, updated_at, going):
    row = AppPendingSession(
        app_id="chess",
        user_id="u",
        session_id="s",
        from_instance="i",
        from_user="x",
        payload=payload,
        created_at="2026-10-29T00:00:00+00:00",
        state=state,
        held=held,
        updated_at=updated_at,
    )
    assert row.is_game_going("2026-10-01T00:00:00+00:00") is going
