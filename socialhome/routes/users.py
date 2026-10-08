"""User routes — account info, preferences, API tokens.

GET  /api/me                  — current user profile
PATCH /api/me                 — update display_name / bio / preferences / status
GET  /api/users               — list all active users (admin view)
POST /api/me/picture          — upload a new profile picture (multipart)
DELETE /api/me/picture        — clear the picture (revert to initials)
GET  /api/users/{user_id}/picture — stream the cached WebP bytes
POST /api/me/username         — rename the authenticated user's login username
POST /api/me/handle           — set the authenticated user's public @handle
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
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from ..app_keys import (
    auth_audit_log_repo_key,
    child_protection_service_key,
    data_export_service_key,
    media_signer_key,
    password_reset_repo_key,
    platform_adapter_key,
    profile_picture_repo_key,
    rate_limiter_key,
    user_repo_key,
    user_service_key,
)
from ..csp import MEDIA_CSP
from ..domain.errors import ImageTooLargeError
from ..domain.media_constraints import PROFILE_PICTURE_MAX_UPLOAD_BYTES
from ..domain.user import User, UserStatus, _picture_url
from ..media_signer import sign_media_urls_in
from ..platform.adapter import Capability
from ..rate_limiter import client_bucket
from ..security import error_response, sanitise_for_api
from ..services.user_service import _UNSET
from .base import BaseView

log = logging.getLogger(__name__)

#: Brute-force budget for ``/api/auth/token`` (section 25.7). Both valid and
#: invalid attempts burn the quota — a throttle that only reacts to
#: failed attempts lets attackers tell valid usernames apart.
AUTH_TOKEN_RATE_LIMIT = 5
AUTH_TOKEN_RATE_WINDOW_S = 15 * 60

#: ``PATCH /api/me`` keys that go to :meth:`UserService.set_status`.
_STATUS_KEYS = ("status_emoji", "status_text", "status_clear_after")


def _user_to_dict(user) -> dict:
    """Convert a User domain object to a sanitised dict.

    Injects the synthetic ``picture_url`` derived from
    ``picture_hash`` so the frontend doesn't have to build the URL.

    Note: this returns the *unsigned* picture_url. Routes returning a
    user dict to the API call :func:`_user_to_dict_signed` instead so
    the URL carries a short-lived signature for browser ``<img>``
    loads.
    """
    if dataclasses.is_dataclass(user) and not isinstance(user, type):
        raw = dataclasses.asdict(user)
    else:
        raw = dict(user)
    # A status past its "clear after" deadline reads as unset even before
    # the expiry sweep clears the row.
    if isinstance(user, User) and user.status.is_expired(datetime.now(timezone.utc)):
        raw["status"] = dataclasses.asdict(UserStatus())
    raw["picture_url"] = _picture_url(
        str(raw.get("user_id") or ""),
        raw.get("picture_hash"),
    )
    return sanitise_for_api(raw)


def _user_to_dict_signed(request: web.Request, user) -> dict:
    """:func:`_user_to_dict` + sign ``picture_url`` for the SPA."""
    payload = _user_to_dict(user)
    signer = request.app.get(media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


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
            raise ImageTooLargeError(PROFILE_PICTURE_MAX_UPLOAD_BYTES)
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
        payload = _user_to_dict_signed(self.request, user)
        # §CP.R — the caller's OWN protection state: that it is protected
        # and which surfaces the server refuses, so the SPA can explain
        # instead of failing on click. Never ``is_minor`` / ``declared_age``
        # (SENSITIVE_FIELDS), and only here — ``/api/users`` and every
        # other user payload stay silent about who is protected.
        restrictions = await self.svc(child_protection_service_key).restrictions_for(
            user.user_id
        )
        payload["protected"] = bool(restrictions)
        payload["restrictions"] = [c.value for c in restrictions]
        return web.json_response(payload)

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
            return web.json_response(_user_to_dict_signed(self.request, user))

        # Timezone — used by the SPA's cold-start probe to mirror the
        # browser's resolved zone into ``users.tz`` so calendar events
        # default to the user's local wall clock without a separate
        # settings step. Validated via ``ZoneInfo`` in the service.
        if "tz" in body:
            try:
                user = await svc.set_tz(ctx.username, str(body["tz"]))
            except ValueError as exc:
                return error_response(422, "UNPROCESSABLE", str(exc))
            except KeyError:
                return error_response(404, "NOT_FOUND", "User not found.")
            return web.json_response(_user_to_dict_signed(self.request, user))

        # Status (emoji + one line + optional "clear after"). A key that
        # is absent keeps its current value, so the expiry chips can send
        # ``status_clear_after`` alone; setting emoji/text without a
        # ``status_clear_after`` means "keep until changed".
        if any(k in body for k in _STATUS_KEYS):
            current = await svc.get(ctx.username)
            if current is None:
                return error_response(404, "NOT_FOUND", "User not found.")
            cur = current.status
            if cur.is_expired(datetime.now(timezone.utc)):
                cur = UserStatus()
            try:
                user = await svc.set_status(
                    ctx.username,
                    emoji=body.get("status_emoji", cur.emoji),
                    text=body.get("status_text", cur.text),
                    clear_after=body.get("status_clear_after"),
                )
            except ValueError as exc:
                return error_response(422, "UNPROCESSABLE", str(exc))
            return web.json_response(_user_to_dict_signed(self.request, user))

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
            return web.json_response(_user_to_dict_signed(self.request, user))

        # No recognised fields → return current user unchanged.
        user = await svc.get(ctx.username)
        if user is None:
            return error_response(404, "NOT_FOUND", "User not found.")
        return web.json_response(_user_to_dict_signed(self.request, user))


class MeUsernameView(BaseView):
    """POST /api/me/username — rename the authenticated user's login username.

    Only ``manual``-source users can rename — an ``ha``-synced row's name is
    owned by Home Assistant, so the service raises :class:`PermissionError`
    (→ 403). The ``user_id`` / cryptographic identity is unaffected by the
    rename (§v_26). Format / reserved / taken failures map to 422.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        new = body.get("username")
        if not isinstance(new, str) or not new.strip():
            return error_response(422, "UNPROCESSABLE", "username is required.")
        svc = self.svc(user_service_key)
        try:
            await svc.rename_username(ctx.username, new)
        except PermissionError:
            return error_response(
                403,
                "HA_CONTROLLED",
                "Your username is managed by Home Assistant.",
            )
        except ValueError as exc:
            return error_response(422, "INVALID_USERNAME", str(exc))
        return web.json_response({"username": new.strip()})


