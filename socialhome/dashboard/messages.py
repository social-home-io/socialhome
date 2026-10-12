"""Household-chat messages on the wall — what a row or tile shows and says.

The read model hands over :class:`~socialhome.domain.household_dashboard.DashboardMessage`
values (only when the admin turned ``show_household_messages`` on); this
module turns each into a :class:`MessageRow` the templates render:

* **Home** shows a row: the sender's face, the text (clamped to two
  lines by CSS) and when — "new" + "4 min ago" for a message under
  :data:`NEW_FOR` old, else the clock time. Media are a word with an
  icon ("📷 Photo"); a voice note is its transcript, or "Voice message".
* **The picture view** shows only the sender's face with a 💬 badge.
* **Tap to speak:** ``say`` is the sentence ``dashboard.js`` hands to the
  browser's speech synthesis ("Anna says: …", "Anna sent a photo.") —
  rendered into a ``data-say`` attribute (escaped by Jinja), so the
  picture view keeps its no-words rule.

Mentions and links stay plain text: nothing on the wall is tappable but
the row itself. Pure: no I/O.

**Mentions** (:class:`~socialhome.domain.household_dashboard.DashboardMention`)
become :class:`MentionCard` values: the ``@here`` banner on Home, the
"Message for you" card on a reader page and the 💬 tile on a picture
page — each with a "Got it" form posting to ``/ack``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from ..domain.household_dashboard import (
    DashboardMention,
    DashboardMessage,
    DashboardPerson,
)
from .clock import ago, hhmm
from .presenter import Fmt

__all__ = [
    "KIND_ICON",
    "NEW_FOR",
    "MentionCard",
    "MessageRow",
    "mention_cards",
    "message_rows",
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
            )
        )
    return out


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
