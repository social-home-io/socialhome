"""Every space member is equal, whichever way their household is reached.

CLAUDE.md ("Federation & Security", owner decision 2026-11-06): a member,
moderator, admin or owner gets the same content and the same rights whether
their household is paired with the host, reached over the mesh, or seated
from an invite link and reached only through the GFS relay
(``InstanceSource.SPACE_SESSION``). Only followers may get a subset.

The few places that may still tell a GFS-reached household apart are listed
below, each with the reason it is allowed: the seat itself (how it is made
and torn down), the transport (how bytes reach it), the inbound validator
(what a seat may send), the member directory, exclusions from NON-space
surfaces (DMs, pairing, moments: a seat is no social relationship), the
follower subset, and the GFS media cache (how live content's files get to a
reader of that cache). A new module that branches on "reached through a
GFS" fails here — make the feature reach every member alike instead, or,
if it is genuinely one of those categories, add it with its reason.

The scan reads code only (an AST walk over names, attributes, parameters,
keywords and exact string constants), never comments or docstrings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "socialhome"

#: Allowed reason categories.
_CATEGORIES = frozenset(
    {
        "seat",
        "transport",
        "validator",
        "directory",
        "non-space exclusion",
        "follower",
        "GFS cache (live content)",
    }
)

#: Per marker: module → (category, one-line reason). EXACT: a module that
#: stops referencing a marker must be removed from its list too.
_ALLOWED: dict[str, dict[str, tuple[str, str]]] = {
    # ``InstanceSource.SPACE_SESSION`` — the seat row's source.
    "SPACE_SESSION": {
        "socialhome/domain/federation.py": (
            "validator",
            "defines InstanceSource and the seat's event vocabulary",
        ),
        "socialhome/repositories/federation_repo.py": (
            "non-space exclusion",
            "list_social_instances leaves seats out of social fan-outs",
        ),
        "socialhome/federation/invite_token_redeem.py": (
            "seat",
            "creates the seat row from an invite link",
        ),
        "socialhome/federation/inbound_validator.py": (
            "validator",
            "the peer-class step: what a seat may send",
        ),
        "socialhome/federation/federation_service.py": (
            "transport",
            "relay sizes, relay frames, mailbox / multi delivery",
        ),
        "socialhome/federation/transport.py": (
            "transport",
            "a seat is reached through its GFS relay only",
        ),
        "socialhome/federation/routed_envelope.py": (
            "transport",
            "mesh routing never hops through a seat",
        ),
        "socialhome/federation/auto_pair_coordinator.py": (
            "non-space exclusion",
            "a seat never vouches for a pairing",
        ),
        "socialhome/federation/sync/space/scheduler.py": (
            "transport",
            "sync over the relay; a directory seat syncs with the host only",
        ),
        "socialhome/app.py": (
            "transport",
            "the outbox never POSTs to a seat (no inbox address)",
        ),
        "socialhome/routes/pairing.py": (
            "transport",
            "the connections list labels a seat's transport gfs_relay",
        ),
        "socialhome/routes/public_spaces.py": (
            "non-space exclusion",
            "a host's name comes from a pairing, never a seat",
        ),
        "socialhome/services/bazaar_thread_service.py": (
            "non-space exclusion",
            "a DM is offered only to a paired seller",
        ),
        "socialhome/services/capabilities_outbound.py": (
            "directory",
            "a directory seat learns our version from our member card",
        ),
        "socialhome/services/dm_group_service.py": (
            "non-space exclusion",
            "DMs are for paired households",
        ),
        "socialhome/services/gfs_channel_service.py": (
            "transport",
            "a private space's GFS channel for households reached through it",
        ),
        "socialhome/services/gfs_route_discovery_service.py": (
            "transport",
            "a seat already rides one known GFS: no route probe",
        ),
        "socialhome/services/moment_reaction_router.py": (
            "non-space exclusion",
            "moments are for paired households",
        ),
        "socialhome/services/moment_service.py": (
            "non-space exclusion",
            "moments are for paired households",
        ),
        "socialhome/services/peer_directory_service.py": (
            "non-space exclusion",
            "the social peer directory",
        ),
        "socialhome/services/space_media_gfs_fetcher.py": (
            "GFS cache (live content)",
            "which GFS caches to fetch a space's files from",
        ),
        "socialhome/services/space_media_gfs_uploader.py": (
            "GFS cache (live content)",
            "which members read a GFS media cache",
        ),
        "socialhome/services/space_member_directory.py": (
            "directory",
            "member cards and mailboxes of GFS-reached members",
        ),
        "socialhome/services/space_service.py": (
            "seat",
            "seat teardown, no-seed rule, private-GFS link check, join routing",
        ),
        "socialhome/services/federation_inbound/pairing.py": (
            "non-space exclusion",
            "a seat never takes an inbox address (URL_UPDATED ignored)",
        ),
        "socialhome/services/url_update_outbound.py": (
            "non-space exclusion",
            "a seat never learns an address",
        ),
    },
    "is_relay_reader": {
        "socialhome/services/space_media_gfs_uploader.py": (
            "GFS cache (live content)",
            "defines it",
        ),
        "socialhome/app.py": (
            "GFS cache (live content)",
            "page files to a host whose relay takes no frames go via the cache",
        ),
        "socialhome/federation/sync/space/provider.py": (
            "GFS cache (live content)",
            "catch-up records carry media_refs for a cache reader",
        ),
        "socialhome/services/page_federation_outbound.py": (
            "GFS cache (live content)",
            "a page version's media_refs for a cache reader",
        ),
        "socialhome/services/page_proposal_forwarder.py": (
            "GFS cache (live content)",
            "a proposal's files via the cache when the host takes no frames",
        ),
        "socialhome/services/space_media_refs.py": (
            "GFS cache (live content)",
            "a live item's media_refs for a cache reader",
        ),
    },
    "reads_from_gfs": {
        "socialhome/services/space_media_gfs_uploader.py": (
            "GFS cache (live content)",
            "defines it",
        ),
        "socialhome/services/space_media_sync_service.py": (
            "GFS cache (live content)",
            "skips a push only while the reader's GFS holds the file",
        ),
    },
    "is_space_session_peer": {
        "socialhome/federation/federation_service.py": ("transport", "defines it"),
        "socialhome/federation/sync/space/provider.py": (
            "transport",
            "a sync record fits one relay frame",
        ),
        "socialhome/federation/sync/space/scheduler.py": (
            "transport",
            "no direct-sync signalling with a seat",
        ),
    },
    "relay_payload": {
        "socialhome/federation/federation_service.py": (
            "transport",
            "a smaller variant under the relay's envelope cap",
        ),
        "socialhome/services/space_config_outbound.py": (
            "transport",
            "the space cover / icon sized for the relay's envelope cap",
        ),
    },
}


def _code_names(tree: ast.AST) -> set[str]:
    """Names the code uses — never comments (not in the AST) nor docstrings
    (only an exact string constant counts, e.g. ``kwargs["relay_payload"]``)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif (
            isinstance(node, ast.arg)
            or isinstance(node, ast.keyword)
            and node.arg is not None
        ):
            out.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def _modules_referencing() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {marker: set() for marker in _ALLOWED}
    for path in sorted(_PKG.rglob("*.py")):
        names = _code_names(ast.parse(path.read_text(encoding="utf-8")))
        module = path.relative_to(_ROOT).as_posix()
        for marker in _ALLOWED:
            if marker in names:
                found[marker].add(module)
    return found


