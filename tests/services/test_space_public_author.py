"""Tests for the canonical per-author signing bytes helper."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_user_id,
    generate_identity_keypair,
    sign_ed25519,
    verify_ed25519,
)
from socialhome.domain.post import LocationData, Post, PostType
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
    verify_signed_author_inner,
)


def _inner(**over) -> dict:
    base = {
        "post_id": "p1",
        "space_id": "sp",
        "author_user_id": "u1",
        "author_pk": "ab" * 32,
        "author_username": "bob",
        "type": "text",
        "content": "hi",
        "media_url": None,
        "image_urls": [],
        "created_at": "2026-06-10T00:00:00+00:00",
        "location": None,
        "origin_instance_id": "remote.home",
    }
    base.update(over)
    return base


#: The EXACT field set the pre-change (v_25) code signed over — frozen here as
#: a regression anchor. ``identity_anchor`` is intentionally NOT in this tuple;
#: a legacy (no-anchor) author MUST sign byte-identical bytes to this layout so
#: a not-yet-upgraded v_25 subscriber/relay (no proto negotiation on the GFS
#: public path) still verifies their posts during rollout.
_V25_SIGNED_FIELDS: tuple[str, ...] = (
    "post_id",
    "space_id",
    "author_user_id",
    "author_pk",
    "author_username",
    "type",
    "content",
    "media_url",
    "image_urls",
    "created_at",
    "location",
    "origin_instance_id",
    "hidden_from_feed",
)


def _v25_author_signing_bytes(inner: dict) -> bytes:
    """Reproduce the pre-change (v_25) signing-bytes construction exactly:
    every field in :data:`_V25_SIGNED_FIELDS` defaulted to ``None``, compact
    sorted JSON, same domain prefix — NO ``identity_anchor`` key."""
    body = {k: inner.get(k) for k in _V25_SIGNED_FIELDS}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b"space-post-author:v1:" + canonical


def test_domain_separated_prefix():
    assert author_signing_bytes(_inner()).startswith(b"space-post-author:v1:")


def test_legacy_no_anchor_bytes_identical_to_v25_layout():
    """REGRESSION (cross-version rollout): a legacy author with no
    ``identity_anchor`` must produce author-signing-bytes byte-for-byte equal
    to the pre-change v_25 layout — otherwise a not-yet-upgraded subscriber
    recomputes the OLD bytes, the author sig mismatches, and the (legacy,
    username-anchored) public post is silently dropped.

    Holds whether the anchor key is absent entirely or present-but-None."""
    base = _inner()
    assert author_signing_bytes(base) == _v25_author_signing_bytes(base)
    assert author_signing_bytes(_inner(identity_anchor=None)) == (
        _v25_author_signing_bytes(base)
    )


def test_legacy_signed_inner_has_no_anchor_key_and_verifies_under_v25():
    """A legacy (no-anchor) signed inner built by the NEW code carries NO
    ``identity_anchor`` key, and its ``author_sig`` verifies against the v_25
    signing-bytes layout (proving a sub-v_26 receiver accepts it)."""
    kp = generate_identity_keypair()
    username = "bob"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=None,
    )
    assert "identity_anchor" not in inner
    # A v_25 verifier (old field set, no anchor) accepts the sig.
    assert verify_ed25519(
        kp.public_key,
        _v25_author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


def test_legacy_username_anchored_author_normalised_to_v25_layout():
    """REGRESSION (live cross-version bug): every real user row carries a
    non-NULL ``identity_anchor`` — migration ``0041`` backfilled
    ``identity_anchor = username`` for every pre-existing user and every
    provision path writes one — so the producers ALWAYS hand the builder an
    anchor. For a legacy (username-anchored) author the builder MUST treat
    ``anchor == username`` as *absent*: no ``identity_anchor`` key in the
    inner, and the signature over the 13-field v_25 layout — otherwise a
    not-yet-upgraded (June 2026, ``OURS=24``) subscriber recomputes 13 fields,
    the sig mismatches, and every public post from an upgraded household is
    silently dropped."""
    kp = generate_identity_keypair()
    username = "alice"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=username,  # what the producers actually pass
    )
    assert "identity_anchor" not in inner
    # Byte-identical to the v_25 layout — a sub-v_26 receiver accepts it.
    assert author_signing_bytes(inner) == _v25_author_signing_bytes(inner)
    assert verify_ed25519(
        kp.public_key,
        _v25_author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )
    # ...and a v_26+ receiver accepts it too (username derivation still holds).
    assert verify_signed_author_inner(inner) is True


def test_empty_anchor_normalised_to_v25_layout():
    """An empty-string anchor is *absent* too. The verifier already treats
    ``""`` as no anchor for the derivation (``anchor if anchor else None``), so
    a producer that signed an ``identity_anchor: ""`` key would verify on v_26+
    but lose the v_25 layout for no reason — the builder normalises it the
    same way as ``anchor == username`` so all three sites agree."""
    kp = generate_identity_keypair()
    username = "alice"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor="",
    )
    assert "identity_anchor" not in inner
    assert author_signing_bytes(inner) == _v25_author_signing_bytes(inner)
    assert verify_signed_author_inner(inner) is True


def test_legacy_normalisation_matches_explicit_none():
    """``anchor == username`` and ``anchor=None`` produce the SAME inner (bar
    the randomised signature) — the normalisation is exactly "treat as
    absent", nothing more."""
    kp = generate_identity_keypair()
    username = "alice"
    kw = dict(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    a = build_signed_author_inner(author_identity_anchor=username, **kw)
    b = build_signed_author_inner(author_identity_anchor=None, **kw)
    a.pop("author_sig")
    b.pop("author_sig")
    assert a == b


def test_uuid_anchored_author_still_signs_14_field_layout():
    """A uuid4-anchored author (``anchor != username``) is NOT normalised: the
    inner carries the anchor and the signature covers the 14-field layout
    (differs from v_25 bytes — such authors legitimately verify on v_26+ only)."""
    kp = generate_identity_keypair()
    username = "alice"
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, anchor)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=anchor,
    )
    assert inner["identity_anchor"] == anchor
    assert author_signing_bytes(inner) != _v25_author_signing_bytes(inner)
    assert json.loads(author_signing_bytes(inner)[len(b"space-post-author:v1:") :]) == {
        **{k: inner.get(k) for k in _V25_SIGNED_FIELDS},
        "identity_anchor": anchor,
    }
    assert verify_signed_author_inner(inner) is True


