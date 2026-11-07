"""The signed descriptor a page proposal carries (``services/page_media_refs``)."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.services.page_media_refs import (
    SIG_FIELD,
    SIG_SUITE_FIELD,
    UnsupportedPageMediaRefsSigSuite,
    signed_page_media_refs,
    verified_page_media_refs,
)

REFS = {"n.webp": {"blob_id": "a" * 64}}
BIND = {
    "space_id": "sp-1",
    "page_id": "pg-1",
    "origin": "hh-e",
    "proposal_hash": "sha256:" + "b" * 64,
}


def _signed(kp, **over):
    return signed_page_media_refs(
        **{**BIND, **over}, media_refs=REFS, identity_seed=kp.private_key
    )


def test_a_signed_descriptor_verifies_for_exactly_its_proposal():
    kp = generate_identity_keypair()
    payload = _signed(kp)
    assert verified_page_media_refs(payload, **BIND, origin_pk=kp.public_key) == REFS
    # Another page, household, proposal or space: never.
    for key, value in (
        ("page_id", "pg-2"),
        ("origin", "hh-x"),
        ("proposal_hash", "sha256:" + "c" * 64),
        ("space_id", "sp-2"),
    ):
        bind = {**BIND, key: value}
        assert (
            verified_page_media_refs(payload, **bind, origin_pk=kp.public_key) is None
        )


def test_an_unsigned_forged_or_swapped_descriptor_is_dropped():
    kp = generate_identity_keypair()
    other = generate_identity_keypair()
    payload = _signed(kp)
    assert verified_page_media_refs(payload, **BIND, origin_pk=other.public_key) is None
    unsigned = {
        k: v for k, v in payload.items() if k not in (SIG_FIELD, SIG_SUITE_FIELD)
    }
    assert verified_page_media_refs(unsigned, **BIND, origin_pk=kp.public_key) is None
    swapped = {**payload, "media_refs": {"x.webp": {"blob_id": "d" * 64}}}
    assert verified_page_media_refs(swapped, **BIND, origin_pk=kp.public_key) is None
    assert verified_page_media_refs(payload, **BIND, origin_pk=None) is None
    assert verified_page_media_refs({}, **BIND, origin_pk=kp.public_key) is None
    bad_sig = {**payload, SIG_FIELD: "!!"}
    assert verified_page_media_refs(bad_sig, **BIND, origin_pk=kp.public_key) is None


def test_an_unknown_suite_is_refused_never_defaulted():
    kp = generate_identity_keypair()
    payload = {**_signed(kp), SIG_SUITE_FIELD: "ed448"}
    with pytest.raises(UnsupportedPageMediaRefsSigSuite):
        verified_page_media_refs(payload, **BIND, origin_pk=kp.public_key)
