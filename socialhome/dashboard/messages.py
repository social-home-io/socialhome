"""Household-chat messages on the wall — what a row or tile shows and says.

The read model hands over :class:`~socialhome.domain.household_dashboard.DashboardMessage`
values (only when the admin turned ``show_household_messages`` on); this
module turns each into a :class:`MessageRow` the templates render:

* **Home** shows a row: the sender's face, the text (clamped to two
  lines by CSS) and when — "new" + "4 min ago" for a message under
  :data:`NEW_FOR` old, else the clock time. Media are a word with an
  icon ("📷 Photo"); a voice note is its transcript, or "Voice message".
* **Home's Messages tab** (:func:`chat_days`) lists the last day newest
  first under "Today" / "Yesterday" (the household's days): the face, the
  name, "new" and the clock time — absolute, so a stale board stays
  truthful — and the text, cut at :data:`CHAT_CHARS` on a word with a
  "tap to hear the rest" cue. Home's card (:func:`card_rows`) is the
  newest few of the last hours; a message an ``@here`` banner shows is
  left out of both.
* **The picture view** shows the sender's face with a 💬 badge and,
  under it, one short line (:attr:`MessageRow.caption`, cut by
  :func:`clip`) for a grown-up reading along.
* **Tap to speak:** ``say`` is the sentence ``dashboard.js`` hands to the
  browser's speech synthesis ("Anna says: …", "Anna sent a photo.") —
  rendered into a ``data-say`` attribute (escaped by Jinja): the whole
  message, read aloud to the child.

Mentions and links stay plain text: nothing on the wall is tappable but
the row itself. Pure: no I/O.

**Mentions** (:class:`~socialhome.domain.household_dashboard.DashboardMention`)
become :class:`MentionCard` values: the ``@here`` banner on Home, the
"Message for you" card on a reader page and the 💬 tile on a picture
page — each with a "Got it" form posting to ``/ack``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ..domain.household_dashboard import (
    MESSAGE_MAX_AGE,
    MESSAGES_SHOWN,
    DashboardMention,
    DashboardMessage,
    DashboardPerson,
)
from .clock import ago, hhmm
from .presenter import Fmt

__all__ = [
    "CAPTION_CHARS",
    "CHAT_CHARS",
    "KIND_ICON",
    "NEW_FOR",
    "ChatDay",
    "ChatRow",
    "MentionCard",
    "MessageRow",
    "card_rows",
    "chat_days",
    "clip",
    "clip_words",
    "mention_cards",
    "message_rows",
    "new_count",
]

#: A message this fresh is "new": an accent bar, a "new" chip and a
#: relative time.
NEW_FOR: Final = timedelta(minutes=15)

#: The icon before a message's text, by kind.
KIND_ICON: Final = {
    "text": "",
    "photo": "📷",
    "video": "🎬",
    "file": "📎",
    "voice": "🎤",
}

#: A picture-view caption is at most this many characters (CSS clips it
#: to one line on top; this keeps a long message out of the markup).
CAPTION_CHARS: Final = 40


#: The Messages tab shows at most this many characters of a message (cut
#: on a word); tap to speak still reads all of it.
CHAT_CHARS: Final = 220


def clip(text: str, limit: int = CAPTION_CHARS) -> str:
    """``text`` on one line (runs of whitespace become one space), cut
    to at most ``limit`` characters with a trailing "…"."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def clip_words(text: str, limit: int = CHAT_CHARS) -> tuple[str, bool]:
    """``text`` on one line, cut on a word boundary to at most ``limit``
    characters with a trailing "…" — and whether it was cut. A single
    word longer than that is cut where it must be."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line, False
    cut = line[: limit - 1]
    if line[limit - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…", True


@dataclass(slots=True, frozen=True)
class MessageRow:
    """One message as the wall renders it (plain text — the template
    escapes)."""

    id: str
    person: DashboardPerson
    #: Shown before ``text`` (``""`` for a plain text message).
    icon: str
    #: What Home shows.
    text: str
    #: What tap-to-speak reads.
    say: str
    new: bool
    #: "4 min ago" while new, else the clock time ("13:05").
    when: str
    #: ``sent_at`` as ISO 8601, for ``<time datetime>``.
    iso: str
    #: When it was sent (the household zone).
    sent_at: datetime

    @property
    def caption(self) -> str:
        """The picture view's one short line: icon and text, clipped."""
        return clip(f"{self.icon} {self.text}")


def _text(m: DashboardMessage, fmt: Fmt) -> str:
    if m.kind == "text" or (m.kind == "voice" and m.text):
        return m.text
    return fmt.t(f"dashboard.messages.{m.kind}")


def _say(m: DashboardMessage, name: str, fmt: Fmt) -> str:
    if m.text:  # a text message, or a voice note's transcript
        return fmt.t("dashboard.messages.say.text", name=name, text=m.text)
    return fmt.t(f"dashboard.messages.say.{m.kind}", name=name)


