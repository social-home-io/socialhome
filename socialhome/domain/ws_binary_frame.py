"""Binary GFS→household WebSocket frames (``ws_binary1``).

A GFS pushes every relayed item to every follower as a JSON text frame, and
the item's ciphertext rides in it as base64url — a third more bytes than the
ciphertext itself, on the largest part of nearly every frame. A household
that lists ``ws_binary1`` in its WS hello ``accepts`` gets, on THAT socket
only, a binary frame carrying the ciphertext as raw bytes:

    version (1 byte, 1) | header length (u16, big-endian) | header | body

``header`` is the frame's compact JSON with the ciphertext field cut down to
its prefix (everything up to and including the last ``:`` — the
``b64url(nonce):`` part of a ``nonce:ciphertext`` string, or ``""``) and one
added key, ``bin_path``, naming where that field sits; ``body`` is the
base64url-decoded rest. The household rebuilds the exact string —
unpadded base64url is deterministic, and the GFS only cuts a field whose
decode re-encodes to the same text — so every signature over it (the
authority signature, a moment's author signature) verifies unchanged and the
publisher is untouched. Other sockets keep text frames: the choice is per
connection, so nobody ever falls back.

Which field is the ciphertext is an explicit list per frame type
(:data:`BINARY_CIPHERTEXT_PATHS`); a frame whose type is not listed, or
whose field is absent, short or not exact base64url, goes as text.

Pure module — no I/O. Used by the GFS (:func:`encode_binary_frame`) and the
household (:func:`decode_binary_frame`).
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from collections.abc import Callable
from typing import Any

import orjson

#: The one binary layout this build writes and reads.
WS_BINARY_VERSION: int = 1

#: The key the header names the cut field's path in.
BIN_PATH_FIELD: str = "bin_path"

#: Where each frame type keeps its ciphertext, tried in order — the first
#: path that holds a usable string is cut. ``relay`` covers a seed holder's
#: authority-signed relay (``encrypted_payload``), a targeted key handoff or
#: profiles bundle (``sealed.ciphertext``) and a member's / channel's
#: ``space_item`` (``payload`` itself); ``envelope`` the §D2b sealed
#: household-to-household relay; ``incoming_public_moment`` a public moment.
BINARY_CIPHERTEXT_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "relay": (
        ("payload", "encrypted_payload"),
        ("payload", "sealed", "ciphertext"),
        ("payload",),
    ),
    "envelope": (("sealed", "ciphertext"),),
    "incoming_public_moment": (("payload", "ciphertext"),),
}

#: Shorter ciphertexts go as text: the saving would not pay for the format.
MIN_BINARY_CIPHERTEXT_CHARS: int = 256

#: Most path elements a header may name.
MAX_BIN_PATH_DEPTH: int = 4

_HEADER = struct.Struct(">BH")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


class InvalidBinaryFrame(ValueError):
    """A binary frame that is not a well-formed ``ws_binary1`` frame."""


def _b64url_decode_exact(text: str) -> bytes | None:
    """``text`` decoded iff it is canonical unpadded base64url (it re-encodes
    to exactly ``text``), else ``None``."""
    if not _B64URL_RE.fullmatch(text):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error, ValueError:
        return None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        return None
    return raw


def _get(frame: dict, path: tuple[str, ...]) -> object:
    node: object = frame
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _with(frame: dict, path: tuple[str, ...], value: object) -> dict:
    """A copy of ``frame`` with ``path`` set to ``value`` (copies only the
    dicts on the path; the caller's frame is never mutated)."""
    out = dict(frame)
    node = out
    for key in path[:-1]:
        child = dict(node[key])
        node[key] = child
        node = child
    node[path[-1]] = value
    return out


def encode_binary_frame(
    frame: dict, *, dumps: Callable[[Any], bytes] = orjson.dumps
) -> bytes | None:
    """``frame`` as a ``ws_binary1`` frame, or ``None`` when it should go as
    text (unlisted type, no usable ciphertext field, oversized header).
    ``dumps`` encodes the header — the GFS passes the same encoder its text
    frames use, so both forms carry identical JSON."""
    paths = BINARY_CIPHERTEXT_PATHS.get(str(frame.get("type")))
    if paths is None or BIN_PATH_FIELD in frame:
        return None
    for path in paths:
        value = _get(frame, path)
        if not isinstance(value, str) or len(value) < MIN_BINARY_CIPHERTEXT_CHARS:
            continue
        cut = value.rfind(":") + 1
        body = _b64url_decode_exact(value[cut:])
        if body is None:
            return None
        header = _with(frame, path, value[:cut])
        header[BIN_PATH_FIELD] = list(path)
        raw_header = dumps(header)
        if len(raw_header) > 0xFFFF:
            return None
        return _HEADER.pack(WS_BINARY_VERSION, len(raw_header)) + raw_header + body
    return None


def decode_binary_frame(data: bytes) -> dict:
    """The JSON frame a ``ws_binary1`` frame carries, the ciphertext string
    rebuilt. Raises :class:`InvalidBinaryFrame` on anything malformed."""
    if len(data) < _HEADER.size:
        raise InvalidBinaryFrame("short frame")
    version, header_len = _HEADER.unpack_from(data)
    if version != WS_BINARY_VERSION:
        raise InvalidBinaryFrame(f"unknown version {version}")
    end = _HEADER.size + header_len
    if end > len(data):
        raise InvalidBinaryFrame("header length past the frame")
    try:
        header = orjson.loads(data[_HEADER.size : end])
    except orjson.JSONDecodeError as exc:
        raise InvalidBinaryFrame("header is not JSON") from exc
    if not isinstance(header, dict):
        raise InvalidBinaryFrame("header is not an object")
    path = header.pop(BIN_PATH_FIELD, None)
    if (
        not isinstance(path, list)
        or not 1 <= len(path) <= MAX_BIN_PATH_DEPTH
        or not all(isinstance(k, str) for k in path)
    ):
        raise InvalidBinaryFrame("bad bin_path")
    prefix = _get(header, tuple(path))
    if not isinstance(prefix, str):
        raise InvalidBinaryFrame("bin_path names no string")
    body = base64.urlsafe_b64encode(data[end:]).rstrip(b"=").decode("ascii")
    return _with(header, tuple(path), prefix + body)
