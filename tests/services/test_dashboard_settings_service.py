"""Tests for DashboardSettingsService (in-memory repo, no disk)."""

from __future__ import annotations

import hashlib
import json

import pytest

from socialhome.domain.household_dashboard import (
    DashboardPasswordRequiredError,
    DashboardPasswordTooShortError,
    DashboardPersonSettings,
    DashboardPortFixedError,
    DashboardSettings,
    DashboardSettingsInvalidError,
)
from socialhome.repositories.instance_config_repo import AbstractInstanceConfigRepo
from socialhome.services.dashboard_settings_service import DashboardSettingsService

MAIN_PORT = 8080


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.rows.get(key)

    async def get_many(self, prefix: str) -> dict[str, str]:
        return {k: v for k, v in self.rows.items() if k.startswith(prefix)}

    async def set(self, key: str, value: str) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> None:
        self.rows.pop(key, None)


@pytest.fixture
def repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def svc(repo) -> DashboardSettingsService:
    return DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)


@pytest.fixture
def fixed_svc(repo) -> DashboardSettingsService:
    return DashboardSettingsService(repo, fixed_port=8100, main_port=MAIN_PORT)


async def _with_password(svc: DashboardSettingsService) -> None:
    await svc.set_password(actor_is_admin=True, password="correct horse")


def test_fake_repo_satisfies_protocol(repo):
    assert isinstance(repo, AbstractInstanceConfigRepo)


# ── get ──────────────────────────────────────────────────────────────────


async def test_get_defaults_when_nothing_stored(svc):
    assert await svc.get() == DashboardSettings()


async def test_get_corrupt_json_falls_back_to_defaults(svc, repo, caplog):
    repo.rows["dashboard.settings"] = "{not json"
    assert await svc.get() == DashboardSettings()
    assert "dashboard settings" in caplog.text.lower()


async def test_get_non_object_json_falls_back_to_defaults(svc, repo):
    repo.rows["dashboard.settings"] = "[1, 2]"
    assert await svc.get() == DashboardSettings()


async def test_get_ignores_invalid_stored_fields(svc, repo):
    repo.rows["dashboard.settings"] = json.dumps(
        {
            "locale": "xx",
            "port": "nope",
            "idle_return_minutes": 10,
            "bogus": 1,
            "people": {"u1": {"show": "maybe"}, "u2": {"show": False}},
        }
    )
    s = await svc.get()
    assert s.locale == "en"
    assert s.port == 8100
    assert s.idle_return_minutes == 10
    assert s.people == (("u2", DashboardPersonSettings(show=False)),)


async def test_get_ignores_non_object_people(svc, repo):
    repo.rows["dashboard.settings"] = json.dumps({"people": [1]})
    assert (await svc.get()).people == ()


async def test_get_includes_password_set_at(svc):
    await _with_password(svc)
    s = await svc.get()
    assert s.password_set_at is not None
    assert s.password_set_at.endswith("+00:00")


async def test_get_fixed_port_always_wins(fixed_svc, repo):
    repo.rows["dashboard.settings"] = json.dumps({"port": 9000})
    assert (await fixed_svc.get()).port == 8100


def test_port_fixed_property(svc, fixed_svc):
    assert svc.port_fixed is False
    assert fixed_svc.port_fixed is True


# ── update ───────────────────────────────────────────────────────────────


async def test_update_requires_admin(svc):
    with pytest.raises(PermissionError):
        await svc.update(actor_is_admin=False, patch={"locale": "de"})


async def test_update_partial_round_trip(svc):
    s = await svc.update(
        actor_is_admin=True,
        patch={
            "port": 9000,
            "locale": "de",
            "idle_return_minutes": 0,
            "night_start": "23:30",
            "night_end": "00:00",
            "keep_screen_on": False,
            "show_status_text": True,
        },
    )
    assert s.port == 9000
    assert s.locale == "de"
    assert s.idle_return_minutes == 0
    assert s.night_start == "23:30"
    assert s.night_end == "00:00"
    assert s.keep_screen_on is False
    assert s.show_status_text is True
    # Partial: an untouched field keeps its value.
    s2 = await svc.update(actor_is_admin=True, patch={"locale": "fr"})
    assert s2.port == 9000
    assert s2.locale == "fr"
    assert await svc.get() == s2


