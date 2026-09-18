"""Canonical per-author signing bytes for the public space-content relay.

The public space relay (``space_public_outbound`` / ``space_public_inbound``)
carries TWO independent signatures:

* the **space-authority** signature on the *outer* envelope — produced by the
  relaying seed-holder's space seed, verified against the TOFU-pinned space
  public key (proves the relay is authorised, lets the GFS gate the fan-out
  while staying content-blind), and
* a **per-author** signature on the *inner* (encrypted) content — produced by
  the author's *household identity seed*, verified against ``author_pk``
  (proves the named author actually wrote the post).

Without the per-author signature, attribution rests only on the authority
signature + a ``derive_user_id(author_pk, username) == author_user_id``
self-cert. Both ``author_pk`` and ``author_user_id`` are PUBLIC, so a
seed-holder could attribute any post to any member. The per-author signature
closes that hole — it can only be produced by a household holding the author's
identity seed.

This module owns the ONE canonical, domain-separated message both sides sign /
verify over so the two never drift. The signed message covers every
attributable field of the inner payload and EXCLUDES ``author_sig`` itself.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_user_id,
    sign_ed25519,
    verify_ed25519,
)

if TYPE_CHECKING:
    from ..domain.post import Post

#: Domain-separation prefix — distinguishes these signing bytes from every
#: other Ed25519 signature in the system (envelope authority sig, user
#: assertions, moment envelopes, …). Bump the suffix on any wire change.
_AUTHOR_SIG_DOMAIN: bytes = b"space-post-author:v1:"

#: The inner-payload fields the author signs over — everything attributable.
#: ``author_pk`` is included so a swap of (pk, sig) pair can't re-target the
#: post; ``author_sig`` itself is excluded (it signs the rest).
#: ``hidden_from_feed`` is signed too — it binds presentation intent to the
#: author so a relayer can't flip feed visibility undetectably.
#: ``identity_anchor`` is signed too — it is the derivation input for
#: ``author_user_id`` when present (a uuid for new users; absent/None for
#: legacy rows whose user_id derives from the mutable username). Binding it to
#: the author signature stops a relayer from stripping/swapping it to bypass
#: the self-cert check undetectably (§v_26 identity anchor).
_SIGNED_FIELDS: tuple[str, ...] = (
    "post_id",
    "space_id",
    "author_user_id",
    "author_pk",
    "author_username",
    "identity_anchor",
    "type",
    "content",
    "media_url",
    "image_urls",
    "created_at",
    "location",
    "origin_instance_id",
    "hidden_from_feed",
)


#: ``identity_anchor`` is the ONLY signed field carried *present-or-absent*
#: rather than *present-with-None-default*. It was added after v_25 and the GFS
#: public/global relay path has no proto-version negotiation, so an absent
#: anchor MUST reproduce the exact v_25 signing bytes (no ``identity_anchor``
#: key at all) — otherwise a not-yet-upgraded subscriber recomputes the old
#: layout, the author sig mismatches, and even legacy username-anchored posts
#: are silently dropped during rollout.
#:
#: "Absent" is decided by :func:`build_signed_author_inner`, NOT by the
#: callers: in production every ``users`` row carries a non-NULL anchor
#: (migration ``0041`` backfilled ``identity_anchor = username`` for every
#: pre-existing user; every provision path writes one), so the producers
#: always pass an anchor. The builder therefore normalises a *legacy*
#: username-anchored author (``anchor == username``) to "absent" — only a
#: uuid4-anchored author (``anchor != username``) writes the key, and those
#: legitimately verify on v_26+ only (the migration tail).
_OPTIONAL_OMITTED_WHEN_ABSENT: frozenset[str] = frozenset({"identity_anchor"})


def author_signing_bytes(inner: dict) -> bytes:
    """Return the canonical, domain-separated bytes the author signs / the
    receiver verifies for one relayed public-space post.

    ``inner`` is the decrypted inner payload (with or without ``author_sig`` —
    it is never read). Most fields default to ``None`` so producer and consumer
    agree even when an optional field is absent. ``identity_anchor`` is the lone
    exception: it is OMITTED from the body entirely when absent/None (see
    :data:`_OPTIONAL_OMITTED_WHEN_ABSENT`), so a legacy author's bytes stay
    byte-identical to the pre-anchor (v_25) layout. The body is compact, sorted
    JSON so the encoding is deterministic across both sides.
    """
    body: dict[str, object] = {}
    for k in _SIGNED_FIELDS:
        if k in _OPTIONAL_OMITTED_WHEN_ABSENT:
            # Omit entirely when absent/None — never write a null key, so the
            # signing bytes match the layout that predates this field.
            value = inner.get(k)
            if value is not None:
                body[k] = value
        else:
            body[k] = inner.get(k)
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _AUTHOR_SIG_DOMAIN + canonical


def build_signed_author_inner(
    *,
    post: "Post",
    space_id: str,
    author_username: str,
    author_pk: bytes,
    author_identity_seed: bytes,
    origin_instance_id: str,
    author_identity_anchor: str | None = None,
) -> dict:
    """Build the per-author-signed inner payload for a public/global space
    post (the object the GFS relay encrypts + a subscriber verifies).

    The author's household identity seed signs the canonical author bytes;
    the resulting ``author_sig`` (b64url) is added to the returned dict.
    Centralised so the local relay (space_public_outbound) and the
    member-broadcast relay-hint (space_post_outbound) produce byte-identical
    signed inners.

    **Legacy-anchor normalisation (v_25 wire compatibility).** Callers pass
    the author's stored ``identity_anchor`` verbatim — and in production that
    is never ``None`` (migration ``0041`` backfilled ``= username`` for every
    pre-existing user; every provision path writes one). A legacy,
    username-anchored author (``author_identity_anchor == author_username``)
    is normalised here to *absent*: no ``identity_anchor`` key is written and
    none is signed, so their bytes are byte-identical to the pre-anchor v_25
    layout and a not-yet-upgraded subscriber (GFS relay path, no proto
    negotiation) still verifies them. A uuid4-anchored author's anchor never
    equals the username, so it is carried and signed unchanged. Done here —
    the single producer choke point — so every present and future producer
    inherits it.

    Security argument: the verifier derives
    ``derive_user_id(author_pk, anchor if present else username)`` and checks
    it against ``author_user_id``. For a legacy author the anchor *is* the
    username, so omitting it leaves the derivation unchanged. A relayer that
    *adds* ``identity_anchor: <username>`` to a legacy inner changes the
    recomputed bytes on a v_26+ receiver (sig fails) and is ignored by a v_25
    receiver (unchanged) — no forged attribution either way. A relayer that
    *removes* the anchor from a uuid4 author makes the derivation fall back
    to the username, which does not equal the uuid-derived ``author_user_id``
    (rejected). See ``tests/services/test_space_public_author.py``."""
    inner: dict[str, object] = {
        "post_id": post.id,
        "space_id": space_id,
        "author_user_id": post.author,
        "author_pk": author_pk.hex(),
        "author_username": author_username,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "image_urls": list(post.image_urls),
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "hidden_from_feed": post.hidden_from_feed,
        "origin_instance_id": origin_instance_id,
    }
    if post.location is not None:
        inner["location"] = {
            "lat": post.location.lat,
            "lon": post.location.lon,
            "label": post.location.label,
        }
    # The derivation input for ``author_user_id`` (a uuid for new users). OMITTED
    # entirely for legacy authors whose user_id derives from the username — the
    # 0041 backfill stores ``identity_anchor = username`` for them, so treat
    # ``anchor == username`` as absent — keeping the signed bytes byte-identical
    # to the pre-anchor (v_25) layout so a not-yet-upgraded subscriber still
    # verifies their posts during rollout (see the docstring for the security
    # argument).
    if author_identity_anchor is not None and author_identity_anchor != author_username:
        inner["identity_anchor"] = author_identity_anchor
    inner["author_sig"] = b64url_encode(
        sign_ed25519(author_identity_seed, author_signing_bytes(inner))
    )
    return inner


def verify_signed_author_inner(inner: dict) -> bool:
    """True iff ``inner`` is a well-formed, self-certifying, author-signed
    public-post payload: required identity fields present, the ``author_pk``
    self-certifies the ``author_user_id`` (:func:`derive_user_id`), and
    ``author_sig`` verifies against ``author_pk`` over the canonical author
    bytes. Fail-closed.

    Mirrors the subscriber-side self-cert + author-sig checks
    (``SpacePublicInbound._self_cert_ok`` / ``_author_sig_ok``) so a relaying
    seed-holder and a receiving subscriber apply the IDENTICAL verification —
    the two can't drift.
    """
    if not isinstance(inner, dict):
        return False
    post_id = str(inner.get("post_id") or "")
    author_user_id = str(inner.get("author_user_id") or "")
    author_pk_hex = str(inner.get("author_pk") or "")
    username = str(inner.get("author_username") or "")
    if not (post_id and author_user_id and author_pk_hex and username):
        return False
    # Author self-cert: the user id MUST be derivable from author_pk + the
    # derivation input — the immutable ``identity_anchor`` (uuid) when the block
    # carries one (§v_26), else the (legacy) username. Mirrors the user-identity
    # assertion derivation in crypto.verify_user_identity_assertion. The anchor
    # is part of the signed bytes, so a relayer can't swap it to forge a match.
    # Binds author_pk ↔ author_user_id (both PUBLIC, so on its own this can't
    # prevent attribution forgery; the author_sig below closes that hole).
    anchor = inner.get("identity_anchor")
    author_identity_anchor = anchor if isinstance(anchor, str) and anchor else None
    derivation_input = author_identity_anchor or username
    try:
        if (
            derive_user_id(bytes.fromhex(author_pk_hex), derivation_input)
            != author_user_id
        ):
            return False
    except ValueError:
        return False
    # Per-author signature: the named author's household identity key must have
    # signed the inner content. Fail-closed on missing / malformed / invalid.
    author_sig = inner.get("author_sig")
    if not isinstance(author_sig, str) or not author_sig:
        return False
    try:
        author_pk = bytes.fromhex(author_pk_hex)
        sig = b64url_decode(author_sig)
    except ValueError:
        return False
    return verify_ed25519(author_pk, author_signing_bytes(inner), sig)
