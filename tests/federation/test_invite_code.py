"""Tests for the ``socialhome://invite#<blob>`` builder.

The blob is the one wire shape three parties agree on: the SPA decoder
(``client/src/lib/spaceInviteCode.ts``), the backend's
``routes/spaces.py::_bootstrap_hint``, and whatever a connection server
hands a browser visitor. These tests pin the field names against the two
backend readers, because a silent rename there turns every issued link
into an unredeemable string.
"""

from __future__ import annotations

import base64
import json

from socialhome.federation.invite_code import (
    INVITE_CODE_PREFIX,
    build_invite_code,
    build_invite_payload,
    encode_invite_blob,
)
from socialhome.routes.spaces import _bootstrap_hint


def _payload(**over):
    base = dict(
        token="tok-1",
        space_id="sp-1",
        space_display_hint="Pascal's family",
        issuer_instance_id="ISSUERINSTANCEID",
        issuer_identity_pk="aa" * 32,
        issuer_keywrap_pk="bb" * 32,
        issuer_keywrap_sig="c" * 43,
        issuer_proto_version=28,
        expires_at="2026-10-01T12:00:00+00:00",
    )
    base.update(over)
    return build_invite_payload(**base)


def _decode(code: str) -> dict:
    assert code.startswith(INVITE_CODE_PREFIX)
    blob = code[len(INVITE_CODE_PREFIX) :]
    padded = blob + "=" * (-len(blob) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_code_decodes_to_the_documented_fields():
    decoded = _decode(build_invite_code(_payload()))
    assert decoded == {
        "token": "tok-1",
        "space_id": "sp-1",
        "space_display_hint": "Pascal's family",
        "issuer_instance_id": "ISSUERINSTANCEID",
        "issuer_identity_pk": "aa" * 32,
        "issuer_keywrap_pk": "bb" * 32,
        "issuer_keywrap_sig": "c" * 43,
        "issuer_proto_version": 28,
        "expires_at": "2026-10-01T12:00:00+00:00",
    }


def test_blob_is_unpadded_base64url():
    """The SPA re-pads on decode; ``=``/``+``/``/`` would be mangled by a
    URL fragment, a chat client or a QR scanner on the way there."""
    blob = encode_invite_blob(_payload())
    assert "=" not in blob and "+" not in blob and "/" not in blob


def test_an_unpublished_link_names_no_relay():
    """``via_gfs`` would tell the redeemer to hand its sealed request to
    a server — an empty one would send it nowhere."""
    assert "via_gfs" not in _decode(build_invite_code(_payload()))


def test_a_published_link_names_the_server_that_serves_it():
    decoded = _decode(build_invite_code(_payload(gfs_url="https://relay.example.org")))
    assert decoded["via_gfs"] == {
        "gfs_url": "https://relay.example.org",
        # Defaults to the household's own space id — that is how the GFS
        # addresses the space (``/gfs/spaces/{space_id}/invite``).
        "gfs_space_id": "sp-1",
    }


def test_the_blob_carries_no_household_address():
    """A published blob is served to anyone with the link. It may name
    the relay, never the household."""
    decoded = _decode(build_invite_code(_payload(gfs_url="https://relay.example.org")))
    flat = json.dumps(decoded)
    assert "inbox" not in flat
    assert "http" not in json.dumps(
        {k: v for k, v in decoded.items() if k != "via_gfs"}
    )


def test_the_backend_bootstrap_reader_accepts_what_we_emit():
    """``_bootstrap_hint`` is the other end of this wire: it parses the
    body the SPA builds from a decoded blob. A field rename on one side
    is caught here rather than by a stranger who cannot join."""
    decoded = _decode(build_invite_code(_payload(gfs_url="https://relay.example.org")))
    body = dict(decoded)
    # The SPA flattens ``via_gfs.gfs_url`` into ``gfs`` for the POST.
    body["gfs"] = decoded["via_gfs"]["gfs_url"]
    hint = _bootstrap_hint(
        body,
        issuer_instance_id=decoded["issuer_instance_id"],
        token=decoded["token"],
    )
    assert hint is not None
    assert hint.invite_token == "tok-1"
    assert hint.space_id == "sp-1"
    assert hint.identity_pk == "aa" * 32
    assert hint.keywrap_pk == "bb" * 32
    assert hint.keywrap_sig == "c" * 43
    assert hint.proto_version == 28
    assert hint.display_hint == "Pascal's family"
    assert hint.expires_at == "2026-10-01T12:00:00+00:00"
    assert hint.gfs_url == "https://relay.example.org"