async def test_update_does_not_store_secrets_in_settings(svc, repo):
    await _with_password(svc)
    await svc.update(actor_is_admin=True, patch={"locale": "nl"})
    stored = json.loads(repo.rows["dashboard.settings"])
    assert "password_set_at" not in stored
    assert "password_hash" not in stored


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", 1),
        ("enabled", "true"),
        ("keep_screen_on", None),
        ("show_status_text", 0),
        ("port", "9000"),
        ("port", True),
        ("port", 1023),
        ("port", 65536),
        ("port", 9000.0),
        ("port", MAIN_PORT),
        ("locale", "it"),
        ("locale", 5),
        ("locale", ["en"]),
        ("locale", {"en": 1}),
        ("idle_return_minutes", 5),
        ("idle_return_minutes", True),
        ("idle_return_minutes", "3"),
        ("night_start", "24:00"),
        ("night_start", "7:00"),
        ("night_end", "06:60"),
        ("night_end", 600),
        ("night_end", "06:00 "),
    ],
)
async def test_update_rejects_invalid_values(svc, field, value):
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, patch={field: value})
    assert exc.value.params == {"field": field}


async def test_update_rejects_unknown_key_without_echoing_it(svc):
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, patch={"<script>": 1})
    assert "<script>" not in exc.value.detail
    assert exc.value.params == {"field": "unknown"}


async def test_update_rejects_secret_keys(svc):
    for key in ("password_set_at", "password_hash"):
        with pytest.raises(DashboardSettingsInvalidError):
            await svc.update(actor_is_admin=True, patch={key: "x"})


async def test_update_rejects_non_mapping_patch(svc):
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.update(actor_is_admin=True, patch=["locale"])  # type: ignore[arg-type]


async def test_update_invalid_patch_writes_nothing(svc, repo):
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.update(actor_is_admin=True, patch={"locale": "de", "port": 1})
    assert "dashboard.settings" not in repo.rows


async def test_enable_without_password_refused(svc):
    with pytest.raises(DashboardPasswordRequiredError):
        await svc.update(actor_is_admin=True, patch={"enabled": True})


async def test_enable_with_password(svc):
    await _with_password(svc)
    s = await svc.update(actor_is_admin=True, patch={"enabled": True})
    assert s.enabled is True


async def test_disable_without_password_ok(svc):
    s = await svc.update(actor_is_admin=True, patch={"enabled": False})
    assert s.enabled is False


async def test_fixed_port_change_refused(fixed_svc):
    with pytest.raises(DashboardPortFixedError):
        await fixed_svc.update(actor_is_admin=True, patch={"port": 9000})


async def test_fixed_port_same_value_accepted(fixed_svc):
    s = await fixed_svc.update(
        actor_is_admin=True, patch={"port": 8100, "locale": "es"}
    )
    assert s.port == 8100
    assert s.locale == "es"


async def test_fixed_port_bool_refused_as_invalid(fixed_svc):
    with pytest.raises(DashboardSettingsInvalidError):
        await fixed_svc.update(actor_is_admin=True, patch={"port": True})


# ── people ───────────────────────────────────────────────────────────────


async def test_people_merge_per_user(svc):
    await svc.update(
        actor_is_admin=True,
        patch={"people": {"u1": {"show": False}, "u2": {"picture": True}}},
    )
    s = await svc.update(
        actor_is_admin=True, patch={"people": {"u1": {"picture": False}}}
    )
    assert s.person("u1") == DashboardPersonSettings(show=False, picture=False)
    assert s.person("u2") == DashboardPersonSettings(show=True, picture=True)
    assert s.person("u3") == DashboardPersonSettings()


async def test_people_picture_reset_to_default(svc):
    await svc.update(actor_is_admin=True, patch={"people": {"u1": {"picture": True}}})
    s = await svc.update(
        actor_is_admin=True, patch={"people": {"u1": {"picture": None}}}
    )
    assert s.person("u1").picture is None


@pytest.mark.parametrize(
    "people",
    [
        [],
        "u1",
        {"": {"show": True}},
        {"u1": "show"},
        {"u1": {"show": 1}},
        {"u1": {"show": None}},
        {"u1": {"picture": "yes"}},
        {"u1": {"colour": "red"}},
    ],
)
async def test_people_invalid(svc, people):
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, patch={"people": people})
    assert exc.value.params == {"field": "people"}


# ── password ─────────────────────────────────────────────────────────────


async def test_set_password_requires_admin(svc):
    with pytest.raises(PermissionError):
        await svc.set_password(actor_is_admin=False, password="long enough")


async def test_set_password_too_short(svc):
    with pytest.raises(DashboardPasswordTooShortError):
        await svc.set_password(actor_is_admin=True, password="short")


async def test_set_password_non_string(svc):
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.set_password(actor_is_admin=True, password=12345678)  # type: ignore[arg-type]


async def test_set_password_stores_hash_not_plaintext(svc, repo):
    await _with_password(svc)
    stored = repo.rows["dashboard.password_hash"]
    assert stored.startswith("scrypt$")
    assert "correct horse" not in stored


async def test_verify_password(svc):
    assert await svc.verify_password("correct horse") is False
    await _with_password(svc)
    assert await svc.verify_password("correct horse") is True
    assert await svc.verify_password("wrong horse") is False
    assert await svc.verify_password(None) is False  # type: ignore[arg-type]


