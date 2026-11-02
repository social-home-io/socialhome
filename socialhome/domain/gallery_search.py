"""Gallery search queries (owner decision D11).

A person finds photos and videos by what people wrote and did — never by
what is *in* the picture:

* **words** — a photo's caption, its album's name or description, or the
  name of who added it;
* **who added it** — ``von Lena`` / ``by Lena`` / ``door Lena`` /
  ``par Lena`` / ``por Lena`` / ``from Lena``: only photos Lena added;
* **month and year** — a month's name (en / de / nl / fr / es), a year,
  ``2025-07`` or ``07/2025``: the day the photo was taken, else the day
  it was uploaded.

There is no face, object or place recognition, now or later; the words
are only ever literal (``Sommer`` matches an album called "Sommer", it is
not a season). Pure parsing — no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .gallery import GalleryAlbum, GalleryItem

#: A query longer than this is cut (it is typed in a search field).
GALLERY_SEARCH_MAX_CHARS: int = 100
#: At most this many words are matched; the rest are ignored.
GALLERY_SEARCH_MAX_TERMS: int = 6
#: Shortest query that searches (the app's §23.2.5 rule).
GALLERY_SEARCH_MIN_CHARS: int = 2
#: Results per page, and the most one page may ask for.
GALLERY_SEARCH_PAGE: int = 60
GALLERY_SEARCH_PAGE_MAX: int = 200
#: Albums whose name matches, shown above the photos.
GALLERY_SEARCH_ALBUMS_MAX: int = 12
#: Characters of a caption shown around the first matching word.
SNIPPET_CHARS: int = 120

#: Words that say "added by" in the app's five languages. ``de`` / ``van``
#: are left out on purpose: they are everyday words and parts of names.
PERSON_WORDS: frozenset[str] = frozenset({"by", "from", "von", "door", "par", "por"})

_MONTHS: dict[str, int] = {}
for _n, _names in enumerate(
    (
        "january januar janner januari janvier enero",
        "february februar februari fevrier febrero",
        "march marz maerz maart mars marzo",
        "april avril abril",
        "may mai mei mayo",
        "june juni juin junio",
        "july juli juillet julio",
        "august augustus aout agosto",
        "september septembre septiembre setiembre",
        "october oktober octobre octubre",
        "november novembre noviembre",
        "december dezember decembre diciembre",
    ),
    start=1,
):
    for _name in _names.split():
        _MONTHS[_name] = _n

_YEAR = re.compile(r"^(19|20)\d\d$")
_YEAR_MONTH = re.compile(r"^((?:19|20)\d\d)-(0?[1-9]|1[0-2])$")
_MONTH_YEAR = re.compile(r"^(0?[1-9]|1[0-2])[/.]((?:19|20)\d\d)$")
_SPLIT = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)


def fold(text: str) -> str:
    """Lower case without accents — ``März`` → ``marz``, ``Août`` →
    ``aout`` — the folding FTS5's ``remove_diacritics`` applies too."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(slots=True, frozen=True)
class GalleryQuery:
    """A parsed search: every part must match (AND)."""

    #: Words to find in a caption, an album name / description or the
    #: name of who added the photo.
    terms: tuple[str, ...] = ()
    #: Names after "von" / "by" …: only photos these people added.
    people: tuple[str, ...] = ()
    year: int | None = None
    month: int | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.terms or self.people or self.year or self.month)

    @property
    def has_date(self) -> bool:
        return self.year is not None or self.month is not None


def parse_gallery_query(raw: str) -> GalleryQuery:
    """Split what someone typed into words, people, a month and a year.

    ``"Urlaub von Lena Juli 2025"`` → terms ``("Urlaub",)``, people
    ``("Lena",)``, month 7, year 2025. A "von" with nothing after it is a
    word like any other. Months and years are literal only."""
    text = (raw or "").strip()[:GALLERY_SEARCH_MAX_CHARS]
    tokens = [t for t in _SPLIT.split(text) if t]
    terms: list[str] = []
    people: list[str] = []
    year: int | None = None
    month: int | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        key = fold(token).strip(".,;:!?\"'()")
        if key in PERSON_WORDS and i + 1 < len(tokens):
            people.append(tokens[i + 1].strip(".,;:!?\"'()"))
            i += 2
            continue
        if _YEAR.match(key):
            year = int(key)
        elif m := _YEAR_MONTH.match(key):
            year, month = int(m.group(1)), int(m.group(2))
        elif m := _MONTH_YEAR.match(key):
            month, year = int(m.group(1)), int(m.group(2))
        elif key in _MONTHS:
            month = _MONTHS[key]
        elif _WORD.search(token):
            terms.append(token)
        i += 1
    return GalleryQuery(
        terms=tuple(terms[:GALLERY_SEARCH_MAX_TERMS]),
        people=tuple(p for p in people if _WORD.search(p))[:GALLERY_SEARCH_MAX_TERMS],
        year=year,
        month=month,
    )


