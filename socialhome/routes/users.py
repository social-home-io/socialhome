"""User routes — account info, preferences, API tokens.

GET  /api/me                  — current user profile
PATCH /api/me                 — update display_name / bio / preferences
GET  /api/users               — list all active users (admin view)
POST /api/me/picture          — upload a new profile picture (multipart)
DELETE /api/me/picture        — clear the picture (revert to initials)
GET  /api/users/{user_id}/picture — stream the cached WebP bytes
POST /api/me/tokens           — create an API token
DELETE /api/me/tokens/{id}    — revoke a token
GET  /api/me/export           — GDPR-style data export
GET  /api/users/{user_id}/export — admin-only data export
POST /api/auth/token          — standalone-mode login

All handlers are THIN — one service call + JSON response. No SQL here.
"""

from __future__ import annotations

import dataclasses
import logging

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from ..app_keys import (
    config_key,
    data_export_service_key,
    platform_adapter_key,
    profile_picture_repo_key,
    rate_limiter_key,
    user_repo_key,
    user_service_key,
)
from ..domain.media_constraints import PROFILE_PICTURE_MAX_UPLOAD_BYTES
from ..domain.user import _picture_url
from ..security import error_response, sanitise_for_api
from ..services.user_service import _UNSET
from .base import BaseView

log = logging.getLogger(__name__)

#: Brute-force budget for ``/api/auth/token`` (section 25.7). Both valid and
#: invalid attempts burn the quota — a throttle that only reacts to
#: failed attempts lets attackers tell valid usernames apart.
AUTH_TOKEN_RATE_LIMIT = 5
AUTH_TOKEN_RATE_WINDOW_S = 15 * 60


def _user_to_dict(user) -> dict:
    """Convert a User domain object to a sanitised dict.

    Injects the synthetic ``picture_url`` derived from
    ``picture_hash`` so the frontend doesn't have to build the URL.
    """
    if dataclasses.is_dataclass(user) and not isinstance(user, type):
        raw = dataclasses.asdict(user)
    else:
        raw = dict(user)
    raw["picture_url"] = _picture_url(
        str(raw.get("user_id") or ""),
        raw.get("picture_hash"),
    )
    return sanitise_for_api(raw)


async def _read_multipart_image(request: web.Request) -> bytes:
    """Read a single ``file=...`` multipart field and return its bytes.

    Raises :class:`ValueError` with a 4xx-friendly message on failure.
    """
    if not request.content_type.startswith("multipart/"):
        raise ValueError("Expected multipart/form-data.")
    reader = await request.multipart()
    field = await reader.next()
    if field is None or not isinstance(field, BodyPartReader):
        raise ValueError("No file part in upload.")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await field.read_chunk(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > PROFILE_PICTURE_MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


class MeView(BaseView):
    """GET/PATCH /api/me — current user profile."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        user = await svc.get(ctx.username)
        if user is None:
            return error_response(404, "NOT_FOUND", "User not found.")
        return web.json_response(_user_to_dict(user))

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        body = await self.body()

        # Preferences are persisted separately (nested JSON blob).
        if "preferences" in body:
            user = await svc.patch_preferences(
                ctx.username,
                body["preferences"],
            )
            return web.json_response(_user_to_dict(user))

        # Display-name + bio go through patch_profile so a
        # UserProfileUpdated event fires for WS + federation fan-out.
        display_name = body.get("display_name", _UNSET)
        bio = body.get("bio", _UNSET)
        if display_name is not _UNSET or bio is not _UNSET:
            try:
                user = await svc.patch_profile(
                    ctx.username,
                    display_name=display_name,
                    bio=bio,
                )
            except ValueError as exc:
                return error_response(422, "UNPROCESSABLE", str(exc))
            except KeyError:
                return error_response(404, "NOT_FOUND", "User not found.")
            return web.json_response(_user_to_dict(user))

        # No recognised fields → return current user unchanged.
        user = await svc.get(ctx.username)
        if user is None:
            return error_response(404, "NOT_FOUND", "User not found.")
        return web.json_response(_user_to_dict(user))


class MePictureView(BaseView):
    """POST + DELETE /api/me/picture — upload / clear the caller's avatar."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        try:
            raw = await _read_multipart_image(self.request)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        try:
            user = await svc.set_picture(ctx.user_id, raw)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_user_to_dict(user))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        await svc.clear_picture(ctx.user_id)
        return web.Response(status=204)


