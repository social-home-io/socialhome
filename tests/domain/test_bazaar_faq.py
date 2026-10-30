"""The listing FAQ's one text rule and its bounds (v_75)."""

from __future__ import annotations

import pytest

from socialhome.domain.bazaar_faq import (
    BAZAAR_FAQ_ANSWER_MAX_CHARS,
    BAZAAR_FAQ_MAX_ENTRIES,
    BAZAAR_FAQ_MAX_ENTRIES_EVER,
    BAZAAR_FAQ_QUESTION_MAX_CHARS,
    BazaarFaqFullError,
    BazaarFaqTextError,
    BazaarFaqUsedUpError,
    clean_answer,
    clean_question,
    is_entry_id,
)


def test_the_owner_bounds():
    assert BAZAAR_FAQ_MAX_ENTRIES == 10
    assert BAZAAR_FAQ_QUESTION_MAX_CHARS == 200
    assert BAZAAR_FAQ_ANSWER_MAX_CHARS == 500


def test_a_listing_ever_holds_at_most_thirty_entries():
    # Every removal leaves a marker for the life of the listing, so the
    # entries ever added (live + removed) are the bound: 10 live + 20 more.
    assert BAZAAR_FAQ_MAX_ENTRIES_EVER == 30
    assert BAZAAR_FAQ_MAX_ENTRIES_EVER >= BAZAAR_FAQ_MAX_ENTRIES


def test_a_question_is_one_trimmed_line():
    assert clean_question("  Does it\n fold?\t ") == "Does it fold?"
    assert clean_question("x" * 200) == "x" * 200


@pytest.mark.parametrize("bad", ["", "   \n ", "x" * 201, "bell\x07", None, 42])
def test_a_bad_question_is_refused(bad):
    with pytest.raises(BazaarFaqTextError) as exc:
        clean_question(bad)
    assert exc.value.code == "BAZAAR_FAQ_TEXT"
    assert exc.value.params == {"question_max": 200, "answer_max": 500}


def test_an_answer_keeps_line_breaks_but_not_runs_of_blank_lines():
    assert clean_answer(" Yes.\r\n\r\n\r\n\n It folds.\tFlat. ") == (
        "Yes.\n\n It folds. Flat."
    )
    assert clean_answer("a\rb") == "a\nb"
    assert clean_answer("y" * 500) == "y" * 500


@pytest.mark.parametrize(
    "bad", ["", " \n\n ", "y" * 501, "nul\x00", "esc\x1b[0m", b"x"]
)
def test_a_bad_answer_is_refused(bad):
    with pytest.raises(BazaarFaqTextError):
        clean_answer(bad)


def test_markup_stays_plain_text():
    # Nothing is interpreted: the SPA renders text nodes only.
    assert clean_answer("<b>bold</b> **x**") == "<b>bold</b> **x**"


def test_entry_ids_are_32_lowercase_hex():
    assert is_entry_id("ab" * 16)
    assert not is_entry_id("AB" * 16)
    assert not is_entry_id("ab" * 15)
    assert not is_entry_id(None)
    assert not is_entry_id("../" + "a" * 29)


def test_the_full_error_names_the_cap():
    err = BazaarFaqFullError()
    assert (err.status, err.code, err.params) == (409, "BAZAAR_FAQ_FULL", {"max": 10})


def test_the_used_up_error_names_the_lifetime_cap():
    err = BazaarFaqUsedUpError()
    assert (err.status, err.code, err.params) == (
        409,
        "BAZAAR_FAQ_USED_UP",
        {"max": 30},
    )
