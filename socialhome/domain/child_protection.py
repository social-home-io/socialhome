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


#: Every capability a protected account loses, in display order. One fixed
#: set today — the §CP model has no per-minor toggles, so a guardian adjusts
#: it by lifting protection, not by picking surfaces.
PROTECTED_ACCOUNT_RESTRICTIONS: tuple[ProtectedCapability, ...] = tuple(
    ProtectedCapability
)


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
