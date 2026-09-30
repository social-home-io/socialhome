"""Shared IANA timezone validation (:mod:`socialhome.utils.timezones`)."""

from __future__ import annotations

import logging

import pytest

from socialhome.utils.timezones import DEFAULT_TZ, coerce_tz, is_valid_tz


@pytest.mark.parametrize(
    "name",
    ["UTC", "Europe/Zurich", "America/Los_Angeles"],
)
def test_is_valid_tz_accepts_real_zones(name):
    assert is_valid_tz(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Foo/Bar",  # well-formed key, not in the database
        "",  # empty
        "/etc/passwd",  # absolute path → ValueError, not KeyError
        "../../etc/passwd",  # traversal → ValueError
    ],
)
def test_is_valid_tz_rejects_unknown_and_malformed(name):
    assert is_valid_tz(name) is False


def test_coerce_tz_passes_through_a_valid_zone():
    assert coerce_tz("Europe/Zurich", context="test") == "Europe/Zurich"


def test_coerce_tz_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome.utils.timezones"):
        assert coerce_tz("Foo/Bar", context="TEST_EVENT from instance i-x") == "UTC"
    # The offending value AND the sender are in the record so a
    # misbehaving peer is diagnosable rather than silent.
    assert "Foo/Bar" in caplog.text
    assert "i-x" in caplog.text


@pytest.mark.parametrize("missing", [None, ""])
def test_coerce_tz_treats_absent_as_default(missing, caplog):
    """An older peer omits the field entirely — that's not an error, so
    it must not log a warning."""
    with caplog.at_level(logging.WARNING, logger="socialhome.utils.timezones"):
        assert coerce_tz(missing, context="test") == DEFAULT_TZ
    assert caplog.text == ""


def test_is_valid_tz_rejects_overlong_name_instead_of_raising():
    """A 5000-char key makes the tz database lookup raise ``OSError``
    (ENAMETOOLONG) — that is "not a zone", not a crash."""
    assert is_valid_tz("a" * 5000) is False
