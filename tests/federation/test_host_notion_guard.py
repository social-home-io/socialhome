"""Guard: "the host" is one notion — ``federation/host_equivalence.py``.

In the member directory, the host hop, the space sync and the missing-file
ask, "host" means the owner household OR the delegated admin household that
invited us (``acts_as_host`` / ``acting_hosts`` / ``serves_as_host``; owner
decision 2026-11-13). The member directory once compared against
``owner_instance_id`` alone, so a household an admin invited got no
directory at all.

This scan fails on any comparison with ``owner_instance_id`` in those
modules that is not listed below with the reason it is right there. A new
host check uses ``host_equivalence``; only an owner-only action (or "is
this household the owner itself") may compare, and then it is added here.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "socialhome"

#: The modules where "the host" decides whom we listen to, ask or hop
#: through.
GUARDED: tuple[str, ...] = (
    "services/space_member_directory.py",
    "services/space_media_wanted.py",
    "federation/host_hop.py",
    *sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "federation/sync/space").glob("*.py")
    ),
)

#: (module, function) → why comparing with ``owner_instance_id`` is right.
ALLOWED: dict[tuple[str, str], str] = {
    ("services/space_member_directory.py", "_owns"): (
        "are WE the owner: only the owner takes a card a delegated admin "
        "passes on, and a delegated admin passes cards on to it"
    ),
    ("services/space_member_directory.py", "_forward_to_owner"): (
        "the owner never passes a card on to itself"
    ),
    ("services/space_media_wanted.py", "_deliver"): (
        "are WE the owner: then the authors are asked; everybody else asks "
        "acting_hosts()"
    ),
    ("federation/sync/space/receiver.py", "_unpaired_provider_key"): (
        "which stored key verifies an unpaired provider: the owner's pinned "
        "host key, else that household's own — authentication, not authority"
    ),
    ("federation/sync/space/receiver.py", "_spare_owner_people"): (
        "a trusted inviting admin never touches the owner household's people"
    ),
    ("federation/sync/space/receiver.py", "_persist_page_tombstones"): (
        "are WE the owner: only the owner sequences pages (v_48)"
    ),
    ("federation/sync/space/receiver.py", "_acts_as_host"): (
        "the owner shortcut of acts_as_host itself (no binder → owner only)"
    ),
    ("federation/sync/space/resume.py", "_replay_pages"): (
        "owner-only: the owner sequences pages and only it ships page files"
    ),
    ("federation/sync/space/scheduler.py", "_on_invite_answered"): (
        "are WE the owner: nothing to catch up; targets come from acting_hosts()"
    ),
    ("federation/sync/space/scheduler.py", "_tick_mesh_announce"): (
        "are WE the owner: then every member household is announced to"
    ),
    ("federation/sync/space/scheduler.py", "_tick_mesh_catchup"): (
        "the authority-epoch echo is the owner's business alone"
    ),
}


def _owner_comparisons(module: str) -> set[tuple[str, str]]:
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        if isinstance(node, ast.Compare) and any(
            isinstance(part, ast.Attribute) and part.attr == "owner_instance_id"
            for part in (node.left, *node.comparators)
        ):
            found.add((module, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_host_checks_go_through_host_equivalence():
    found: set[tuple[str, str]] = set()
    for module in GUARDED:
        found |= _owner_comparisons(module)
    new = sorted(found - set(ALLOWED))
    assert not new, (
        "comparison with owner_instance_id in a module where 'the host' is "
        "the owner OR the delegated inviting admin — use "
        "federation/host_equivalence.py (acts_as_host / acting_hosts / "
        "serves_as_host), or add it to ALLOWED with the reason it is "
        f"owner-only: {new}"
    )
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"ALLOWED lists comparisons that are gone: {stale}"


def test_the_scan_sees_a_comparison(tmp_path, monkeypatch):
    """The scan itself: either side of ``==`` / ``!=`` / ``in``, nested."""
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    async def a(self, space, x):\n"
        "        return x == space.owner_instance_id\n"
        "    def b(self, space, x):\n"
        "        if space.owner_instance_id != x:\n"
        "            return [space.owner_instance_id]\n"
        "    def c(self, space):\n"
        "        return space.owner_instance_id\n"
    )
    monkeypatch.setattr(
        "tests.federation.test_host_notion_guard.ROOT", tmp_path, raising=True
    )
    assert _owner_comparisons("m.py") == {("m.py", "a"), ("m.py", "b")}


def test_the_directory_and_the_hop_are_guarded():
    assert "services/space_member_directory.py" in GUARDED
    assert "federation/host_hop.py" in GUARDED
    assert "federation/sync/space/scheduler.py" in GUARDED
