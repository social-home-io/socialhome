"""@-mention parsing (§23.42).

:class:`MentionParser` is a pure domain utility. It scans post / comment
content for ``@here`` and ``@handle`` tokens and yields typed
:class:`Mention` values.

The parser has no I/O dependency — callers inject a ``lookup_member``
callable that resolves a token to a ``user_id`` and returns ``None`` when
the token is unknown or ambiguous (silence over notifying the wrong
person). :func:`candidate_lookup` builds that callable from a scope's
member list (:class:`MentionCandidate` rows); :func:`mention_tokens` picks,
per member, the exact token a composer should insert so it resolves back to
that member and nobody else.

Token grammar::

    @<base>            base = \\w[\\w.-]*   (trailing '.'/'-' is punctuation)
    @<base>@<prefix>   prefix = leading chars of the member's user_id

A ``base`` matches a member's public ``handle`` or login username
(case-insensitive). When two members of the same space share a base (two
households each have an ``anna``) the bare ``@anna`` resolves to nobody and
the composer inserts the qualified ``@anna@k3f9x2`` instead — ``user_id`` is
global, so every member household resolves the qualified token the same
way against its own member view. A token is only ever resolved against the
space's members, so a mention can never reach a non-member.

``@here`` is recognised (:attr:`MentionType.HERE`); whether it may page
everyone is decided per space and per author by
:meth:`~socialhome.services.space_mentions.SpaceMentionResolver.may_use_here`
(space toggle ``allow_here_mention`` + owner/admin role).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum


class MentionType(StrEnum):
    HERE = "here"  # @here — broadcast to all members of the current scope
    USER = "user"  # @handle — specific user


@dataclass(slots=True, frozen=True)
class Mention:
    type: MentionType
    raw: str  # raw token as written, e.g. "@anna"
    user_id: str | None  # resolved user_id; None for HERE


@dataclass(slots=True, frozen=True)
class MentionCandidate:
    """One member a token may resolve to.

    ``handles`` lists the names a token may match, most-preferred first:
    the public ``handle``, then the login / remote username. Order matters
    only for :func:`mention_tokens` (the first token-safe name is the one a
    composer inserts); :func:`candidate_lookup` matches any of them.
    """

    user_id: str
    handles: tuple[str, ...]


# ``@`` not glued to a preceding word char or ``@`` (so ``bob@example.com``
# is not a mention), a base of word chars / ``.`` / ``-``, and an optional
# ``@prefix`` qualifier of word chars.
_MENTION_RE = re.compile(r"(?<![\w@])@(\w[\w.\-]{0,63})(?:@(\w{1,64}))?")
_BASE_RE = re.compile(r"\w(?:[\w.\-]{0,62}\w)?")
_QUALIFIER_RE = re.compile(r"\w+")
_HERE = "here"
#: Shortest user_id prefix used to qualify a colliding base.
_MIN_QUALIFIER = 6

#: Lookup signature: ``lookup(token, scope_id) -> user_id | None``.
LookupMember = Callable[[str, str], "str | None"]


class MentionParser:
    """Resolve @-tokens in content via an injected ``lookup_member``.

    ``lookup_member`` must return the matching ``user_id`` for an
    unambiguous token, or ``None`` when it matched nothing or more than one
    person (§23.42 prefers silence over notifying the wrong user).

    ``@here`` yields one :attr:`MentionType.HERE` entry (first) and parsing
    continues, so ``@here @anna`` still carries anna's user mention.
    """

    __slots__ = ("_lookup",)

    def __init__(self, lookup_member: LookupMember) -> None:
        self._lookup = lookup_member

    def parse(self, content: str, scope_id: str) -> tuple[Mention, ...]:
        if not content:
            return ()

        here: Mention | None = None
        mentions: list[Mention] = []
        seen_user_ids: set[str] = set()

        for m in _MENTION_RE.finditer(content):
            raw_base = m.group(1)
            base = raw_base.rstrip(".-")
            if not base:
                continue
            # A qualifier only binds to an unpunctuated base (``@anna.@x``
            # is "@anna" + text, not a qualified token).
            qualifier = m.group(2) if base == raw_base else None
            if qualifier is None and base.casefold() == _HERE:
                if here is None:
                    here = Mention(type=MentionType.HERE, raw="@here", user_id=None)
                continue
            token = f"{base}@{qualifier}" if qualifier else base
            user_id = self._lookup(token, scope_id)
            if user_id and user_id not in seen_user_ids:
                seen_user_ids.add(user_id)
                mentions.append(
                    Mention(type=MentionType.USER, raw=f"@{token}", user_id=user_id)
                )

        return ((here,) if here else ()) + tuple(mentions)


def _is_token_safe(name: str) -> bool:
    return bool(_BASE_RE.fullmatch(name)) and name.casefold() != _HERE


def _keys(candidate: MentionCandidate) -> set[str]:
    return {h.casefold() for h in candidate.handles if h}


def candidate_lookup(candidates: Iterable[MentionCandidate]) -> LookupMember:
    """Build a ``lookup_member`` over *candidates* (one scope's members).

    ``base`` matches any of a candidate's handles case-insensitively; a
    ``base@prefix`` token additionally requires the user_id to start with
    ``prefix``. Exactly one match → its user_id; zero or several → ``None``.
    """
    indexed = [(c.user_id, _keys(c)) for c in candidates]

    def lookup(token: str, _scope_id: str) -> str | None:
        base, _, qualifier = token.partition("@")
        key = base.casefold()
        if not key:
            return None
        hits = {uid for uid, keys in indexed if key in keys}
        if qualifier:
            q = qualifier.casefold()
            hits = {uid for uid in hits if uid.casefold().startswith(q)}
        return next(iter(hits)) if len(hits) == 1 else None

    return lookup


def mention_tokens(candidates: Iterable[MentionCandidate]) -> dict[str, str | None]:
    """Map each candidate's user_id to the token (without ``@``) a composer
    should insert so it resolves back to exactly that member.

    The first token-safe handle is the base. If another candidate also
    answers to that base, the token is qualified with the shortest user_id
    prefix (≥ :data:`_MIN_QUALIFIER` chars) no other such candidate shares.
    ``None`` when no handle fits the grammar or no word-char prefix is
    unique — the member can't be mentioned by token.
    """
    cands = list(candidates)
    keyed = [(c, _keys(c)) for c in cands]
    out: dict[str, str | None] = {}
    for cand in cands:
        base = next((h for h in cand.handles if h and _is_token_safe(h)), None)
        if base is None:
            out[cand.user_id] = None
            continue
        key = base.casefold()
        rivals = [
            c.user_id.casefold()
            for c, keys in keyed
            if c.user_id != cand.user_id and key in keys
        ]
        if not rivals:
            out[cand.user_id] = base
            continue
        out[cand.user_id] = _qualified(base, cand.user_id, rivals)
    return out


def _qualified(base: str, user_id: str, rivals: list[str]) -> str | None:
    for n in range(min(_MIN_QUALIFIER, len(user_id)), len(user_id) + 1):
        prefix = user_id[:n]
        if not _QUALIFIER_RE.fullmatch(prefix):
            return None
        p = prefix.casefold()
        if not any(r.startswith(p) for r in rivals):
            return f"{base}@{prefix}"
    return None