def test_every_reason_is_a_known_category():
    for marker, modules in _ALLOWED.items():
        for module, (category, reason) in modules.items():
            assert category in _CATEGORIES, (marker, module, category)
            assert reason.strip(), (marker, module)


def test_gfs_member_special_cases_stay_on_the_allow_list():
    found = _modules_referencing()
    for marker, allowed in _ALLOWED.items():
        extra = sorted(found[marker] - set(allowed))
        gone = sorted(set(allowed) - found[marker])
        assert not extra, (
            f"{extra} reference {marker!r}: a space member must be treated the "
            "same however their household is reached (CLAUDE.md, 'Every space "
            "member is equal, whichever way their household is reached'). "
            "Make the feature reach every member alike; only a seat / transport "
            "/ validator / directory / non-space exclusion / follower / GFS "
            "cache reason may tell them apart — then add the module to "
            f"_ALLOWED[{marker!r}] in {Path(__file__).name} with that reason."
        )
        assert not gone, (
            f"{gone} no longer reference {marker!r}: drop them from "
            f"_ALLOWED[{marker!r}] so the list stays exact."
        )


def test_the_scan_ignores_comments_and_docstrings():
    tree = ast.parse(
        '"""InstanceSource.SPACE_SESSION is_relay_reader( relay_payload"""\n'
        "# InstanceSource.SPACE_SESSION reads_from_gfs(\n"
        "x = 1\n"
    )
    assert not _code_names(tree) & set(_ALLOWED)
    code = ast.parse(
        "if inst.source is InstanceSource.SPACE_SESSION:\n"
        "    await u.is_relay_reader(i)\n"
        'kw["relay_payload"] = 1\n'
    )
    assert {"SPACE_SESSION", "is_relay_reader", "relay_payload"} <= _code_names(code)
