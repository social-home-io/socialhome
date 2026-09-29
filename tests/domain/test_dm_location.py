"""Tests for socialhome.domain.dm_location — DM location content shape."""

from __future__ import annotations

import json

import pytest

from socialhome.domain.dm_location import (
    ACCURACY_BUCKETS_M,
    DM_LOCATION_LABEL_MAX,
    normalise_location_content,
    parse_location_content,
)


def _norm(obj) -> dict:
    return json.loads(normalise_location_content(json.dumps(obj)))


def test_rounds_coordinates_to_four_decimals():
    out = _norm({"lat": 52.370216789, "lon": 4.895167912})
    assert out == {
        "lat": 52.3702,
        "lon": 4.8952,
        "label": None,
        "accuracy_m": None,
    }


def test_canonical_output_is_stable():
    raw = json.dumps({"lon": 4.1, "lat": 52.0, "junk": "x"})
    once = normalise_location_content(raw)
    assert normalise_location_content(once) == once
    # Unknown keys are dropped.
    assert "junk" not in once


def test_accepts_integer_coordinates():
    assert _norm({"lat": 0, "lon": -180})["lon"] == -180.0


def test_negative_zero_is_plain_zero():
    out = normalise_location_content(json.dumps({"lat": -0.00001, "lon": 0.0}))
    assert '"lat":0.0' in out


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        json.dumps({"lat": 1.0}),
        json.dumps({"lon": 1.0}),
        json.dumps({"lat": "52.1", "lon": 4.0}),
        json.dumps({"lat": True, "lon": 4.0}),
        json.dumps({"lat": 90.5, "lon": 4.0}),
        json.dumps({"lat": -90.5, "lon": 4.0}),
        json.dumps({"lat": 1.0, "lon": 180.01}),
        json.dumps({"lat": 1.0, "lon": -181}),
        '{"lat": NaN, "lon": 1.0}',
        '{"lat": 1.0, "lon": Infinity}',
    ],
)
def test_rejects_malformed_or_out_of_range(value):
    with pytest.raises(ValueError):
        normalise_location_content(value)


def test_label_is_trimmed_and_blank_becomes_none():
    assert _norm({"lat": 1, "lon": 1, "label": "  Beach  "})["label"] == "Beach"
    assert _norm({"lat": 1, "lon": 1, "label": "   "})["label"] is None


def test_label_control_characters_are_stripped():
    assert _norm({"lat": 1, "lon": 1, "label": "Ca\u0000fe\n"})["label"] == "Cafe"


def test_label_over_cap_is_rejected():
    with pytest.raises(ValueError):
        _norm({"lat": 1, "lon": 1, "label": "x" * (DM_LOCATION_LABEL_MAX + 1)})
    assert (
        len(_norm({"lat": 1, "lon": 1, "label": "x" * DM_LOCATION_LABEL_MAX})["label"])
        == DM_LOCATION_LABEL_MAX
    )


def test_label_must_be_string():
    with pytest.raises(ValueError):
        _norm({"lat": 1, "lon": 1, "label": 5})


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [
        (0, ACCURACY_BUCKETS_M[0]),
        (3.2, 25),
        (25, 25),
        (25.1, 50),
        (99, 100),
        (740, 1000),
        (9999, 10000),
        (123456, ACCURACY_BUCKETS_M[-1]),
    ],
)
def test_accuracy_rounds_up_to_coarse_bucket(raw, bucket):
    assert _norm({"lat": 1, "lon": 1, "accuracy_m": raw})["accuracy_m"] == bucket


@pytest.mark.parametrize("raw", [-1, "10", True, float("inf")])
def test_accuracy_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        normalise_location_content(
            json.dumps({"lat": 1, "lon": 1, "accuracy_m": raw}),
        )


def test_accuracy_null_is_none():
    assert _norm({"lat": 1, "lon": 1, "accuracy_m": None})["accuracy_m"] is None


def test_oversized_content_is_rejected():
    with pytest.raises(ValueError):
        normalise_location_content(" " * 5000 + '{"lat":1,"lon":1}')


def test_parse_returns_dataclass():
    loc = parse_location_content(
        json.dumps({"lat": 1.234567, "lon": 2.345678, "label": "Home", "accuracy_m": 7})
    )
    assert (loc.lat, loc.lon, loc.label, loc.accuracy_m) == (1.2346, 2.3457, "Home", 25)
