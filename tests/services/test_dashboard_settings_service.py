"""Tests for DashboardSettingsService (in-memory repo, no disk)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from unittest.mock import patch

import pytest

from socialhome.domain.household_dashboard import (
    MAX_DISPLAYS,
    DashboardDisplay,
    DashboardDisplayLimitError,
    DashboardDisplayNotFoundError,
    DashboardPasswordRequiredError,
    DashboardPasswordTooShortError,
    DashboardPersonSettings,
    DashboardPinInvalidError,
    DashboardPortFixedError,
    DashboardSettings,
    DashboardSettingsInvalidError,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.instance_config_repo import AbstractInstanceConfigRepo
from socialhome.services.dashboard_settings_service import DashboardSettingsService

MAIN_PORT = 8080


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.rows.get(key)

    async def set(self, key: str, value: str) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> None:
        self.rows.pop(key, None)

    async def set_many(self, items) -> None:
        self.rows.update(items)


class _SpyRepo(_FakeRepo):
    """Records every key read, to prove ``get`` never loads secrets."""

    def __init__(self) -> None:
        super().__init__()
        self.read: list[str] = []

    async def get(self, key: str) -> str | None:
        self.read.append(key)
        return await super().get(key)


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
            "show_household_messages": True,
            "mention_sound": False,
        },
    )
    assert s.port == 9000
    assert s.locale == "de"
    assert s.idle_return_minutes == 0
    assert s.night_start == "23:30"
    assert s.night_end == "00:00"
    assert s.keep_screen_on is False
    assert s.show_status_text is True
    assert s.show_household_messages is True
    assert s.mention_sound is False
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
        ("show_household_messages", 1),
        ("show_household_messages", "true"),
        ("show_household_messages", None),
        ("mention_sound", 1),
        ("mention_sound", None),
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


async def test_enable_rejected_when_stored_port_clashes_with_main(repo):
    """The clash is checked on the merged state, not only the patch."""
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=8100)
    await _with_password(svc)
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, patch={"enabled": True})
    assert exc.value.params == {"field": "port"}


async def test_disabled_dashboard_may_keep_clashing_default_port(repo):
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=8100)
    s = await svc.update(actor_is_admin=True, patch={"locale": "de"})
    assert s.locale == "de"


async def test_fixed_port_skips_main_port_clash(repo):
    svc = DashboardSettingsService(repo, fixed_port=8100, main_port=8100)
    await _with_password(svc)
    s = await svc.update(actor_is_admin=True, patch={"enabled": True})
    assert s.enabled is True


async def test_get_reads_no_secrets():
    spy = _SpyRepo()
    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    await svc.get()
    assert sorted(spy.read) == [
        "dashboard.password_set_at",
        "dashboard.pin_set_at",
        "dashboard.settings",
    ]


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


async def test_people_null_deletes_entry(svc):
    await svc.update(
        actor_is_admin=True,
        patch={"people": {"u1": {"show": False}, "u2": {"show": False}}},
    )
    s = await svc.update(actor_is_admin=True, patch={"people": {"u1": None}})
    assert s.people_map() == {"u2": DashboardPersonSettings(show=False)}
    assert s.person("u1") == DashboardPersonSettings()


async def test_people_null_for_absent_user_is_noop(svc):
    s = await svc.update(actor_is_admin=True, patch={"people": {"ghost": None}})
    assert s.people == ()


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


async def test_set_password_writes_atomically(svc, repo):
    """Hash, timestamp and new epoch land in one ``set_many`` batch."""
    batches: list[dict[str, str]] = []
    real = repo.set_many

    async def spy(items):
        batches.append(dict(items))
        await real(items)

    repo.set_many = spy
    await _with_password(svc)
    assert len(batches) == 1
    assert set(batches[0]) == {
        "dashboard.password_hash",
        "dashboard.password_set_at",
        "dashboard.epoch",
        # A new password unpairs every wall panel too.
        "dashboard.displays",
    }
    assert batches[0]["dashboard.epoch"] == "1"
    assert batches[0]["dashboard.displays"] == "[]"


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


class _YieldingRepo(_FakeRepo):
    """Yields to the loop on every call, like a real DB round-trip."""

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await super().get(key)

    async def set(self, key: str, value: str) -> None:
        await asyncio.sleep(0)
        await super().set(key, value)

    async def set_many(self, items) -> None:
        await asyncio.sleep(0)
        await super().set_many(items)


async def test_concurrent_revokes_each_bump():
    svc = DashboardSettingsService(_YieldingRepo(), fixed_port=None, main_port=1)
    await asyncio.gather(*(svc.revoke_sessions(actor_is_admin=True) for _ in range(5)))
    assert await svc.epoch() == 5


async def test_concurrent_updates_do_not_lose_people():
    svc = DashboardSettingsService(_YieldingRepo(), fixed_port=None, main_port=1)
    await asyncio.gather(
        *(
            svc.update(
                actor_is_admin=True, patch={"people": {f"u{i}": {"show": False}}}
            )
            for i in range(5)
        )
    )
    assert len((await svc.get()).people) == 5


async def test_corrupt_epoch_is_reseeded_high(svc, repo):
    """A corrupt epoch must not reset to 0 — old cookies minted at small
    epochs would validate again. A fresh random epoch is persisted."""
    repo.rows["dashboard.epoch"] = "x"
    first = await svc.epoch()
    assert first > 1_000_000
    assert repo.rows["dashboard.epoch"] == str(first)
    assert await svc.epoch() == first
    await svc.revoke_sessions(actor_is_admin=True)
    assert await svc.epoch() == first + 1


async def test_corrupt_epoch_reseed_unpairs_every_display(svc, repo):
    """A reseed is an epoch bump: panel tokens die with it, in the same
    write as the new epoch (a crash can't keep panels paired)."""
    await svc.add_display(_display(1))
    repo.rows["dashboard.epoch"] = "x"
    batches: list[dict[str, str]] = []
    real = repo.set_many

    async def spy(items):
        batches.append(dict(items))
        await real(items)

    repo.set_many = spy
    fresh = await svc.epoch()
    assert batches == [{"dashboard.epoch": str(fresh), "dashboard.displays": "[]"}]
    assert await svc.displays() == ()


async def test_corrupt_epoch_unpairs_displays_on_their_first_read(repo):
    """Panels authenticate without reading the epoch — a cold load of the
    panel list must still notice a corrupt epoch and unpair them."""
    first = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    await first.add_display(_display(1))
    repo.rows["dashboard.epoch"] = "x"
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert await svc.displays() == ()
    assert repo.rows["dashboard.displays"] == "[]"
    assert int(repo.rows["dashboard.epoch"]) > 1_000_000


# ── cookie secret ────────────────────────────────────────────────────────


def _km() -> KeyManager:
    return KeyManager(os.urandom(32))


async def test_cookie_secret_requires_key_manager(svc, repo):
    """Fail closed: never store the cookie key in plaintext."""
    with pytest.raises(RuntimeError):
        await svc.cookie_secret()
    assert "dashboard.cookie_secret" not in repo.rows


async def test_cookie_secret_created_once_and_kek_wrapped(svc, repo):
    km = _km()
    svc.attach_key_manager(km)
    first = await svc.cookie_secret()
    assert len(first) == 32
    stored = repo.rows["dashboard.cookie_secret"]
    assert stored != first.hex()
    assert first.hex() not in stored
    assert km.decrypt(stored, associated_data=b"dashboard.cookie_secret") == first
    assert await svc.cookie_secret() == first


async def test_cookie_secret_survives_new_service(repo):
    km = _km()
    a = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    b = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    a.attach_key_manager(km)
    b.attach_key_manager(km)
    assert await a.cookie_secret() == await b.cookie_secret()


async def test_rotate_cookie_secret_replaces_it_durably(repo):
    """A new KEK-wrapped key, seen by a fresh service too; the epoch and
    the paired panels are untouched."""
    km = _km()
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    svc.attach_key_manager(km)
    old = await svc.cookie_secret()
    await svc.add_display(_display(1))
    await svc.rotate_cookie_secret()
    new = await svc.cookie_secret()
    assert new != old and len(new) == 32
    stored = repo.rows["dashboard.cookie_secret"]
    assert km.decrypt(stored, associated_data=b"dashboard.cookie_secret") == new
    fresh = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    fresh.attach_key_manager(km)
    assert await fresh.cookie_secret() == new
    assert await fresh.epoch() == 0
    assert len(await fresh.displays()) == 1


async def test_rotate_cookie_secret_requires_key_manager(svc, repo):
    with pytest.raises(RuntimeError):
        await svc.rotate_cookie_secret()
    assert "dashboard.cookie_secret" not in repo.rows


async def test_cookie_secret_other_kek_is_replaced(repo):
    a = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    a.attach_key_manager(_km())
    old = await a.cookie_secret()
    b = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    km_b = _km()
    b.attach_key_manager(km_b)
    new = await b.cookie_secret()
    assert new != old
    assert len(new) == 32
    assert (
        km_b.decrypt(
            repo.rows["dashboard.cookie_secret"],
            associated_data=b"dashboard.cookie_secret",
        )
        == new
    )


@pytest.mark.parametrize("stored", ["zz", "ab" * 32])
async def test_cookie_secret_corrupt_is_replaced(svc, repo, stored):
    repo.rows["dashboard.cookie_secret"] = stored
    svc.attach_key_manager(_km())
    secret = await svc.cookie_secret()
    assert len(secret) == 32
    assert repo.rows["dashboard.cookie_secret"] != stored


async def test_cookie_secret_wrong_length_plaintext_is_replaced(svc, repo):
    km = _km()
    repo.rows["dashboard.cookie_secret"] = km.encrypt(
        b"short", associated_data=b"dashboard.cookie_secret"
    )
    svc.attach_key_manager(km)
    assert len(await svc.cookie_secret()) == 32


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


async def test_cookie_secret_cached_after_first_load():
    """Per-request verify must not take the lock + KEK-decrypt every time."""
    spy = _SpyRepo()
    km = _km()
    seed = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    seed.attach_key_manager(km)
    stored_secret = await seed.cookie_secret()

    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    svc.attach_key_manager(km)
    spy.read.clear()
    assert await svc.cookie_secret() == stored_secret  # loaded + unwrapped once
    assert spy.read == ["dashboard.cookie_secret"]
    spy.read.clear()
    for _ in range(3):
        assert await svc.cookie_secret() == stored_secret
    assert spy.read == []


async def test_cookie_secret_cache_holds_a_freshly_created_key():
    spy = _SpyRepo()
    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    svc.attach_key_manager(_km())
    first = await svc.cookie_secret()
    spy.read.clear()
    assert await svc.cookie_secret() == first
    assert spy.read == []


async def test_cookie_secret_cache_dropped_on_new_key_manager(svc, repo):
    svc.attach_key_manager(_km())
    old = await svc.cookie_secret()
    # A different KEK can't unwrap the stored key → it is replaced, not
    # served from the stale cache.
    svc.attach_key_manager(_km())
    assert await svc.cookie_secret() != old


class _YieldingRepo(_FakeRepo):
    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await super().get(key)


async def test_cookie_secret_concurrent_first_use_mints_one_key():
    svc = DashboardSettingsService(
        _YieldingRepo(), fixed_port=None, main_port=MAIN_PORT
    )
    svc.attach_key_manager(_km())
    secrets_ = await asyncio.gather(*(svc.cookie_secret() for _ in range(5)))
    assert len(set(secrets_)) == 1


# ── Admin-set address ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("http://dash.local:8100", "http://dash.local:8100"),
        ("http://dash.local:8100/", "http://dash.local:8100"),
        ("HTTPS://Dash.Example.COM", "https://dash.example.com"),
        ("http://192.168.1.5", "http://192.168.1.5"),
        ("http://[fd00::1]:8100/", "http://[fd00::1]:8100"),
    ],
)
async def test_address_accepted_and_normalised(svc, raw, stored):
    s = await svc.update(actor_is_admin=True, patch={"address": raw})
    assert s.address == stored
    assert (await svc.get()).address == stored


