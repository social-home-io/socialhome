"""Tests for socialhome.domain.writer_key — the writer key suite tag."""

from __future__ import annotations

import pytest

from socialhome.domain import writer_key
from socialhome.domain.writer_key import (
    SUPPORTED_WRITER_KEY_SUITES,
    WRITER_KEY_SUITE_ED25519,
    UnsupportedWriterKeySuite,
)


def test_suite_constants() -> None:
    assert WRITER_KEY_SUITE_ED25519 == "ed25519"
    assert WRITER_KEY_SUITE_ED25519 in SUPPORTED_WRITER_KEY_SUITES
    assert issubclass(UnsupportedWriterKeySuite, ValueError)


@pytest.mark.parametrize(
    "name", ["WriterKeyCert", "WriterKeyGrant", "WRITER_KEY_FIELD"]
)
def test_the_space_writer_key_shapes_are_gone(name: str) -> None:
    """v_96 — no space writer key cert or grant rides any wire."""
    assert not hasattr(writer_key, name)
