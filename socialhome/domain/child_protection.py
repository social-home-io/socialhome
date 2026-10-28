"""Protected-account vocabulary (§CP.R).

A *protected* account is a household user an admin placed under child
protection (``users.child_protection_enabled``). On top of the existing
§CP rules (space age gate, direct-pair-only DMs, guardian blocks) the
server refuses a fixed set of surfaces that would put the account in
front of people outside the household, or hand a credential to a
third-party tool:

* ``bazaar`` — create a listing, bid, or make an offer (trading with
  other households).
* ``public_spaces`` — create a public / global space, or turn a space
  public / global (advertised to peers and connection servers).
* ``public_moments`` — register a public profile on a connection
  server, or follow a public profile there.
* ``public_links`` — publish a highlight as a public web link.
* ``api_tokens`` — mint a personal API token (a bearer credential an
  external tool holds).
* ``calendar_feeds`` — mint a space-calendar subscription link (an
  unauthenticated URL that exposes the account's schedule).
* ``ha_automations`` — send the account's notifications and activity to
  Home Assistant automations (``socialhome.notification_new`` and every
  other personal ``socialhome.*`` event on HA's event bus, readable by
  anyone with HA admin or automation access).

The capability ids are the wire values ``/api/me`` reports and the
``ACCOUNT_PROTECTED`` error carries, so the SPA can explain *what* is
limited. They never reveal *why* — ``is_minor`` / ``declared_age`` stay in
:data:`socialhome.security.SENSITIVE_FIELDS`.
"""

from __future__ import annotations

from enum import StrEnum


class ProtectedCapability(StrEnum):
    """A surface a protected account may not use (§CP.R)."""

    BAZAAR = "bazaar"
    PUBLIC_SPACES = "public_spaces"
    PUBLIC_MOMENTS = "public_moments"
    PUBLIC_LINKS = "public_links"
    API_TOKENS = "api_tokens"
    CALENDAR_FEEDS = "calendar_feeds"
    HA_AUTOMATIONS = "ha_automations"


#: Every capability a protected account loses, in display order. One fixed
#: set today — the §CP model has no per-minor toggles, so a guardian adjusts
#: it by lifting protection, not by picking surfaces.
PROTECTED_ACCOUNT_RESTRICTIONS: tuple[ProtectedCapability, ...] = tuple(
    ProtectedCapability
)

#: Why a space owned by a protected account can't go public / global —
#: whoever asks (an adult admin, a remote admin, a quorum). Deliberately
#: says nothing about the owner.
PROTECTED_OWNER_PUBLISH_DETAIL = "This space can't be made public."

#: Why a public / global space can't be handed to a protected account.
PROTECTED_OWNER_TRANSFER_DETAIL = (
    "This member can't own a public space. Make the space private first."
)


#: Profile keys withheld between the two sides of a guardian block (§CP.F2).
#: Toward someone a guardian blocked, a protected account shows by name
#: only — and, like every other guardian-block rule, the other way round.
NAME_ONLY_WITHHELD_KEYS: tuple[str, ...] = ("picture_hash", "picture_url", "bio")

#: Session-presence keys a name-only row reads as offline with (#948 keeps
#: online state from the other side of a guardian block).
NAME_ONLY_OFFLINE: dict[str, object] = {
    "is_online": False,
    "is_idle": False,
    "last_seen_at": None,
}


def name_only(row: dict) -> dict:
    """A copy of the person *row* with picture and bio withheld (§CP.F2).

    Only keys the row already carries change, so the payload shape a
    client parses never does: picture and bio read ``None``, and any
    session-presence keys read offline — the person just shows as a name.
    """
    blanked = {k: None for k in NAME_ONLY_WITHHELD_KEYS if k in row}
    offline = {k: v for k, v in NAME_ONLY_OFFLINE.items() if k in row}
    return {**row, **blanked, **offline}


class AccountProtectedError(PermissionError):
    """The caller's account is protected and may not use *capability*.

    Mapped to ``403 ACCOUNT_PROTECTED`` (with ``capability``) by
    :class:`socialhome.routes.base.BaseView`.
    """

    def __init__(self, capability: ProtectedCapability) -> None:
        self.capability = capability
        super().__init__(
            f"Your account is protected by your household ({capability.value})."
        )