@pytest.mark.parametrize("cleared", [None, ""])
async def test_address_cleared(svc, cleared):
    await svc.update(actor_is_admin=True, patch={"address": "http://dash.local"})
    s = await svc.update(actor_is_admin=True, patch={"address": cleared})
    assert s.address is None


@pytest.mark.parametrize(
    "raw",
    [
        5,
        True,
        ["http://dash.local"],
        "dash.local:8100",
        "ftp://dash.local",
        "javascript:alert(1)",
        "http://",
        "http:///path",
        "http://dash.local/path",
        "http://dash.local/?",
        "http://dash.local?a=1",
        "http://dash.local#top",
        "http://dash.local?",
        "http://user:pw@dash.local",
        "http://user@dash.local",
        "http://dash.local:99999",
        "http://dash.local:0",
        "http://dash.local:abc",
        "http://dash.local:",
        "http://dash local",
        "http://dash.local\\x",
        "http://dash.local\n",
        " http://dash.local",
        "http://[fd00::1",
        "http://[not-ip]",
        "http://-dash.local",
        "http://dash..local",
    ],
)
async def test_address_rejected(svc, raw):
    with pytest.raises(DashboardSettingsInvalidError) as exc:
        await svc.update(actor_is_admin=True, patch={"address": raw})
    assert exc.value.params == {"field": "address"}


