"""Tests for :mod:`socialhome.canonical_json` — the bytes signatures and
hashes cover.

Golden vectors pin the exact encoding (non-ASCII, astral / emoji, lone
surrogates, small and large floats, nested key order): a peer on either side
of an upgrade recomputes these bytes from parsed JSON, so any drift breaks
verification between households and GFSs. The tripwire keeps every signing
or hashing function on these helpers instead of orjson.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib

import orjson
import pytest

from socialhome import authority_sig
from socialhome.canonical_json import (
    canonical_bytes,
    canonical_str,
    canonical_utf8_bytes,
    canonical_utf8_str,
    legacy_json_dumps,
)
from socialhome.crypto import ed25519_public_key
from socialhome.domain import page_version
from socialhome.domain.space_author_profile import profile_rev
from socialhome.federation import moment_origin
from socialhome.federation.federation_service import FederationEventType
from socialhome.services import space_public_author

#: Every shape a signature or hash may meet: non-ASCII (Latin, CJK), an
#: astral emoji (a surrogate pair under ``ensure_ascii``), a lone surrogate,
#: control characters, the JS line separator, small / large floats (where
#: orjson and stdlib disagree), negative zero and unsorted nested keys.
TRICKY: dict = {
    "text": 'Zoë 漢字 😀   <>&"\\\n\t\x00\x7f',
    "n": 5e-05,
    "tiny": 1.5e-7,
    "big": 1e16,
    "neg": -0.0,
    "lat": 52.1234,
    "nested": {"b": 1, "a": [0.0001, "é", None, True]},
    "lone": "\ud800",
    "Ä": "key",
}

GOLDEN_ASCII = (
    '{"big":1e+16,"lat":52.1234,"lone":"\\ud800","n":5e-05,"neg":-0.0,'
    '"nested":{"a":[0.0001,"\\u00e9",null,true],"b":1},'
    '"text":"Zo\\u00eb \\u6f22\\u5b57 \\ud83d\\ude00 \\u2028 <>&\\"\\\\\\n\\t\\u0000\\u007f",'
    '"tiny":1.5e-07,"\\u00c4":"key"}'
)


def test_canonical_str_golden_vector() -> None:
    assert canonical_str(TRICKY) == GOLDEN_ASCII


def test_canonical_bytes_is_the_ascii_text() -> None:
    out = canonical_bytes(TRICKY)
    assert out == GOLDEN_ASCII.encode("ascii")
    assert out.isascii()


def test_canonical_is_independent_of_insertion_order() -> None:
    a = {"z": 1, "a": {"y": 2, "b": 3}}
    b = {"a": {"b": 3, "y": 2}, "z": 1}
    assert canonical_bytes(a) == canonical_bytes(b) == b'{"a":{"b":3,"y":2},"z":1}'


def test_canonical_utf8_golden_vector() -> None:
    value = {k: v for k, v in TRICKY.items() if k != "lone"}
    expected = (
        '{"big":1e+16,"lat":52.1234,"n":5e-05,"neg":-0.0,'
        '"nested":{"a":[0.0001,"é",null,true],"b":1},'
        '"text":"Zoë 漢字 😀   <>&\\"\\\\\\n\\t\\u0000\x7f",'
        '"tiny":1.5e-07,"Ä":"key"}'
    )
    assert canonical_utf8_str(value) == expected
    assert canonical_utf8_bytes(value) == expected.encode("utf-8")


def test_canonical_utf8_refuses_a_lone_surrogate() -> None:
    """Raw UTF-8 can't carry a lone surrogate — the encode fails loudly
    rather than signing something a peer can't reproduce."""
    with pytest.raises(UnicodeEncodeError):
        canonical_utf8_bytes({"x": "\ud800"})


def test_legacy_json_dumps_passes_arguments_through() -> None:
    value = {"b": "ü", "a": 1}
    assert legacy_json_dumps(value) == '{"b": "\\u00fc", "a": 1}'
    assert legacy_json_dumps(value, sort_keys=True) == '{"a": 1, "b": "\\u00fc"}'
    assert (
        legacy_json_dumps(value, separators=(",", ":"), ensure_ascii=False)
        == '{"b":"ü","a":1}'
    )
    assert legacy_json_dumps({"d": object()}, default=lambda _o: "D") == '{"d": "D"}'


@pytest.mark.parametrize(
    "value",
    [{"n": 5e-05}, {"n": 1.5e-07}, {"s": "ü"}, {"s": "😀"}],
)
def test_orjson_differs_so_signing_must_not_use_it(value: dict) -> None:
    """Why this module exists: orjson renders these differently."""
    assert orjson.dumps(value, option=orjson.OPT_SORT_KEYS) != canonical_bytes(value)


# ── Mixed-version vectors: bytes / signatures made by the code before the
#    orjson migration (origin/main at 2026-10-23) still verify / match. ──


def test_old_authority_signature_still_verifies() -> None:
    """Signed by the pre-migration code (stdlib ``json.dumps``) with seed
    ``bytes(range(32))`` over a non-ASCII, small-float payload."""
    seed = bytes(range(32))
    payload = {
        "text": "Grüße aus Zürich 漢字 😀",
        "lat": 52.1234,
        "tiny": 5e-05,
        "big": 1e16,
    }
    assert authority_sig.verify_authority_event(
        event_type="space_post_public",
        space_id="sp-1",
        payload=payload,
        authority_sig=(
            "cHafR0jawuzgoFpDbVVzYE8PitK__BnX8lxhMBsIQa4EgWX5Hw9TYgd83STy"
            "-dvmCVKjHuo2AzwWh5ULiKuJAg"
        ),
        authority_sig_suite="ed25519",
        space_public_key=ed25519_public_key(seed),
    )