async def test_set_password_bumps_epoch(svc):
    assert await svc.epoch() == 0
    await _with_password(svc)
    assert await svc.epoch() == 1
    await _with_password(svc)
    assert await svc.epoch() == 2


# ── epoch / sessions ─────────────────────────────────────────────────────


async def test_revoke_sessions_requires_admin(svc):
    with pytest.raises(PermissionError):
        await svc.revoke_sessions(actor_is_admin=False)


async def test_revoke_sessions_bumps_epoch(svc):
    await svc.revoke_sessions(actor_is_admin=True)
    await svc.revoke_sessions(actor_is_admin=True)
    assert await svc.epoch() == 2


async def test_corrupt_epoch_reads_zero(svc, repo):
    repo.rows["dashboard.epoch"] = "x"
    assert await svc.epoch() == 0
    await svc.revoke_sessions(actor_is_admin=True)
    assert await svc.epoch() == 1


# ── cookie secret ────────────────────────────────────────────────────────


async def test_cookie_secret_created_once(svc, repo):
    first = await svc.cookie_secret()
    assert len(first) == 32
    assert repo.rows["dashboard.cookie_secret"] == first.hex()
    assert await svc.cookie_secret() == first


async def test_cookie_secret_survives_new_service(repo):
    a = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    b = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert await a.cookie_secret() == await b.cookie_secret()


@pytest.mark.parametrize("stored", ["zz", "ab" * 8])
async def test_cookie_secret_corrupt_is_replaced(svc, repo, stored):
    repo.rows["dashboard.cookie_secret"] = stored
    secret = await svc.cookie_secret()
    assert len(secret) == 32
    assert repo.rows["dashboard.cookie_secret"] == secret.hex()


# ── setup token ──────────────────────────────────────────────────────────


async def test_issue_setup_token_requires_admin(svc):
    await _with_password(svc)
    with pytest.raises(PermissionError):
        await svc.issue_setup_token(actor_is_admin=False)


async def test_issue_setup_token_requires_password(svc):
    with pytest.raises(DashboardPasswordRequiredError):
        await svc.issue_setup_token(actor_is_admin=True)


async def test_setup_token_stored_hashed(svc, repo):
    await _with_password(svc)
    raw = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    stored = json.loads(repo.rows["dashboard.setup_token"])
    assert stored == {
        "hash": hashlib.sha256(raw.encode()).hexdigest(),
        "expires_at": 1600.0,
    }
    assert raw not in repo.rows["dashboard.setup_token"]


async def test_setup_token_single_use(svc):
    await _with_password(svc)
    raw = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert await svc.consume_setup_token(raw, now=1001.0) is True
    assert await svc.consume_setup_token(raw, now=1002.0) is False


async def test_setup_token_default_clock(svc):
    await _with_password(svc)
    raw = await svc.issue_setup_token(actor_is_admin=True)
    assert await svc.consume_setup_token(raw) is True


async def test_setup_token_wrong_value_keeps_token(svc):
    await _with_password(svc)
    raw = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert await svc.consume_setup_token("nope", now=1001.0) is False
    assert await svc.consume_setup_token(raw, now=1001.0) is True


async def test_setup_token_non_string(svc):
    await _with_password(svc)
    await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert await svc.consume_setup_token(None, now=1001.0) is False  # type: ignore[arg-type]


async def test_setup_token_absent(svc):
    assert await svc.consume_setup_token("anything", now=1.0) is False


async def test_setup_token_expired_is_deleted(svc, repo):
    await _with_password(svc)
    raw = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert await svc.consume_setup_token(raw, now=1600.0) is False
    assert "dashboard.setup_token" not in repo.rows


async def test_setup_token_replaced_by_new_issue(svc):
    await _with_password(svc)
    old = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    new = await svc.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert old != new
    assert await svc.consume_setup_token(old, now=1001.0) is False
    assert await svc.consume_setup_token(new, now=1001.0) is True


@pytest.mark.parametrize(
    "stored",
    [
        "{bad",
        "[]",
        json.dumps({"hash": 1, "expires_at": 9e9}),
        json.dumps({"hash": "a"}),
    ],
)
async def test_setup_token_corrupt_is_deleted(svc, repo, stored):
    repo.rows["dashboard.setup_token"] = stored
    assert await svc.consume_setup_token("x", now=1.0) is False
    assert "dashboard.setup_token" not in repo.rows


async def test_setup_token_non_ascii_stored_hash_is_a_mismatch(svc, repo):
    repo.rows["dashboard.setup_token"] = json.dumps(
        {"hash": "é" * 64, "expires_at": 9e9}
    )
    assert await svc.consume_setup_token("x", now=1.0) is False
