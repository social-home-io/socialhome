"""Tripwire — every write to a space-content table carries a space scope.

Issue #693: the §24.11 gates judge a sender against ONE space id, but the
content handlers used to mutate rows by bare row id, so a household with a
seat in space A could name a row of space B and have the write land there.
The fix puts the predicate in the repositories; this file is the guard that
keeps it there when someone adds mutator number forty-five.

It is deliberately *structural* rather than behavioural: the per-repo test
modules already drive a two-space fixture through each mutator, which
proves the predicates work today. What they cannot prove is that a NEW
mutator was written with one. So this walks the SQL string literals in each
space-content repository and asserts that every ``INSERT`` / ``UPDATE`` /
``DELETE`` naming one of the space-content tables mentions ``space_id``
somewhere in the same statement — as a column of its own, or inside the
``EXISTS`` that resolves a child row's parent.

A failure here is not a style nit: it is a statement that can write across
the space boundary. Fix the statement, don't extend the allow-list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import socialhome.repositories as repositories_pkg

#: Tables holding content that belongs to exactly one space. Writing a row
#: of one of these without naming the space is the #693 bug.
SPACE_CONTENT_TABLES = frozenset(
    {
        "space_posts",
        "space_post_comments",
        "space_tasks",
        "space_task_lists",
        "space_pages",
        "stickies",
        "space_calendar_events",
        "space_calendar_rsvps",
        "space_polls",
        "space_poll_options",
        "space_poll_votes",
        "space_schedule_poll_meta",
        "space_schedule_slots",
        "space_schedule_responses",
        "gallery_items",
        "space_zones",
        "bazaar_listings",
        "bazaar_bids",
    }
)

#: The repository modules that own those tables.
REPO_MODULES = (
    "space_post_repo",
    "task_repo",
    "page_repo",
    "sticky_repo",
    "calendar_repo",
    "space_poll_repo",
    "gallery_repo",
    "space_zone_repo",
    "bazaar_repo",
)

_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][\w]*)",
    re.IGNORECASE,
)

_REPO_DIR = Path(repositories_pkg.__file__).parent


def _sql_literals(module_name: str) -> list[tuple[str, str]]:
    """Every string constant in the module that looks like SQL.

    Returns ``(enclosing function name, statement text)`` pairs with
    whitespace collapsed, so a triple-quoted statement reads as one line.
    """
    source = (_REPO_DIR / f"{module_name}.py").read_text()
    tree = ast.parse(source)
    found: list[tuple[str, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            outer, self.scope = self.scope, node.name
            self.generic_visit(node)
            self.scope = outer

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            outer, self.scope = self.scope, node.name
            self.generic_visit(node)
            self.scope = outer

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and _WRITE_RE.search(node.value):
                found.append((self.scope, " ".join(node.value.split())))

    _Visitor().visit(tree)
    return found


def _offending_statements(module_name: str) -> list[tuple[str, str, str]]:
    """``(function, table, statement)`` for every unscoped space write."""
    bad: list[tuple[str, str, str]] = []
    for scope, sql in _sql_literals(module_name):
        for table in _WRITE_RE.findall(sql):
            if table.lower() not in SPACE_CONTENT_TABLES:
                continue
            if "space_id" in sql:
                continue
            bad.append((scope, table, sql))
    return bad


@pytest.mark.parametrize("module_name", REPO_MODULES)
def test_every_space_content_write_names_the_space(module_name: str) -> None:
    offenders = _offending_statements(module_name)
    assert not offenders, (
        f"{module_name}.py writes a space-content table without naming "
        "space_id — a sender gated for one space could land this write in "
        "another (#693). Scope the statement with `AND space_id = ?`, or, "
        "for a child row, an EXISTS on its parent:\n"
        + "\n".join(f"  {fn}(): {table} ← {sql}" for fn, table, sql in offenders)
    )


def test_the_tripwire_actually_catches_an_unscoped_statement() -> None:
    """The detector must fail on the pre-#693 shape, or it guards nothing."""
    pre_fix = "UPDATE space_posts SET deleted=1 WHERE id=?"
    assert _WRITE_RE.search(pre_fix)
    assert "space_id" not in pre_fix

    scoped = "UPDATE space_posts SET deleted=1 WHERE id=? AND space_id=?"
    assert "space_id" in scoped

    via_parent = (
        "UPDATE space_post_comments SET deleted=1 WHERE id=? AND EXISTS ("
        "SELECT 1 FROM space_posts WHERE id=space_post_comments.post_id "
        "AND space_id=?)"
    )
    assert "space_id" in via_parent


def test_the_table_list_matches_the_schema() -> None:
    """Every table we guard must actually exist, so a rename can't silently
    empty the list."""
    schema = (Path(repositories_pkg.__file__).parent.parent / "migrations").glob(
        "*.sql"
    )
    ddl = "\n".join(p.read_text() for p in schema)
    missing = [
        t for t in SPACE_CONTENT_TABLES if f"CREATE TABLE IF NOT EXISTS {t}" not in ddl
    ]
    assert not missing, f"guarded tables absent from the schema: {missing}"
