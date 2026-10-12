"""Tests for the dashboard's household-chat message rows (pure)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.messages import (
    CAPTION_CHARS,
    CHAT_CHARS,
    NEW_FOR,
    card_rows,
    chat_days,
    clip,
    clip_words,
    mention_cards,
    message_rows,
    new_count,
)
from socialhome.dashboard.presenter import ColorBook, Fmt
from socialhome.i18n import Catalog

from .factories import (
    FOR_LEO,
    HERE,
    MESSAGES,
    NOW,
    PEOPLE,
    TODAY,
    at,
    mention,
    message,
)

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


# ─── Mentions (@here banner, @name card / tile) ─────────────────────────


def test_mention_cards_show_the_sender_text_and_a_sentence():
    here, leo = mention_cards([HERE, FOR_LEO], _fmt())
    assert (here.id, here.person.user_id) == ("h1", "u-anna")
    assert here.key == "h1:here"
    assert here.text == "@here Dinner is <b>ready</b>!"  # escaped by the template
    assert here.say == "Anna says: @here Dinner is <b>ready</b>!"
    assert here.when == "3 min ago"
    assert here.iso == (NOW - timedelta(minutes=3)).isoformat()
    assert here.person_id is None
    assert (leo.key, leo.person_id) == ("p1:u-leo", "u-leo")
    assert leo.person.user_id == "u-pascal"


def test_mention_cards_skip_senders_not_shown():
    hidden = mention("x", "u-hidden", 1, text="@here secret")
    assert [c.id for c in mention_cards([hidden, HERE], _fmt())] == ["h1"]


# ─── Picture-view captions: one short line ──────────────────────────────


def test_clip_keeps_short_text_and_cuts_long_text_on_one_line():
    assert clip("Dinner at 7") == "Dinner at 7"
    assert clip("  two\n lines\t here ") == "two lines here"
    long = "word " * 30
    out = clip(long)
    assert len(out) <= CAPTION_CHARS and out.endswith("…")
    assert not out.endswith(" …")
    assert clip("x" * CAPTION_CHARS) == "x" * CAPTION_CHARS


def test_message_captions():
    anna, pascal, mia = message_rows(MESSAGES, _fmt())
    assert anna.caption == 'Dinner at 7 <b>"sharp"</b> @Pascal'
    assert pascal.caption == "📷 Photo"
    assert mia.caption == "🎤 Can I go to Lena's?"
    long = message("m9", "u-anna", 1, text="Please " * 20)
    (row,) = message_rows([long], _fmt())
    assert row.caption == clip("Please " * 20) and row.caption.endswith("…")


def test_mention_caption_is_clipped():
    (leo,) = mention_cards([FOR_LEO], _fmt())
    assert leo.caption == "@leo shoes on"
    long = mention("x", "u-pascal", 1, person="u-leo", text="@leo " + "a" * 80)
    (card,) = mention_cards([long], _fmt())
    assert len(card.caption) == CAPTION_CHARS and card.caption.endswith("…")


# ─── Home's Messages card and Messages tab ──────────────────────────────


def _at_msg(mid: str, sent) -> object:
    return replace(message(mid, "u-anna", 0), sent_at=sent)


YESTERDAY = TODAY - timedelta(days=1)


def test_rows_carry_their_sent_time():
    (row,) = message_rows([message("a", "u-anna", 4)], _fmt())
    assert row.sent_at == NOW - timedelta(minutes=4)


def test_card_rows_are_the_newest_three_of_six_hours():
    rows = message_rows(
        [message(f"m{i}", "u-anna", 60 * i + 1) for i in range(8)], _fmt()
    )
    assert [r.id for r in card_rows(rows, NOW)] == ["m0", "m1", "m2"]
    # Older than six hours: not on the card (but on the tab).
    old = message_rows([message("old", "u-anna", 6 * 60 + 1)], _fmt())
    assert card_rows(old, NOW) == []


def test_card_rows_leave_a_bannered_message_out_without_backfilling():
    rows = message_rows([message(f"m{i}", "u-anna", i + 1) for i in range(5)], _fmt())
    card = card_rows(rows, NOW, hide=frozenset({"m0"}))
    assert [r.id for r in card] == ["m1", "m2"]


def test_new_count_is_time_based():
    rows = message_rows(
        [
            message("a", "u-anna", 1),
            message("b", "u-mia", 14),
            message("c", "u-leo", 16),
        ],
        _fmt(),
    )
    assert new_count(rows) == 2
    assert new_count([]) == 0


def test_clip_words_cuts_on_a_word_boundary():
    assert clip_words("Dinner at 7") == ("Dinner at 7", False)
    assert clip_words("  two\n lines ") == ("two lines", False)
    out, rest = clip_words("word " * 60)
    assert rest and out.endswith("…") and len(out) <= CHAT_CHARS
    assert out == ("word " * 44).strip() + "…"
    out, _ = clip_words("words " * 60)
    assert out == ("words " * 36).strip() + "…"  # never half a word
    assert clip_words("x" * CHAT_CHARS) == ("x" * CHAT_CHARS, False)
    # One endless word is cut where it must be.
    out, rest = clip_words("x" * (CHAT_CHARS + 5))
    assert rest and out == "x" * (CHAT_CHARS - 1) + "…"
    # A space right at the cut keeps the whole last word.
    out, _ = clip_words("a" * (CHAT_CHARS - 1) + " tail")
    assert out == "a" * (CHAT_CHARS - 1) + "…"


def test_chat_days_group_today_and_yesterday_in_household_time():
    long = message("long", "u-pascal", 30, text="Remember " * 40)
    rows = message_rows(
        [
            message("now", "u-anna", 4, text="Dinner at 7"),
            long,
            _at_msg("late", at(0, 5)),
            _at_msg("eve", at(21, 10, YESTERDAY)),
        ],
        _fmt(),
    )
    today, yesterday = chat_days(rows, _fmt())
    assert (today.key, today.label) == ("today", "Today")
    assert (yesterday.key, yesterday.label) == ("yesterday", "Yesterday")
    assert [r.message.id for r in today.rows] == ["now", "long", "late"]
    assert [r.message.id for r in yesterday.rows] == ["eve"]
    first = today.rows[0]
    # Absolute times, even while new (a stale board stays truthful).
    assert first.message.new and first.time == "14:28"
    assert first.playing == "Reading Anna's message aloud"
    assert (first.text, first.rest) == ("Dinner at 7", False)
    clipped = today.rows[1]
    assert clipped.rest and clipped.text.endswith("…")
    assert len(clipped.text) <= CHAT_CHARS
    # The whole message is still read aloud.
    assert clipped.message.say == "Pascal says: " + "Remember " * 40
    assert yesterday.rows[0].time == "21:10"


def test_chat_days_use_the_household_day_for_a_utc_stamp():
    """00:30 in Berlin is 22:30 UTC the day before: it is today's."""
    utc = at(0, 30).astimezone(timezone.utc)
    (today,) = chat_days(message_rows([_at_msg("x", utc)], _fmt()), _fmt())
    assert today.key == "today" and today.rows[0].time == "00:30"


def test_chat_days_only_name_days_with_messages():
    rows = message_rows([message("a", "u-anna", 4)], _fmt())
    assert [d.key for d in chat_days(rows, _fmt())] == ["today"]
    assert chat_days([], _fmt()) == []
    only = message_rows([_at_msg("y", at(20, 0, YESTERDAY))], _fmt())
    assert [d.key for d in chat_days(only, _fmt())] == ["yesterday"]


def test_chat_days_drop_anything_older_than_yesterday():
    older = _at_msg("o", at(23, 0, TODAY - timedelta(days=2)))
    assert chat_days(message_rows([older], _fmt()), _fmt()) == []


def test_chat_days_in_german():
    rows = message_rows(
        [message("a", "u-anna", 4), _at_msg("y", at(20, 0, YESTERDAY))],
        _fmt("de"),
    )
    today, yesterday = chat_days(rows, _fmt("de"))
    assert (today.label, yesterday.label) == ("Heute", "Gestern")
    assert today.rows[0].playing == "Nachricht von Anna wird vorgelesen"
