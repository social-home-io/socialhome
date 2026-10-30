"""A listing's public questions and answers — the seller's FAQ (v_75).

The seller of a bazaar listing publishes short questions with their answers
on the listing, often taken from an "Ask the seller" thread and reworded.
Everyone who can see the listing sees them; nobody but the seller writes
them, and an entry never names who asked.

Pure helpers — no I/O:

* :class:`BazaarFaqEntry` — one entry as every household holds it.
* :func:`clean_question` / :func:`clean_answer` — the one text rule, used
  by the seller's household before it stores or sends and by every
  receiver before it stores: plain text, bounded, no control characters.
* :func:`is_entry_id` — the id shape the seller's household mints.
* The bounds (:data:`BAZAAR_FAQ_MAX_ENTRIES` …), enforced on both ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from .errors import CodedError

#: At most this many live entries on one listing (owner decision).
BAZAAR_FAQ_MAX_ENTRIES: int = 10

#: A question is one line of at most this many characters.
BAZAAR_FAQ_QUESTION_MAX_CHARS: int = 200

#: An answer is at most this many characters (line breaks allowed).
BAZAAR_FAQ_ANSWER_MAX_CHARS: int = 500

#: How many entries one listing ever holds, live or removed. A removal
#: leaves a blank marker for the life of the listing, so a late or replayed
#: edit of a removed entry can never bring it back on any household, however
#: many removals came after it. This bounds the markers without ever
#: forgetting one: at most 30 rows per listing, at most 10 of them live.
#: Rewording an entry doesn't count, only adding a new one. Every household
#: enforces it: the seller's on add (:class:`BazaarFaqUsedUpError`), a
#: receiver on a new entry id. A §25.6 ``faq`` record carries at most this
#: many items.
BAZAAR_FAQ_MAX_ENTRIES_EVER: int = 30

#: How many FAQ changes the seller's household sends for one listing per
#: window — a backstop: every change goes to every member household. The
#: receiver allows twice as many before it drops (a hostile sender).
BAZAAR_FAQ_RATE_CAP: tuple[timedelta, int] = (timedelta(hours=1), 30)

#: An entry id this household mints: ``uuid4().hex``.
_ENTRY_ID = re.compile(r"^[0-9a-f]{32}$")

#: C0 controls and DEL; a line feed is let through where a rule allows it.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

#: Three or more line breaks (with spaces between) collapse to one blank line.
_BLANK_LINES = re.compile(r"\n\s*\n(\s*\n)+")


@dataclass(slots=True, frozen=True)
class BazaarFaqEntry:
    """One question and its answer on one listing.

    ``rev`` is the seller's household's revision of this listing's FAQ when
    the entry last changed; a receiver keeps the highest. A ``removed``
    entry is a marker with blank text.
    """

    listing_post_id: str
    entry_id: str
    question: str
    answer: str
    position: int
    rev: int
    removed: bool = False


class BazaarFaqTextError(CodedError, ValueError):
    """A question or an answer that is empty, too long or not plain text."""

    status = 422
    code = "BAZAAR_FAQ_TEXT"
    detail = "A question needs 1–200 characters and an answer 1–500, as plain text."

    def __init__(self) -> None:
        super().__init__(
            params={
                "question_max": BAZAAR_FAQ_QUESTION_MAX_CHARS,
                "answer_max": BAZAAR_FAQ_ANSWER_MAX_CHARS,
            }
        )


class BazaarFaqFullError(CodedError):
    """The listing already shows :data:`BAZAAR_FAQ_MAX_ENTRIES` entries."""

    status = 409
    code = "BAZAAR_FAQ_FULL"
    detail = "A listing can show at most 10 questions."

    def __init__(self) -> None:
        super().__init__(params={"max": BAZAAR_FAQ_MAX_ENTRIES})


class BazaarFaqUsedUpError(CodedError):
    """The listing already had :data:`BAZAAR_FAQ_MAX_ENTRIES_EVER` entries,
    removed ones included: no new one, but a live one can be reworded."""

    status = 409
    code = "BAZAAR_FAQ_USED_UP"
    detail = "This listing already had 30 questions. Edit one of them instead."

    def __init__(self) -> None:
        super().__init__(params={"max": BAZAAR_FAQ_MAX_ENTRIES_EVER})


class BazaarFaqRateLimitedError(CodedError, PermissionError):
    """More FAQ changes on one listing than :data:`BAZAAR_FAQ_RATE_CAP`."""

    status = 429
    code = "BAZAAR_FAQ_RATE_LIMITED"
    detail = "Too many changes to this listing's questions. Wait a while."


class BazaarFaqEntryNotFoundError(CodedError, KeyError):
    """No such (live) entry on this listing."""

    status = 404
    code = "NOT_FOUND"
    detail = "Question not found."


def is_entry_id(value: object) -> bool:
    """``value`` is an entry id as the seller's household mints it."""
    return isinstance(value, str) and _ENTRY_ID.match(value) is not None


