"""Builder for the ``socialhome://invite#<blob>`` space-invite code.

One builder, two consumers. The string returned by :func:`build_invite_code`
is what the SPA renders for a copy-paste redeem, and the payload returned by
:func:`build_invite_payload` (base64url-encoded by
:func:`encode_invite_blob`) is exactly what gets parked on a connection
server's bulletin board for a browser visitor to copy. They must not drift:
a link that works pasted but not from the /join page — or the reverse — is a
bug nobody notices until a stranger tries to join.

The wire shape is owned by the SPA decoder
(``client/src/lib/spaceInviteCode.ts``) and the backend's
``routes/spaces.py::_bootstrap_hint``; the field list below matches both:

``token``
    The bearer credential. Mandatory.
``space_id`` / ``space_display_hint``
    Preview + post-join navigation, so the join card can name the space
    without a round-trip.
``issuer_instance_id``
    Base32 instance id of the issuing household. Never an address.
``issuer_identity_pk`` / ``issuer_keywrap_pk`` / ``issuer_keywrap_sig`` /
``issuer_proto_version`` / ``expires_at``
    The §D2b bootstrap block — the issuer's Ed25519 identity key (its
    instance id derives from it, so a substituted key cannot keep the
    advertised id), its static X25519 key-wrap key, and the signature
    binding the two. A redeemer who has never federated with the issuer
    seals its request to that key-wrap key.
``via_gfs``
    ``{gfs_url, gfs_space_id}`` — the one connection server the blob was
    published to, i.e. the one relay known to reach the issuer. Absent for
    a link that was never published.

**Never** a household address: the blob is served to anyone holding the
link, and the redeem travels by instance id through the envelope relay.

The bootstrap block rides on *every* code, published or not, so a code
copied out of the SPA and pasted by a stranger redeems through the relay
just like one lifted off a /join page.
"""

from __future__ import annotations

import orjson

from ..crypto import b64url_encode

#: URI scheme + fragment separator. Matches ``URI_PREFIX`` in
#: ``client/src/lib/spaceInviteCode.ts``.
INVITE_CODE_PREFIX = "socialhome://invite#"


def build_invite_payload(
    *,
    token: str,
    space_id: str,
    space_display_hint: str,
    issuer_instance_id: str,
    issuer_identity_pk: str,
    issuer_keywrap_pk: str,
    issuer_keywrap_sig: str,
    issuer_proto_version: int,
    expires_at: str | None = None,
    gfs_url: str | None = None,
    gfs_space_id: str | None = None,
) -> dict:
    """The decoded invite payload. See the module docstring for the fields.

    ``via_gfs`` is included only when ``gfs_url`` is set — an unpublished
    link has no relay to name, and an empty one would have the redeemer
    hand its sealed request to nowhere.
    """
    payload: dict = {
        "token": token,
        "space_id": space_id,
        "space_display_hint": space_display_hint,
        "issuer_instance_id": issuer_instance_id,
        "issuer_identity_pk": issuer_identity_pk,
        "issuer_keywrap_pk": issuer_keywrap_pk,
        "issuer_keywrap_sig": issuer_keywrap_sig,
        "issuer_proto_version": int(issuer_proto_version),
        "expires_at": expires_at,
    }
    if gfs_url:
        payload["via_gfs"] = {
            "gfs_url": gfs_url,
            # The GFS addresses a space by the household's own space id
            # (``POST /gfs/spaces/{space_id}/invite``), so the two are the
            # same value today; it stays a separate field because the SPA
            # decoder and the directory both treat them as independent.
            "gfs_space_id": gfs_space_id or space_id,
        }
    return payload


def encode_invite_blob(payload: dict) -> str:
    """base64url(JSON) — the opaque string a connection server stores.

    Unpadded, so it survives a URL fragment, a chat client and a QR code
    unmangled (``client/src/lib/base64Url.ts`` re-pads on decode).
    """
    return b64url_encode(orjson.dumps(payload))


def build_invite_code(payload: dict) -> str:
    """``socialhome://invite#<blob>`` — what the SPA renders for a paste."""
    return f"{INVITE_CODE_PREFIX}{encode_invite_blob(payload)}"
