"""Canonical JSON — the bytes a signature or a hash covers.

Social Home serializes JSON with :mod:`orjson` everywhere except here. A
signature (or a digest) that a peer recomputes from *parsed* JSON covers the
exact bytes of one fixed encoding, and orjson cannot reproduce it: it has no
``ensure_ascii=True`` (a non-ASCII name would sign as raw UTF-8 instead of
``\\uXXXX`` escapes) and it renders small or large floats differently
(``5e-05`` vs ``5e-5``, ``1e+16`` vs ``1e16``). Every signed or hashed JSON
byte therefore goes through these helpers and nothing else, so households and
GFSs on either side of an upgrade keep agreeing on them.

This is the only module under ``socialhome/`` (migrations aside) that may
import the stdlib :mod:`json` — ruff ``TID251`` bans it everywhere else.

- :func:`canonical_bytes` / :func:`canonical_str` — sorted keys, compact
  separators, ASCII-only. The form nearly every signed shape uses.
- :func:`canonical_utf8_bytes` / :func:`canonical_utf8_str` — the same, but
  non-ASCII stays raw UTF-8 (``ensure_ascii=False``). Only for the few shapes
  that were first signed / hashed that way; never pick it for a new one.
- :func:`legacy_json_dumps` — the stdlib call with its arguments spelled out,
  for the handful of signed / hashed shapes that predate both forms (unsorted
  keys, default separators). Never pick it for a new shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

__all__ = [
    "canonical_bytes",
    "canonical_str",
    "canonical_utf8_bytes",
    "canonical_utf8_str",
    "legacy_json_dumps",
]


def canonical_str(obj: Any) -> str:
    """``obj`` as sorted-key, compact, ASCII-only JSON text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_bytes(obj: Any) -> bytes:
    """:func:`canonical_str` as UTF-8 (all ASCII) bytes."""
    return canonical_str(obj).encode("utf-8")


def canonical_utf8_str(obj: Any) -> str:
    """``obj`` as sorted-key, compact JSON text with non-ASCII kept raw."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_utf8_bytes(obj: Any) -> bytes:
    """:func:`canonical_utf8_str` encoded as UTF-8."""
    return canonical_utf8_str(obj).encode("utf-8")


def legacy_json_dumps(
    obj: Any,
    *,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = True,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Exactly ``json.dumps(obj, sort_keys=…, separators=…,
    ensure_ascii=…, default=…)`` — for a pre-existing signed / hashed shape whose
    encoding is neither canonical form. Each caller passes its historical
    arguments explicitly so the bytes stay what peers already verify."""
    return json.dumps(
        obj,
        sort_keys=sort_keys,
        separators=separators,
        ensure_ascii=ensure_ascii,
        default=default,
    )
