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

from ..app_keys import household_features_service_key
from ..auth import current_user
from ..domain.household_features import FeatureDisabledError
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
from ..services.child_protection_service import (
    ChildProtectionError,
    GuardianRequiredError,
)
from ..services.gallery_service import GalleryNotFoundError, GalleryPermissionError
from ..services.page_conflict_service import NoActiveConflictError
from ..services.poll_service import PollClosedError, PollNotFoundError
from ..services.storage_quota_service import StorageQuotaExceeded

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
        svc = self.request.app.get(household_features_service_key)
        if svc is not None:
            await svc.require_enabled(section)

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
        ) as exc:
            return error_response(404, "NOT_FOUND", str(exc))
        except KeyError as exc:
            return error_response(404, "NOT_FOUND", str(exc).strip("'\""))
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
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except Exception:
            log.exception("Unhandled error in %s", type(self).__name__)
            return error_response(500, "INTERNAL_ERROR")
