"""PreferencesService — household-wide + per-user toggles.

Both scopes live in the same ``preferences`` table; this service
enforces the Python-side scope policy via ``PREFERENCE_SCOPE``.

* update_household — rejects any key whose scope is not 'household'.
* update_user — rejects any key whose scope is not 'user'.
* require_enabled / require_post_type — household-only gating.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.errors import WrongTypeError
from ..domain.events import HouseholdConfigChanged, UserPreferencesChanged
from ..domain.preferences import (
    PREFERENCE_SCOPE,
    SECTIONS,
    FeatureDisabledError,
    HouseholdPreferences,
    UserPreferences,
)
from ..domain.space import SpacePermissionError
from ..repositories.preferences_repo import HOUSEHOLD_ROW_ID, AbstractPreferencesRepo

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

# Re-exports so callers can import everything from one module.
__all__ = [
    "SECTIONS",
    "FeatureDisabledError",
    "HouseholdPreferences",
    "PreferencesService",
    "ScopeMismatchError",
    "UserPreferences",
]


class ScopeMismatchError(ValueError):
    """Raised when update_household receives a user-scope key (or vice versa),
    or when a key is not present in PREFERENCE_SCOPE at all."""


class PreferencesService:
    __slots__ = ("_bus", "_repo")

    def __init__(
        self,
        repo: AbstractPreferencesRepo,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus

    # ── Household scope ──────────────────────────────────────────────

    async def get_household(self) -> HouseholdPreferences:
        return await self._repo.get_household()

    async def require_enabled(self, section: str) -> HouseholdPreferences:
        """Refresh + assert *section* is enabled.

        Raises :class:`FeatureDisabledError` if the household admin has
        turned off ``feat_{section}``. Returns the fresh
        :class:`HouseholdPreferences` row so callers that also need to
        check post-type toggles can reuse the lookup.
        """
        prefs = await self._repo.get_household()
        prefs.require_enabled(section)
        return prefs

    async def require_post_type(self, post_type: str) -> HouseholdPreferences:
        """Refresh + assert household allows *post_type* in the feed."""
        prefs = await self._repo.get_household()
        prefs.require_post_type(post_type)
        return prefs

    # ── HA REST bridge ─────────────────────────────────────────────

    async def set_tz_from_ha(self, tz: str) -> None:
        """Mirror ``core.config.time_zone`` from HA Core into the
        household tz column.

        Called by the ha / haos adapter's startup poll. Validates the
        zone (an unknown name is silently dropped — the operator's
        next HA config edit will retry). Skips writing if the value is
        already current, avoiding a redundant ``HouseholdConfigChanged``
        broadcast on every poll cycle. Bypasses the admin check
        because the source-of-truth in HA modes is HA Core itself, not
        the SH operator.
        """
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            log.warning(
                "HA Core reported unknown timezone %r — leaving household tz unchanged",
                tz,
            )
            return
        await self._repo.ensure_row(HOUSEHOLD_ROW_ID)
        current = await self._repo.get_household()
        if current.tz == tz:
            return
        await self._repo.set_household_value("tz", tz)
        if self._bus is not None:
            try:
                await self._bus.publish(
                    HouseholdConfigChanged(changed={"tz": tz}),
                )
            except Exception as exc:  # pragma: no cover
                log.debug("household tz publish failed: %s", exc, exc_info=True)

    # ── Admin update ───────────────────────────────────────────────

    async def update_household(
        self,
        *,
        actor_is_admin: bool,
        household_name: str | None = None,
        toggles: dict[str, bool] | None = None,
        tz: str | None = None,
    ) -> HouseholdPreferences:
        """Apply ``household_name``, a partial ``toggles`` dict, and / or
        a new ``tz`` IANA name to the household row.

        Every key in ``toggles`` is validated against :data:`PREFERENCE_SCOPE`.
        Unknown keys and user-scope keys are both rejected with
        :class:`ScopeMismatchError`. Only ``True`` / ``False`` are accepted
        as toggle values. ``tz`` is validated via ``ZoneInfo`` so an unknown /
        malformed IANA name is rejected with :class:`ValueError`. On successful
        change the service publishes a ``HouseholdConfigChanged`` event so every
        connected client can refresh its nav state without a page reload
        (spec §23.13).
        """
        if not actor_is_admin:
            raise SpacePermissionError(
                "Only household admins may change household preferences",
            )

        # Make sure the row exists.
        await self._repo.ensure_row(HOUSEHOLD_ROW_ID)

        before = await self._repo.get_household()
        changed: dict = {}

        if household_name is not None:
            name = household_name.strip()
            if not name or len(name) > 80:
                raise ValueError("household_name must be 1-80 characters")
            if name != before.household_name:
                await self._repo.set_household_value("household_name", name)
                changed["household_name"] = name

        if tz is not None:
            tz_clean = tz.strip()
            if not tz_clean:
                raise ValueError("tz must be a non-empty IANA name")
            try:
                ZoneInfo(tz_clean)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    f"unknown IANA timezone {tz_clean!r}",
                ) from exc
            if tz_clean != before.tz:
                await self._repo.set_household_value("tz", tz_clean)
                changed["tz"] = tz_clean

        if toggles:
            for key, value in toggles.items():
                scope = PREFERENCE_SCOPE.get(key)
                if scope != "household":
                    raise ScopeMismatchError(
                        f"{key!r} is not a household-scope preference (scope={scope!r})"
                    )
                if not isinstance(value, bool):
                    raise WrongTypeError(
                        f"toggle {key!r} must be a boolean, got {type(value).__name__}"
                    )
                if getattr(before, key) == value:
                    continue
                await self._repo.set_household_value(key, 1 if value else 0)
                changed[key] = value

        after = await self._repo.get_household()
        if changed and self._bus is not None:
            try:
                await self._bus.publish(HouseholdConfigChanged(changed=changed))
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "household config_changed publish failed: %s", exc, exc_info=True
                )
        return after

    # ── User scope ───────────────────────────────────────────────────

    async def get_user(self, user_id: str) -> UserPreferences:
        return await self._repo.get_user(user_id)

    async def update_user(
        self,
        user_id: str,
        *,
        toggles: dict[str, bool],
    ) -> UserPreferences:
        """Apply a partial ``toggles`` dict to the user's preferences row.

        Every key in ``toggles`` is validated against :data:`PREFERENCE_SCOPE`.
        Unknown keys and household-scope keys are both rejected with
        :class:`ScopeMismatchError`. On successful change the service publishes
        a ``UserPreferencesChanged`` event.
        """
        await self._repo.ensure_row(user_id)
        before = await self._repo.get_user(user_id)

        changed: dict = {}
        for key, value in toggles.items():
            scope = PREFERENCE_SCOPE.get(key)
            if scope != "user":
                raise ScopeMismatchError(
                    f"{key!r} is not a user-scope preference (scope={scope!r})"
                )
            if not isinstance(value, bool):
                raise WrongTypeError(
                    f"toggle {key!r} must be a boolean, got {type(value).__name__}"
                )
            if getattr(before, key) == value:
                continue
            await self._repo.set_user_value(user_id, key, 1 if value else 0)
            changed[key] = value

        if changed and self._bus is not None:
            try:
                await self._bus.publish(
                    UserPreferencesChanged(user_id=user_id, changed=changed),
                )
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "user preferences_changed publish failed: %s", exc, exc_info=True
                )
        return await self._repo.get_user(user_id)