class MeHandleView(BaseView):
    """POST /api/me/handle — set the authenticated user's public @handle.

    Unlike username rename, handle is editable by ALL users, including
    ``ha``-source rows. Format / reserved / taken failures map to 422.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        new = body.get("handle")
        if not isinstance(new, str) or not new.strip():
            return error_response(422, "UNPROCESSABLE", "handle is required.")
        svc = self.svc(user_service_key)
        try:
            await svc.set_handle(ctx.username, new)
        except ValueError as exc:
            return error_response(422, "INVALID_HANDLE", str(exc))
        return web.json_response({"handle": new.strip()})


class MePictureView(BaseView):
    """POST + DELETE /api/me/picture — upload / clear the caller's avatar."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        try:
            raw = await _read_multipart_image(self.request)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        # An unreadable image raises ``ImageUnreadableError`` — BaseView
        # answers it (422 IMAGE_UNREADABLE), never the library's error text.
        user = await svc.set_picture(ctx.user_id, raw)
        return web.json_response(_user_to_dict_signed(self.request, user))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        await svc.clear_picture(ctx.user_id)
        return web.Response(status=204)


class MeOnboardingCompleteView(BaseView):
    """``POST /api/me/onboarding-complete`` — flip the caller's
    ``is_new_member`` flag false so the SPA stops re-showing the
    first-run wizard. Idempotent — already-cleared accounts no-op."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        await svc.clear_onboarding(ctx.username)
        return web.Response(status=204)


class MePictureRefreshFromHaView(BaseView):
    """POST /api/me/picture/refresh-from-ha — import the HA ``person.*``
    entity_picture as the caller's avatar.

    Available on any platform whose adapter declares the
    ``HA_PERSON_DIRECTORY`` capability (HA Core + HAOS today); the
    standalone adapter returns 501. No background sync loop by
    design — users trigger a refresh manually when their HA avatar
    changes.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        adapter = self.svc(platform_adapter_key)
        if Capability.HA_PERSON_DIRECTORY not in adapter.capabilities:
            return error_response(
                501,
                "NOT_IMPLEMENTED",
                "HA avatar refresh is only available when the user "
                "directory is HA's person registry.",
            )
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
        # An unreadable image raises ``ImageUnreadableError`` — BaseView
        # answers it (422 IMAGE_UNREADABLE), never the library's error text.
        user = await svc.set_picture(ctx.user_id, raw)
        return web.json_response(_user_to_dict_signed(self.request, user))


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
                "Content-Security-Policy": MEDIA_CSP,
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
        return web.json_response(
            [_user_to_dict_signed(self.request, u) for u in users],
        )


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
        return web.json_response(_user_to_dict_signed(self.request, updated))


