"""Bytes and frames this GFS node pushed to households, by kind and size.

Every byte the GFS fans out is multiplied by its followers, so this is the
number that says whether a payload change paid off. :class:`FanoutStats`
counts what :class:`~socialhome.global_server.ws_registry
.GfsWebSocketRegistry` actually wrote to sockets — per frame kind
(``relay:space_post_public``, ``relay:space_item``, ``envelope``, …) and per
size class — since the node started. The admin overview
(``GET /admin/api/overview``) shows it.

The GFS learns nothing new here: the frame kind is the routing-visible
``type`` / ``event_type`` it already reads, and the size is the length of
what it sent. Counters are per node and in memory; a restart zeroes them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.space_item import ITEM_SIZE_BUCKETS

#: At most this many distinct frame kinds are kept apart; any further kind
#: (a hostile publisher inventing event types) counts as :data:`OTHER_KIND`.
MAX_KINDS: int = 64

OTHER_KIND: str = "other"

#: Longest kind label kept verbatim.
_MAX_KIND_CHARS: int = 64

#: Size classes, named after the padding bucket a frame of that size
#: carries: a sealed item of ``b`` bytes travels as base64 (×4/3) plus the
#: routing and signature fields (under 2 KiB), so its frame is at most
#: ``b * 4 / 3 + 2048`` bytes. Anything larger is ``"larger"``.
SIZE_CLASSES: tuple[tuple[str, int], ...] = tuple(
    (f"{b // 1024}k", b * 4 // 3 + 2048) for b in ITEM_SIZE_BUCKETS
)

LARGER_CLASS: str = "larger"


def size_class(frame_bytes: int) -> str:
    """The size class (padding-bucket name) a frame of ``frame_bytes`` is in."""
    for name, ceiling in SIZE_CLASSES:
        if frame_bytes <= ceiling:
            return name
    return LARGER_CLASS


def frame_kind(frame: dict) -> str:
    """``type`` (plus ``:event_type`` when the frame names one) — the label
    a frame is counted under."""
    kind = frame.get("type")
    label = kind if isinstance(kind, str) and kind else "untyped"
    event_type = frame.get("event_type")
    if isinstance(event_type, str) and event_type:
        label = f"{label}:{event_type}"
    return label[:_MAX_KIND_CHARS]


@dataclass(slots=True)
class _Tally:
    frames: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.frames += 1
        self.bytes += size

    def to_dict(self) -> dict[str, int]:
        return {"frames": self.frames, "bytes": self.bytes}


class FanoutStats:
    """In-memory counters of the frames pushed to household sockets."""

    __slots__ = ("_by_kind", "_by_size", "_total")

    def __init__(self) -> None:
        self._by_kind: dict[str, _Tally] = {}
        self._by_size: dict[str, _Tally] = {}
        self._total = _Tally()

    def record(self, kind: str, frame_bytes: int) -> None:
        """Count one frame of ``frame_bytes`` bytes pushed as ``kind``."""
        if kind not in self._by_kind and len(self._by_kind) >= MAX_KINDS:
            kind = OTHER_KIND
        self._by_kind.setdefault(kind, _Tally()).add(frame_bytes)
        self._by_size.setdefault(size_class(frame_bytes), _Tally()).add(frame_bytes)
        self._total.add(frame_bytes)

    def snapshot(self) -> dict:
        """``{"total", "by_kind", "by_size"}`` — each a ``{frames, bytes}``."""
        return {
            "total": self._total.to_dict(),
            "by_kind": {k: t.to_dict() for k, t in sorted(self._by_kind.items())},
            "by_size": {k: t.to_dict() for k, t in self._by_size.items()},
        }