def test_relayer_adding_username_anchor_to_legacy_inner_rejected():
    """Security: a relayer that ADDS ``identity_anchor: <username>`` to a
    legacy (normalised, anchor-free) inner cannot forge anything. On a v_26+
    receiver the recomputed bytes now include the key, so the author_sig
    fails; a v_25 receiver ignores the unknown key and is unchanged. The
    derivation is unaffected either way (anchor == username)."""
    kp = generate_identity_keypair()
    username = "alice"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=username,
    )
    assert verify_signed_author_inner(inner) is True
    tampered = dict(inner)
    tampered["identity_anchor"] = username
    assert verify_signed_author_inner(tampered) is False
    # The v_25 view of the tampered inner is byte-identical to the original —
    # the addition is inert there, not a forgery vector.
    assert _v25_author_signing_bytes(tampered) == _v25_author_signing_bytes(inner)


def test_relayer_removing_anchor_from_uuid_inner_rejected():
    """Security: a relayer that REMOVES the anchor from a uuid4-anchored inner
    makes the self-cert fall back to username derivation, which does not equal
    the uuid-derived ``author_user_id`` — rejected before the sig is even
    checked (and the sig would fail too, since the anchor was signed)."""
    kp = generate_identity_keypair()
    username = "alice"
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, anchor)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=anchor,
    )
    assert verify_signed_author_inner(inner) is True
    stripped = dict(inner)
    stripped.pop("identity_anchor")
    assert derive_user_id(kp.public_key, username) != stripped["author_user_id"]
    assert verify_signed_author_inner(stripped) is False
    # Even a v_25 receiver (13-field bytes, username derivation) rejects it —
    # the uuid author never verified there in the first place.
    assert not verify_ed25519(
        kp.public_key,
        _v25_author_signing_bytes(stripped),
        b64url_decode(stripped["author_sig"]),
    )


