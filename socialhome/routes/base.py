"""Base class for all route views (Resource-View pattern).

Every route file defines one or more :class:`BaseView` subclasses that
group handlers by REST resource.  ``BaseView`` provides:

* **Centralised error mapping** — domain exceptions raised inside
  ``get()`` / ``post()`` / ``patch()`` / ``delete()`` are caught by
  :meth:`_iter` and converted into the canonical
  ``{"error": {"code": ..., "detail": ...}}`` JSON envelope. Individual
  handlers never need their own try/except.
* **Typed service access** — ``self.svc(key)`` shortcut.
* **Auth shortcut** — ``self.user`` property.
* **Body parsing** — ``await self.body()`` with automatic 400 on bad JSON.
* **Serialisation** — ``self._json(data, status=200)`` runs
  ``sanitise_for_api`` + ``web.json_response``.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ..app_keys import preferences_service_key, space_repo_key
from ..auth import current_user
from ..domain.preferences import FeatureDisabledError
from ..services.preferences_service import ScopeMismatchError
from ..domain.space import (
    ModerationAlreadyDecidedError,
    PublicSpaceLimitError,
    SpacePermissionError,
)
from ..domain.space_bot import (
    SpaceBotDisabledError,
    SpaceBotError,
    SpaceBotSlugTakenError,
)
from ..repositories.page_repo import PageLockError, PageNotFoundError
from ..security import error_response, sanitise_for_api
from ..services.bazaar_service import BazaarServiceError, ListingNotFoundError
from ..services.dm_service import MediaRequiresDirectPairingError
from ..services.child_protection_service import (
    ChildProtectionError,
    GuardianRequiredError,
    UserNotFoundError as CpUserNotFoundError,
)
from ..services.gallery_service import GalleryNotFoundError, GalleryPermissionError
from ..services.page_conflict_service import NoActiveConflictError
from ..services.poll_service import PollClosedError, PollNotFoundError
from ..services.presence_service import UserNotFoundError as PresenceUserNotFoundError
from ..services.space_zone_service import (
    SpaceZoneLimitError,
    SpaceZoneNameConflictError,
    SpaceZoneNotFoundError,
)
from ..services.storage_quota_service import StorageQuotaExceeded
from ..domain.apps import (
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppContactNotFoundError,
    AppIntegrityError,
    AppNotEnabledError,
    AppNotFoundError,
    AppQuotaExceededError,
)
from ..services.moment_service import MomentNotFoundError, MomentRateLimitError
from ..services.highlight_publication_service import (
    HighlightNotFoundError as HighlightPublicationNotFoundError,
)
from ..services.gfs_connection_service import GfsConnectionError
from ..services.highlight_publication_service import HighlightPublicationError
from ..services.map_tile_service import TileCoordinateError, TileUnavailableError
from ..services.highlight_service import (
    HighlightFrameLimitError,
    HighlightNotFoundError,
)

log = logging.getLogger(__name__)


class BaseView(web.View):
    """Shared base for every route view in Social Home.

    Subclasses define ``async def get/post/patch/delete(self)`` methods.
    aiohttp dispatches by HTTP method automatically.
    """

    # ── Convenience accessors ────────────────────────────────────────────

    @property
    def user(self):
        """Authenticated user context (calls ``current_user``)."""
        return current_user(self.request)

    def svc(self, key: web.AppKey) -> Any:
        """Fetch a service from the app container by typed key."""
        return self.request.app[key]

    def match(self, name: str) -> str:
        """Shortcut for ``self.request.match_info[name]``."""
        return self.request.match_info[name]

    async def require_household_feature(self, section: str) -> None:
        """Raise :class:`FeatureDisabledError` if ``feat_{section}`` is off.

        Used from routes that talk directly to a repo (pages, stickies)
        rather than through a service layer — the service-layer check
        stays the authoritative gate for everything else (§18).
        """
        svc = self.request.app.get(preferences_service_key)
        if svc is not None:
            await svc.require_enabled(section)

    async def require_space_feature(self, space_id: str, feature: str) -> None:
        """Raise :class:`FeatureDisabledError` if the space has *feature* off.

        ``feature`` is a :class:`SpaceFeatures` field name (``pages``,
        ``calendar``, ``todo``, ``stickies``, ``gallery``). When the
        admin has flipped the toggle off in SpaceSettings, every
        space-scoped handler for that surface returns 403
        FEATURE_DISABLED with section ``"space:<feature>"`` — the
        SPA hides the tab in parallel, so members never see the
        endpoint, but this gate keeps the surface honest against a
        client that constructs the URL directly. Silent no-op when
        the space row doesn't exist; the caller's membership check
        owns the 403 in that case.
        """
        space_repo = self.request.app.get(space_repo_key)
        if space_repo is None:
            return
        space = await space_repo.get(space_id)
        if space is None:
            return
        if not getattr(space.features, feature, True):
            raise FeatureDisabledError(f"space:{feature}")

    async def body(self) -> dict:
        """Parse JSON request body; returns 400 on bad input."""
        try:
            return await self.request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=error_response(400, "BAD_REQUEST", "Invalid JSON body.").text,
                content_type="application/json",
            ) from exc

    def _json(self, data: Any, *, status: int = 200) -> web.Response:
        """Return a sanitised JSON response.

        Handles both dict and list payloads. Lists have each dict
        element sanitised individually.
        """
        if isinstance(data, list):
            sanitised: Any = [
                sanitise_for_api(item) if isinstance(item, dict) else item
                for item in data
            ]
        elif isinstance(data, dict):
            sanitised = sanitise_for_api(data)
        else:
            sanitised = data
        return web.json_response(sanitised, status=status)

    # ── Dispatch with centralised error mapping ──────────────────────────

    async def _iter(self) -> web.StreamResponse:
        """Override aiohttp's dispatch to wrap with error mapping.

        Domain exceptions raised by any handler method are caught here
        and converted into the canonical error envelope.  Individual
        handlers never need their own try/except blocks.
        """
        try:
            return await super()._iter()
        except web.HTTPException:
            raise  # aiohttp errors pass through
        except (
            PageNotFoundError,
            PollNotFoundError,
            ListingNotFoundError,
            GalleryNotFoundError,
            SpaceZoneNotFoundError,
            HighlightNotFoundError,
            HighlightPublicationNotFoundError,
            MomentNotFoundError,
            PresenceUserNotFoundError,
            CpUserNotFoundError,
            AppNotFoundError,
        ) as exc:
            return error_response(404, "NOT_FOUND", str(exc))
        except TileCoordinateError as exc:
            # Subclasses ValueError — must precede the generic ValueError
            # clause below, and the coordinates are client-supplied, so
            # this is a 400 rather than the blanket 422.
            return error_response(400, "BAD_REQUEST", str(exc))
        except TileUnavailableError as exc:
            # We are the gateway to the upstream tile server.
            log.warning("map tile unavailable: %s", exc)
            return error_response(
                502,
                "TILE_UNAVAILABLE",
                "Map tiles are temporarily unavailable.",
            )
        except AppAlreadyInstalledError as exc:
            return error_response(409, "CONFLICT", str(exc))
        except AppIntegrityError as exc:
            # §App-integrity: bundle download/verify failure is a bad
            # request (bad app_id, tampered catalog) — 400 UNPROCESSABLE
            # mirrors the existing ValueError convention.
            return error_response(400, "UNPROCESSABLE", str(exc))
        except AppAgeRestrictedError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except AppNotEnabledError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except AppContactNotFoundError as exc:
            # Authorization gap (Task 6): the actor addressed a person who is
            # not in their challengeable roster. 403 FORBIDDEN matches the
            # "not allowed" convention used by AppNotEnabledError /
            # AppAgeRestrictedError above.
            return error_response(403, "FORBIDDEN", str(exc))
        except AppQuotaExceededError as exc:
            return error_response(413, "QUOTA_EXCEEDED", str(exc))
        except HighlightPublicationError as exc:
            return error_response(502, "GFS_UNAVAILABLE", str(exc))
        except GfsConnectionError as exc:
            # A paired global server is unreachable / refused the call (e.g.
            # subscribing to a GFS-discovered space while that GFS is down).
            # 502 + GFS_UNAVAILABLE matches HighlightPublicationError above:
            # an upstream we depend on failed, not the caller's request.
            return error_response(502, "GFS_UNAVAILABLE", str(exc))
        except HighlightFrameLimitError as exc:
            return error_response(429, "HIGHLIGHT_FRAME_LIMIT", str(exc))
        except MomentRateLimitError as exc:
            return error_response(429, "MOMENT_RATE_LIMIT", str(exc))
        except SpaceZoneLimitError as exc:
            return error_response(409, "ZONE_LIMIT", str(exc))
        except SpaceZoneNameConflictError as exc:
            return error_response(409, "ZONE_NAME_TAKEN", str(exc))
        except KeyError as exc:
            # §Audit #7 / #15: never echo the raw KeyError text — the
            # missing dict-key name leaks payload structure / internal
            # IDs to a client probing a 404 surface. Fixed string only;
            # the underlying detail is logged for triage.
            log.warning(
                "%s: KeyError surfaced from handler: %s",
                type(self).__name__,
                exc,
            )
            return error_response(404, "NOT_FOUND", "Resource not found.")
        except PublicSpaceLimitError as exc:
            return error_response(409, "SPACE_LIMIT", str(exc))
        except ModerationAlreadyDecidedError as exc:
            return error_response(409, "ALREADY_DECIDED", str(exc))
        except PollClosedError as exc:
            return error_response(409, "POLL_CLOSED", str(exc))
        except SpaceBotSlugTakenError as exc:
            return error_response(409, "SLUG_TAKEN", str(exc))
        except SpaceBotDisabledError as exc:
            return error_response(403, "BOT_DISABLED", str(exc))
        except SpaceBotError as exc:
            # Generic validation error from the bot-bridge domain.
            return error_response(422, "UNPROCESSABLE", str(exc))
        except StorageQuotaExceeded as exc:
            # Spec §5.2 maps storage-quota exceeded to HTTP 507
            # "Insufficient Storage" (not 413) so clients can
            # disambiguate from per-file size limits.
            return error_response(507, "STORAGE_FULL", str(exc))
        except ScopeMismatchError as exc:
            log.warning(
                "%s: ScopeMismatchError from handler: %s",
                type(self).__name__,
                exc,
            )
            return error_response(400, "UNPROCESSABLE", str(exc))
        except FeatureDisabledError as exc:
            return error_response(
                403,
                "FEATURE_DISABLED",
                str(exc),
                extra={"section": exc.section},
            )
        except NoActiveConflictError as exc:
            return error_response(409, "NO_CONFLICT", str(exc))
        except PageLockError as exc:
            return error_response(409, "LOCKED", str(exc))
        except (
            SpacePermissionError,
            GalleryPermissionError,
            GuardianRequiredError,
        ) as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except (ChildProtectionError, BazaarServiceError) as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except MediaRequiresDirectPairingError as exc:
            # Distinct from the generic ``ValueError`` clause below
            # because the SPA renders a specific copy ("only paired
            # households can receive media") instead of the safe
            # blanket "Request could not be processed." Issue #319
            # paragraph 5 — the user needs to know *why* their picture
            # didn't go through.
            return error_response(
                422,
                "MEDIA_REQUIRES_DIRECT_PAIRING",
                str(exc),
            )
        except ValueError as exc:
            # §Audit #7: ``str(exc)`` on a ValueError can carry
            # implementation detail (e.g. "Replay detected: msg_id=…"
            # bubbled up from a service layer). Log full context;
            # surface only a generic validation message.
            log.warning(
                "%s: ValueError surfaced from handler: %s",
                type(self).__name__,
                exc,
            )
            return error_response(
                422,
                "UNPROCESSABLE",
                "Request could not be processed.",
            )
        except Exception:
            log.exception("Unhandled error in %s", type(self).__name__)
            return error_response(500, "INTERNAL_ERROR")
