"""Render an exception for a log line without producing an empty string.

``log.warning("… failed: %s", exc)`` is the idiom throughout the codebase,
and it works right up until the exception carries no message. Several
aiohttp connection errors are exactly that: ``ClientOSError``,
``ClientConnectionError``, ``ClientPayloadError`` and
``ServerTimeoutError`` all render as ``""``, as do bare ``OSError`` /
``TimeoutError`` / ``ConnectionResetError``. So the operator gets:

    WARNING federation.transport: HTTPS-inbox send to <peer> failed:

A real production log showed five of those in a row while a household had
silently lost federation with every peer: the line names the peer and then
says nothing about what went wrong, which is the one thing it exists to
report.

:func:`describe_exception` falls back to the class name (and the module,
when it isn't a builtin) so the line always carries something actionable.
"""

from __future__ import annotations

__all__ = ["describe_exception"]


def describe_exception(exc: BaseException) -> str:
    """Return a non-empty, human-readable description of ``exc``.

    Prefers ``str(exc)`` — that is what the author of the call site meant
    to show. Falls back to the qualified class name when the message is
    empty or whitespace, which is the case for several aiohttp connection
    errors.

    The type is included alongside a present-but-terse message too, since
    ``"Cannot connect to host x:443 ssl:default [None]"`` reads very
    differently from a ``TimeoutError`` and the distinction drives what an
    operator does next.
    """
    text = str(exc).strip()
    cls = type(exc)
    name = cls.__qualname__
    module = getattr(cls, "__module__", "")
    if module and module not in ("builtins", "__main__"):
        name = f"{module}.{name}"
    if not text:
        return f"{name} (no message)"
    return f"{name}: {text}"
