"""Tests for the household-dashboard domain types."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.errors import CodedError
from socialhome.domain.household_dashboard import (
    DEFAULT_DASHBOARD_PORT,
    IDLE_RETURN_CHOICES,
    MIN_PASSWORD_LENGTH,
    SETUP_TOKEN_TTL_SECONDS,
    SUPPORTED_DASHBOARD_LOCALES,
    DashboardPasswordRequiredError,
    DashboardNotRunningError,
    DashboardPasswordTooShortError,
    DashboardPersonSettings,
    DashboardPortFixedError,
    DashboardPortInUseError,
    DashboardSettings,
    DashboardSettingsInvalidError,
)


def test_constants():
    assert DEFAULT_DASHBOARD_PORT == 8100
    assert IDLE_RETURN_CHOICES == frozenset({0, 1, 3, 10})
    assert SUPPORTED_DASHBOARD_LOCALES == frozenset({"en", "de", "es", "fr", "nl"})
    assert MIN_PASSWORD_LENGTH == 8
    assert SETUP_TOKEN_TTL_SECONDS == 600


def test_settings_defaults():
    s = DashboardSettings()
    assert s.enabled is False
    assert s.port == DEFAULT_DASHBOARD_PORT
    assert s.locale == "en"
    assert s.idle_return_minutes == 3
    assert s.night_start == "22:00"
    assert s.night_end == "06:00"
    assert s.keep_screen_on is True
    assert s.show_status_text is False
    assert s.address is None
    assert s.people == ()
    assert s.password_set_at is None


def test_settings_are_frozen_and_hashable():
    s = DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.enabled = True  # type: ignore[misc]
    assert hash(s) == hash(
        DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    )


def test_person_defaults():
    p = DashboardPersonSettings()
    assert p.show is True
    assert p.picture is None


def test_person_lookup_returns_stored_entry():
    stored = DashboardPersonSettings(show=False, picture=True)
    s = DashboardSettings(people=(("u1", stored),))
    assert s.person("u1") == stored


def test_person_lookup_returns_default_when_absent():
    s = DashboardSettings(people=(("u1", DashboardPersonSettings(show=False)),))
    assert s.person("u2") == DashboardPersonSettings()


def test_people_map_view():
    stored = DashboardPersonSettings(show=False)
    s = DashboardSettings(people=(("u1", stored),))
    assert s.people_map() == {"u1": stored}


def test_password_required_error():
    e = DashboardPasswordRequiredError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PASSWORD_REQUIRED"
    assert e.detail == "Set a dashboard password before enabling the dashboard."


def test_port_fixed_error():
    e = DashboardPortFixedError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PORT_FIXED"


def test_port_in_use_error():
    e = DashboardPortInUseError(8100)
    assert isinstance(e, CodedError)
    assert e.status == 409
    assert e.code == "DASHBOARD_PORT_IN_USE"
    assert e.params == {"port": 8100}


def test_settings_invalid_error_names_field():
    e = DashboardSettingsInvalidError("port")
    assert isinstance(e, CodedError)
    assert isinstance(e, ValueError)
    assert e.status == 422
    assert e.code == "DASHBOARD_SETTINGS_INVALID"
    assert e.params == {"field": "port"}
    assert "port" in e.detail


def test_password_too_short_error():
    e = DashboardPasswordTooShortError()
    assert isinstance(e, CodedError)
    assert e.status == 422
    assert e.code == "DASHBOARD_PASSWORD_TOO_SHORT"
    assert e.params == {"min": MIN_PASSWORD_LENGTH}


def test_not_running_error():
    exc = DashboardNotRunningError()
    assert isinstance(exc, CodedError)
    assert exc.status == 409
    assert exc.code == "DASHBOARD_NOT_RUNNING"
    assert exc.detail == "Turn the dashboard on before creating a sign-in link."