def _address_of_length(n: int) -> str:
    # "http://" + three 60-char labels + "." + tail + ".lan"
    tail = "d" * (n - 194)
    return "http://" + ".".join(["a" * 60, "b" * 60, "c" * 60, tail]) + ".lan"


async def test_address_max_length(svc):
    ok = _address_of_length(200)
    assert len(ok) == 200
    s = await svc.update(actor_is_admin=True, patch={"address": ok})
    assert s.address == ok
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.update(
            actor_is_admin=True, patch={"address": _address_of_length(201)}
        )


async def test_address_label_too_long_rejected(svc):
    with pytest.raises(DashboardSettingsInvalidError):
        await svc.update(
            actor_is_admin=True, patch={"address": "http://" + "a" * 64 + ".lan"}
        )


async def test_stored_invalid_address_falls_back(svc, repo):
    repo.rows["dashboard.settings"] = json.dumps({"address": "ftp://x"})
    assert (await svc.get()).address is None


# ── Restore (undo a settings change the listener could not follow) ──────


async def test_restore_requires_admin(svc):
    with pytest.raises(PermissionError):
        await svc.restore(actor_is_admin=False, previous=DashboardSettings())


async def test_restore_puts_back_previous_settings(svc):
    await _with_password(svc)
    await svc.update(
        actor_is_admin=True,
        patch={"locale": "de", "address": "http://dash.local", "port": 9001},
    )
    previous = await svc.snapshot()
    await svc.update(actor_is_admin=True, patch={"enabled": True, "port": 9002})
    await svc.restore(actor_is_admin=True, previous=previous)
    restored = await svc.get()
    assert restored == previous
    assert restored.enabled is False
    assert restored.port == 9001


