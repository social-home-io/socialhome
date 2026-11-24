"""§27.9 release blocker: a household never tells a GFS which space it follows.

Before v_96 a follower took an identity-bound, signed ``POST /gfs/subscribe``
seat per space (and dropped it with ``/gfs/unsubscribe``); the request alone
told the operator "this household follows that space", so it had to be scoped
to the one server that seated it. Since v_96 a follower is a member with the
read-only role and GFS-borne content rides the opaque channel (seats keyed by
channel id, a pass bound to the household key, no space id), so the household
side of the identity-bound subscribe is gone entirely.

This guard proves the removal: no household module (anything under
``socialhome/`` but the GFS itself, ``socialhome/global_server/``, which still
serves the route until P7) builds a request to either route, and the
connection service carries no subscribe sender. A re-introduced sender fails
here before it can leak an interest to an operator.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security

_PKG = Path(__file__).resolve().parents[2] / "socialhome"
_ROUTES = ("/gfs/subscribe", "/gfs/unsubscribe")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings (prose, not requests)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                out.add(id(body[0].value))
    return out


def _household_request_literals() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG.parent)
        if rel.parts[1] == "global_server":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and any(r in node.value for r in _ROUTES)
            ):
                hits.append((str(rel), node.lineno, node.value))
    return hits


def test_no_household_module_builds_a_gfs_subscribe_request() -> None:
    assert _household_request_literals() == []


def test_the_scan_sees_an_f_string_route() -> None:
    """The scan reads f-string parts too (``f"{base}/gfs/subscribe"``)."""
    tree = ast.parse('url = f"{base}/gfs/subscribe"')
    assert any(
        isinstance(n, ast.Constant) and "/gfs/subscribe" in str(n.value)
        for n in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "name",
    ["subscribe_to_gfs_space", "unsubscribe_from_gfs_space"],
)
def test_the_connection_service_has_no_identity_bound_subscribe(name: str) -> None:
    assert not hasattr(GfsConnectionService, name)
