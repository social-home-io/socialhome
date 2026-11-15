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
import re
from collections import Counter
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
    # The source as a string: ``InstanceSource("space_session")``,
    # ``== "space_session"``, SQL text naming it.
    "space_session": {
        "socialhome/domain/federation.py": (
            "validator",
            "the InstanceSource enum value itself",
        ),
        "socialhome/federation/invite_token_redeem.py": (
            "seat",
            "the redeem's space_session= flag that seats a household",
        ),
        "socialhome/services/dm_service.py": (
            "non-space exclusion",
            "DMs are for paired households",
        ),
        "socialhome/repositories/outbox_repo.py": (
            "transport",
            "the v_91 ordering gate for rows a multi upload can carry",
        ),
    },
    "relay_media_ok": {
        "socialhome/federation/federation_service.py": (
            "transport",
            "defines it: binary relay frames to a seat",
        ),
        "socialhome/services/space_media_gfs_uploader.py": (
            "transport",
            "takes_relay_frames asks it",
        ),
    },
    "takes_relay_frames": {
        "socialhome/services/space_media_gfs_uploader.py": (
            "transport",
            "defines it",
        ),
        "socialhome/app.py": (
            "transport",
            "page files reach a frame-taking host by push",
        ),
        "socialhome/services/page_proposal_forwarder.py": (
            "transport",
            "a proposal's files go by push to a frame-taking host",
        ),
        "socialhome/federation/sync/space/provider.py": (
            "transport",
            "the narrow v_92 descriptor only when pushes go as frames",
        ),
    },
    "stored_on_gfs": {
        "socialhome/services/space_media_gfs_uploader.py": (
            "GFS cache (live content)",
            "defines it",
        ),
    },
    "stored_plan_for_reader": {
        "socialhome/services/space_media_gfs_uploader.py": (
            "GFS cache (live content)",
            "defines it",
        ),
        "socialhome/federation/sync/space/provider.py": (
            "GFS cache (live content)",
            "a v_92 catch-up describes only blobs its GFS holds",
        ),
    },
    # The non-social filter: a seat is no social relationship.
    "list_social_instances": {
        "socialhome/repositories/federation_repo.py": (
            "non-space exclusion",
            "defines it",
        ),
        "socialhome/federation/federation_service.py": (
            "non-space exclusion",
            "peer broadcasts and home-location fan-out",
        ),
        "socialhome/federation/route_discovery.py": (
            "non-space exclusion",
            "mesh routes run over social peers",
        ),
        "socialhome/routes/calendar.py": (
            "non-space exclusion",
            "personal calendar sharing",
        ),
        "socialhome/services/app_federation_service.py": (
            "non-space exclusion",
            "household apps",
        ),
        "socialhome/services/calendar_service.py": (
            "non-space exclusion",
            "personal calendar sharing",
        ),
        "socialhome/services/dm_routing_service.py": (
            "non-space exclusion",
            "DM routing",
        ),
        "socialhome/services/friends_service.py": (
            "non-space exclusion",
            "friends",
        ),
        "socialhome/services/online_status_service.py": (
            "non-space exclusion",
            "presence",
        ),
        "socialhome/services/peer_directory_service.py": (
            "non-space exclusion",
            "the social peer directory",
        ),
        "socialhome/services/peer_outbound.py": (
            "non-space exclusion",
            "confirmed-peer broadcasts",
        ),
        "socialhome/services/resync_on_upgrade.py": (
            "non-space exclusion",
            "capability re-ask on upgrade",
        ),
    },
}