class MePictureRefreshFromHaView(BaseView):
    """POST /api/me/picture/refresh-from-ha — import the HA ``person.*``
    entity_picture as the caller's avatar.

    HA mode only; standalone returns 501. No background sync loop by
    design — users trigger a refresh manually when their HA avatar
    changes.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        config = self.svc(config_key)
        if config.mode != "ha":
            return error_response(
                501,
                "NOT_IMPLEMENTED",
                "HA avatar refresh is only available in HA mode.",
            )
        adapter = self.svc(platform_adapter_key)
        fetcher = getattr(adapter, "fetch_entity_picture_bytes", None)
        if fetcher is None:
            return error_response(
                501,
                "NOT_IMPLEMENTED",
                "This HA adapter does not expose person-picture fetch.",
            )
        raw = await fetcher(ctx.username)
        if raw is None:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Home Assistant has no picture for this user.",
            )
        svc = self.svc(user_service_key)
        try:
            user = await svc.set_picture(ctx.user_id, raw)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_user_to_dict(user))


class UserPictureView(BaseView):
    """GET /api/users/{user_id}/picture — stream the cached WebP."""

    async def get(self) -> web.Response:
        self.user  # auth check
        user_id = self.match("user_id")
        repo = self.svc(profile_picture_repo_key)
        got = await repo.get_user_picture(user_id)
        if got is None:
            return error_response(
                404,
                "NOT_FOUND",
                "No picture set for this user.",
            )
        bytes_webp, _hash = got
        return web.Response(
            body=bytes_webp,
            content_type="image/webp",
            headers={
                # The URL carries ?v=<hash>, so the content is immutable
                # for that version — aggressive caching is safe.
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )


class UserCollectionView(BaseView):
    """GET /api/users — list all active users."""

    async def get(self) -> web.Response:
        svc = self.svc(user_service_key)
        users = await svc.list_active()
        return web.json_response([_user_to_dict(u) for u in users])


class UserDetailView(BaseView):
    """``PATCH /api/users/{user_id}`` — admin edits another user.

    Currently supports ``{is_admin: bool}`` only. The caller must be an
    admin; a non-admin attempting to change anyone's flag (even their
    own) via this route is rejected with 403. Self-demotion is allowed
    but the *last* admin can't demote themselves — that's guarded by
    the service.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        if not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        svc = self.svc(user_service_key)
        repo = self.svc(user_repo_key)
        target_id = self.match("user_id")
        target = await repo.get_by_user_id(target_id)
        if target is None:
            return error_response(404, "NOT_FOUND", "User not found.")
        body = await self.body()
        if "is_admin" not in body:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Only 'is_admin' is editable via this route.",
            )
        desired = bool(body["is_admin"])
        # Guard: refuse to demote the last remaining admin.
        if target.is_admin and not desired:
            actives = await svc.list_active()
            admins = [u for u in actives if u.is_admin]
            if len(admins) <= 1:
                return error_response(
                    409,
                    "LAST_ADMIN",
                    "Cannot demote the last remaining admin.",
                )
        updated = await svc.set_admin(target.username, desired)
        return web.json_response(_user_to_dict(updated))