class TokenCollectionView(BaseView):
    """GET/POST /api/me/tokens — list or create API tokens for the caller."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        svc = self.svc(user_service_key)
        rows = await svc.list_api_tokens(ctx.username)
        # The origin an external client (script, integration) reaches us
        # at — ``None`` when the deployment has no direct public address
        # (e.g. an add-on reachable only through ingress).
        base_url = await self.svc(platform_adapter_key).get_public_base_url()
        return web.json_response(
            {
                "base_url": base_url,
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
                ],
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
    """DELETE /api/me/tokens/{id} — revoke one of the caller's API tokens.

    Scoped to the caller: an id that belongs to another user is a silent
    no-op (still 204, so the response doesn't reveal whether the id
    exists). Household-wide revocation is ``/api/admin/tokens/{id}``.
    """

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(user_service_key)
        token_id = self.match("id")
        await svc.revoke_own_api_token(ctx.username, token_id)
        return web.Response(status=204)


class AdminUserCollectionView(BaseView):
    """``POST /api/admin/users`` — admin user creation on platforms
    that own their own user table.

    Returns 405 when the adapter declares ``HA_PERSON_DIRECTORY`` —
    on those platforms users come from HA's ``person.*`` registry and
    provisioning goes through ``POST /api/admin/ha-users/{username}/provision``.
    The body is ``{username, password, display_name?, is_admin?}``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        adapter = self.svc(platform_adapter_key)
        if Capability.HA_PERSON_DIRECTORY in adapter.capabilities:
            return error_response(
                405,
                "WRONG_MODE",
                "Use /api/admin/ha-users/{username}/provision when the "
                "user directory is HA's person registry.",
            )
        body = await self.body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "username and password are required.",
            )
        if len(password) < 8:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Password must be at least 8 characters.",
            )
        display_name = str(body.get("display_name") or username)
        is_admin = bool(body.get("is_admin"))
        # Reject duplicates loudly so the SPA can surface a clean
        # message rather than a generic 422 from the UNIQUE
        # constraint downstream.
        existing = await adapter.users.get(username)
        if existing is not None:
            return error_response(
                409,
                "USERNAME_TAKEN",
                f"User {username!r} already exists.",
            )
        # Adapter handles the platform_users + password row.
        await adapter.users.enable(username, password=password)
        # Domain side — the user_service emits the UserProvisioned event
        # and writes the users row used by federation, feeds, etc.
        user_service = self.svc(user_service_key)
        user = await user_service.provision(
            username=username,
            display_name=display_name,
            is_admin=is_admin,
            email=None,
            picture_url=None,
            source="manual",
        )
        return web.json_response(
            {
                "username": user.username,
                "user_id": user.user_id,
                "is_admin": user.is_admin,
            },
            status=201,
        )


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


