"""Parsing a gallery search (D11): words, who added it, month and year."""

from __future__ import annotations

import pytest

from socialhome.domain.gallery_search import (
    GALLERY_SEARCH_MAX_TERMS,
    GalleryQuery,
    fold,
    fts_prefix_query,
    name_matches,
    parse_gallery_query,
    snippet,
)


def test_words_person_month_and_year():
    q = parse_gallery_query("Urlaub von Lena Juli 2025")
    assert q == GalleryQuery(terms=("Urlaub",), people=("Lena",), year=2025, month=7)
    assert q.has_date
    assert not q.is_empty


@pytest.mark.parametrize(
    ("text", "month"),
    [
        ("März", 3),
        ("maerz", 3),
        ("maart", 3),
        ("mars", 3),
        ("marzo", 3),
        ("Août", 8),
        ("augustus", 8),
        ("décembre", 12),
        ("Dezember", 12),
        ("mei", 5),
        ("July", 7),
        ("Jänner", 1),
    ],
)
def test_month_names_in_five_languages(text, month):
    q = parse_gallery_query(text)
    assert q.month == month
    assert q.terms == ()


@pytest.mark.parametrize(
    ("text", "year", "month"),
    [
        ("2025", 2025, None),
        ("2025-07", 2025, 7),
        ("07/2025", 2025, 7),
        ("7.2025", 2025, 7),
    ],
)
def test_numeric_dates(text, year, month):
    q = parse_gallery_query(text)
    assert (q.year, q.month) == (year, month)


def test_no_seasons_or_short_month_names():
    # Only literal month names: "Sommer" is a word, and "Jan" is a name.
    q = parse_gallery_query("Sommer Jan")
    assert q.terms == ("Sommer", "Jan")
    assert q.month is None


@pytest.mark.parametrize("word", ["by", "von", "door", "par", "por", "from", "VON"])
def test_person_words(word):
    assert parse_gallery_query(f"{word} Lena").people == ("Lena",)


def test_de_and_van_are_words_not_people():
    q = parse_gallery_query("fotos de playa van Gogh")
    assert q.people == ()
    assert q.terms == ("fotos", "de", "playa", "van", "Gogh")


def test_trailing_person_word_is_a_word():
    q = parse_gallery_query("Spaziergang von")
    assert q.terms == ("Spaziergang", "von")
    assert q.people == ()


def test_punctuation_only_is_empty():
    assert parse_gallery_query(" -- ?? ").is_empty
    assert parse_gallery_query("").is_empty


def test_terms_are_capped():
    q = parse_gallery_query(" ".join(f"w{i}" for i in range(20)))
    assert len(q.terms) == GALLERY_SEARCH_MAX_TERMS


def test_fts_prefix_query_quotes_operators():
    assert fts_prefix_query("som") == '"som"*'
    assert fts_prefix_query('a"b OR c') == '"a""b OR c"*'


def test_fold():
    assert fold("Über Façade") == "uber facade"


def test_name_matches_any_word_prefix():
    assert name_matches("Lena Müller", "lena")
    assert name_matches("Lena Müller", "mul")
    assert not name_matches("Lena Müller", "ena")
    assert not name_matches(None, "lena")
    assert not name_matches("Lena", "")


def test_snippet_marks_matches():
    assert snippet("Am Strand mit Oma", ("strand",)) == "Am <mark>Strand</mark> mit Oma"
    assert snippet("Sommerfest", ("som",)) == "<mark>Sommerfest</mark>"
    assert snippet(None, ("x",)) == ""
    # No word: the caption as it is.
    assert snippet("Hello", ()) == "Hello"


def test_snippet_long_caption_is_cut_around_the_match():
    caption = "x " * 100 + "Geburtstag " + "y " * 100
    out = snippet(caption, ("geburtstag",))
    assert "<mark>Geburtstag</mark>" in out
    assert out.startswith("…")
    assert out.endswith("…")
    assert len(out.replace("<mark>", "").replace("</mark>", "")) <= 122


def test_snippet_with_a_letter_that_folds_to_two():
    assert snippet("Straße am Meer", ("meer",)) == "Straße am Meer"
