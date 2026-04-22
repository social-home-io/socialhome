"""Full-text search repository — wraps the FTS5 ``search_index`` virtual table.

The index is contentless from the user's perspective: callers don't need
to know about FTS5 syntax. The repo accepts plain text queries and a
``scope`` filter, returning hit rows for the service layer to hydrate
from the canonical source tables.

Why a single unified index: searching across posts, space posts, page
bodies and DM messages from one query is the common UX. Having one
index also keeps schema migrations small and the index size bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts


SCOPE_POST = "post"
SCOPE_SPACE_POST = "space_post"
SCOPE_MESSAGE = "message"
SCOPE_PAGE = "page"
SCOPE_USER = "user"  # §23.2 — "People" filter (local + known remote)
SCOPE_SPACE = "space"  # §23.2 — "Spaces" filter
ALLOWED_SCOPES = frozenset(
    {
        SCOPE_POST,
        SCOPE_SPACE_POST,
        SCOPE_MESSAGE,
        SCOPE_PAGE,
        SCOPE_USER,
        SCOPE_SPACE,
    }
)


#: Scope groups — the public API exposes these higher-level "types".
#: Callers pass ``type="posts"`` and the service expands to the two
#: underlying scopes (`post` + `space_post`).
SCOPE_TYPE_GROUPS: dict[str, frozenset[str]] = {
    "posts": frozenset({SCOPE_POST, SCOPE_SPACE_POST}),
    "people": frozenset({SCOPE_USER}),
    "spaces": frozenset({SCOPE_SPACE}),
    "pages": frozenset({SCOPE_PAGE}),
    "messages": frozenset({SCOPE_MESSAGE}),
}


@dataclass(slots=True, frozen=True)
class SearchHit:
    """One row from a search query."""

    scope: str
    ref_id: str
    space_id: str | None
    title: str
    snippet: str
    #: FTS5 raw rank (lower = better; negative). Used for ordering +
    #: composing with recency / access boosts in the service layer.
    rank: float = 0.0


@runtime_checkable
class AbstractSearchRepo(Protocol):
    async def upsert(
        self,
        *,
        scope: str,
        ref_id: str,
        space_id: str | None,
        title: str,
        body: str,
    ) -> None: ...

    async def delete(self, *, scope: str, ref_id: str) -> None: ...

    async def search(
        self,
        query: str,
        *,
        scopes: frozenset[str] | None = None,
        space_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SearchHit]: ...

    async def count_by_scope(
        self,
        query: str,
        *,
        space_id: str | None = None,
    ) -> dict[str, int]: ...


class SqliteSearchRepo:
    """SQLite FTS5 implementation of :class:`AbstractSearchRepo`.

    User input is treated as a literal phrase by quoting (no FTS5
    operator injection). The query is normalised to lowercase before
    sending to FTS5; the unicode61 tokeniser handles diacritic folding.
    """

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        scope: str,
        ref_id: str,
        space_id: str | None,
        title: str,
        body: str,
    ) -> None:
        if scope not in ALLOWED_SCOPES:
            raise ValueError(f"Invalid scope: {scope!r}")
        # FTS5 has no UPSERT — delete then insert is the idiomatic pattern.
        await self._db.enqueue(
            "DELETE FROM search_index WHERE scope=? AND ref_id=?",
            (scope, ref_id),
        )
        await self._db.enqueue(
            "INSERT INTO search_index(scope, ref_id, space_id, title, body)"
            " VALUES(?,?,?,?,?)",
            (scope, ref_id, space_id, title or "", body or ""),
        )

    async def delete(self, *, scope: str, ref_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM search_index WHERE scope=? AND ref_id=?",
            (scope, ref_id),
        )

    async def search(
        self,
        query: str,
        *,
        scopes: frozenset[str] | None = None,
        space_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        # Quote to neutralise FTS5 operators — treat input as a phrase.
        # Escape embedded double-quotes per FTS5 spec.
        safe = '"' + query.replace('"', '""') + '"'
        limit = max(1, min(limit, 100))
        offset = max(0, int(offset))

        clauses = ["search_index MATCH ?"]
        params: list = [safe]
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            clauses.append(f"scope IN ({placeholders})")
            params.extend(sorted(scopes))
        if space_id:
            clauses.append("space_id = ?")
            params.append(space_id)

        sql = (
            "SELECT scope, ref_id, space_id, title,"
            " snippet(search_index, 4, '<mark>', '</mark>', '...', 16) AS snippet,"
            " rank"
            f" FROM search_index WHERE {' AND '.join(clauses)}"
            " ORDER BY rank LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = await self._db.fetchall(sql, tuple(params))
        return [
            SearchHit(
                scope=r["scope"],
                ref_id=r["ref_id"],
                space_id=r["space_id"],
                title=r["title"] or "",
                snippet=r["snippet"] or "",
                rank=float(r["rank"]) if r.get("rank") is not None else 0.0,
            )
            for r in rows_to_dicts(rows)
        ]

    async def count_by_scope(
        self,
        query: str,
        *,
        space_id: str | None = None,
    ) -> dict[str, int]:
        """Return ``{scope: count}`` for a query.

        Used so the response can drive per-type empty-state messages
        without running N separate searches. Counts respect the
        ``space_id`` filter but not the scope filter — the point is to
        know how many hits each scope has so the UI can label the
        filter chips.
        """
        q = (query or "").strip()
        if not q:
            return {}
        safe = '"' + q.replace('"', '""') + '"'
        clauses = ["search_index MATCH ?"]
        params: list = [safe]
        if space_id:
            clauses.append("space_id = ?")
            params.append(space_id)
        sql = (
            "SELECT scope, COUNT(*) AS n FROM search_index"
            f" WHERE {' AND '.join(clauses)} GROUP BY scope"
        )
        rows = await self._db.fetchall(sql, tuple(params))
        return {r["scope"]: int(r["n"]) for r in rows_to_dicts(rows)}
