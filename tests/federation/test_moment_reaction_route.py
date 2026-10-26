"""Unit tests for :mod:`socialhome.federation.moment_reaction_route` (v_67)."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_routing_secret,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.keywrap_seal import UnsupportedKemSuite
from socialhome.federation.moment_reaction_route import (
    MAX_RETURN_TOKENS,
    PAD_BUCKET,
    REACTION_SIG_SUITE_ED25519,
    RETURN_TOKEN_SUITE_AESGCM_256,
    SUPPORTED_REACTION_SIG_SUITES,
    SUPPORTED_RETURN_TOKEN_SUITES,
    ReturnTokenRefused,
    UnsupportedReactionSigSuite,
    UnsupportedReturnTokenSuite,
    build_reaction,
    mint_return_token,
    open_reaction,
    open_return_token,
    origin_keywrap_fields,
    return_token_key,
    return_tokens_of,
    seal_reaction,
    sealed_shape_ok,
    verified_origin_keywrap,
    verify_reaction,
)

HOUSEHOLD = generate_identity_keypair()
ORIGIN = generate_identity_keypair()
ORIGIN_ID = derive_instance_id(ORIGIN.public_key)
KW = generate_x25519_keypair()


def _inner(**over) -> dict:
    inner = build_reaction(
        seed=HOUSEHOLD.private_key,
        identity_pk=HOUSEHOLD.public_key,
        reactor_instance_id=derive_instance_id(HOUSEHOLD.public_key),
        reactor_user_id=derive_user_id(HOUSEHOLD.public_key, "anchor"),
        reactor_anchor="anchor",
        moment_id="m-1",
        author_user_id="u-author",
        origin_instance_id=ORIGIN_ID,
        emoji="👏",
        occurred_at_ms=1_700_000_000_000,
    )
    inner.update(over)
    return inner


def test_suites_are_tagged_constants():
    assert REACTION_SIG_SUITE_ED25519 in SUPPORTED_REACTION_SIG_SUITES
    assert RETURN_TOKEN_SUITE_AESGCM_256 in SUPPORTED_RETURN_TOKEN_SUITES
    assert _inner()["reaction_sig_suite"] == "ed25519"


def test_a_signed_reaction_verifies_and_a_changed_one_does_not():
    assert verify_reaction(_inner()) is None
    assert verify_reaction(_inner(emoji="🔥")) == "reaction signature does not verify"
    assert verify_reaction(_inner(reactor_instance_id="x")) is not None
    assert verify_reaction(_inner(reactor_user_id="x")) is not None
    assert verify_reaction(_inner(reactor_identity_pk="zz")) is not None


@pytest.mark.parametrize("suite", ["rot13", None, ""])
def test_an_unknown_signature_suite_raises(suite):
    with pytest.raises(UnsupportedReactionSigSuite):
        verify_reaction(_inner(reaction_sig_suite=suite))


def test_seal_pads_to_one_bucket_and_opens_with_the_key_wrap_key():
    short = seal_reaction(origin_keywrap_pub=KW.public_key, inner=_inner(emoji=None))
    long = seal_reaction(origin_keywrap_pub=KW.public_key, inner=_inner())
    # A relay can't tell an add from a removal by size.
    assert len(short["ciphertext"]) == len(long["ciphertext"])
    assert sealed_shape_ok(long)
    opened = open_reaction(sealed=long, keywrap_priv=KW.private_key)
    assert opened == _inner()
    assert len(opened) < PAD_BUCKET


def test_a_seal_to_another_key_does_not_open():
    sealed = seal_reaction(origin_keywrap_pub=KW.public_key, inner=_inner())
    with pytest.raises(InvalidTag):
        open_reaction(sealed=sealed, keywrap_priv=generate_x25519_keypair().private_key)


def test_an_unknown_kem_suite_is_refused():
    sealed = seal_reaction(origin_keywrap_pub=KW.public_key, inner=_inner())
    assert not sealed_shape_ok({**sealed, "kem_suite": "x448"})
    with pytest.raises(UnsupportedKemSuite):
        open_reaction(
            sealed={**sealed, "kem_suite": "x448"}, keywrap_priv=KW.private_key
        )


@pytest.mark.parametrize(
    "inner",
    [
        {"kind": "other"},
        {**_inner(), "kind": "space_key"},
        {**_inner(), "extra": 1},
        {**_inner(), "emoji": "two words"},
        {**_inner(), "emoji": "🔥" * 9},
        {**_inner(), "occurred_at_ms": "soon"},
        {**_inner(), "occurred_at_ms": True},
        {**_inner(), "moment_id": ""},
        {**_inner(), "display_name": "x" * 65},
    ],
)
def test_a_malformed_reaction_does_not_open(inner):
    sealed = seal_reaction(origin_keywrap_pub=KW.public_key, inner=inner)
    with pytest.raises(ValueError):
        open_reaction(sealed=sealed, keywrap_priv=KW.private_key)


def test_a_display_name_up_to_64_is_taken():
    inner = {**_inner(), "display_name": "Rita"}
    sealed = seal_reaction(origin_keywrap_pub=KW.public_key, inner=inner)
    assert (
        open_reaction(sealed=sealed, keywrap_priv=KW.private_key)["display_name"]
        == "Rita"
    )


@pytest.mark.parametrize(
    "sealed",
    [
        None,
        {},
        {"kem_suite": "x25519"},
        {"kem_suite": "x25519", "eph_pk": "a", "ciphertext": "b" * 3001},
    ],
)
def test_sealed_shape_bounds(sealed):
    assert not sealed_shape_ok(sealed)


# ── Return tokens ─────────────────────────────────────────────────────────


KEY = return_token_key(generate_routing_secret())


def _token(**over) -> str:
    args = {
        "key": KEY,
        "moment_id": "m-1",
        "next_hop": "peer-next",
        "prev_hop": "peer-prev",
        "expires_at_s": 2_000_000_000,
    }
    args.update(over)
    return mint_return_token(**args)


def test_a_token_opens_to_its_previous_hop():
    assert (
        open_return_token(
            key=KEY, token=_token(), moment_id="m-1", from_instance="peer-next", now_s=1
        )
        == "peer-prev"
    )


@pytest.mark.parametrize(
    ("kwargs", "token_over"),
    [
        ({"moment_id": "m-2"}, {}),
        ({"from_instance": "peer-other"}, {}),
        ({"now_s": 2_000_000_000}, {}),
        ({"key": return_token_key(generate_routing_secret())}, {}),
        ({}, {"expires_at_s": 5}),
    ],
    ids=["other moment", "other neighbour", "expired", "another household", "past"],
)
def test_a_token_opens_only_for_its_moment_neighbour_and_time(kwargs, token_over):
    args = {
        "key": KEY,
        "token": _token(**token_over),
        "moment_id": "m-1",
        "from_instance": "peer-next",
        "now_s": 10,
    }
    args.update(kwargs)
    with pytest.raises(ReturnTokenRefused):
        open_return_token(**args)


@pytest.mark.parametrize("token", ["", "AAAA", "not base64!!"])
def test_a_garbage_token_is_refused(token):
    with pytest.raises(ReturnTokenRefused):
        open_return_token(
            key=KEY, token=token, moment_id="m-1", from_instance="peer-next", now_s=1
        )


def test_return_tokens_of_bounds_and_suite():
    assert return_tokens_of({}) == []
    assert return_tokens_of({"rt": ["a"], "rt_suite": "aesgcm-256"}) == ["a"]
    assert return_tokens_of({"rt": "a"}) is None
    assert return_tokens_of({"rt": [1]}) is None
    assert return_tokens_of({"rt": ["a"] * (MAX_RETURN_TOKENS + 1)}) is None
    with pytest.raises(UnsupportedReturnTokenSuite):
        return_tokens_of({"rt": ["a"], "rt_suite": "chacha"})
    with pytest.raises(UnsupportedReturnTokenSuite):
        return_tokens_of({"rt": ["a"]})


# ── The origin's key-wrap key on a create ─────────────────────────────────


def _create_with(fields: dict, identity=ORIGIN) -> dict:
    return {"origin_identity_pk": identity.public_key.hex(), **fields}


def _bound_fields(identity=ORIGIN, kw=KW) -> dict:
    return origin_keywrap_fields(
        kw.public_key, b64url_encode(sign_ed25519(identity.private_key, kw.public_key))
    )


def test_a_bound_origin_key_wrap_key_is_trusted():
    payload = _create_with(_bound_fields())
    assert set(_bound_fields()) == {
        "origin_keywrap_pk",
        "origin_keywrap_sig",
        "origin_keywrap_suite",
    }
    assert (
        verified_origin_keywrap(
            payload, origin_instance_id=ORIGIN_ID, pinned_identity_pk=None
        )
        == KW.public_key
    )
    assert (
        verified_origin_keywrap(
            payload, origin_instance_id=ORIGIN_ID, pinned_identity_pk=ORIGIN.public_key
        )
        == KW.public_key
    )


@pytest.mark.parametrize(
    ("payload", "pinned"),
    [
        (_create_with({}), None),
        (_create_with({**_bound_fields(), "origin_keywrap_suite": "x448"}), None),
        (_create_with({**_bound_fields(), "origin_keywrap_pk": "zz"}), None),
        (
            _create_with(
                {
                    **_bound_fields(),
                    "origin_keywrap_pk": generate_x25519_keypair().public_key.hex(),
                }
            ),
            None,
        ),
        (_create_with(_bound_fields(identity=HOUSEHOLD), identity=HOUSEHOLD), None),
        (_create_with(_bound_fields()), HOUSEHOLD.public_key),
    ],
    ids=[
        "none shipped",
        "unknown suite",
        "malformed key",
        "substituted key",
        "bound by another household",
        "differs from pinned",
    ],
)
def test_an_untrusted_origin_key_wrap_key_is_ignored(payload, pinned):
    assert (
        verified_origin_keywrap(
            payload, origin_instance_id=ORIGIN_ID, pinned_identity_pk=pinned
        )
        is None
    )
