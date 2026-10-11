"""Tests for the dashboard's household-chat message rows (pure)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.messages import NEW_FOR, message_rows
from socialhome.dashboard.presenter import ColorBook, Fmt
from socialhome.i18n import Catalog

from .factories import MESSAGES, NOW, PEOPLE, TODAY, message

CATALOG = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")


def _fmt(locale: str = "en", people=PEOPLE) -> Fmt:
    def t(key: str, **fmt: object) -> str:
        return CATALOG.gettext(key, locale=locale, **fmt)

    return Fmt(now=NOW, today=TODAY, t=t, people=people, colors=ColorBook())


def test_rows_show_text_and_say_a_sentence():
    rows = message_rows(MESSAGES, _fmt())
    assert [r.id for r in rows] == ["m1", "m2", "m3"]
    anna, pascal, mia = rows
    assert anna.person.user_id == "u-anna"
    assert (anna.icon, anna.text) == ("", 'Dinner at 7 <b>"sharp"</b> @Pascal')
    assert anna.say == 'Anna says: Dinner at 7 <b>"sharp"</b> @Pascal'
    assert anna.new and anna.when == "4 min ago"
    assert anna.iso == (NOW - timedelta(minutes=4)).isoformat()
    assert (pascal.icon, pascal.text, pascal.say) == (
        "📷",
        "Photo",
        "Pascal sent a photo.",
    )
    assert not pascal.new and pascal.when == "13:52"
    # A voice note reads its transcript.
    assert (mia.icon, mia.text) == ("🎤", "Can I go to Lena's?")
    assert mia.say == "Mia says: Can I go to Lena's?"
    assert mia.when == "12:27"


@pytest.mark.parametrize(
    ("kind", "icon", "text", "say"),
    [
        ("video", "🎬", "Video", "Anna sent a video."),
        ("file", "📎", "File", "Anna sent a file."),
        ("voice", "🎤", "Voice message", "Anna sent a voice message."),
    ],
)
def test_media_and_untranscribed_voice(kind, icon, text, say):
    (row,) = message_rows([message("m", "u-anna", 1, kind=kind)], _fmt())
    assert (row.icon, row.text, row.say) == (icon, text, say)


def test_new_ends_at_fifteen_minutes():
    minutes = NEW_FOR.total_seconds() / 60
    fresh, old = message_rows(
        [
            message("a", "u-anna", minutes - 0.5),
            message("b", "u-anna", minutes),
        ],
        _fmt(),
    )
    assert fresh.new and fresh.when == "14 min ago"
    assert not old.new and old.when == "14:17"


def test_just_now():
    (row,) = message_rows([message("a", "u-anna", 0.2)], _fmt())
    assert row.when == "just now"


def test_a_sender_not_on_the_page_is_left_out():
    rows = message_rows(
        [message("a", "u-hidden", 1), message("b", "u-anna", 1)],
        _fmt(),
    )
    assert [r.id for r in rows] == ["b"]


def test_rows_in_german():
    rows = message_rows(MESSAGES, _fmt("de"))
    assert rows[0].say.startswith("Anna sagt: ")
    assert rows[0].when == "vor 4 Min."
    assert rows[1].text == "Foto" and rows[1].say == "Pascal hat ein Foto geschickt."
