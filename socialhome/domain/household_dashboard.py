"""Household dashboard — settings shape, limits and coded refusals.

The household dashboard is a wall-tablet view served on its own port and
unlocked with a household-wide dashboard password. These types carry the
admin-editable settings; persistence and validation live in
:mod:`socialhome.services.dashboard_settings_service`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CodedError

__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "IDLE_RETURN_CHOICES",
    "MAX_ADDRESS_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "SETUP_TOKEN_TTL_SECONDS",
    "SUPPORTED_DASHBOARD_LOCALES",
    "DashboardPasswordRequiredError",
    "DashboardPasswordTooShortError",
    "DashboardPersonSettings",
    "DashboardPortFixedError",
    "DashboardPortInUseError",
    "DashboardSettings",
    "DashboardSettingsInvalidError",
]

#: Port the dashboard listens on unless an admin picks another one (and
#: the only port in the Home Assistant App, whose host mapping HA
#: Supervisor owns).
DEFAULT_DASHBOARD_PORT = 8100

#: Minutes of inactivity before the tablet returns to the overview
#: (``0`` = never).
IDLE_RETURN_CHOICES = frozenset({0, 1, 3, 10})

SUPPORTED_DASHBOARD_LOCALES = frozenset({"en", "de", "es", "fr", "nl"})

MIN_PASSWORD_LENGTH = 8

#: Longest admin-set dashboard address (``DashboardSettings.address``).
MAX_ADDRESS_LENGTH = 200

#: Lifetime of a one-time tablet setup token (QR / link pairing).
SETUP_TOKEN_TTL_SECONDS = 600


@dataclass(slots=True, frozen=True)
class DashboardPersonSettings:
    """Per-person dashboard display choices.

    ``picture=None`` means "use the default" — resolved by the caller
    (pictures are on by default for child-protected minors), not here.
    """

    show: bool = True
    picture: bool | None = None


_DEFAULT_PERSON = DashboardPersonSettings()


@dataclass(slots=True, frozen=True)
class DashboardSettings:
    """Admin-editable household dashboard settings.

    ``people`` is a tuple of ``(user_id, settings)`` pairs so the whole
    value stays frozen and hashable; use :meth:`person` for a lookup.
    """

    enabled: bool = False
    port: int = DEFAULT_DASHBOARD_PORT
    locale: str = "en"
    idle_return_minutes: int = 3
    night_start: str = "22:00"
    night_end: str = "06:00"
    keep_screen_on: bool = True
    show_status_text: bool = False
    #: Admin-set base URL tablets use (``http(s)://host[:port]``), shown
    #: in the admin UI and setup links instead of a guessed address.
    address: str | None = None
    people: tuple[tuple[str, DashboardPersonSettings], ...] = ()
    password_set_at: str | None = None

    def person(self, user_id: str) -> DashboardPersonSettings:
        """The stored settings for ``user_id``, or the defaults."""
        for uid, settings in self.people:
            if uid == user_id:
                return settings
        return _DEFAULT_PERSON

    def people_map(self) -> dict[str, DashboardPersonSettings]:
        """A fresh ``{user_id: settings}`` dict of the stored entries."""
        return dict(self.people)


class DashboardPasswordRequiredError(CodedError):
    """The dashboard can't be enabled (or paired) without a password."""

    status = 422
    code = "DASHBOARD_PASSWORD_REQUIRED"
    detail = "Set a dashboard password before enabling the dashboard."


class DashboardPortFixedError(CodedError):
    """The platform owns the dashboard port; it can't be changed here."""

    status = 422
    code = "DASHBOARD_PORT_FIXED"
    detail = "The dashboard port is fixed on this installation."


class DashboardPortInUseError(CodedError):
    """Another process already listens on the dashboard port."""

    status = 409
    code = "DASHBOARD_PORT_IN_USE"
    detail = "The dashboard port is already in use."

    def __init__(self, port: int) -> None:
        super().__init__(params={"port": port})


class DashboardSettingsInvalidError(CodedError, ValueError):
    """A dashboard setting failed validation. ``params.field`` names the
    setting — never the submitted value."""

    status = 422
    code = "DASHBOARD_SETTINGS_INVALID"
    detail = "A dashboard setting is invalid."

    def __init__(self, field: str) -> None:
        super().__init__(
            f"Invalid dashboard setting: {field}",
            params={"field": field},
        )


class DashboardPasswordTooShortError(CodedError, ValueError):
    """The dashboard password is shorter than :data:`MIN_PASSWORD_LENGTH`."""

    status = 422
    code = "DASHBOARD_PASSWORD_TOO_SHORT"
    detail = "The dashboard password is too short."

    def __init__(self) -> None:
        super().__init__(params={"min": MIN_PASSWORD_LENGTH})
