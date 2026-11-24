"""Tests for socialhome.writer_key — the channel writer key's sign / verify.

The space-keyed writer GROUP key of v_50 (its derivation, authority cert and
grant) went with the GFS's space-keyed routes in v_96; the removal proof is
:func:`test_the_space_keyed_writer_group_key_is_gone`.
"""

from __future__ import annotations

import os

import pytest

from socialhome import writer_key
from socialhome.crypto import ed25519_public_key
from socialhome.domain.writer_key import UnsupportedWriterKeySuite
from socialhome.writer_key import sign_with_writer_key, verify_writer_sig


def test_writer_sig_round_trip() -> None:
    seed = os.urandom(32)
    pk = ed25519_public_key(seed)
    sig = sign_with_writer_key(seed, b"hello")
    assert verify_writer_sig(
        writer_pk=pk, message=b"hello", writer_sig=sig, suite="ed25519"
    )
    assert not verify_writer_sig(
        writer_pk=pk, message=b"other", writer_sig=sig, suite="ed25519"
    )
    assert not verify_writer_sig(
        writer_pk=pk, message=b"hello", writer_sig="!!", suite="ed25519"
    )
    with pytest.raises(UnsupportedWriterKeySuite):
        verify_writer_sig(writer_pk=pk, message=b"hello", writer_sig=sig, suite="rsa")


@pytest.mark.parametrize(
    "name",
    [
        "derive_writer_seed",
        "sign_writer_key_cert",
        "verify_writer_key_cert",
        "issue_writer_key_grant",
        "verify_writer_key_grant",
    ],
)
def test_the_space_keyed_writer_group_key_is_gone(name: str) -> None:
    """v_96 — nothing derives, certifies or hands out the space writer
    group key any more."""
    assert not hasattr(writer_key, name)