def fts_prefix_query(term: str) -> str:
    """One word as an FTS5 prefix phrase — quoted, so nothing typed is an
    FTS operator (``"som"*`` finds "Sommer" and "Sommerfest")."""
    return '"' + term.replace('"', '""') + '"*'


def name_matches(name: str | None, term: str) -> bool:
    """Does a word of *name* start with *term* (folded)? ``Lena`` matches
    "Lena Müller" and "lena", ``mül`` matches "Lena Müller"."""
    want = fold(term)
    if not want or not name:
        return False
    return any(w.startswith(want) for w in _WORD.findall(fold(name)))


def snippet(caption: str | None, terms: tuple[str, ...]) -> str:
    """The caption around its first matching word, each match wrapped in
    ``<mark>…</mark>`` — the shape ``GET /api/search`` snippets have. Built
    for a row the person may see, after the search filtered (a caption
    never leaves through a snippet of a row they may not see). The text
    is the caption as typed: clients render it as text, never HTML."""
    text = " ".join((caption or "").split())
    if not text:
        return ""
    folded = fold(text)
    if len(folded) != len(text):
        # A letter that folds to several (ß → ss): no positions to map.
        return text[:SNIPPET_CHARS]
    wants = [fold(t) for t in terms if fold(t)]
    spans: list[tuple[int, int]] = []
    for m in _WORD.finditer(folded):
        if any(m.group().startswith(w) for w in wants):
            spans.append((m.start(), m.end()))
    start = 0
    if spans and spans[0][0] > SNIPPET_CHARS // 3 and len(text) > SNIPPET_CHARS:
        start = spans[0][0] - SNIPPET_CHARS // 3
    end = min(len(text), start + SNIPPET_CHARS)
    out: list[str] = ["…" if start > 0 else ""]
    pos = start
    for s, e in spans:
        if s < start or e > end:
            continue
        out.append(text[pos:s])
        out.append("<mark>" + text[s:e] + "</mark>")
        pos = e
    out.append(text[pos:end])
    if end < len(text):
        out.append("…")
    return "".join(out)


@dataclass(slots=True, frozen=True)
class GallerySearchHit:
    """One photo or video found, with its album as the person sees it."""

    item: GalleryItem
    album: GalleryAlbum
    #: The caption around what matched (``<mark>``), or ``""``.
    snippet: str = ""


@dataclass(slots=True, frozen=True)
class GallerySearchResult:
    """A page of a gallery search."""

    items: tuple[GallerySearchHit, ...] = ()
    #: Albums whose name or description matches every word (first page
    #: only, and only for a search of words alone).
    albums: tuple[GalleryAlbum, ...] = ()
    #: Another page follows.
    more: bool = False


@dataclass(slots=True, frozen=True)
class GallerySearchWord:
    """One word of a search, ready for the repository: its FTS5 prefix
    phrase and the people whose name it matches."""

    fts: str
    uploader_ids: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class GallerySearchSpec:
    """What the repository searches, for one viewer.

    ``household`` — include the household albums the viewer may see (open
    to everyone at home, their own, a private one they were chosen for);
    ``space_ids`` — the spaces whose albums they may see. Every word must
    match a caption, the album's name / description or who added it;
    ``uploader_ids`` (``None`` = anyone) narrows to who added it; ``year`` /
    ``month`` to the day taken, else uploaded."""

    viewer_user_id: str
    household: bool
    space_ids: frozenset[str] = frozenset()
    words: tuple[GallerySearchWord, ...] = ()
    uploader_ids: frozenset[str] | None = None
    year: int | None = None
    month: int | None = None
