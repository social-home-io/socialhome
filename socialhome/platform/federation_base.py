"""Admin-set federation inbox base URL, shared across platform adapters.

Every mode needs an answer to "what URL do peers POST federation envelopes
to?" (:meth:`PlatformAdapter.get_federation_base`), and each mode has its
own *automatic* source:

* ``standalone`` — ``[standalone].external_url`` from ``socialhome.toml``.
* ``ha`` / ``haos`` — pushed by the companion Home Assistant integration
  into ``instance_config['ha_federation_base']``.

None of those is settable from the UI: the TOML file belongs to the
operator, and the integration's value belongs to the integration. This
module adds the manual, admin-set option behind
``GET / PUT /api/admin/federation/external-url``.

**Semantics.** The stored value is the base at which *this Social Home* is
directly reachable — exactly what ``[standalone].external_url`` means — so
the adapter appends Social Home's own inbox route,
:data:`INBOX_PATH`. That differs from the integration's value, which is
Home Assistant's URL and gets ``/api/socialhome/inbox`` appended instead,
because there it is an HA-hosted view forwarding into the add-on. Keeping
the two keys separate is what lets each carry the right path; conflating
them would produce an unreachable URL for one source or the other.

The manual value takes precedence when set, so typing one is never a
silent no-op, and clearing it hands control back to the automatic source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.database import AsyncDatabase

#: ``instance_config`` key holding the admin-set base URL. Deliberately
#: not ``ha_federation_base`` — that one is the integration's, means a
#: different thing (Home Assistant's URL, not ours) and would be
#: overwritten on the integration's next push.
MANUAL_BASE_KEY = "federation_base_url"

#: Social Home's own federation inbox route, registered unconditionally in
#: ``routes/__init__`` for every mode.
INBOX_PATH = "/federation/inbox"


async def read_manual_base(db: "AsyncDatabase | None") -> str | None:
    """Return the admin-set base URL, or ``None`` when unset/blank.

    Bare value as stored — no inbox path appended. Callers that need a
    peer-facing base want :func:`manual_federation_base`.
    """
    if db is None:
        return None
    row = await db.fetchone(
        "SELECT value FROM instance_config WHERE key=?",
        (MANUAL_BASE_KEY,),
    )
    if row is None:
        return None
    raw = str(row["value"] or "").strip()
    return raw.rstrip("/") or None


async def manual_federation_base(db: "AsyncDatabase | None") -> str | None:
    """The admin-set base with Social Home's inbox path appended.

    Idempotent against a value that already carries the path, so an admin
    who pastes the full inbox URL doesn't get it doubled.
    """
    base = await read_manual_base(db)
    if base is None:
        return None
    if base.endswith(INBOX_PATH):
        return base
    return f"{base}{INBOX_PATH}"


def public_base_from_federation_base(base: str | None) -> str | None:
    """Derive the base at which *this Social Home* is directly reachable.

    Out-of-app URLs — an iCal feed a calendar app polls, the address an
    API-token script talks to — need Social Home's own public origin,
    not the SPA's ``document.baseURI`` (under Home Assistant ingress that
    is an ingress path only a signed-in HA browser can load).

    Reuses the adapter's federation answer instead of a second setting:
    a base ending in :data:`INBOX_PATH` is Social Home's own inbox
    (``[standalone].external_url`` or the admin-set value), so stripping
    the path yields a URL that serves every other Social Home route too.
    Any other shape — the Home Assistant integration's
    ``…/api/socialhome/inbox`` forwarder — reaches only the inbox, so
    there is no public origin to hand out and the answer is ``None``.
    """
    if not base:
        return None
    trimmed = base.rstrip("/")
    if not trimmed.endswith(INBOX_PATH):
        return None
    root = trimmed[: -len(INBOX_PATH)].rstrip("/")
    return root or None


__all__ = [
    "INBOX_PATH",
    "MANUAL_BASE_KEY",
    "manual_federation_base",
    "public_base_from_federation_base",
    "read_manual_base",
]
