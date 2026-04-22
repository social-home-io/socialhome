"""@-mention parsing (§23.42).

:class:`MentionParser` is a pure domain utility. It scans post content for
``@here`` and ``@username`` tokens and yields typed :class:`Mention` values.

The parser has no I/O dependency — callers inject a ``lookup_member``
callable that resolves a token to a ``user_id``. The lookup is responsible
for priority-ordered matching (username → display-name → alias) and for
returning ``None`` when the token is ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class MentionType(StrEnum):
    HERE = "here"  # @here — broadcast to all members of the current scope
    USER = "user"  # @username / @DisplayName — specific user


@dataclass(slots=True, frozen=True)
class Mention:
    type: MentionType
    raw: str  # raw token as written, e.g. "@Anna"
    user_id: str | None  # resolved user_id; None for HERE


# A token is ``@`` followed by ``here`` or an identifier up to 30 chars of
# word / whitespace. The lookahead stops at whitespace, sentence terminators
# or end-of-string so the token itself doesn't absorb trailing punctuation.
_MENTION_RE = re.compile(
    r"@(here|\w[\w\s]{0,30}?)(?=\s|$|[,\.!?])",
    re.UNICODE,
)


#: Lookup signature: ``lookup(token, scope_id) -> user_id | None``.
LookupMember = Callable[[str, str], "str | None"]


class MentionParser:
    """Pure domain utility for resolving @-tokens in post content.

    ``lookup_member`` is injected so the parser has no database dependency.
    It must return:

    * the matching ``user_id`` (string) for an unambiguous lookup, or
    * ``None`` when the token did not match anything or matched more than
      one person (the spec explicitly prefers silence over notifying the
      wrong user — §23.42).

    If a post contains ``@here`` the parser short-circuits and returns a
    single-element tuple — individual @-mentions after ``@here`` are
    redundant.
    """

    __slots__ = ("_lookup",)

    def __init__(self, lookup_member: LookupMember) -> None:
        self._lookup = lookup_member

    def parse(self, content: str, scope_id: str) -> tuple[Mention, ...]:
        if not content:
            return ()

        mentions: list[Mention] = []
        seen_user_ids: set[str] = set()

        for m in _MENTION_RE.finditer(content):
            token = m.group(1).strip()
            if token.lower() == "here":
                return (Mention(type=MentionType.HERE, raw="@here", user_id=None),)
            user_id = self._lookup(token, scope_id)
            if user_id and user_id not in seen_user_ids:
                seen_user_ids.add(user_id)
                mentions.append(
                    Mention(
                        type=MentionType.USER,
                        raw=f"@{token}",
                        user_id=user_id,
                    )
                )

        return tuple(mentions)
