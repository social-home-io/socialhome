"""Tests for :func:`socialhome.exception_text.describe_exception`."""

from __future__ import annotations

import aiohttp
import pytest

from socialhome.exception_text import describe_exception


def test_empty_message_falls_back_to_the_type():
    """The bug this exists for: ``str(exc)`` is empty, so the log line
    said nothing at all about what failed."""
    out = describe_exception(aiohttp.ClientOSError())
    assert out.strip()
    assert "ClientOSError" in out
    assert "no message" in out


def test_whitespace_only_message_is_treated_as_empty():
    class _Blank(Exception):
        def __str__(self) -> str:
            return "   "

    assert "no message" in describe_exception(_Blank())


def test_present_message_is_kept_and_typed():
    """A real message survives verbatim, with the type alongside it —
    'Cannot connect to host' and a bare timeout call for different
    operator responses."""
    out = describe_exception(OSError("Cannot connect to host h:443"))
    assert "Cannot connect to host h:443" in out
    assert "OSError" in out


def test_builtin_types_are_not_module_qualified():
    """``builtins.ValueError`` is noise; ``ValueError`` is not."""
    out = describe_exception(ValueError("bad"))
    assert out == "ValueError: bad"


def test_non_builtin_types_are_module_qualified():
    """Two libraries can each define ``ClientError``; say which one."""
    out = describe_exception(aiohttp.ClientError("boom"))
    assert out.startswith("aiohttp.")
    assert "ClientError" in out


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ServerDisconnectedError(),  # has a default message
        aiohttp.ClientOSError(),  # renders empty
        aiohttp.ClientConnectionError(),
        aiohttp.ServerTimeoutError(),
        ConnectionResetError(),
        TimeoutError(),
        OSError(),
    ],
)
def test_never_returns_an_empty_string(exc):
    """The single invariant every caller depends on."""
    assert describe_exception(exc).strip()