class IssuePasswordResetView(BaseView):
    """POST /api/admin/users/{username}/issue-password-reset — admin only.

    Standalone mode has no SMTP, so a forgotten password is recovered
    by an admin issuing a one-time, single-use, 1h-TTL token. The
    admin then hands the resulting reset URL to the user out-of-band.
    The raw token is returned exactly once in the response — only its
    SHA-256 hash is stored.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        adapter = self.svc(platform_adapter_key)
        if not adapter.supports_bearer_token_auth:
            return error_response(
                404,
                "NOT_FOUND",
                "Password reset is not available on this platform.",
            )
        username = self.match("username")
        existing = await adapter.users.get(username)
        if existing is None:
            return error_response(404, "NOT_FOUND", "User not found.")
        repo = self.svc(password_reset_repo_key)
        token, expires_at = await repo.create_token(
            username,
            ctx.user_id,
        )
        audit = self.svc(auth_audit_log_repo_key)
        await audit.record(
            "reset_issue",
            username=username,
            ip_address=self.request.remote,
            metadata={"issued_by": ctx.user_id},
        )
        log.info(
            "password reset issued for %s by admin %s (expires %s)",
            username,
            ctx.user_id,
            expires_at,
        )
        return web.json_response(
            {
                "token": token,
                "expires_at": expires_at,
                "username": username,
            }
        )


class RedeemPasswordResetView(BaseView):
    """POST /api/auth/redeem-password-reset — public, rate-limited.

    Body: ``{token, new_password}``. On success the user's password
    is rotated and the token is marked used (single-use). Errors:

    * 422 if either field is missing / new_password too short
    * 410 if the token is unknown / expired / already used
    * 429 if too many attempts from this IP
    """

    async def post(self) -> web.Response:
        adapter = self.svc(platform_adapter_key)
        if not adapter.supports_bearer_token_auth:
            return error_response(
                404,
                "NOT_FOUND",
                "Password reset is not available on this platform.",
            )

        # Same IP-bucket throttle as /api/auth/token. The reset path
        # is the obvious target for a brute-force attempt at guessing
        # tokens, so we share the bucket budget with login attempts.
        limiter = self.request.app.get(rate_limiter_key)
        if limiter is not None:
            client_ip = client_bucket(self.request.remote)
            bucket = f"password-reset:{client_ip}"
            if not limiter.is_allowed(
                bucket,
                limit=AUTH_TOKEN_RATE_LIMIT,
                window_s=AUTH_TOKEN_RATE_WINDOW_S,
            ):
                return error_response(
                    429,
                    "RATE_LIMITED",
                    "Too many attempts — wait a few minutes.",
                )

        body = await self.body()
        token = str(body.get("token") or "")
        new_password = str(body.get("new_password") or "")
        if not token or not new_password:
            return error_response(
                422,
                "UNPROCESSABLE",
                "token and new_password are required.",
            )
        if len(new_password) < 8:
            return error_response(
                422,
                "UNPROCESSABLE",
                "Password must be at least 8 characters.",
            )
        repo = self.svc(password_reset_repo_key)
        audit = self.svc(auth_audit_log_repo_key)
        username = await repo.consume_token(token)
        if username is None:
            await audit.record(
                "reset_redeem_failure",
                ip_address=self.request.remote,
            )
            return error_response(
                410,
                "INVALID_TOKEN",
                "This reset link has expired or already been used.",
            )
        await adapter.change_password(username, new_password)
        await audit.record(
            "reset_redeem_success",
            username=username,
            ip_address=self.request.remote,
        )
        log.info("password reset redeemed for %s", username)
        return web.Response(status=204)


class AdminAuthAuditLogView(BaseView):
    """GET /api/admin/auth-audit — admin only.

    Returns the most recent rows from the auth audit log so an admin
    can spot brute-force attempts or correlate "I can't sign in"
    reports with what the server saw.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        limit_raw = self.request.query.get("limit", "100")
        try:
            limit = max(1, min(500, int(limit_raw)))
        except TypeError, ValueError:
            limit = 100
        repo = self.svc(auth_audit_log_repo_key)
        rows = await repo.list_recent(limit)
        return web.json_response({"events": rows})


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
            client_ip = client_bucket(self.request.remote)
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
        audit = self.svc(auth_audit_log_repo_key)
        if token is None:
            await audit.record(
                "login_failure",
                username=username,
                ip_address=self.request.remote,
            )
            return error_response(401, "UNAUTHENTICATED", "Invalid credentials.")
        await audit.record(
            "login_success",
            username=username,
            ip_address=self.request.remote,
        )
        return web.json_response({"token": token})