#: EXACT count of each marker's code occurrences per allowed module: a new
#: special case inside an allowed module (the removed ``_is_relay_only_peer``
#: re-added, say) moves the count and fails. Update a count only with the
#: reason the new occurrence is allowed.
_COUNTS: dict[str, dict[str, int]] = {
    "SPACE_SESSION": {
        "socialhome/app.py": 1,
        "socialhome/domain/federation.py": 1,
        "socialhome/federation/auto_pair_coordinator.py": 1,
        "socialhome/federation/federation_service.py": 17,
        "socialhome/federation/inbound_validator.py": 3,
        "socialhome/federation/invite_token_redeem.py": 5,
        "socialhome/federation/routed_envelope.py": 1,
        "socialhome/federation/sync/space/scheduler.py": 1,
        "socialhome/federation/transport.py": 6,
        "socialhome/repositories/federation_repo.py": 1,
        "socialhome/routes/pairing.py": 1,
        "socialhome/routes/public_spaces.py": 1,
        "socialhome/services/bazaar_thread_service.py": 1,
        "socialhome/services/capabilities_outbound.py": 1,
        "socialhome/services/dm_group_service.py": 2,
        "socialhome/services/federation_inbound/pairing.py": 1,
        "socialhome/services/gfs_channel_service.py": 1,
        "socialhome/services/gfs_route_discovery_service.py": 1,
        "socialhome/services/moment_reaction_router.py": 1,
        "socialhome/services/moment_service.py": 1,
        "socialhome/services/peer_directory_service.py": 1,
        "socialhome/services/space_media_gfs_fetcher.py": 1,
        "socialhome/services/space_media_gfs_uploader.py": 1,
        "socialhome/services/space_member_directory.py": 4,
        "socialhome/services/space_service.py": 4,
        "socialhome/services/url_update_outbound.py": 1,
    },
    "is_relay_reader": {
        "socialhome/app.py": 1,
        "socialhome/federation/sync/space/provider.py": 3,
        "socialhome/services/page_federation_outbound.py": 1,
        "socialhome/services/page_proposal_forwarder.py": 1,
        "socialhome/services/space_media_gfs_uploader.py": 2,
        "socialhome/services/space_media_refs.py": 1,
    },
    "reads_from_gfs": {
        "socialhome/services/space_media_gfs_uploader.py": 1,
        "socialhome/services/space_media_sync_service.py": 2,
    },
    "is_space_session_peer": {
        "socialhome/federation/federation_service.py": 3,
        "socialhome/federation/sync/space/provider.py": 1,
        "socialhome/federation/sync/space/scheduler.py": 1,
    },
    "relay_payload": {
        "socialhome/federation/federation_service.py": 3,
        "socialhome/services/space_config_outbound.py": 1,
    },
    "space_session": {
        "socialhome/domain/federation.py": 1,
        "socialhome/federation/invite_token_redeem.py": 4,
        "socialhome/repositories/outbox_repo.py": 1,
        "socialhome/services/dm_service.py": 2,
    },
    "relay_media_ok": {
        "socialhome/federation/federation_service.py": 3,
        "socialhome/services/space_media_gfs_uploader.py": 1,
    },
    "takes_relay_frames": {
        "socialhome/app.py": 1,
        "socialhome/federation/sync/space/provider.py": 2,
        "socialhome/services/page_proposal_forwarder.py": 1,
        "socialhome/services/space_media_gfs_uploader.py": 1,
    },
    "stored_on_gfs": {
        "socialhome/services/space_media_gfs_uploader.py": 2,
    },
    "stored_plan_for_reader": {
        "socialhome/federation/sync/space/provider.py": 2,
        "socialhome/services/space_media_gfs_uploader.py": 1,
    },
    "list_social_instances": {
        "socialhome/federation/federation_service.py": 2,
        "socialhome/federation/route_discovery.py": 1,
        "socialhome/repositories/federation_repo.py": 2,
        "socialhome/routes/calendar.py": 1,
        "socialhome/services/app_federation_service.py": 1,
        "socialhome/services/calendar_service.py": 1,
        "socialhome/services/dm_routing_service.py": 1,
        "socialhome/services/friends_service.py": 2,
        "socialhome/services/online_status_service.py": 3,
        "socialhome/services/peer_directory_service.py": 1,
        "socialhome/services/peer_outbound.py": 1,
        "socialhome/services/resync_on_upgrade.py": 1,
    },
}

#: Functions that must stay free of every marker — and so must every
#: ``self._helper`` they call (followed one level): the roster repair
#: reaches every member household by one rule.
_MARKER_FREE: dict[str, tuple[str, ...]] = {
    "socialhome/services/space_service.py": (
        "on_sync_begin_roster",
        "_repair_by_digest",
        "_repair_by_echo",
        "_schedule_roster_repair",
        "_roster_seats_of",
        "roster_digest_of",
        "roster_digest_for",
        "send_roster_snapshot",
    ),
}

#: Functions that MUST keep a seat check (defence in depth): the binary
#: HTTPS media leg never POSTs to a household met through a GFS, even if a
#: seat row somehow held an address.
_SEAT_CHECKED: dict[str, tuple[str, ...]] = {
    "socialhome/federation/federation_service.py": ("_send_media_direct",),
}

_SPACE_SESSION_WORD = re.compile(r"\bspace_session\b")


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                out.add(id(body[0].value))
    return out


def _code_name_list(tree: ast.AST) -> list[str]:
    """Names the code uses, one per occurrence — never comments (not in the
    AST) nor docstrings. A string constant counts when it equals a marker
    (``kwargs["relay_payload"]``), and as ``space_session`` when it names
    that word (SQL text, ``== "space_session"``)."""
    docs = _docstrings(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node.name)
        elif isinstance(node, ast.arg) or (
            isinstance(node, ast.keyword) and node.arg is not None
        ):
            out.append(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
        ):
            if node.value != "space_session" and _SPACE_SESSION_WORD.search(node.value):
                out.append("space_session")
            out.append(node.value)
    return out


def _code_names(tree: ast.AST) -> set[str]:
    return set(_code_name_list(tree))


def _marker_counts(tree: ast.AST) -> Counter[str]:
    return Counter(n for n in _code_name_list(tree) if n in _ALLOWED)


