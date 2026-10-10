"""The GFSs a space is listed on — the owner's list, as members store it.

A global space is listed on one or more connection servers (GFSs) the owner
chose. The owner household derives the list from its active
``gfs_space_publications`` rows and hands it to every member household inside
the encrypted, authority-signed ``space_meta`` (``SPACE_CONFIG_CHANGED`` and
the §D1b join snapshots), so a member can reach every listed GFS — including
ones its own household never connected to — while the host is offline.

It is stored in ``spaces.gfs_servers`` (migration 0089) as a JSON array of
wire dicts::

    {gfs_instance_id, inbox_url, public_key, gfs_key_suite, display_name}

``NULL`` means "not known" (an older owner, or a space that was never
listed) and readers fall back to the household's own connections; ``[]``
means "listed nowhere".

The entry delivers a key a member will pin and verify against, so it carries
``gfs_key_suite`` (CLAUDE.md "Crypto wire shapes carry a ``*_suite`` tag"):
an unknown or missing suite is rejected, never defaulted. ``inbox_url`` is
held to the connection-server URL policy
(:func:`socialhome.peer_url.validate_peer_url` with
``require_tls_unless_private=True``) — the same rule a household applies to a
GFS it pairs with itself.

Pure: no I/O and no logging. :func:`gfs_servers_from_wire` returns the reasons
it dropped entries so the caller can log them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from ..peer_url import validate_peer_url

#: The algorithm of ``public_key`` — the GFS identity key, Ed25519, hex.
#: Phase-2 of ``docs/crypto.md`` adds a hybrid sibling.
GFS_KEY_SUITE_ED25519: str = "ed25519"
SUPPORTED_GFS_KEY_SUITES: frozenset[str] = frozenset({GFS_KEY_SUITE_ED25519})

#: Most GFSs one space is listed on. A longer list from a peer is cut here.
MAX_GFS_SERVERS: int = 8

#: Longest ``gfs_instance_id`` accepted. It is the operator's configured id
#: (``[server].instance_id``, e.g. ``gfs-node-0``): free text without
#: control characters.
MAX_GFS_INSTANCE_ID_LENGTH: int = 128

#: ``display_name`` is shown only; a longer one is cut, not rejected.
MAX_GFS_DISPLAY_NAME_LENGTH: int = 128

#: Every key of one entry on the wire (and in the column).
GFS_SERVER_WIRE_KEYS: frozenset[str] = frozenset(
    {"gfs_instance_id", "inbox_url", "public_key", "gfs_key_suite", "display_name"}
)

_ED25519_HEX = re.compile(r"[0-9a-f]{64}")


class UnsupportedGfsKeySuite(ValueError):
    """A GFS server entry names a key suite this build does not know (or
    none). Rejected — never defaulted to Ed25519."""


def _has_control_chars(value: str) -> bool:
    return any(unicodedata.category(ch)[0] == "C" for ch in value)


def _display_name(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("gfs server: display_name must be text")
    clean = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    return clean.strip()[:MAX_GFS_DISPLAY_NAME_LENGTH]


@dataclass(slots=True, frozen=True)
class GfsServer:
    """One GFS a space is listed on, as the owner named it."""

    gfs_instance_id: str
    inbox_url: str
    public_key: str
    gfs_key_suite: str
    display_name: str

    @classmethod
    def from_wire(cls, raw: object) -> "GfsServer":
        """Parse one wire / column entry.

        Raises :class:`UnsupportedGfsKeySuite` for an unknown or missing
        suite and :class:`ValueError` for any other bad field.
        """
        if not isinstance(raw, dict):
            raise ValueError("gfs server: entry is not an object")
        suite = raw.get("gfs_key_suite")
        if not isinstance(suite, str) or suite not in SUPPORTED_GFS_KEY_SUITES:
            raise UnsupportedGfsKeySuite(
                f"gfs server: unsupported gfs_key_suite {suite!r}; this build "
                f"supports {sorted(SUPPORTED_GFS_KEY_SUITES)!r}"
            )
        gfs_instance_id = raw.get("gfs_instance_id")
        if (
            not isinstance(gfs_instance_id, str)
            or not gfs_instance_id
            or len(gfs_instance_id) > MAX_GFS_INSTANCE_ID_LENGTH
            or _has_control_chars(gfs_instance_id)
        ):
            raise ValueError("gfs server: bad gfs_instance_id")
        public_key = raw.get("public_key")
        if not isinstance(public_key, str):
            raise ValueError("gfs server: bad public_key")
        public_key = public_key.lower()
        if _ED25519_HEX.fullmatch(public_key) is None:
            raise ValueError("gfs server: bad public_key")
        inbox_url = validate_peer_url(
            raw.get("inbox_url"),
            field="inbox_url",
            require_tls_unless_private=True,
        )
        return cls(
            gfs_instance_id=gfs_instance_id,
            inbox_url=inbox_url,
            public_key=public_key,
            gfs_key_suite=suite,
            display_name=_display_name(raw.get("display_name")),
        )

    def to_wire(self) -> dict:
        return {
            "gfs_instance_id": self.gfs_instance_id,
            "inbox_url": self.inbox_url,
            "public_key": self.public_key,
            "gfs_key_suite": self.gfs_key_suite,
            "display_name": self.display_name,
        }


def gfs_servers_from_wire(
    raw: object,
) -> tuple[tuple[GfsServer, ...] | None, tuple[str, ...]]:
    """Parse a list leniently: ``(servers, dropped_reasons)``.

    Not a list → ``(None, ())`` ("not known"). Entries with an unknown suite
    or a bad field are dropped, a repeated ``gfs_instance_id`` keeps its
    first entry, and at most :data:`MAX_GFS_SERVERS` are kept. Every dropped
    entry adds one reason (never the entry's content) for the caller to log.
    """
    if not isinstance(raw, list):
        return None, ()
    servers: list[GfsServer] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for index, entry in enumerate(raw):
        if len(servers) >= MAX_GFS_SERVERS:
            dropped.append(f"entry {index}: over the limit of {MAX_GFS_SERVERS}")
            continue
        try:
            server = GfsServer.from_wire(entry)
        except UnsupportedGfsKeySuite:
            dropped.append(f"entry {index}: unsupported gfs_key_suite")
            continue
        except ValueError as exc:
            dropped.append(f"entry {index}: {exc}")
            continue
        if server.gfs_instance_id in seen:
            dropped.append(f"entry {index}: repeated gfs_instance_id")
            continue
        seen.add(server.gfs_instance_id)
        servers.append(server)
    return tuple(servers), tuple(dropped)


def gfs_servers_to_wire(servers: tuple[GfsServer, ...]) -> list[dict]:
    return [server.to_wire() for server in servers]


def gfs_servers_to_column(servers: tuple[GfsServer, ...] | None) -> str | None:
    """The ``spaces.gfs_servers`` text for *servers* (``None`` → NULL)."""
    if servers is None:
        return None
    return json.dumps(
        gfs_servers_to_wire(servers), separators=(",", ":"), sort_keys=True
    )


def gfs_servers_from_column(text: str | None) -> tuple[GfsServer, ...] | None:
    """Read ``spaces.gfs_servers`` back. NULL or unreadable → ``None``."""
    if text is None:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    servers, _dropped = gfs_servers_from_wire(raw)
    return servers


__all__ = [
    "GFS_KEY_SUITE_ED25519",
    "GFS_SERVER_WIRE_KEYS",
    "MAX_GFS_DISPLAY_NAME_LENGTH",
    "MAX_GFS_INSTANCE_ID_LENGTH",
    "MAX_GFS_SERVERS",
    "SUPPORTED_GFS_KEY_SUITES",
    "GfsServer",
    "UnsupportedGfsKeySuite",
    "gfs_servers_from_column",
    "gfs_servers_from_wire",
    "gfs_servers_to_column",
    "gfs_servers_to_wire",
]