async def test_snapshot_is_the_stored_value_not_the_fixed_port(fixed_svc, repo):
    repo.rows["dashboard.settings"] = json.dumps({"port": 9000})
    snap = await fixed_svc.snapshot()
    assert snap.port == 9000
    await fixed_svc.update(actor_is_admin=True, patch={"locale": "de"})
    await fixed_svc.restore(actor_is_admin=True, previous=snap)
    # The platform-fixed port never leaks into storage on a restore.
    assert json.loads(repo.rows["dashboard.settings"])["port"] == 9000
    assert json.loads(repo.rows["dashboard.settings"])["locale"] == "en"
    assert (await fixed_svc.get()).port == 8100


# ── Settings cache ──────────────────────────────────────────────────────


async def test_get_is_cached_after_first_load():
    spy = _SpyRepo()
    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    first = await svc.get()
    reads = len(spy.read)
    for _ in range(5):
        assert await svc.get() == first
    assert len(spy.read) == reads


async def test_cache_follows_every_write():
    spy = _SpyRepo()
    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    await svc.get()
    await svc.update(actor_is_admin=True, patch={"locale": "de"})
    assert (await svc.get()).locale == "de"
    snap = await svc.snapshot()
    await _with_password(svc)
    assert (await svc.get()).password_set_at is not None
    await svc.update(actor_is_admin=True, patch={"locale": "fr"})
    await svc.restore(actor_is_admin=True, previous=snap)
    restored = await svc.get()
    assert restored.locale == "de"
    assert restored.password_set_at is not None
    # A fresh service reading the same storage agrees with the cache.
    assert (
        await DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT).get()
        == restored
    )


async def test_cache_never_keeps_a_read_that_raced_a_write():
    repo = _YieldingRepo()
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    reader = asyncio.create_task(svc.get())
    await asyncio.sleep(0)
    await svc.update(actor_is_admin=True, patch={"locale": "nl"})
    await reader
    assert (await svc.get()).locale == "nl"


# ── privacy-lock PIN ─────────────────────────────────────────────────────


async def test_pin_not_set_by_default(svc):
    assert (await svc.get()).pin_set is False
    assert await svc.verify_pin("1234") is False


@pytest.mark.parametrize("pin", ["1234", "12345", "123456"])
async def test_set_pin_accepts_four_to_six_digits(svc, pin):
    await svc.set_pin(actor_is_admin=True, pin=pin)
    assert (await svc.get()).pin_set is True
    assert await svc.verify_pin(pin) is True