def _marker_free_hits(tree: ast.AST, names: tuple[str, ...]) -> dict[str, set[str]]:
    """Markers in each of *names*' bodies and in the ``self._helper``
    methods they call (one level)."""
    fns = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    out: dict[str, set[str]] = {}
    for name in names:
        fn = fns[name]
        hit = _body_names(fn) & set(_ALLOWED)
        helpers = {
            n.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "self"
            and n.attr.startswith("_")
            and n.attr in fns
        }
        for helper in helpers:
            hit |= _body_names(fns[helper]) & set(_ALLOWED)
        out[name] = hit
    return out


def _functions(path: str) -> dict[str, ast.AST]:
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _body_names(fn: ast.AST) -> set[str]:
    """:func:`_code_names` of a function body (its own name left out)."""
    return _code_names(ast.Module(body=list(fn.body), type_ignores=[]))


def _scan() -> dict[str, dict[str, int]]:
    """marker → module → occurrences, over every module under socialhome/."""
    found: dict[str, dict[str, int]] = {marker: {} for marker in _ALLOWED}
    for path in sorted(_PKG.rglob("*.py")):
        counts = _marker_counts(ast.parse(path.read_text(encoding="utf-8")))
        module = path.relative_to(_ROOT).as_posix()
        for marker, n in counts.items():
            found[marker][module] = n
    return found


def _modules_referencing() -> dict[str, set[str]]:
    return {marker: set(mods) for marker, mods in _scan().items()}


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


def test_each_allowed_module_keeps_its_exact_count():
    """A new special case inside an already-allowed module fails too."""
    assert set(_COUNTS) == set(_ALLOWED)
    for marker, modules in _ALLOWED.items():
        assert set(_COUNTS[marker]) == set(modules), marker
    found = _scan()
    for marker, counts in _COUNTS.items():
        for module, n in counts.items():
            got = found[marker].get(module, 0)
            assert got == n, (
                f"{module} references {marker!r} {got} times, allowed {n}: "
                "a member reached through a GFS must be treated like any "
                "other (CLAUDE.md, 'Every space member is equal'). Add an "
                "occurrence only for a seat / transport / validator / "
                "directory / non-space / follower / GFS-cache reason, and "
                f"update _COUNTS in {Path(__file__).name}."
            )


def test_the_roster_repair_has_no_seat_special_case():
    for path, names in _MARKER_FREE.items():
        tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
        for name, hit in _marker_free_hits(tree, names).items():
            assert not hit, (
                f"{path}:{name} (or a self._helper it calls) references "
                f"{sorted(hit)}: the roster repair reaches every member "
                "household by one rule (CLAUDE.md, 'Every space member is "
                "equal')."
            )


def test_re_adding_the_removed_seat_skip_fails_the_guard():
    """Review should-fix 3: the exact special case this work removed —
    ``_is_relay_only_peer`` (a SPACE_SESSION check) called from the roster
    path — is caught both by the helper-following pin and by the count."""
    path = "socialhome/services/space_service.py"
    src = (_ROOT / path).read_text(encoding="utf-8")
    anchor = "        if sender not in members:\n"
    assert anchor in src
    mutated = src.replace(
        anchor,
        "        if await self._is_relay_only_peer(sender):\n            return\n"
        + anchor,
        1,
    ).replace(
        "    async def _cert_epoch_for(",
        "    async def _is_relay_only_peer(self, instance_id):\n"
        "        row = await self._federation_repo.get_instance(instance_id)\n"
        "        return row is not None and row.source is "
        "InstanceSource.SPACE_SESSION\n\n"
        "    async def _cert_epoch_for(",
        1,
    )
    tree = ast.parse(mutated)
    hits = _marker_free_hits(tree, _MARKER_FREE[path])
    assert "SPACE_SESSION" in hits["on_sync_begin_roster"]
    assert _marker_counts(tree)["SPACE_SESSION"] == _COUNTS["SPACE_SESSION"][path] + 1


def test_the_https_media_leg_keeps_its_seat_check():
    """Every branch of the leg that reads ``remote_inbox_url`` (the POST
    target) also checks the seat source in the same condition."""
    for path, names in _SEAT_CHECKED.items():
        fns = _functions(path)
        for name in names:
            posts = [
                n.test
                for n in ast.walk(fns[name])
                if isinstance(n, ast.If) and "remote_inbox_url" in _code_names(n.test)
            ]
            assert posts, f"{path}:{name} has no inbox branch any more"
            for test in posts:
                assert "SPACE_SESSION" in _code_names(test), (
                    f"{path}:{name} lost its seat check: a household met "
                    "through a GFS is never POSTed to directly (defence in "
                    "depth)."
                )


def test_the_scan_finds_the_source_as_a_string_but_not_in_docstrings():
    code = ast.parse(
        'def f():\n    """A ``space_session`` seat."""\n'
        "    return \"WHERE source <> 'space_session'\"\n"
    )
    assert "space_session" in _code_names(code)
    doc_only = ast.parse('def f():\n    """A ``space_session`` seat."""\n')
    assert "space_session" not in _code_names(doc_only)
