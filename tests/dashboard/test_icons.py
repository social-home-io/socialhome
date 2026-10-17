"""Tests for the dashboard's title → emoji guesses."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from socialhome.dashboard.icons import (
    FALLBACK_ICON,
    GENERAL_KEYWORDS,
    LESSON_KEYWORDS,
    _needle,
    normalized_words,
    suggest_icon,
    title_icon,
)
from socialhome.domain.household_dashboard import is_pure_emoji


def test_normalized_words_fold_case_diacritics_and_sharp_s():
    assert normalized_words("Zähne PUTZEN!") == ["zahne", "putzen"]
    assert normalized_words("Blumen gießen") == ["blumen", "giessen"]
    assert normalized_words("Natación 2x") == ["natacion", "x"]
    assert normalized_words("b-day_party") == ["b", "day", "party"]
    assert normalized_words("") == []


@pytest.mark.parametrize(
    ("title", "icon"),
    [
        # English
        ("Swimming", "🏊"),
        ("Brush teeth", "🦷"),
        ("Empty dishwasher", "🍽️"),
        ("Take out the trash", "🗑️"),
        ("Feed the cat", "🐱"),
        ("Walk the dog", "🐶"),
        ("Water the plants", "🌱"),
        ("Tidy your room", "🧹"),
        ("Laundry", "🧺"),
        ("Bedtime", "🛏️"),
        ("Bath", "🛁"),
        ("Piano lesson", "🎹"),
        ("Football practice", "⚽"),
        ("Dinner with Gran", "🍝"),
        ("Mia's birthday", "🎂"),
        ("Dentist", "🦷"),
        ("Homework", "📚"),
        ("Grocery run", "🛒"),
        ("Set the table", "🍽️"),
        ("Flight to Rome", "✈️"),
        # German, incl. compounds
        ("Schwimmtraining", "🏊"),
        ("Kinderschwimmen", "🏊"),
        ("Zähneputzen", "🦷"),
        ("Zaehne putzen", "🦷"),
        ("Spülmaschine ausräumen", "🍽️"),
        ("Geschirrspüler", "🍽️"),
        ("Restmüll rausbringen", "🗑️"),
        ("Wäsche aufhängen", "🧺"),
        ("Zimmer aufräumen", "🧹"),
        ("Fensterputzen", "🧹"),
        ("Blumen gießen", "🌱"),
        ("Katze füttern", "🐱"),
        ("Mit dem Hund Gassi gehen", "🐶"),
        ("Kinderarzt", "🩺"),
        ("Klavierunterricht", "🎹"),
        ("Fußballtraining", "⚽"),
        ("Hausaufgaben", "📚"),
        ("Mittagsschlaf", "🛏️"),
        ("Geburtstag Oma", "🎂"),
        ("Reitstunde", "🐴"),
        ("Kuchen backen", "🍪"),
        ("Tisch decken", "🍽️"),
        ("Kindergarten", "🎒"),
        ("Preise abholen", "🚗"),
        ("Baby shower", "🎉"),
        ("Shower", "🛁"),
        ("Müll rausbringen", "🗑️"),
        ("Mülltonne", "🗑️"),
        ("Biomüll", "🗑️"),
        ("Feiertag", "🎉"),
        ("Gartenarbeit", "🌱"),
        ("Plätzchen backen", "🍪"),
        ("Bett machen", "🛏️"),
        ("Fischfutter", "🐟"),
        ("Staub wischen", "🧹"),
        ("Reiseapotheke", "🧳"),
        ("Kerstmis", "🎄"),
        # Spanish
        ("Natación", "🏊"),
        ("Lavarse los dientes", "🦷"),
        ("Sacar la basura", "🗑️"),
        ("Cumpleaños de Mia", "🎂"),
        ("Clase de guitarra", "🎸"),
        ("Hacer los deberes", "📚"),
        ("Poner la mesa", "🍽️"),
        # French
        ("Natation", "🏊"),
        ("Brosser les dents", "🦷"),
        ("Vider le lave-vaisselle", "🍽️"),
        ("Sortir la poubelle", "🗑️"),
        ("Anniversaire de Léo", "🎂"),
        ("Devoirs", "📚"),
        ("Cours de piano", "🎹"),
        # Dutch
        ("Zwemles", "🏊"),
        ("Tanden poetsen", "🦷"),
        ("Vaatwasser uitruimen", "🍽️"),
        ("Vuilnis buiten zetten", "🗑️"),
        ("Verjaardag oma", "🎂"),
        ("Huiswerk", "📚"),
        ("Voetbaltraining", "⚽"),
    ],
)
def test_suggest_icon_per_locale(title, icon):
    assert suggest_icon(title) == icon


@pytest.mark.parametrize(
    "title",
    [
        "Transport",  # not "sport"
        "Apartment",  # not "art"
        "Planning",  # not "plant"
        "Hundert Euro",  # not "Hund"
        "Vorbereiten",  # not "reiten"
        "Workshop",  # not "shop"
        "Ausflug",  # not "Flug"
        "Bedford",  # not "bed"
        "Cathedral",  # not "cat"
        "Distanz",  # not "Tanz"
        "Nachmittag",  # not lunch
        "Treffen mit Kerstin",  # not Christmas
        "Termin bei Müller",  # not trash
        "Frau Mueller",
        "Zwischenprüfung",  # not cleaning
        "Kaffee zwischen zwei Terminen",
        "Lieferung geplant",  # not plants
        "Feierabend",  # not a party
        "Backend deploy",  # not baking
        "Investir",  # not getting dressed
        "Attendance",  # not dancing
        "Unterzeichnen",  # not drawing
        "Churchill",  # not church
        "Bettina",  # not bed
        "Herr Fischer",  # not fish
        "Marcello",  # not cello
        "Fleur",  # a name, not flowers
        "Kitty",  # a name
        "Chevalier",
        "Obediente",  # not teeth
        "Barrera",  # not sweeping
        "Rechercher",  # not picking up
        "Afvallen",  # not trash
        "Aircraft",  # not crafts
        "Spielberg",  # not play
        "Biergarten",  # not gardening
        "Termin in Gießen",  # a town, not watering
        "Relief",  # not religion (subject table is for lessons)
        "Germany",  # not German (subject table is for lessons)
        "Breaking news",  # not a break
        "Captain Cook",  # surnames
        "Familie Flores",
        "Julio Iglesias",
        "Noel kommt",
        "Xyz",
        "",
        "123",
    ],
)
def test_no_false_positive_on_short_substrings(title):
    assert suggest_icon(title) is None


def test_more_specific_rows_win():
    # Teeth before cleaning; table tennis before tennis; music school
    # before school.
    assert suggest_icon("Zähneputzen") == "🦷"
    assert suggest_icon("Tischtennis") == "🏓"
    assert suggest_icon("Tennis") == "🎾"
    assert suggest_icon("Musikschule") == "🎵"
    assert suggest_icon("Grundschule") == "🎒"
    assert suggest_icon("Schwimmschule") == "🏊"


def test_lessons_use_the_spa_subject_table_first():
    assert suggest_icon("Mathe", school=True) == "🔢"
    assert suggest_icon("Englisch", school=True) == "🇬🇧"
    assert suggest_icon("Sport", school=True) == "⚽"
    assert suggest_icon("Naturwissenschaft", school=True) == "🔬"
    assert suggest_icon("Pause", school=True) == "🧸"
    assert suggest_icon("Mittagessen", school=True) == "🍽️"
    # SPA rules: ≤ 3 letters a whole word; else the start of a word.
    assert suggest_icon("Art", school=True) == "🎨"
    assert suggest_icon("Transport", school=True) is None
    assert suggest_icon("Homework", school=True) == "📚"  # not "home$"
    # A lesson title only the everyday table knows.
    assert suggest_icon("Schwimmtraining", school=True) == "🏊"
    # The subject table is for lessons only: SPA-style word starts
    # ("reli", "german", "break") misfire on everyday event titles.
    assert suggest_icon("Mathe") is None


def test_title_icon_prefers_a_leading_emoji():
    assert title_icon("🎻 Swimming") == "🎻"
    assert title_icon("  🏊 Training") == "🏊"
    assert title_icon("👨‍👩‍👧 Family time") == "👨‍👩‍👧"


def test_title_icon_falls_back_to_a_neutral_diamond():
    assert FALLBACK_ICON == "🔹"
    assert title_icon("Taxes") == "🔹"
    assert title_icon(None) == "🔹"
    assert title_icon("") == "🔹"
    assert title_icon("Dentist") == "🦷"
    assert title_icon("Mathe", school=True) == "🔢"


@pytest.mark.parametrize("table", [GENERAL_KEYWORDS, LESSON_KEYWORDS])
def test_every_table_icon_is_one_emoji(table):
    for emoji, keywords in table:
        assert is_pure_emoji(emoji), emoji
        assert keywords
        for k in keywords:
            word = k.lstrip("=^*")
            assert word == word.strip() and word, k
            # Keywords are normalised already (what they're matched against).
            assert " ".join(normalized_words(word)) == word, k


@pytest.mark.parametrize(
    ("keyword", "needle"),
    [
        ("=bed", " bed "),
        ("^shop", " shop"),
        ("*mull", "mull"),
        ("art", " art "),  # ≤ 3 letters: a whole word
        ("swim", " swim"),  # 4 letters: the start of a word
        ("schwimm", "schwimm"),  # 5+: anywhere in a word
        ("tisch decken", " tisch decken"),  # a phrase starts on a word
    ],
)
def test_keyword_forms(keyword, needle):
    assert _needle(keyword, by_length=True) == needle


def test_spa_keyword_forms_never_match_inside_a_word():
    assert _needle("art", by_length=False) == " art "
    assert _needle("schwimm", by_length=False) == " schwimm"


ICONS_TS = (
    Path(__file__).resolve().parents[2] / "client/src/features/timetable/icons.ts"
)


def _spa_tables() -> list[tuple[str, tuple[str, ...]]]:
    """``KEYWORDS`` of the SPA's ``icons.ts``, in this module's notation:
    ``$``-marked and ≤ 3-letter keywords are whole words (``=``), the
    rest word starts (``^``) — with each row's preset emoji."""
    ts = ICONS_TS.read_text(encoding="utf-8")
    emoji = {key: e for e, key in re.findall(r"preset\('([^']+)', '(\w+)'\)", ts)}
    block = ts[ts.index("const KEYWORDS") : ts.index("export function suggestIcon")]
    rows = []
    for key, body in re.findall(r"\['(\w+)',\s*\[(.*?)\]\]", block, flags=re.DOTALL):
        words = []
        for k in re.findall(r"'([^']+)'", body):
            if k.endswith("$"):
                words.append("=" + k[:-1])
            elif len(k) <= 3:
                words.append("=" + k)
            else:
                words.append("^" + k)
        rows.append((emoji[key], tuple(words)))
    return rows


def test_lesson_table_matches_the_spa():
    """Keep in sync with client/src/features/timetable/icons.ts."""
    spa = _spa_tables()
    assert len(spa) >= 20
    assert [(e, tuple(k)) for e, k in LESSON_KEYWORDS] == spa