@pytest.mark.parametrize(
    "pin",
    [
        "123",
        "1234567",
        "",
        "12a4",
        "abcd",
        "12 34",
        " 1234",
        "1234\n",
        "١٢٣٤",  # Arabic-Indic digits — str.isdigit() would accept them
        "１２３４",  # full-width digits
        "+123",
        "-1234",
        1234,
        None,
    ],
)
async def test_set_pin_rejects_anything_but_ascii_digits(svc, repo, pin):
    with pytest.raises(DashboardPinInvalidError):
        await svc.set_pin(actor_is_admin=True, pin=pin)
    assert "dashboard.pin_hash" not in repo.rows
    assert (await svc.get()).pin_set is False


async def test_set_pin_requires_admin(svc, repo):
    with pytest.raises(PermissionError):
        await svc.set_pin(actor_is_admin=False, pin="1234")
    assert repo.rows == {}


async def test_set_pin_stores_a_scrypt_hash_not_the_pin(svc, repo):
    await svc.set_pin(actor_is_admin=True, pin="482913")
    stored = repo.rows["dashboard.pin_hash"]
    assert stored.startswith("scrypt$")
    assert "482913" not in stored
    assert all("482913" not in v for v in repo.rows.values())


async def test_set_pin_writes_hash_and_marker_atomically(svc, repo):
    batches: list[dict[str, str]] = []
    real = repo.set_many

    async def spy(items):
        batches.append(dict(items))
        await real(items)

    repo.set_many = spy
    await svc.set_pin(actor_is_admin=True, pin="1234")
    assert len(batches) == 1
    assert set(batches[0]) == {"dashboard.pin_hash", "dashboard.pin_set_at"}
    assert batches[0]["dashboard.pin_set_at"].endswith("+00:00")


async def test_set_pin_hashes_off_the_event_loop(svc):
    with patch(
        "socialhome.services.dashboard_settings_service.asyncio.to_thread",
        wraps=asyncio.to_thread,
    ) as to_thread:
        await svc.set_pin(actor_is_admin=True, pin="1234")
        await svc.verify_pin("1234")
    assert to_thread.call_count == 2


async def test_set_pin_does_not_sign_tablets_out(svc):
    before = await svc.epoch()
    await svc.set_pin(actor_is_admin=True, pin="1234")
    assert await svc.epoch() == before


