"""The GFSs a space is listed on — the owner's signed list, as members store it.

A global space is listed on one or more connection servers (GFSs) the owner
chose. The owner household derives the list from its active
``gfs_space_publications`` rows, SIGNS it with its household identity key and
hands it to every member household inside the encrypted ``space_meta``
(``SPACE_CONFIG_CHANGED``, the §D1b join snapshots, the rotation bundle), so a
member can reach every listed GFS — including ones its own household never
connected to — while the host is offline.

Signed list (wire and ``spaces.gfs_servers`` column, migration 0092)::

    {"servers": [{gfs_instance_id, inbox_url, public_key, gfs_key_suite,
                  display_name}, ...],
     "issued_at": N,                 # owner's monotonic counter (unix ms)
     "owner_pk": "<owner household Ed25519 identity key, hex>",
     "gfs_servers_sig_suite": "ed25519",
     "gfs_servers_sig": "<b64url>"}

The signature (:mod:`socialhome.gfs_server_list_sig`) binds the space id and
every field above, so any household — the owner or a delegated admin relaying
the owner's list in a join snapshot — can carry it, but nobody except the
owner household can write or alter one, and a receiver takes only an
``issued_at`` newer than the one it stores (no replay of an older list).
``NULL`` means "not known" (an older owner, a space never listed); a signed
empty list means "listed nowhere".

Two suite tags, each rejected when unknown and never defaulted (CLAUDE.md
"Crypto wire shapes carry a ``*_suite`` tag"): ``gfs_servers_sig_suite`` for
the owner's signature, ``gfs_key_suite`` per entry for the GFS identity key a
member will pin.

Entries are checked when read, not when stored (the stored list must stay
byte-identical to what the owner signed so it can be relayed):

* ``inbox_url`` must pass the connection-server URL rule
  (:func:`socialhome.peer_url.validate_peer_url`, https unless private);
* an entry whose host is loopback / private / link-local / ULA is dropped
  unless THIS household has an active connection to the same
  ``gfs_instance_id`` at the same ``inbox_url`` (``own_routes``) — a list from
  another household must never aim our requests at our own network. A DNS
  name is not resolved here: whoever sends to these servers (federation v_56,
  a later change) must go through the SSRF-guarded
  :class:`socialhome.outbound_fetch.OutboundFetcher` path (resolve, pin,
  refuse private addresses);
* at most :data:`MAX_GFS_SERVERS` entries are looked at; duplicates by
  ``gfs_instance_id``, ``inbox_url`` or ``public_key`` keep the first.

Pure: no I/O, no crypto, no logging.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..peer_url import is_private_host, validate_peer_url

#: The algorithm of ``public_key`` — the GFS identity key, Ed25519, hex.
#: Phase-2 of ``docs/crypto.md`` adds a hybrid sibling.
GFS_KEY_SUITE_ED25519: str = "ed25519"
SUPPORTED_GFS_KEY_SUITES: frozenset[str] = frozenset({GFS_KEY_SUITE_ED25519})

#: The algorithm of the owner household's signature over the list.
GFS_SERVERS_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_GFS_SERVERS_SIG_SUITES: frozenset[str] = frozenset(
    {GFS_SERVERS_SIG_SUITE_ED25519}
)

#: Most GFSs one space is listed on.
MAX_GFS_SERVERS: int = 8

#: Longest ``gfs_instance_id`` accepted. It is the operator's configured id
#: (``[server].instance_id``, e.g. ``gfs-node-0``): free text without
#: control characters.
MAX_GFS_INSTANCE_ID_LENGTH: int = 128

#: ``display_name`` is shown only; a longer one is cut, not rejected.
MAX_GFS_DISPLAY_NAME_LENGTH: int = 128

#: Upper bound on ``issued_at`` (SQLite INTEGER range).
MAX_GFS_SERVERS_ISSUED_AT: int = 2**63 - 1

#: Largest canonical ``servers`` JSON accepted in a signed list.
MAX_GFS_SERVERS_JSON_BYTES: int = 32 * 1024

#: Every key of one entry on the wire.
GFS_SERVER_WIRE_KEYS: frozenset[str] = frozenset(
    {"gfs_instance_id", "inbox_url", "public_key", "gfs_key_suite", "display_name"}
)

#: Every key of a signed list on the wire / in the column.
SIGNED_GFS_SERVER_LIST_KEYS: frozenset[str] = frozenset(
    {"servers", "issued_at", "owner_pk", "gfs_servers_sig_suite", "gfs_servers_sig"}
)

_ED25519_HEX = re.compile(r"[0-9a-f]{64}")
_B64URL = re.compile(r"[A-Za-z0-9_-]{1,256}")


class UnsupportedGfsKeySuite(ValueError):
    """A GFS server entry names a key suite this build does not know (or
    none). Rejected — never defaulted to Ed25519."""


class UnsupportedGfsServersSigSuite(ValueError):
    """A signed GFS list names a signature suite this build does not know
    (or none). Rejected — never defaulted to Ed25519."""


def _has_control_chars(value: str) -> bool:
    return any(unicodedata.category(ch)[0] == "C" for ch in value)


def _display_name(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("gfs server: display_name must be text")
    clean = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    return clean.strip()[:MAX_GFS_DISPLAY_NAME_LENGTH]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_json(value: object) -> str:
    """Sorted keys, compact separators — the form that is signed."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
        """Parse one entry.

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

    @property
    def route(self) -> tuple[str, str]:
        """``(gfs_instance_id, inbox_url)`` — what an own connection must
        match for a private-host entry to be usable."""
        return (self.gfs_instance_id, self.inbox_url)

    def has_private_host(self) -> bool:
        return is_private_host(urlsplit(self.inbox_url).hostname or "")


def gfs_servers_from_wire(
    raw: object,
    *,
    own_routes: Collection[tuple[str, str]] = (),
) -> tuple[tuple[GfsServer, ...] | None, int]:
    """Parse a list leniently: ``(servers, dropped_count)``.

    Not a list → ``(None, 0)`` ("not known"). Only the first
    :data:`MAX_GFS_SERVERS` entries are looked at (the rest count as
    dropped, unparsed). An entry with an unknown suite or a bad field is
    dropped; so is a private-host entry unless its ``(gfs_instance_id,
    inbox_url)`` is in *own_routes* (an active connection of ours); a
    repeated ``gfs_instance_id``, ``inbox_url`` or ``public_key`` keeps its
    first entry. The caller logs the count once.
    """
    if not isinstance(raw, list):
        return None, 0
    dropped = max(0, len(raw) - MAX_GFS_SERVERS)
    allowed = set(own_routes)
    servers: list[GfsServer] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    for entry in raw[:MAX_GFS_SERVERS]:
        try:
            server = GfsServer.from_wire(entry)
        except ValueError:
            dropped += 1
            continue
        if server.has_private_host() and server.route not in allowed:
            dropped += 1
            continue
        url_key = server.inbox_url.rstrip("/").lower()
        if (
            server.gfs_instance_id in seen_ids
            or url_key in seen_urls
            or server.public_key in seen_keys
        ):
            dropped += 1
            continue
        seen_ids.add(server.gfs_instance_id)
        seen_urls.add(url_key)
        seen_keys.add(server.public_key)
        servers.append(server)
    return tuple(servers), dropped


def gfs_servers_to_wire(servers: tuple[GfsServer, ...]) -> list[dict]:
    return [server.to_wire() for server in servers]


@dataclass(slots=True, frozen=True)
class SignedGfsServerList:
    """The owner's signed list, kept byte-identical to what was signed.

    ``servers_json`` is the canonical JSON of the ``servers`` array exactly
    as signed (entries are not normalised, so the signature stays checkable
    and the list relayable). Build one only through :meth:`from_wire` (a
    received / stored list) or :func:`socialhome.gfs_server_list_sig
    .sign_gfs_server_list` (the owner). Structure only — the signature is
    checked by :func:`socialhome.gfs_server_list_sig.verify_gfs_server_list`.
    """

    servers_json: str
    issued_at: int
    owner_pk: str
    gfs_servers_sig_suite: str
    gfs_servers_sig: str

    @classmethod
    def from_wire(cls, raw: object) -> "SignedGfsServerList":
        """Raises :class:`UnsupportedGfsServersSigSuite` for an unknown or
        missing suite, :class:`ValueError` for any other bad field."""
        if not isinstance(raw, dict):
            raise ValueError("gfs server list: not an object")
        suite = raw.get("gfs_servers_sig_suite")
        if not isinstance(suite, str) or suite not in SUPPORTED_GFS_SERVERS_SIG_SUITES:
            raise UnsupportedGfsServersSigSuite(
                f"gfs server list: unsupported gfs_servers_sig_suite {suite!r}; "
                f"this build supports {sorted(SUPPORTED_GFS_SERVERS_SIG_SUITES)!r}"
            )
        servers = raw.get("servers")
        if not isinstance(servers, list) or len(servers) > MAX_GFS_SERVERS:
            raise ValueError("gfs server list: bad servers")
        servers_json = canonical_json(servers)
        if len(servers_json) > MAX_GFS_SERVERS_JSON_BYTES:
            raise ValueError("gfs server list: servers too large")
        issued_at = raw.get("issued_at")
        if not isinstance(issued_at, int) or not _is_int(issued_at):
            raise ValueError("gfs server list: bad issued_at")
        if issued_at < 0 or issued_at > MAX_GFS_SERVERS_ISSUED_AT:
            raise ValueError("gfs server list: bad issued_at")
        owner_pk = raw.get("owner_pk")
        if not isinstance(owner_pk, str) or _ED25519_HEX.fullmatch(owner_pk) is None:
            raise ValueError("gfs server list: bad owner_pk")
        sig = raw.get("gfs_servers_sig")
        if not isinstance(sig, str) or _B64URL.fullmatch(sig) is None:
            raise ValueError("gfs server list: bad gfs_servers_sig")
        return cls(
            servers_json=servers_json,
            issued_at=issued_at,
            owner_pk=owner_pk,
            gfs_servers_sig_suite=suite,
            gfs_servers_sig=sig,
        )

    def to_wire(self) -> dict:
        return {
            "servers": json.loads(self.servers_json),
            "issued_at": self.issued_at,
            "owner_pk": self.owner_pk,
            "gfs_servers_sig_suite": self.gfs_servers_sig_suite,
            "gfs_servers_sig": self.gfs_servers_sig,
        }

    def servers(
        self, *, own_routes: Collection[tuple[str, str]] = ()
    ) -> tuple[tuple[GfsServer, ...], int]:
        """The usable entries and how many were dropped (see
        :func:`gfs_servers_from_wire`)."""
        servers, dropped = gfs_servers_from_wire(
            json.loads(self.servers_json), own_routes=own_routes
        )
        return servers or (), dropped


def signed_gfs_server_list_to_column(value: SignedGfsServerList | None) -> str | None:
    """The ``spaces.gfs_servers`` text (a JSON object) — ``None`` → NULL."""
    if value is None:
        return None
    return canonical_json(value.to_wire())


def signed_gfs_server_list_from_column(text: str | None) -> SignedGfsServerList | None:
    """Read ``spaces.gfs_servers`` back. NULL or unreadable → ``None``."""
    if text is None:
        return None
    try:
        return SignedGfsServerList.from_wire(json.loads(text))
    except ValueError:
        return None


__all__ = [
    "GFS_KEY_SUITE_ED25519",
    "GFS_SERVERS_SIG_SUITE_ED25519",
    "GFS_SERVER_WIRE_KEYS",
    "MAX_GFS_DISPLAY_NAME_LENGTH",
    "MAX_GFS_INSTANCE_ID_LENGTH",
    "MAX_GFS_SERVERS",
    "MAX_GFS_SERVERS_ISSUED_AT",
    "MAX_GFS_SERVERS_JSON_BYTES",
    "SIGNED_GFS_SERVER_LIST_KEYS",
    "SUPPORTED_GFS_KEY_SUITES",
    "SUPPORTED_GFS_SERVERS_SIG_SUITES",
    "GfsServer",
    "SignedGfsServerList",
    "UnsupportedGfsKeySuite",
    "UnsupportedGfsServersSigSuite",
    "canonical_json",
    "gfs_servers_from_wire",
    "gfs_servers_to_wire",
    "signed_gfs_server_list_from_column",
    "signed_gfs_server_list_to_column",
]