class TokenCollectionView(BaseView):
    """GET/POST /api/me/tokens — list or create API tokens for the caller."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(user_service_key)
        rows = await svc.list_api_tokens(ctx.username)
        return web.json_response(
            {
                "tokens": [
                    {
                        "token_id": r["token_id"],
                        "label": r.get("label") or "",
                        "created_at": r.get("created_at"),
                        "last_used_at": r.get("last_used_at"),
                        "expires_at": r.get("expires_at"),
                        "revoked_at": r.get("revoked_at"),
                    }
                    for r in rows
                    if not r.get("revoked_at")
                ]
            }
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        body = await self.body()
        label = body.get("label", "")
        expires_at = body.get("expires_at")
        token_id, raw_token = await svc.create_api_token(
            ctx.username,
            label=label,
            expires_at=expires_at,
        )
        return web.json_response(
            {"token_id": token_id, "token": raw_token},
            status=201,
        )


class TokenDetailView(BaseView):
    """DELETE /api/me/tokens/{id} — revoke an API token."""

    async def delete(self) -> web.Response:
        svc = self.svc(user_service_key)
        token_id = self.match("id")
        await svc.revoke_api_token(token_id)
        return web.Response(status=204)


class AdminTokenCollectionView(BaseView):
    """``GET /api/admin/tokens`` — §A7 list every user's active API
    tokens for the household sessions admin panel. Admin-only.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        repo = self.svc(user_repo_key)
        rows = await repo.list_all_api_tokens()
        return web.json_response(
            {
                "tokens": [
                    {
                        "token_id": r["token_id"],
                        "label": r.get("label") or "",
                        "created_at": r.get("created_at"),
                        "last_used_at": r.get("last_used_at"),
                        "expires_at": r.get("expires_at"),
                        "user_id": r.get("user_id"),
                        "username": r.get("username"),
                        "display_name": r.get("display_name"),
                    }
                    for r in rows
                    if not r.get("revoked_at")
                ]
            }
        )


class AdminTokenDetailView(BaseView):
    """``DELETE /api/admin/tokens/{id}`` — admin revokes any user's
    session. Wraps the existing revoke path without scoping by owner.
    """

    async def delete(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        svc = self.svc(user_service_key)
        await svc.revoke_api_token(self.match("id"))
        return web.Response(status=204)


class MeExportView(BaseView):
    """GET /api/me/export — GDPR-style export of the caller's data."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(data_export_service_key)
        body = await svc.export_to_bytes(ctx.user_id)
        return web.Response(
            body=body,
            content_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="socialhome-export-{ctx.username}.json"',
            },
        )


class UserExportView(BaseView):
    """GET /api/users/{user_id}/export — admin-only data export."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        target = self.match("user_id")
        svc = self.svc(data_export_service_key)
        body = await svc.export_to_bytes(target)
        return web.Response(
            body=body,
            content_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="socialhome-export-{target}.json"',
            },
        )


class AuthTokenView(BaseView):
    """POST /api/auth/token — standalone-mode login, returns a bearer token."""

    async def post(self) -> web.Response:
        adapter = self.svc(platform_adapter_key)
        if not adapter.supports_bearer_token_auth:
            return error_response(
                404,
                "NOT_FOUND",
                "Token auth is not available on this platform.",
            )

        # IP-bucket throttle — independent of the authenticated rate limiter.
        limiter = self.request.app.get(rate_limiter_key)
        if limiter is not None:
            client_ip = self.request.remote or "unknown"
            bucket = f"auth-token:{client_ip}"
            if not limiter.is_allowed(
                bucket,
                limit=AUTH_TOKEN_RATE_LIMIT,
                window_s=AUTH_TOKEN_RATE_WINDOW_S,
            ):
                return error_response(
                    429,
                    "RATE_LIMITED",
                    "Too many login attempts — wait a few minutes.",
                )

        body = await self.body()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not username or not password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username and password are required.",
            )
        token = await adapter.issue_bearer_token(username, password)
        if token is None:
            return error_response(401, "UNAUTHENTICATED", "Invalid credentials.")
        return web.json_response({"token": token})