async def test_verify_pin(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    assert await svc.verify_pin("1234") is True
    assert await svc.verify_pin("4321") is False
    assert await svc.verify_pin("") is False
    assert await svc.verify_pin("١٢٣٤") is False
    assert await svc.verify_pin(None) is False  # type: ignore[arg-type]


async def test_malformed_pin_never_reaches_scrypt(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    with patch(
        "socialhome.services.dashboard_settings_service.verify_password"
    ) as verify:
        assert await svc.verify_pin("x" * 10_000) is False
    verify.assert_not_called()


async def test_change_pin(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    await svc.set_pin(actor_is_admin=True, pin="98765")
    assert await svc.verify_pin("1234") is False
    assert await svc.verify_pin("98765") is True


async def test_delete_pin(svc, repo):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    await svc.delete_pin(actor_is_admin=True)
    assert (await svc.get()).pin_set is False
    assert await svc.verify_pin("1234") is False
    assert "dashboard.pin_hash" not in repo.rows
    assert "dashboard.pin_set_at" not in repo.rows


async def test_delete_pin_drops_the_marker_first(svc, repo):
    """A crash between the two deletes leaves "no PIN" (what the admin
    asked for) — never "PIN set" with nothing that can unlock."""
    await svc.set_pin(actor_is_admin=True, pin="1234")
    order: list[str] = []
    real = repo.delete

    async def spy(key):
        order.append(key)
        await real(key)

    repo.delete = spy
    await svc.delete_pin(actor_is_admin=True)
    assert order == ["dashboard.pin_set_at", "dashboard.pin_hash"]


async def test_delete_pin_when_none_is_a_noop(svc):
    await svc.delete_pin(actor_is_admin=True)
    assert (await svc.get()).pin_set is False


async def test_delete_pin_requires_admin(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    with pytest.raises(PermissionError):
        await svc.delete_pin(actor_is_admin=False)
    assert (await svc.get()).pin_set is True


async def test_pin_writes_keep_the_settings_cache_coherent(svc):
    assert (await svc.get()).pin_set is False  # cached
    await svc.set_pin(actor_is_admin=True, pin="1234")
    assert (await svc.get()).pin_set is True
    await svc.delete_pin(actor_is_admin=True)
    assert (await svc.get()).pin_set is False


async def test_pin_set_survives_settings_edits_and_restore(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    snap = await svc.snapshot()
    await svc.update(actor_is_admin=True, patch={"locale": "de"})
    assert (await svc.get()).pin_set is True
    await svc.restore(actor_is_admin=True, previous=snap)
    assert (await svc.get()).pin_set is True


async def test_pin_set_is_never_stored_in_the_settings_json(svc, repo):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    await svc.update(actor_is_admin=True, patch={"locale": "de"})
    assert "pin" not in repo.rows["dashboard.settings"]


# ── wall panels (displays) ───────────────────────────────────────────────


def _display(i: int = 1, **kw) -> DashboardDisplay:
    fields = {
        "id": f"d{i}",
        "name": f"Panel {i}",
        "token_hash": hashlib.sha256(f"tok{i}".encode()).hexdigest(),
        "created_at": "2026-10-13T08:00:00+00:00",
    }
    fields.update(kw)
    return DashboardDisplay(**fields)


async def test_displays_empty_by_default(svc):
    assert await svc.displays() == ()


async def test_add_display_round_trips_through_storage(repo):
    first = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    d = _display(firmware="1.0", model="esp32-s3", locked=True)
    await first.add_display(d)
    stored = json.loads(repo.rows["dashboard.displays"])
    assert stored[0]["token_hash"] == d.token_hash
    # A fresh service (restart) reads the same panel back.
    second = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert await second.displays() == (d,)


async def test_add_display_caps_the_household(svc):
    for i in range(MAX_DISPLAYS):
        await svc.add_display(_display(i))
    with pytest.raises(DashboardDisplayLimitError):
        await svc.add_display(_display(99))
    assert len(await svc.displays()) == MAX_DISPLAYS


async def test_display_mac_round_trips_and_old_records_load(repo):
    first = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    await first.add_display(_display(1, mac="aa:bb:cc:dd:ee:01"))
    stored = json.loads(repo.rows["dashboard.displays"])
    assert stored[0]["mac"] == "aa:bb:cc:dd:ee:01"
    # A record from before ``mac`` existed, and a garbled one, load mac-less.
    del stored[0]["mac"]
    stored.append({**stored[0], "id": "d2", "mac": "not-a-mac"})
    repo.rows["dashboard.displays"] = json.dumps(stored)
    second = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert [(d.id, d.mac) for d in await second.displays()] == [
        ("d1", None),
        ("d2", None),
    ]


async def test_add_display_can_supersede_the_same_mac(svc):
    mac = "aa:bb:cc:dd:ee:01"
    await svc.add_display(_display(1, mac=mac))
    await svc.add_display(_display(2, mac="aa:bb:cc:dd:ee:02"))
    # Code pairing keeps both; a reconnect takes the old records over —
    # they stay until its first use.
    assert await svc.add_display(_display(3, mac=mac)) == ()
    assert await svc.add_display(_display(4, mac=mac), supersede_same_mac=True) == ()
    stored = {d.id: d for d in await svc.displays()}
    assert list(stored) == ["d1", "d2", "d3", "d4"]
    assert stored["d4"].replaces == ("d1", "d3")
    # Another reconnect before d4 was used drops d4 and inherits its takeover.
    dropped = await svc.add_display(_display(5, mac=mac), supersede_same_mac=True)
    assert dropped == ("d4",)
    stored = {d.id: d for d in await svc.displays()}
    assert list(stored) == ["d1", "d2", "d3", "d5"]
    assert stored["d5"].replaces == ("d1", "d3")
    # First use: the old ones go in one write.
    assert await svc.take_over("d5") == ("d1", "d3")
    assert [(d.id, d.replaces) for d in await svc.displays()] == [
        ("d2", ()),
        ("d5", ()),
    ]
    assert await svc.take_over("d5") == ()
    assert await svc.take_over("missing") is None
    # No MAC → nothing to match.
    assert await svc.add_display(_display(6), supersede_same_mac=True) == ()


async def test_replaces_round_trips_and_bad_values_skip_the_entry(repo):
    first = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    mac = "aa:bb:cc:dd:ee:01"
    await first.add_display(_display(1, mac=mac))
    await first.add_display(_display(2, mac=mac), supersede_same_mac=True)
    stored = json.loads(repo.rows["dashboard.displays"])
    assert stored[1]["replaces"] == ["d1"]
    second = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert [d.replaces for d in await second.displays()] == [(), ("d1",)]
    # Records from before ``replaces`` load with none; garbled ones are skipped.
    del stored[0]["replaces"]
    stored.append({**stored[0], "id": "d3", "replaces": "d1"})
    stored.append({**stored[0], "id": "d4", "replaces": [""]})
    repo.rows["dashboard.displays"] = json.dumps(stored)
    third = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert [(d.id, d.replaces) for d in await third.displays()] == [
        ("d1", ()),
        ("d2", ("d1",)),
    ]


async def test_add_display_refuses_a_stale_epoch(svc):
    """A claim minted before a sign-out must not store a display after it —
    checked under the same lock as the epoch bump."""
    epoch = await svc.epoch()
    assert await svc.add_display(_display(1), expected_epoch=epoch) == ()
    await svc.revoke_sessions(actor_is_admin=True)
    assert await svc.add_display(_display(2), expected_epoch=epoch) is None
    assert await svc.displays() == ()


async def test_replacing_makes_room_at_the_limit(svc):
    mac = "aa:bb:cc:dd:ee:01"
    await svc.add_display(_display(0, mac=mac))
    for i in range(1, MAX_DISPLAYS):
        await svc.add_display(_display(i))
    await svc.add_display(_display(99, mac=mac), supersede_same_mac=True)
    # Over by the record it takes over — until its first use.
    assert len(await svc.displays()) == MAX_DISPLAYS + 1
    await svc.take_over("d99")
    assert len(await svc.displays()) == MAX_DISPLAYS
    # Without anything to take over, the cap holds.
    with pytest.raises(DashboardDisplayLimitError):
        await svc.add_display(
            _display(98, mac="aa:bb:cc:dd:ee:02"), supersede_same_mac=True
        )


async def test_record_display_seen_keeps_a_reported_mac(svc):
    await svc.add_display(_display(1))
    await svc.record_display_seen(
        "d1", at="x", firmware=None, model=None, mac="aa:bb:cc:dd:ee:01"
    )
    await svc.record_display_seen("d1", at="y", firmware=None, model=None)
    assert (await svc.displays())[0].mac == "aa:bb:cc:dd:ee:01"
    # Write-once: a known MAC is never moved.
    await svc.record_display_seen(
        "d1", at="z", firmware=None, model=None, mac="aa:bb:cc:dd:ee:02"
    )
    d = (await svc.displays())[0]
    assert (d.mac, d.last_seen_at) == ("aa:bb:cc:dd:ee:01", "z")


async def test_rename_display(svc):
    await svc.add_display(_display(1))
    renamed = await svc.update_display("d1", {"name": "Hallway"})
    assert renamed.name == "Hallway"
    assert (await svc.displays())[0].name == "Hallway"


async def test_rename_unknown_display(svc):
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.update_display("nope", {"name": "x"})


async def test_remove_display(svc):
    await svc.add_display(_display(1))
    await svc.add_display(_display(2))
    await svc.remove_display("d1")
    assert [d.id for d in await svc.displays()] == ["d2"]


async def test_remove_unknown_display(svc):
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.remove_display("nope")


async def test_record_display_seen(svc):
    await svc.add_display(_display(1, firmware="1.0"))
    await svc.record_display_seen(
        "d1", at="2026-10-13T09:00:00+00:00", firmware="1.1", model=None
    )
    d = (await svc.displays())[0]
    assert d.last_seen_at == "2026-10-13T09:00:00+00:00"
    assert d.firmware == "1.1"
    # A header the panel didn't send keeps what we knew.
    assert d.model is None
    await svc.record_display_seen(
        "d1", at="2026-10-13T09:01:00+00:00", firmware=None, model="m"
    )
    d = (await svc.displays())[0]
    assert (d.firmware, d.model) == ("1.1", "m")


async def test_record_seen_for_a_removed_display_is_a_no_op(svc, repo):
    await svc.record_display_seen("gone", at="x", firmware=None, model=None)
    assert "dashboard.displays" not in repo.rows


async def test_revoke_sessions_unpairs_every_display(svc, repo):
    await svc.add_display(_display(1))
    batches: list[dict[str, str]] = []
    real = repo.set_many

    async def spy(items):
        batches.append(dict(items))
        await real(items)

    repo.set_many = spy
    await svc.revoke_sessions(actor_is_admin=True)
    assert await svc.displays() == ()
    # Epoch and panel list change in one batch — a crash can't keep panels.
    assert batches == [{"dashboard.epoch": "1", "dashboard.displays": "[]"}]


async def test_set_password_unpairs_every_display(svc):
    await svc.add_display(_display(1))
    await _with_password(svc)
    assert await svc.displays() == ()


async def test_corrupt_display_storage_fails_closed(repo):
    repo.rows["dashboard.displays"] = "{not json"
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert await svc.displays() == ()


@pytest.mark.parametrize(
    "entry",
    [
        "x",
        {"id": "d1"},
        {"id": 1, "name": "n", "token_hash": "h", "created_at": "t"},
        {
            "id": "d1",
            "name": "n",
            "token_hash": "h",
            "created_at": "t",
            "locked": "yes",
        },
        {
            "id": "d1",
            "name": "n",
            "token_hash": "h",
            "created_at": "t",
            "firmware": 3,
        },
    ],
)
async def test_invalid_stored_display_is_skipped(repo, entry):
    good = {
        "id": "d2",
        "name": "Kitchen",
        "token_hash": "ab" * 32,
        "created_at": "2026-10-13T08:00:00+00:00",
    }
    repo.rows["dashboard.displays"] = json.dumps([entry, good])
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert [d.id for d in await svc.displays()] == ["d2"]


async def test_display_list_not_a_list_fails_closed(repo):
    repo.rows["dashboard.displays"] = json.dumps({"id": "d1"})
    svc = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert await svc.displays() == ()


async def test_displays_are_cached_after_first_read(repo):
    spy = _SpyRepo()
    svc = DashboardSettingsService(spy, fixed_port=None, main_port=MAIN_PORT)
    await svc.add_display(_display(1))
    spy.read.clear()
    await svc.displays()
    await svc.displays()
    assert "dashboard.displays" not in spy.read


# ── wall-panel privacy lock ──────────────────────────────────────────────


async def test_set_display_locked_persists_across_a_restart(repo):
    first = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    await first.add_display(_display(1))
    locked = await first.update_display("d1", {"locked": True})
    assert locked.locked is True
    # A panel reboot (or a server restart) must not lift the lock.
    second = DashboardSettingsService(repo, fixed_port=None, main_port=MAIN_PORT)
    assert (await second.displays())[0].locked is True
    unlocked = await second.update_display("d1", {"locked": False})
    assert unlocked.locked is False
    assert (await second.displays())[0].locked is False


async def test_set_display_locked_unknown_display(svc):
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.update_display("nope", {"locked": True})


async def test_seen_keeps_the_lock(svc):
    await svc.add_display(_display(1))
    await svc.update_display("d1", {"locked": True})
    await svc.record_display_seen("d1", at="t", firmware="2", model=None)
    assert (await svc.displays())[0].locked is True


async def test_delete_pin_lifts_every_panel_lock(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    await svc.add_display(_display(1))
    await svc.add_display(_display(2))
    await svc.update_display("d1", {"locked": True})
    await svc.delete_pin(actor_is_admin=True)
    assert [d.locked for d in await svc.displays()] == [False, False]
    assert not (await svc.get()).pin_set


async def test_changing_the_pin_keeps_panel_locks(svc):
    await svc.set_pin(actor_is_admin=True, pin="1234")
    await svc.add_display(_display(1))
    await svc.update_display("d1", {"locked": True})
    await svc.set_pin(actor_is_admin=True, pin="5678")
    assert (await svc.displays())[0].locked is True


async def test_first_pin_clears_a_stale_panel_lock(svc):
    # No PIN means nothing can be locked: a flag left from a removed PIN
    # (a crash mid-delete) must not come back to life with a new one.
    await svc.add_display(_display(1, locked=True))
    await svc.set_pin(actor_is_admin=True, pin="1234")
    assert (await svc.displays())[0].locked is False


@pytest.mark.parametrize("change", [{}, {"token_hash": "x"}, {"name": "a", "id": "b"}])
async def test_update_display_only_changes_name_and_lock(svc, change):
    await svc.add_display(_display(1))
    with pytest.raises(ValueError):
        await svc.update_display("d1", change)
    assert (await svc.displays())[0] == _display(1)


async def test_remove_display_takes_its_pending_takeover_along(svc):
    mac = "aa:bb:cc:dd:ee:01"
    await svc.add_display(_display(1, mac=mac))
    await svc.add_display(_display(2, mac="aa:bb:cc:dd:ee:02"))
    await svc.add_display(_display(3, mac=mac), supersede_same_mac=True)
    assert await svc.remove_display("d1") == ("d1", "d3")
    assert [d.id for d in await svc.displays()] == ["d2"]
    with pytest.raises(DashboardDisplayNotFoundError):
        await svc.remove_display("d1")
