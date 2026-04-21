"""HouseholdFeaturesService — household-wide feature toggles (§22 / §23.13).

A single ``household_features`` row controls which surfaces (feed,
pages, tasks, calendar, bazaar, stickies) are enabled for the
household and which post types are allowed in the household feed.

The service is the **single source of truth** for enforcement — every
mutating service call that corresponds to a toggleable surface asks
the service to gate the call via :meth:`require_enabled` /
:meth:`require_post_type`. Disabling a section from the admin UI
immediately blocks server-side writes with :class:`FeatureDisabledError`
(mapped to HTTP 403 by ``routes/base.py``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import HouseholdConfigChanged
from ..domain.household_features import (
    FeatureDisabledError,
    HouseholdFeatures,
)
from ..domain.space import SpacePermissionError
from ..repositories.household_features_repo import (
    ALL_KEYS as _ALL_KEYS,
    AbstractHouseholdFeaturesRepo,
)

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


# Re-export for backwards compatibility — older code imports
# ``HouseholdFeatures`` from this module.
__all__ = (
    "FeatureDisabledError",
    "HouseholdFeatures",
    "HouseholdFeaturesService",
)


class HouseholdFeaturesService:
    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        repo: AbstractHouseholdFeaturesRepo,
        *,
        bus: "EventBus | None" = None,
    ) -> None:
        self._repo = repo
        self._bus = bus

    async def get(self) -> HouseholdFeatures:
        return await self._repo.get()

    # ── Enforcement helpers (called by other services) ─────────────

    async def require_enabled(self, section: str) -> HouseholdFeatures:
        """Refresh + assert *section* is enabled.

        Raises :class:`FeatureDisabledError` if the household admin has
        turned off ``feat_{section}``. Returns the fresh
        :class:`HouseholdFeatures` row so callers that also need to
        check post-type toggles can reuse the lookup.
        """
        features = await self._repo.get()
        features.require_enabled(section)
        return features

    async def require_post_type(self, post_type: str) -> HouseholdFeatures:
        """Refresh + assert household allows *post_type* in the feed."""
        features = await self._repo.get()
        features.require_post_type(post_type)
        return features

    # ── Admin update ───────────────────────────────────────────────

    async def update(
        self,
        *,
        actor_is_admin: bool,
        household_name: str | None = None,
        toggles: dict | None = None,
    ) -> HouseholdFeatures:
        """Apply ``household_name`` + a partial ``toggles`` dict.

        Unknown toggle keys are silently ignored — the service is the
        contract; the frontend is just a consumer.  Only ``True`` /
        ``False`` are accepted as toggle values. On successful change
        the service publishes a ``HouseholdConfigChanged`` event so
        every connected client can refresh its nav state without a
        page reload (spec §23.13).
        """
        if not actor_is_admin:
            raise SpacePermissionError(
                "Only household admins may change feature toggles",
            )

        # Make sure the row exists.
        await self._repo.ensure_row()

        before = await self._repo.get()
        changed: dict = {}

        if household_name is not None:
            name = household_name.strip()
            if not name or len(name) > 80:
                raise ValueError("household_name must be 1-80 characters")
            if name != before.household_name:
                await self._repo.set_household_name(name)
                changed["household_name"] = name

        if toggles:
            for key, value in toggles.items():
                if key not in _ALL_KEYS:
                    continue
                if not isinstance(value, bool):
                    raise ValueError(
                        f"toggle {key!r} must be a boolean, got {type(value).__name__}"
                    )
                if getattr(before, key) != value:
                    await self._repo.set_toggle(key, value)
                    changed[key] = value

        after = await self.get()
        if changed and self._bus is not None:
            try:
                await self._bus.publish(HouseholdConfigChanged(changed=changed))
            except Exception as exc:  # pragma: no cover
                log.debug("household config_changed publish failed: %s", exc)
        return after