def message_rows(messages: Iterable[DashboardMessage], fmt: Fmt) -> list[MessageRow]:
    """The rows for ``messages``, in their order — a message whose
    sender isn't a shown person on this page is left out."""
    out: list[MessageRow] = []
    for m in messages:
        found = fmt.people((m.sender_id,))
        if not found:
            continue
        person = found[0]
        new = fmt.now - m.sent_at < NEW_FOR
        out.append(
            MessageRow(
                id=m.id,
                person=person,
                icon=KIND_ICON.get(m.kind, ""),
                text=_text(m, fmt),
                say=_say(m, person.display_name, fmt),
                new=new,
                when=ago(fmt.now, m.sent_at, fmt.t) if new else hhmm(m.sent_at),
                iso=m.sent_at.isoformat(),
                sent_at=m.sent_at,
            )
        )
    return out


def card_rows(
    rows: Sequence[MessageRow],
    now: datetime,
    *,
    hide: frozenset[str] = frozenset(),
) -> list[MessageRow]:
    """Home's messages card: the newest :data:`MESSAGES_SHOWN` of the last
    :data:`MESSAGE_MAX_AGE` (``rows`` newest first), minus those in
    ``hide`` (on an ``@here`` banner) — not backfilled, so the card
    shrinks while a banner is up."""
    fresh = [r for r in rows if now - r.sent_at <= MESSAGE_MAX_AGE]
    return [r for r in fresh[:MESSAGES_SHOWN] if r.id not in hide]


def new_count(rows: Iterable[MessageRow]) -> int:
    """How many of ``rows`` are new (under :data:`NEW_FOR` old) — the
    Messages tab badge. Time-based: a shared board has no read state."""
    return sum(1 for r in rows if r.new)


@dataclass(slots=True, frozen=True)
class ChatRow:
    """One row of Home's Messages tab (plain text — the template escapes)."""

    message: MessageRow
    #: What the row shows: the text cut at :data:`CHAT_CHARS`.
    text: str
    #: The text was cut: the row says "tap to hear the rest".
    rest: bool
    #: The clock time it was sent ("15:41") — always absolute.
    time: str
    #: What the live region says while it is read aloud.
    playing: str


@dataclass(slots=True, frozen=True)
class ChatDay:
    """The Messages tab's rows of one household day."""

    #: ``"today"`` or ``"yesterday"`` (its heading's id).
    key: str
    label: str
    rows: tuple[ChatRow, ...]


def chat_days(rows: Iterable[MessageRow], fmt: Fmt) -> list[ChatDay]:
    """``rows`` (newest first) grouped under "Today" and "Yesterday" by
    the household's day — only days with a message. The tab reads one
    day back, so anything older never shows."""
    zone = fmt.now.tzinfo
    days: dict[str, list[ChatRow]] = {"today": [], "yesterday": []}
    for r in rows:
        local = r.sent_at.astimezone(zone)
        age = (fmt.today - local.date()).days
        if age not in (0, 1):
            continue
        text, rest = clip_words(r.text)
        days["today" if age == 0 else "yesterday"].append(
            ChatRow(
                message=r,
                text=text,
                rest=rest,
                time=hhmm(local),
                playing=fmt.t("dashboard.messages.playing", name=r.person.display_name),
            )
        )
    labels = {
        "today": fmt.t("dashboard.home.today"),
        "yesterday": fmt.t("dashboard.messages.yesterday"),
    }
    return [
        ChatDay(key=key, label=labels[key], rows=tuple(found))
        for key, found in days.items()
        if found
    ]


@dataclass(slots=True, frozen=True)
class MentionCard:
    """One unacknowledged mention as the wall renders it (plain text)."""

    #: The household-chat message id (what the "Got it" form names).
    id: str
    #: ``None`` for an ``@here``; else the mentioned person (the form's
    #: ``person_id``).
    person_id: str | None
    #: The sender.
    person: DashboardPerson
    text: str
    #: What tap-to-speak reads.
    say: str
    #: "4 min ago".
    when: str
    iso: str

    @property
    def caption(self) -> str:
        """The picture view's one short line of the text."""
        return clip(self.text)

    @property
    def key(self) -> str:
        """Unique per mention on a page (``dashboard.js`` remembers which
        it has chimed for): ``<message id>:<person id | "here">``."""
        return f"{self.id}:{self.person_id or 'here'}"


def mention_cards(mentions: Iterable[DashboardMention], fmt: Fmt) -> list[MentionCard]:
    """The cards for ``mentions``, in their order — one whose sender
    isn't a shown person on this page is left out."""
    out: list[MentionCard] = []
    for m in mentions:
        found = fmt.people((m.sender_id,))
        if not found:
            continue
        person = found[0]
        out.append(
            MentionCard(
                id=m.message_id,
                person_id=m.person_id,
                person=person,
                text=m.text,
                say=fmt.t(
                    "dashboard.messages.say.text",
                    name=person.display_name,
                    text=m.text,
                ),
                when=ago(fmt.now, m.sent_at, fmt.t),
                iso=m.sent_at.isoformat(),
            )
        )
    return out