_OLD_INNER = {
    "content": "Grüße 😀",
    "author_user_id": "u-1",
    "space_id": "sp-1",
    "type": "text",
    "tiny": 5e-05,
}


def test_old_author_signing_bytes_unchanged() -> None:
    got = space_public_author.author_signing_bytes(_OLD_INNER)
    assert (
        hashlib.sha256(got).hexdigest()
        == "58b015857360a5ced07757458e8837c8325d467dd43d5f3d1cc15b76abeed9c4"
    )


def test_old_moment_origin_bytes_unchanged() -> None:
    """The ``ensure_ascii=False`` signed shape."""
    got = moment_origin.moment_origin_signing_bytes(
        FederationEventType.MOMENT_CREATED, _OLD_INNER, sig_suite="ed25519"
    )
    assert (
        hashlib.sha256(got).hexdigest()
        == "4107b61b64d4c3e106b2fc2a4fbfb533034594cb5e5582e2ce70f35a3fe2b5c6"
    )


def test_old_page_version_hash_unchanged() -> None:
    assert page_version.version_hash("Titel ü", "Inhalt 😀", None) == (
        "sha256:61337cbe2462f804167b31e337147d7280c61c2ce605e44fecef6b80c194d378"
    )


def test_profile_rev_matches_the_stdlib_form() -> None:
    expected = hashlib.sha256(
        json.dumps(
            ["Zoë", "bio 😀", None], separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()[:16]
    assert profile_rev("Zoë", "bio 😀", None) == expected


# ── Tripwire ──────────────────────────────────────────────────────────────

_PKG = pathlib.Path(__file__).resolve().parents[1] / "socialhome"

#: Calls that sign, verify or hash bytes.
_SIGNING_CALLS = frozenset(
    {
        "sign_ed25519",
        "verify_ed25519",
        "sign",
        "verify",
        "new",  # hmac.new / hashlib.new
        "sha256",
        "sha512",
        "blake2b",
        "blake2s",
        "compare_digest",
    }
)

#: Functions that hash orjson bytes ON PURPOSE — shapes born orjson that
#: never had a stdlib form: the §25.6 sync records digest (both sides have
#: always computed it with orjson) and two wall-dashboard ETags (compared
#: only against the same server's own answer). Adding one here needs the
#: same review as a new crypto wire shape. (The federation envelope, the
#: sync stream and pairing also sign orjson bytes, but build them in a
#: helper apart from the signing call, so this scan does not see them.)
_ORJSON_SIGNING_ALLOWED = frozenset(
    {
        ("federation/sync/space/provider.py", "_records_digest"),
        ("dashboard/display_api.py", "json_answer"),
        ("dashboard/map_scene.py", "scene_key"),
    }
)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _is_orjson_dumps(node: ast.Call) -> bool:
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "dumps"
        and isinstance(f.value, ast.Name)
        and f.value.id in {"orjson", "_orjson"}
    )


def _offenders() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _PKG.rglob("*.py"):
        rel = path.relative_to(_PKG).as_posix()
        if rel.startswith("migrations/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            signs = any(_call_name(c) in _SIGNING_CALLS for c in calls)
            if signs and any(_is_orjson_dumps(c) for c in calls):
                found.add((rel, fn.name))
    return found


def test_no_function_signs_or_hashes_orjson_bytes() -> None:
    """A function that signs, verifies or hashes must build its JSON with
    :mod:`socialhome.canonical_json`, never orjson — unless it is one of the
    born-orjson shapes listed above."""
    unexpected = _offenders() - _ORJSON_SIGNING_ALLOWED
    assert not unexpected, (
        "these functions sign / hash orjson bytes; use socialhome.canonical_json "
        f"(or justify an allow-list entry): {sorted(unexpected)}"
    )


def test_orjson_signing_allow_list_has_no_stale_entries() -> None:
    stale = _ORJSON_SIGNING_ALLOWED - _offenders()
    assert not stale, f"allow-list entries no longer match code: {sorted(stale)}"


#: Modules that build signed / hashed JSON — each must use the helpers.
_SIGNING_MODULES = (
    "authority_sig.py",
    "authority_cert.py",
    "writer_cert.py",
    "writer_key.py",
    "capabilities_sig.py",
    "gfs_proof.py",
    "space_owner_statement.py",
    "moment_public_seal.py",
    "domain/gfs_server.py",
    "domain/gfs_channel.py",
    "domain/gfs_member_publish.py",
    "domain/page_version.py",
    "domain/space_author_profile.py",
    "federation/moment_origin.py",
    "federation/moderation_approval.py",
    "dashboard/auth.py",
    "dashboard/claim_seal.py",
    "services/space_public_author.py",
    "services/space_item_author.py",
    "services/gfs_connection_service.py",
    "services/gfs_member_publish_service.py",
    "services/moment_public_service.py",
    "services/highlight_publication_service.py",
    "services/highlight_signaling_handler.py",
    "services/peer_directory_service.py",
    "global_server/admin_service.py",
    "global_server/federation.py",
    "global_server/cluster.py",
    "global_server/cluster_relay.py",
)


@pytest.mark.parametrize("rel", _SIGNING_MODULES)
def test_signing_module_uses_canonical_json(rel: str) -> None:
    tree = ast.parse((_PKG / rel).read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert any(m.endswith("canonical_json") for m in imports), rel


def test_only_canonical_json_imports_stdlib_json() -> None:
    offenders = []
    for path in _PKG.rglob("*.py"):
        rel = path.relative_to(_PKG).as_posix()
        if rel == "canonical_json.py" or rel.startswith("migrations/"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(
                a.name == "json" for a in node.names
            ):
                offenders.append(rel)
            if isinstance(node, ast.ImportFrom) and node.module == "json":
                offenders.append(rel)
    assert not offenders, offenders