def clean_question(value: object) -> str:
    """The question as it is stored and sent: one line (every run of white
    space, line breaks included, becomes one space), trimmed, 1–200
    characters, no control characters. :class:`BazaarFaqTextError`
    otherwise."""
    if not isinstance(value, str):
        raise BazaarFaqTextError
    text = " ".join(value.split())
    if not text or len(text) > BAZAAR_FAQ_QUESTION_MAX_CHARS or _CONTROL.search(text):
        raise BazaarFaqTextError
    return text


def clean_answer(value: object) -> str:
    """The answer as it is stored and sent: ``\\r\\n`` / ``\\r`` become
    ``\\n``, a tab a space, more than one blank line collapses to one,
    trimmed, 1–500 characters, no control character but the line feed.
    :class:`BazaarFaqTextError` otherwise."""
    if not isinstance(value, str):
        raise BazaarFaqTextError
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if not text or len(text) > BAZAAR_FAQ_ANSWER_MAX_CHARS or _CONTROL.search(text):
        raise BazaarFaqTextError
    return text


#: The highest ``position`` / ``rev`` a receiver takes (an int64 column,
#: and far beyond what any seller's household reaches).
_MAX_COUNTER = 2**53


def entry_from_wire(listing_post_id: str, data: object) -> BazaarFaqEntry:
    """One entry as the seller's household sent it (an event payload or a
    §25.6 ``faq`` record item), checked exactly as that household checks
    its own: a well-formed id, a positive ``rev``, a sane ``position``,
    and for a live entry text :func:`clean_question` / :func:`clean_answer`
    leave unchanged (so what is stored is what was sent). A removal carries
    no text. :class:`ValueError` for anything else."""
    if not isinstance(data, dict):
        data = {}  # not an object: refused below as a malformed id
    entry_id = data.get("entry_id")
    rev = data.get("rev")
    position = data.get("position", 0)
    removed = data.get("removed", False)
    if not isinstance(entry_id, str) or not is_entry_id(entry_id):
        raise ValueError("malformed entry id")
    if (
        not _is_counter(rev, minimum=1)
        or not _is_counter(position, minimum=0)
        or not isinstance(removed, bool)
    ):
        raise ValueError("malformed rev / position / removed")
    assert isinstance(rev, int) and isinstance(position, int)
    if removed:
        return BazaarFaqEntry(
            listing_post_id=listing_post_id,
            entry_id=entry_id,
            question="",
            answer="",
            position=position,
            rev=rev,
            removed=True,
        )
    question = data.get("question")
    answer = data.get("answer")
    if clean_question(question) != question or clean_answer(answer) != answer:
        raise ValueError("question / answer is not clean plain text")
    assert isinstance(question, str) and isinstance(answer, str)
    return BazaarFaqEntry(
        listing_post_id=listing_post_id,
        entry_id=entry_id,
        question=question,
        answer=answer,
        position=position,
        rev=rev,
    )


def _is_counter(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value < _MAX_COUNTER
    )


def entry_to_wire(entry: BazaarFaqEntry) -> dict[str, object]:
    """The fields of one entry on the wire (event payload or sync record);
    a removal carries no text."""
    out: dict[str, object] = {
        "entry_id": entry.entry_id,
        "rev": entry.rev,
        "position": entry.position,
        "removed": entry.removed,
    }
    if not entry.removed:
        out["question"] = entry.question
        out["answer"] = entry.answer
    return out