def test_author_sig_excluded_from_signed_bytes():
    """Presence of ``author_sig`` must not change the signing bytes."""
    without = author_signing_bytes(_inner())
    with_sig = author_signing_bytes(_inner(author_sig="anything"))
    assert without == with_sig


def test_deterministic_and_order_independent():
    a = author_signing_bytes(_inner())
    # Same logical content, different dict insertion order.
    reordered = {}
    for k in reversed(list(_inner().keys())):
        reordered[k] = _inner()[k]
    b = author_signing_bytes(reordered)
    assert a == b


def test_changing_an_attributable_field_changes_bytes():
    base = author_signing_bytes(_inner())
    assert author_signing_bytes(_inner(content="tampered")) != base
    assert author_signing_bytes(_inner(author_user_id="someone-else")) != base
    assert author_signing_bytes(_inner(author_pk="cd" * 32)) != base


def test_missing_optional_field_defaults_to_none():
    full = _inner()
    full.pop("media_url")
    # media_url absent should equal media_url=None.
    assert author_signing_bytes(full) == author_signing_bytes(_inner(media_url=None))


def _post(**over) -> Post:
    base = dict(
        id="p1",
        author="u1",
        type=PostType.TEXT,
        created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        content="hi",
        media_url=None,
        image_urls=("a.jpg", "b.jpg"),
        hidden_from_feed=False,
        location=None,
    )
    base.update(over)
    return Post(**base)


