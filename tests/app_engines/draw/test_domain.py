"""The gift draw's pure domain (``app_engines/draw/domain.py``, v_78): settings on the
wire, the pairs rule (couples, at most a third, four people at least), the
wish-list bound, the verbs."""

from __future__ import annotations

import pytest

from socialhome.app_engines.draw.domain import (
    DRAW_STEPS,
    DRAW_TO_HUB_STEPS,
    DRAW_WISH_MAX_CHARS,
    AppDrawHouseholdTooOldError,
    AppDrawSettingsError,
    AppDrawWishTooLongError,
    DrawSettings,
    DrawWireError,
    pairs_fit,
    wish_ok,
)
from socialhome.domain.app_group import GROUP_VERBS


def test_settings_round_trip_and_canonical_pairs():
    s = DrawSettings.from_wire(
        {
            "name": " Familie Müller ",
            "budget": "20 €",
            "day": "2026-12-24",
            "pairs": [[4, 3], [1, 2]],
        }
    )
    assert s.name == "Familie Müller"
    assert s.pairs == ((1, 2), (3, 4))
    assert DrawSettings.from_wire(s.to_wire()) == s
    assert s.digest_bytes() == DrawSettings.from_wire(s.to_wire()).digest_bytes()
    assert DrawSettings.from_wire({}) == DrawSettings()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"name": "x" * 81},
        {"name": "two\nlines"},
        {"budget": 20},
        {"day": "24.12.2026"},
        {"day": "2026-02-30"},
        {"pairs": "1-2"},
        {"pairs": [[1]]},
        {"pairs": [[1, 1]]},
        {"pairs": [[1, 2], [2, 3]]},
        {"pairs": [[0, 2]]},
        {"pairs": [[True, 2]]},
        {"pairs": [[2 * i + 1, 2 * i + 2] for i in range(11)]},
    ],
)
def test_settings_out_of_shape(raw):
    with pytest.raises(DrawWireError):
        DrawSettings.from_wire(raw)


def test_only_keeps_pairs_whose_both_people_are_in():
    s = DrawSettings(pairs=((1, 2), (3, 4)))
    assert s.only(frozenset({1, 2, 3})).pairs == ((1, 2),)
    assert s.excludes(2, 1) and not s.excludes(1, 3)


def test_pairs_fit():
    seats = frozenset(range(1, 7))
    assert pairs_fit((), frozenset({1, 2, 3}))
    assert pairs_fit(((1, 2), (3, 4)), seats)  # 2 ≤ 6 // 3
    assert not pairs_fit(((1, 2), (3, 4), (5, 6)), seats)  # more than a third
    assert not pairs_fit(((1, 2),), frozenset({1, 2, 3}))  # three and a pair: no circle
    assert not pairs_fit(((1, 9),), seats)


def test_wish_ok():
    assert wish_ok("  socks\r\nand a book ") == "socks\nand a book"
    assert wish_ok("x" * DRAW_WISH_MAX_CHARS)
    with pytest.raises(AppDrawWishTooLongError):
        wish_ok("x" * (DRAW_WISH_MAX_CHARS + 1))
    with pytest.raises(AppDrawSettingsError):
        wish_ok(5)
    with pytest.raises(AppDrawSettingsError):
        wish_ok("bell\x07")


def test_verbs():
    assert len(DRAW_STEPS) == 21
    assert DRAW_TO_HUB_STEPS < DRAW_STEPS
    assert not DRAW_STEPS & GROUP_VERBS
    assert all(v.isidentifier() and not v.startswith("draw_") for v in DRAW_STEPS)


def test_too_old_names_the_households():
    err = AppDrawHouseholdTooOldError(["Familie Okafor", "Home b"])
    assert err.params == {"households": "Familie Okafor, Home b"}
    assert (err.status, err.code) == (409, "APP_DRAW_HOUSEHOLD_TOO_OLD")
