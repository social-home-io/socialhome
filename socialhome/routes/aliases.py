"""Personal user aliases — read/write surface (§4.1.6).

These endpoints scope strictly to the requesting user. Aliases are
viewer-private: they never federate, and one user cannot read or set
another user's aliases.

* ``GET /api/aliases/users``                     — list the viewer's aliases.
* ``PUT /api/aliases/users/{user_id}``           — set the viewer's alias for a target.
* ``DELETE /api/aliases/users/{user_id}``        — clear it.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from ..services.alias_service import (
    AliasInvalidError,
    AliasNotFoundError,
)
from .base import BaseView


class AliasCollectionView(BaseView):
    """``GET /api/aliases/users`` — list the viewer's aliases."""

    async def get(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(K.alias_service_key)
        rows = await svc.list_aliases(self.user.user_id)
        return self._json(
            {
                "aliases": [
                    {"target_user_id": uid, "alias": alias}
                    for uid, alias in rows.items()
                ],
            },
        )


class AliasItemView(BaseView):
    """``PUT|DELETE /api/aliases/users/{user_id}`` — viewer's alias for *user_id*."""

    async def put(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        body = await self.body()
        alias = str(body.get("alias") or "")
        target_user_id = self.match("user_id")
        svc = self.svc(K.alias_service_key)
        try:
            await svc.set_user_alias(
                viewer_user_id=self.user.user_id,
                target_user_id=target_user_id,
                alias=alias,
            )
        except AliasInvalidError as exc:
            return error_response(422, "INVALID_ALIAS", str(exc))
        except AliasNotFoundError:
            return error_response(404, "USER_NOT_FOUND", "Unknown user.")
        return self._json(
            {
                "target_user_id": target_user_id,
                "alias": alias.strip(),
            }
        )

    async def delete(self) -> web.Response:
        if self.user is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        target_user_id = self.match("user_id")
        svc = self.svc(K.alias_service_key)
        await svc.clear_user_alias(
            viewer_user_id=self.user.user_id,
            target_user_id=target_user_id,
        )
        return self._json({"target_user_id": target_user_id, "alias": None})
