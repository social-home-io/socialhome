"""Specification pattern — composable filters for repo list/search reads.

A :class:`Spec` captures a list query in three pieces — ``where``,
``order_by``, and ``limit/offset`` — that the repo translates into SQL
via :func:`spec_to_sql`. Every column referenced in the spec is checked
against an ``allowed_cols`` allow-list before being interpolated into
the SQL string, so callers cannot inject arbitrary identifiers.

Why bother? Repos accumulate one bespoke ``list_*`` method per call site
(filter by author, by since-date, by space, …). The Specification
pattern lets the caller compose those filters declaratively without the
repo having to expose an N×M cartesian of methods. Existing bespoke
methods stay (they're shorter at the call site) — Spec is the escape
hatch when a caller needs an unusual combination.

The implementation is intentionally tiny — a dataclass + one helper. We
do not aim to support arbitrary AND/OR trees, JOINs, or aggregates;
those still belong in dedicated repo methods.

Example::

    spec = Spec(
        where=[("author", "=", "alice"), ("created_at", ">=", since)],
        order_by=[("created_at", "DESC")],
        limit=20,
    )
    sql, params = spec_to_sql(
        spec, table="feed_posts",
        allowed_cols={"author", "created_at"},
    )
    rows = await db.fetchall(f"SELECT * FROM feed_posts {sql}", params)
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: SQL operators we accept. A tighter allow-list than "anything" is
#: deliberate — typos like ``=!`` would otherwise produce confusing
#: errors. ``IN``/``NOT IN`` would need parameter expansion logic;
#: keep them out of v1.
_ALLOWED_OPS: frozenset[str] = frozenset(
    {
        "=",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "LIKE",
        "IS",
        "IS NOT",
    }
)

_ALLOWED_DIRECTIONS: frozenset[str] = frozenset({"ASC", "DESC"})


@dataclass(slots=True, frozen=True)
class Spec:
    """A read query: where + order + paginate."""

    #: List of ``(column, op, value)`` triples joined with AND.
    where: list[tuple[str, str, object]] = field(default_factory=list)

    #: List of ``(column, direction)`` pairs.
    order_by: list[tuple[str, str]] = field(default_factory=list)

    #: ``None`` means "no limit"; ``0`` is treated the same way.
    limit: int | None = None

    offset: int = 0


def spec_to_sql(
    spec: Spec,
    *,
    table: str,
    allowed_cols: set[str] | frozenset[str],
) -> tuple[str, tuple]:
    """Convert *spec* into a ``WHERE … ORDER BY … LIMIT … OFFSET …`` fragment.

    Returns ``(sql_fragment, params)`` — the fragment includes the
    leading whitespace and the keywords, so callers concatenate it
    after a base ``SELECT … FROM <table>``.

    *table* is currently informational (used only in error messages);
    the spec doesn't reference it. *allowed_cols* gates every column
    name appearing in ``where`` / ``order_by`` to defeat injection.
    """
    parts: list[str] = []
    params: list[object] = []

    if spec.where:
        clauses: list[str] = []
        for col, op, value in spec.where:
            if col not in allowed_cols:
                raise ValueError(
                    f"column {col!r} not allowed for {table!r} "
                    f"(allowed: {sorted(allowed_cols)})"
                )
            op_upper = op.upper()
            if op_upper not in _ALLOWED_OPS:
                raise ValueError(
                    f"operator {op!r} not allowed (allowed: {sorted(_ALLOWED_OPS)})"
                )
            clauses.append(f"{col} {op_upper} ?")
            params.append(value)
        parts.append("WHERE " + " AND ".join(clauses))

    if spec.order_by:
        order_parts: list[str] = []
        for col, direction in spec.order_by:
            if col not in allowed_cols:
                raise ValueError(f"order_by column {col!r} not allowed for {table!r}")
            dir_upper = direction.upper()
            if dir_upper not in _ALLOWED_DIRECTIONS:
                raise ValueError(f"order direction {direction!r} not allowed")
            order_parts.append(f"{col} {dir_upper}")
        parts.append("ORDER BY " + ", ".join(order_parts))

    if spec.limit:
        parts.append("LIMIT ?")
        params.append(int(spec.limit))
        if spec.offset:
            parts.append("OFFSET ?")
            params.append(int(spec.offset))

    return " ".join(parts), tuple(params)
