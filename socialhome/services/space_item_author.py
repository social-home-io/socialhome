"""Author-signed inner of a generic member-published space item (v_49).

A member household publishes comments, comment edits and deletes, its post
deletes and reactions over the connection server as the generic
``space_item`` (:mod:`socialhome.domain.space_item`). Each item's inner —
inside the space-content-key ciphertext — is signed by the author's
HOUSEHOLD identity key, exactly like the post inner of
:mod:`socialhome.services.space_public_author`, but over its own
domain-separated bytes (``space-item-author:v1:``) and with a suite tag
(``author_sig_suite``), so

* neither shape can be passed off as the other — a generic signature never
  verifies as a post inner, nor the reverse;
* the real ``item_type`` and the id it acts on (``item_target``) are always
  signed, so nobody holding the content key can re-wrap a comment as a
  delete, or point a delete at another row;
* ``ts`` — the author's action time, tz-aware ISO 8601 — is signed and is
  what receivers order edits and reactions by (last writer wins).

Signed fields (every one always present — ``None`` when unused — except
``identity_anchor``, carried present-or-absent as in the post inner)::

    item_type, item_target, space_id, post_id, author_user_id, author_pk,
    author_username, identity_anchor?, origin_instance_id, ts,
    comment_type, parent_id, content, media_url, created_at, emoji,
    author_sig_suite

``author_sig`` (b64url Ed25519) signs the rest.
"""

from __future__ import annotations

import json

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_user_id,
    sign_ed25519,
    verify_ed25519,
)

#: Suite of the generic item author signature. Receivers reject any other
#: value — never a default fallback.
ITEM_AUTHOR_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_ITEM_AUTHOR_SIG_SUITES: frozenset[str] = frozenset(
    {ITEM_AUTHOR_SIG_SUITE_ED25519}
)

#: Domain separation from every other Ed25519 signature in the system (the
#: post author inner signs under ``space-post-author:v1:``).
_ITEM_AUTHOR_SIG_DOMAIN: bytes = b"space-item-author:v1:"

_SIGNED_FIELDS: tuple[str, ...] = (
    "item_type",
    "item_target",
    "space_id",
    "post_id",
    "author_user_id",
    "author_pk",
    "author_username",
    "identity_anchor",
    "origin_instance_id",
    "ts",
    "comment_type",
    "parent_id",
    "content",
    "media_url",
    "created_at",
    "emoji",
    "author_sig_suite",
)

#: Present-or-absent, like the post inner: a username-anchored author's
#: bytes carry no ``identity_anchor`` key at all.
_OMITTED_WHEN_ABSENT: frozenset[str] = frozenset({"identity_anchor"})


class UnsupportedItemAuthorSigSuite(ValueError):
    """A space item names an author-signature suite we don't support."""


def item_signing_bytes(inner: dict) -> bytes:
    """The canonical, domain-separated bytes the author signs / a receiver
    verifies (``author_sig`` itself is never read)."""
    body: dict[str, object] = {}
    for k in _SIGNED_FIELDS:
        value = inner.get(k)
        if k in _OMITTED_WHEN_ABSENT and value is None:
            continue
        body[k] = value
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _ITEM_AUTHOR_SIG_DOMAIN + canonical


def build_signed_item_inner(
    *,
    item_type: str,
    item_target: str,
    space_id: str,
    post_id: str,
    author_user_id: str,
    author_username: str,
    author_pk: bytes,
    author_identity_seed: bytes,
    origin_instance_id: str,
    ts: str,
    author_identity_anchor: str | None = None,
    comment_type: str | None = None,
    parent_id: str | None = None,
    content: str | None = None,
    media_url: str | None = None,
    created_at: str | None = None,
    emoji: str | None = None,
) -> dict:
    """Build and sign one generic item inner (see the module docstring).

    A username-anchored author (``anchor == username`` or empty) carries no
    ``identity_anchor`` — the same normalisation as
    :func:`~socialhome.services.space_public_author.build_signed_author_inner`."""
    inner: dict[str, object] = {
        "item_type": item_type,
        "item_target": item_target,
        "space_id": space_id,
        "post_id": post_id,
        "author_user_id": author_user_id,
        "author_pk": author_pk.hex(),
        "author_username": author_username,
        "origin_instance_id": origin_instance_id,
        "ts": ts,
        "comment_type": comment_type,
        "parent_id": parent_id,
        "content": content,
        "media_url": media_url,
        "created_at": created_at,
        "emoji": emoji,
        "author_sig_suite": ITEM_AUTHOR_SIG_SUITE_ED25519,
    }
    if author_identity_anchor and author_identity_anchor != author_username:
        inner["identity_anchor"] = author_identity_anchor
    inner["author_sig"] = b64url_encode(
        sign_ed25519(author_identity_seed, item_signing_bytes(inner))
    )
    return inner


def verify_signed_item_inner(inner: dict) -> bool:
    """True iff ``inner`` is a well-formed generic item signed by its named
    author: the type, target, space, author identity and ``ts`` are present,
    ``author_pk`` self-certifies ``author_user_id`` (via the
    ``identity_anchor`` when carried, else the username) and ``author_sig``
    verifies. Fail-closed; an unknown ``author_sig_suite`` raises
    :class:`UnsupportedItemAuthorSigSuite`."""
    if not isinstance(inner, dict):
        return False
    suite = inner.get("author_sig_suite")
    if suite not in SUPPORTED_ITEM_AUTHOR_SIG_SUITES:
        raise UnsupportedItemAuthorSigSuite(str(suite))
    required = (
        "item_type",
        "item_target",
        "space_id",
        "post_id",
        "author_user_id",
        "author_pk",
        "author_username",
        "origin_instance_id",
        "ts",
    )
    if not all(isinstance(inner.get(k), str) and inner.get(k) for k in required):
        return False
    anchor = inner.get("identity_anchor")
    derivation_input = (
        anchor if isinstance(anchor, str) and anchor else str(inner["author_username"])
    )
    try:
        author_pk = bytes.fromhex(str(inner["author_pk"]))
        if derive_user_id(author_pk, derivation_input) != inner["author_user_id"]:
            return False
        sig_text = inner.get("author_sig")
        if not isinstance(sig_text, str) or not sig_text:
            return False
        sig = b64url_decode(sig_text)
    except ValueError:
        return False
    return verify_ed25519(author_pk, item_signing_bytes(inner), sig)