def test_build_signed_author_inner_fields_and_roundtrip():
    kp = generate_identity_keypair()
    post = _post()
    inner = build_signed_author_inner(
        post=post,
        space_id="sp",
        author_username="bob",
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    assert inner["post_id"] == "p1"
    assert inner["space_id"] == "sp"
    assert inner["author_user_id"] == "u1"
    assert inner["author_pk"] == kp.public_key.hex()
    assert inner["author_username"] == "bob"
    assert inner["type"] == "text"
    assert inner["content"] == "hi"
    assert inner["media_url"] is None
    assert inner["image_urls"] == ["a.jpg", "b.jpg"]
    assert inner["created_at"] == "2026-06-10T00:00:00+00:00"
    assert inner["hidden_from_feed"] is False
    assert inner["origin_instance_id"] == "origin.home"
    assert "location" not in inner
    # author_sig verifies against the author_pk via the canonical bytes.
    assert verify_ed25519(
        kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


def test_build_signed_author_inner_with_location_roundtrip():
    kp = generate_identity_keypair()
    post = _post(
        type=PostType.LOCATION,
        location=LocationData(lat=1.2345, lon=6.789, label="home"),
    )
    inner = build_signed_author_inner(
        post=post,
        space_id="sp",
        author_username="bob",
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    assert inner["location"] == {"lat": 1.2345, "lon": 6.789, "label": "home"}
    assert verify_ed25519(
        kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


def _signed_inner(**over):
    """A well-formed, self-certifying, author-signed inner for verify tests."""
    kp = generate_identity_keypair()
    username = "bob"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    inner.update(over)
    return kp, inner


def test_verify_signed_author_inner_accepts_valid():
    _kp, inner = _signed_inner()
    assert verify_signed_author_inner(inner) is True


def test_verify_signed_author_inner_rejects_missing_identity_fields():
    _kp, inner = _signed_inner()
    for field_name in ("post_id", "author_user_id", "author_pk", "author_username"):
        bad = dict(inner)
        bad.pop(field_name)
        assert verify_signed_author_inner(bad) is False


def test_verify_signed_author_inner_rejects_self_cert_mismatch():
    # author_user_id does not derive from (author_pk, author_username).
    _kp, inner = _signed_inner(author_user_id="not-the-right-id")
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_tampered_content():
    # author_sig was computed over the original content; tampering breaks it.
    _kp, inner = _signed_inner()
    inner["content"] = "tampered"
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_missing_sig():
    _kp, inner = _signed_inner()
    inner.pop("author_sig")
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_malformed_sig():
    _kp, inner = _signed_inner()
    inner["author_sig"] = "!!!not base64!!!"
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_rejects_wrong_signing_key():
    # author_pk is the real author, but the sig was made by a different key.
    impostor = generate_identity_keypair()
    kp, inner = _signed_inner()
    inner["author_sig"] = b64url_encode(
        sign_ed25519(impostor.private_key, author_signing_bytes(inner))
    )
    assert verify_signed_author_inner(inner) is False


def test_verify_signed_author_inner_fail_closed_on_non_dict():
    assert verify_signed_author_inner({}) is False


def test_identity_anchor_derivation_accepted():
    """A public author whose ``author_user_id`` derives from a uuid
    ``identity_anchor`` (NOT the username) is accepted — the anchor is the
    derivation input when present."""
    kp = generate_identity_keypair()
    username = "bob"
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"  # uuid4 hex, != username
    # user_id derives from the ANCHOR, not the username.
    user_id = derive_user_id(kp.public_key, anchor)
    inner = build_signed_author_inner(
        post=_post(author=user_id),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=anchor,
    )
    assert inner["identity_anchor"] == anchor
    # Username-derivation would NOT match this user_id (proves the anchor is
    # genuinely the derivation input, not the username).
    assert derive_user_id(kp.public_key, username) != user_id
    assert verify_signed_author_inner(inner) is True


def test_legacy_no_anchor_still_uses_username_derivation():
    """Without an ``identity_anchor`` (legacy), username-derivation applies:
    a user_id that doesn't derive from the username is rejected."""
    kp = generate_identity_keypair()
    username = "bob"
    # Legacy: user_id derives from the username, no anchor on the block.
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=None,
    )
    # The key is OMITTED entirely (not present-but-None) so the signed bytes
    # match the pre-change v_25 layout.
    assert "identity_anchor" not in inner
    assert verify_signed_author_inner(inner) is True
    # A username-derivation MISMATCH (no anchor) is rejected.
    bad = dict(inner)
    bad["author_user_id"] = "not-the-right-id"
    assert verify_signed_author_inner(bad) is False


def test_forged_identity_anchor_rejected():
    """A swapped ``identity_anchor`` (user_id no longer derives from it) is
    rejected — and because the anchor is signed, the author_sig also breaks."""
    kp = generate_identity_keypair()
    username = "bob"
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    user_id = derive_user_id(kp.public_key, anchor)
    inner = build_signed_author_inner(
        post=_post(author=user_id),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=anchor,
    )
    assert verify_signed_author_inner(inner) is True
    # Forge the anchor — user_id no longer derives from it.
    inner["identity_anchor"] = "ffffffffffffffffffffffffffffffff"
    assert verify_signed_author_inner(inner) is False


def test_identity_anchor_is_signed_and_roundtrips():
    """``identity_anchor`` is bound to the author signature so a relayer cannot
    strip or swap it undetectably."""
    kp = generate_identity_keypair()
    username = "bob"
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, anchor)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
        author_identity_anchor=anchor,
    )
    # The anchor is part of the signed bytes.
    assert author_signing_bytes(_inner(identity_anchor=anchor)) != author_signing_bytes(
        _inner(identity_anchor="different")
    )
    # Round-trips with the anchor signed.
    assert verify_signed_author_inner(inner) is True
    # Stripping the anchor after signing breaks verification (sig was over it).
    stripped = dict(inner)
    stripped.pop("identity_anchor")
    assert verify_signed_author_inner(stripped) is False


def test_hidden_from_feed_is_signed_and_roundtrips():
    """``hidden_from_feed`` is bound to the author signature so a relayer
    cannot flip feed visibility undetectably."""
    kp = generate_identity_keypair()
    username = "bob"
    inner = build_signed_author_inner(
        post=_post(author=derive_user_id(kp.public_key, username)),
        space_id="sp",
        author_username=username,
        author_pk=kp.public_key,
        author_identity_seed=kp.private_key,
        origin_instance_id="origin.home",
    )
    # Round-trips with hidden_from_feed signed.
    assert verify_signed_author_inner(inner) is True
    # Flipping hidden_from_feed after signing breaks verification.
    inner["hidden_from_feed"] = not inner["hidden_from_feed"]
    assert verify_signed_author_inner(inner) is False
