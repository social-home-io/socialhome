"""Space routes — /api/spaces/* (section 23.48-23.73).

Thin handlers delegating to :class:`SpaceService`.
"""

from __future__ import annotations

from aiohttp import web

from aiohttp.multipart import BodyPartReader

import math

from ..app_keys import (
    alias_resolver_key,
    federation_repo_key,
    media_signer_key,
    notification_repo_key,
    presence_service_key,
    profile_picture_repo_key,
    space_bot_repo_key,
    space_cover_repo_key,
    space_repo_key,
    space_service_key,
    space_sync_scheduler_key,
    space_zone_repo_key,
    user_repo_key,
)
from ..domain.post import LocationData
from ..domain.space import SpaceZone
from ..domain.user import SYSTEM_AUTHOR
from ..domain.federation import PairingStatus
from ..domain.media_constraints import PROFILE_PICTURE_MAX_UPLOAD_BYTES
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..security import error_response, sanitise_for_api
from ..services.space_service import _UNSET_MEMBER_PROFILE
from .base import BaseView

_PROFILE_PICTURE_MAX_UPLOAD_BYTES = PROFILE_PICTURE_MAX_UPLOAD_BYTES

# Earth's mean radius — used by the zone-match helper. Mirrors the
# constant in :mod:`services.space_location_outbound`.
_EARTH_RADIUS_M = 6_371_000.0


def _match_zone(
    zones: "list[SpaceZone]", latitude: float, longitude: float
) -> "SpaceZone | None":
    """Return the closest zone whose great-circle distance to
    ``(latitude, longitude)`` is within its ``radius_m``. Used by
    :class:`SpacePresenceView` to render zone-only-mode responses
    server-side. Mirrors the algorithm in
    ``services/space_location_outbound._match_zone`` and the
    client-side ``matchZoneName`` helper.
    """
    best: "tuple[float, SpaceZone] | None" = None
    p1 = math.radians(latitude)
    for z in zones:
        p2 = math.radians(z.latitude)
        dp = p2 - p1
        dl = math.radians(z.longitude - longitude)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        d = 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))
        if d <= z.radius_m and (best is None or d < best[0]):
            best = (d, z)
    return best[1] if best is not None else None


class SpaceCollectionView(BaseView):
    """``GET /api/spaces`` + ``POST /api/spaces`` — list-or-create spaces."""

    async def get(self) -> web.Response:
        """List every space the caller is a member of (§23.48).

        Returns a lightweight row per space — enough for the sidebar
        rendering. Use ``GET /api/spaces/{id}`` for the full config.
        """
        ctx = self.user
        repo = self.svc(space_repo_key)
        spaces = await repo.list_for_user(ctx.user_id)
        return web.json_response(
            [
                sanitise_for_api(
                    {
                        "id": s.id,
                        "name": s.name,
                        "emoji": s.emoji,
                        "space_type": s.space_type.value,
                        "join_mode": s.join_mode.value,
                    }
                )
                for s in spaces
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        space = await svc.create_space(
            owner_username=ctx.username,
            name=body.get("name", ""),
            description=body.get("description"),
            emoji=body.get("emoji"),
            space_type=body.get("space_type", "private"),
            join_mode=body.get("join_mode", "invite_only"),
            retention_days=body.get("retention_days"),
            lat=body.get("lat"),
            lon=body.get("lon"),
            radius_km=body.get("radius_km"),
        )
        return web.json_response(
            sanitise_for_api(
                {
                    "id": space.id,
                    "name": space.name,
                    "space_type": space.space_type.value,
                }
            ),
            status=201,
        )


class SpaceDetailView(BaseView):
    """GET/PATCH/DELETE /api/spaces/{id} — get, update, dissolve a space."""

    async def get(self) -> web.Response:
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        space = await svc._require_space(space_id)
        cover_url = (
            f"/api/spaces/{space.id}/cover?v={space.cover_hash}"
            if space.cover_hash
            else None
        )
        payload = sanitise_for_api(
            {
                "id": space.id,
                "name": space.name,
                "description": space.description,
                "emoji": space.emoji,
                "space_type": space.space_type.value,
                "join_mode": space.join_mode.value,
                "features": space.features.to_wire_dict(),
                "retention_days": space.retention_days,
                "retention_exempt_types": list(space.retention_exempt_types),
                "about_markdown": space.about_markdown,
                "cover_hash": space.cover_hash,
                "cover_url": cover_url,
                "bot_enabled": space.bot_enabled,
            }
        )
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return web.json_response(payload)

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        updated = await svc.update_config(
            space_id,
            actor_username=ctx.username,
            name=body.get("name"),
            description=body.get("description"),
            emoji=body.get("emoji"),
            join_mode=body.get("join_mode"),
            space_type=body.get("space_type"),
            retention_days=body.get("retention_days"),
            retention_exempt_types=body.get("retention_exempt_types"),
            about_markdown=(
                body["about_markdown"]
                if "about_markdown" in body
                else _UNSET_MEMBER_PROFILE
            ),
            bot_enabled=body.get("bot_enabled"),
        )
        return web.json_response(
            {
                "id": updated.id,
                "name": updated.name,
                "about_markdown": updated.about_markdown,
            }
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        await svc.dissolve_space(space_id, actor_username=ctx.username)
        return web.json_response({"ok": True})


def _member_to_dict(
    m,
    space_id: str,
    *,
    display_name: str | None = None,
    personal_alias: str | None = None,
) -> dict:
    picture_url = (
        f"/api/spaces/{space_id}/members/{m.user_id}/picture?v={m.picture_hash}"
        if m.picture_hash
        else None
    )
    return {
        "user_id": m.user_id,
        "role": m.role,
        "joined_at": m.joined_at,
        "display_name": display_name,
        "space_display_name": m.space_display_name,
        # Spec §4.1.6 — viewer-private rename for this user. The frontend
        # applies the resolution priority space_display_name >
        # personal_alias > display_name when picking what to render.
        "personal_alias": personal_alias,
        "picture_hash": m.picture_hash,
        "picture_url": picture_url,
    }


def _member_to_dict_signed(
    request: web.Request,
    m,
    space_id: str,
    *,
    display_name: str | None = None,
    personal_alias: str | None = None,
) -> dict:
    """:func:`_member_to_dict` + sign ``picture_url`` for the SPA."""
    payload = _member_to_dict(
        m,
        space_id,
        display_name=display_name,
        personal_alias=personal_alias,
    )
    signer = request.app.get(media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


class SpaceMembersView(BaseView):
    """GET/POST /api/spaces/{id}/members — list or add members."""

    async def get(self) -> web.Response:
        space_id = self.match("id")
        members = await self.svc(space_repo_key).list_members(space_id)
        # Resolve global display_name + viewer's personal alias for each
        # member in two bulk calls (no N+1).
        user_repo = self.svc(user_repo_key)
        display_names: dict[str, str] = {}
        for m in members:
            local = await user_repo.get_by_user_id(m.user_id)
            if local is not None:
                display_names[m.user_id] = local.display_name
                continue
            remote = await user_repo.get_remote(m.user_id)
            if remote is not None:
                display_names[m.user_id] = remote.display_name
        viewer = self.user
        viewer_id = viewer.user_id if viewer is not None else ""
        aliases = await self.svc(alias_resolver_key).resolve_users(
            viewer_id,
            [m.user_id for m in members],
        )
        return web.json_response(
            [
                _member_to_dict_signed(
                    self.request,
                    m,
                    space_id,
                    display_name=display_names.get(m.user_id),
                    personal_alias=aliases.get(m.user_id),
                )
                for m in members
            ],
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        member = await svc.add_member(
            space_id,
            actor_username=ctx.username,
            user_id=body["user_id"],
        )
        return web.json_response(
            {
                "user_id": member.user_id,
                "role": member.role,
            },
            status=201,
        )


async def _read_multipart_image_bytes(request: web.Request) -> bytes:
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
        if total > _PROFILE_PICTURE_MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


class SpaceMemberMeProfileView(BaseView):
    """``PATCH /api/spaces/{id}/members/me`` — edit per-space profile.

    Picture uploads go to the dedicated picture endpoint; this handler
    handles ``space_display_name`` only.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        if "space_display_name" not in body:
            return error_response(
                422,
                "UNPROCESSABLE",
                "space_display_name is required.",
            )
        try:
            member = await svc.update_member_profile(
                space_id,
                ctx.user_id,
                actor_user_id=ctx.user_id,
                space_display_name=body["space_display_name"],
            )
        except KeyError:
            return error_response(
                404,
                "NOT_FOUND",
                "You are not a member of this space.",
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.json_response(_member_to_dict_signed(self.request, member, space_id))

    async def delete(self) -> web.Response:
        """Preserve the pre-existing ``DELETE /api/spaces/{id}/members/me``
        "leave space" shortcut that the frontend still uses."""
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        await svc.remove_member(
            space_id,
            actor_username=ctx.username,
            user_id=ctx.user_id,
        )
        return web.json_response({"ok": True})


class SpaceMemberLocationSharingView(BaseView):
    """``PATCH /api/spaces/{id}/members/me/location-sharing`` (§23.8.8).

    Member-self-service: flip the caller's
    ``space_members.location_share_enabled`` for this space without
    needing admin rights. Body: ``{"enabled": bool}``. Returns ``200``
    with ``{"location_share_enabled": bool}`` so the client store can
    update without an extra GET.

    The space admin can still see this member's GPS only when the
    space has ``feature_location = 1`` AND this flag is ``true``;
    flipping it OFF here stops the next presence broadcast from
    reaching this space. No data already in flight is "recalled" —
    the next ``PresenceUpdated`` is the one that's gated.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(
                401,
                "UNAUTHENTICATED",
                "Authentication required.",
            )
        space_id = self.match("id")
        body = await self.body()
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            return error_response(
                422,
                "UNPROCESSABLE",
                "`enabled` must be a boolean.",
            )
        repo = self.svc(space_repo_key)
        ok = await repo.set_member_location_sharing(
            space_id,
            ctx.user_id,
            body["enabled"],
        )
        if not ok:
            return error_response(
                404,
                "NOT_FOUND",
                "You are not a member of this space.",
            )
        return web.json_response(
            {"location_share_enabled": body["enabled"]},
        )


class SpaceMemberMePictureView(BaseView):
    """``POST`` / ``DELETE /api/spaces/{id}/members/me/picture``."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        try:
            raw = await _read_multipart_image_bytes(self.request)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        try:
            member = await svc.set_member_picture(
                space_id,
                ctx.user_id,
                actor_user_id=ctx.user_id,
                raw_bytes=raw,
            )
        except KeyError:
            return error_response(
                404,
                "NOT_FOUND",
                "You are not a member of this space.",
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_member_to_dict_signed(self.request, member, space_id))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        try:
            await svc.clear_member_picture(
                space_id,
                ctx.user_id,
                actor_user_id=ctx.user_id,
            )
        except KeyError:
            return error_response(
                404,
                "NOT_FOUND",
                "You are not a member of this space.",
            )
        return web.Response(status=204)


class SpaceMemberPictureView(BaseView):
    """``GET /api/spaces/{id}/members/{user_id}/picture`` — stream WebP."""

    async def get(self) -> web.Response:
        self.user  # auth check
        space_id = self.match("id")
        user_id = self.match("user_id")
        repo = self.svc(profile_picture_repo_key)
        got = await repo.get_member_picture(space_id, user_id)
        if got is None:
            return error_response(
                404,
                "NOT_FOUND",
                "No per-space picture for this member.",
            )
        bytes_webp, _hash = got
        return web.Response(
            body=bytes_webp,
            content_type="image/webp",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )


class SpaceCoverView(BaseView):
    """Space hero image (§23 customization).

    * ``GET    /api/spaces/{id}/cover`` — stream the WebP bytes.
    * ``POST   /api/spaces/{id}/cover`` — multipart upload; owner/admin only.
    * ``DELETE /api/spaces/{id}/cover`` — remove the cover.
    """

    async def get(self) -> web.Response:
        # Auth enforced upstream by :class:`SignedMediaStrategy` (signed
        # URL) or :class:`BearerTokenStrategy` (fetch() callers).
        space_id = self.match("id")
        repo = self.svc(space_cover_repo_key)
        got = await repo.get(space_id)
        if got is None:
            return error_response(
                404,
                "NOT_FOUND",
                "This space has no cover image set.",
            )
        bytes_webp, _hash = got
        return web.Response(
            body=bytes_webp,
            content_type="image/webp",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        try:
            raw = await _read_multipart_image_bytes(self.request)
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        try:
            updated = await svc.set_cover(
                space_id,
                actor_username=ctx.username,
                raw_bytes=raw,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        payload = {
            "cover_hash": updated.cover_hash,
            "cover_url": f"/api/spaces/{space_id}/cover?v={updated.cover_hash}",
        }
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return web.json_response(payload)

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        try:
            await svc.clear_cover(
                space_id,
                actor_username=ctx.username,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.Response(status=204)


class SpaceMemberDetailView(BaseView):
    """``PATCH`` / ``DELETE /api/spaces/{id}/members/{user_id}`` —
    change a member's role or remove them.

    ``{user_id}`` may be the literal string ``"me"``, which is resolved
    to the caller's own ``user_id`` — used by the "Leave space" button
    in the UI so it doesn't need to know the caller's id.
    """

    def _resolve_user_id(self) -> str:
        raw = self.match("user_id")
        return self.user.user_id if raw == "me" else raw

    async def patch(self) -> web.Response:
        """Owner-only: promote / demote a member (``role`` in body)."""
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        user_id = self._resolve_user_id()
        body = await self.body()
        role = str(body.get("role") or "").strip()
        if role not in ("admin", "member"):
            return web.json_response(
                {
                    "error": {
                        "code": "UNPROCESSABLE",
                        "detail": "role must be 'admin' or 'member'",
                    }
                },
                status=422,
            )
        await svc.set_role(
            space_id,
            actor_username=ctx.username,
            user_id=user_id,
            role=role,
        )
        return web.json_response({"user_id": user_id, "role": role})

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        user_id = self._resolve_user_id()
        await svc.remove_member(
            space_id,
            actor_username=ctx.username,
            user_id=user_id,
        )
        return web.json_response({"ok": True})


class SpaceBanView(BaseView):
    """POST /api/spaces/{id}/ban — ban a member."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        await svc.ban(
            space_id,
            actor_username=ctx.username,
            user_id=body["user_id"],
            reason=body.get("reason"),
        )
        return web.json_response({"ok": True})


class SpaceInviteTokenView(BaseView):
    """POST /api/spaces/{id}/invite-tokens — create an invite token."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        token = await svc.create_invite_token(
            space_id,
            actor_username=ctx.username,
            uses=body.get("uses", 1),
        )
        return web.json_response({"token": token}, status=201)


class SpaceRemoteInviteView(BaseView):
    """``POST /api/spaces/{id}/remote-invites`` — §D1b cross-household
    private-space invitation. Body: ``{invitee_instance_id,
    invitee_user_id}``. Admin/owner only; target household must be a
    CONFIRMED peer.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        invitee_instance_id = body.get("invitee_instance_id")
        invitee_user_id = body.get("invitee_user_id")
        if not invitee_instance_id or not invitee_user_id:
            return error_response(
                422,
                "UNPROCESSABLE",
                "invitee_instance_id and invitee_user_id are required",
            )
        token = await svc.invite_remote_user(
            space_id,
            actor_username=ctx.username,
            invitee_instance_id=str(invitee_instance_id),
            invitee_user_id=str(invitee_user_id),
        )
        return web.json_response({"token": token}, status=201)


class RemoteInviteCollectionView(BaseView):
    """``GET /api/remote_invites`` — inbound cross-household private-space
    invites pending for the caller (§D1b). Allows the UI to render an
    "accept / decline" banner.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        repo = self.svc(space_repo_key)
        rows = await repo.list_pending_remote_invites_for(ctx.user_id)
        return web.json_response(
            [
                {
                    "invite_token": r.get("invite_token"),
                    "space_id": r["space_id"],
                    "inviter_user_id": r.get("invited_by"),
                    "inviter_instance_id": r.get("remote_instance_id"),
                    "space_display_hint": r.get("space_display_hint"),
                    "expires_at": r.get("expires_at"),
                    "created_at": r.get("created_at"),
                }
                for r in rows
            ]
        )


class RemoteInviteDecisionView(BaseView):
    """``POST /api/remote_invites/{token}/accept`` or ``/decline``.

    Dispatched via the URL suffix — the matching route pattern carries
    the decision in ``decision``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        token = self.match("token")
        decision = self.match("decision")
        svc = self.svc(space_service_key)
        if decision == "accept":
            await svc.accept_remote_invite(token=token, user_id=ctx.user_id)
        elif decision == "decline":
            await svc.decline_remote_invite(token=token, user_id=ctx.user_id)
        else:
            return error_response(
                404,
                "NOT_FOUND",
                f"unknown decision {decision!r}",
            )
        return web.Response(status=204)


class SpaceJoinView(BaseView):
    """POST /api/spaces/join — accept an invite token."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        member = await svc.accept_invite_token(
            body["token"],
            user_id=ctx.user_id,
        )
        return web.json_response(
            {
                "space_id": member.space_id,
                "role": member.role,
            }
        )


class SpaceModerationQueueView(BaseView):
    """GET /api/spaces/{id}/moderation — list pending queue items (admin)."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        items = await svc.list_pending_moderation(
            space_id,
            actor_username=ctx.username,
        )
        return web.json_response([_moderation_item_dict(i) for i in items])


class SpaceModerationApproveView(BaseView):
    """POST /api/spaces/{id}/moderation/{item_id}/approve."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        item_id = self.match("item_id")
        post = await svc.approve_moderation_item(
            space_id,
            item_id,
            actor_username=ctx.username,
        )
        return web.json_response(
            {
                "item_id": item_id,
                "post_id": post.id,
                "status": "approved",
            }
        )


class SpaceModerationRejectView(BaseView):
    """POST /api/spaces/{id}/moderation/{item_id}/reject."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        item_id = self.match("item_id")
        body = await self.body()
        reason = body.get("reason") if isinstance(body, dict) else None
        await svc.reject_moderation_item(
            space_id,
            item_id,
            actor_username=ctx.username,
            reason=str(reason).strip() if reason else None,
        )
        return web.json_response({"item_id": item_id, "status": "rejected"})


class SpaceBanListView(BaseView):
    """GET /api/spaces/{id}/bans — list banned members (admin)."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_svc = self.svc(space_service_key)
        space_id = self.match("id")
        space = await space_svc._require_space(space_id)
        await space_svc._require_admin_or_owner(space, ctx.username)
        bans = await self.svc(space_repo_key).list_bans(space_id)
        return web.json_response(bans)


class SpaceUnbanView(BaseView):
    """DELETE /api/spaces/{id}/bans/{user_id} — unban (admin)."""

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        user_id = self.match("user_id")
        await svc.unban(
            space_id,
            actor_username=ctx.username,
            user_id=user_id,
        )
        return web.json_response({"ok": True})


def _moderation_item_dict(item) -> dict:
    return {
        "id": item.id,
        "space_id": item.space_id,
        "feature": item.feature,
        "action": item.action,
        "submitted_by": item.submitted_by,
        "payload": item.payload,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "status": item.status.value,
        "rejection_reason": item.rejection_reason,
    }


class SpaceSyncTriggerView(BaseView):
    """POST /api/spaces/{id}/sync — admin-triggered space-sync kickoff.

    Enqueues a :class:`FederationEventType.SPACE_SYNC_BEGIN` to every
    confirmed peer instance that is a member of this space. Returns
    ``202 Accepted`` with the list of peers targeted — the actual
    streaming happens on the reconnect queue.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        space_svc = self.svc(space_service_key)
        space_id = self.match("id")
        space = await space_svc._require_space(space_id)
        await space_svc._require_admin_or_owner(space, ctx.username)

        space_repo = self.svc(space_repo_key)
        federation_repo = self.svc(federation_repo_key)
        scheduler = self.svc(space_sync_scheduler_key)

        member_instances = await space_repo.list_member_instances(space_id)
        targets: list[str] = []
        for inst_id in member_instances:
            instance = await federation_repo.get_instance(inst_id)
            if instance is None or instance.status is not PairingStatus.CONFIRMED:
                continue
            await scheduler.enqueue_sync_for_space(
                space_id=space_id,
                peer_instance_id=inst_id,
            )
            targets.append(inst_id)

        return web.json_response(
            {
                "space_id": space_id,
                "targets": targets,
            },
            status=202,
        )


class SpaceFeedView(BaseView):
    """GET /api/spaces/{id}/feed — space post feed.

    Each post dict carries an optional ``bot`` sub-object when the post
    was authored by the bot-bridge (``author == SYSTEM_AUTHOR``). The
    frontend uses it to render the bot icon + name + attribution line
    instead of the generic "Home Assistant" system chrome.
    """

    async def get(self) -> web.Response:
        svc = self.svc(space_service_key)
        bot_repo = self.svc(space_bot_repo_key)
        user_repo = self.svc(user_repo_key)
        space_id = self.match("id")
        before = self.request.query.get("before")
        limit = min(max(int(self.request.query.get("limit", 20)), 1), 50)
        posts = await svc.list_feed(space_id, before=before, limit=limit)

        # Resolve bot personas for any system-author posts in one round-trip
        # per unique bot_id. The feed is capped at 50 so this is bounded.
        bot_ids = {
            p.bot_id
            for p in posts
            if p.author == SYSTEM_AUTHOR and p.bot_id is not None
        }
        bots_by_id = {}
        creators_by_user_id: dict[str, str] = {}
        for bid in bot_ids:
            b = await bot_repo.get(bid)
            if b is None:
                continue
            bots_by_id[bid] = b
            if b.created_by not in creators_by_user_id:
                creator = await user_repo.get_by_user_id(b.created_by)
                creators_by_user_id[b.created_by] = (
                    creator.display_name if creator else b.created_by
                )

        return web.json_response(
            [
                sanitise_for_api(
                    {
                        "id": p.id,
                        "author": p.author,
                        "type": p.type.value,
                        "content": p.content,
                        "media_url": p.media_url,
                        "comment_count": p.comment_count,
                        "pinned": p.pinned,
                        "created_at": p.created_at.isoformat()
                        if p.created_at
                        else None,
                        "bot": _bot_view(p.bot_id, bots_by_id, creators_by_user_id)
                        if p.author == SYSTEM_AUTHOR
                        else None,
                    }
                )
                for p in posts
            ]
        )


def _bot_view(
    bot_id: str | None,
    bots_by_id: dict,
    creators_by_user_id: dict[str, str],
) -> dict | None:
    """Render the ``bot`` sub-object for a feed post, or ``None`` fallback.

    None signals "this is a system-authored post but the bot has been
    deleted" — the frontend falls back to the generic HA avatar.
    """
    if bot_id is None:
        return None
    bot = bots_by_id.get(bot_id)
    if bot is None:
        return None
    return {
        "bot_id": bot.bot_id,
        "scope": bot.scope.value,
        "name": bot.name,
        "icon": bot.icon,
        "created_by_display_name": creators_by_user_id.get(
            bot.created_by, bot.created_by
        ),
    }


class SpacePresenceView(BaseView):
    """``GET /api/spaces/{id}/presence`` — §23.80 space-scoped presence.

    Only members of the space see this. When ``feature_location`` is
    disabled, returns an empty list so the frontend can hide the map
    without a special 403. When enabled, returns each member's GPS pin
    (subject to their per-member ``location_share_enabled`` opt-in).

    The response NEVER carries ``zone_name`` — HA-defined zone names
    are stripped at the household boundary. Per-space display zones
    live in ``space_zones`` (§23.8.7) and are matched to GPS
    client-side. See §25.10.3 WS-events table.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(
                401,
                "UNAUTHENTICATED",
                "Authentication required.",
            )
        space_id = self.match("id")
        repo = self.svc(space_repo_key)
        space = await repo.get(space_id)
        if space is None:
            return error_response(404, "NOT_FOUND", "Space not found.")
        member = await repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(
                403,
                "FORBIDDEN",
                "Not a member of this space.",
            )
        if not space.features.location:
            return web.json_response(
                {
                    "feature_enabled": False,
                    "entries": [],
                }
            )
        members = await repo.list_members(space_id)
        opted_in = {m.user_id for m in members if m.location_share_enabled}
        presence_svc = self.svc(presence_service_key)
        entries = await presence_svc.list_presence_for_members(opted_in)
        mode = space.features.location_mode

        if mode == "zone_only":
            # Match each opted-in member's GPS to a space zone server-side
            # so the response carries zone labels only — never raw
            # coordinates. Members outside every zone are dropped from
            # the response (matches the outbound's silent-skip rule).
            zone_repo = self.svc(space_zone_repo_key)
            zones = await zone_repo.list_for_space(space_id)
            response_entries = []
            for p in entries:
                if p.latitude is None or p.longitude is None:
                    continue
                matched = _match_zone(zones, p.latitude, p.longitude)
                if matched is None:
                    continue
                response_entries.append(
                    {
                        "user_id": p.user_id,
                        "username": p.username,
                        "display_name": p.display_name,
                        "state": p.state,
                        "zone_id": matched.id,
                        "zone_name": matched.name,
                        "picture_url": p.picture_url,
                    },
                )
            return web.json_response(
                {
                    "feature_enabled": True,
                    "location_mode": "zone_only",
                    "entries": response_entries,
                },
            )

        # gps mode (default)
        return web.json_response(
            {
                "feature_enabled": True,
                "location_mode": "gps",
                "entries": [
                    {
                        "user_id": p.user_id,
                        "username": p.username,
                        "display_name": p.display_name,
                        "state": p.state,
                        "latitude": p.latitude,
                        "longitude": p.longitude,
                        "gps_accuracy_m": p.gps_accuracy_m,
                        "picture_url": p.picture_url,
                    }
                    for p in entries
                ],
            }
        )


class SpacePostCollectionView(BaseView):
    """POST /api/spaces/{id}/posts — create a post in a space."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        post = await svc.create_post(
            space_id,
            author_user_id=ctx.user_id,
            type=body.get("type", "text"),
            content=body.get("content"),
            media_url=strip_signature_query(body.get("media_url")),
            location=_extract_location(body),
        )
        if post is None:
            return web.json_response({"queued": True}, status=202)
        response: dict = {
            "id": post.id,
            "type": post.type.value,
            "content": post.content,
        }
        if post.location is not None:
            response["location"] = {
                "lat": post.location.lat,
                "lon": post.location.lon,
                "label": post.location.label,
            }
        return web.json_response(response, status=201)


def _extract_location(body: dict) -> LocationData | None:
    """Pull a ``location`` block out of the request body.

    Mirrors :func:`socialhome.routes.feed._extract_location`. Returns
    ``None`` when no location was supplied; the service rejects a
    ``LOCATION`` post that arrives without coords.
    """
    raw = body.get("location")
    if not isinstance(raw, dict):
        return None
    lat = raw.get("lat")
    lon = raw.get("lon")
    if lat is None or lon is None:
        return None
    return LocationData(lat=float(lat), lon=float(lon), label=raw.get("label"))


class SpaceOwnershipView(BaseView):
    """``POST /api/spaces/{id}/ownership`` — transfer ownership (§23.48)."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        new_owner = str(body.get("to_user_id") or "").strip()
        if not new_owner:
            return web.json_response(
                {
                    "error": {
                        "code": "UNPROCESSABLE",
                        "detail": "to_user_id is required",
                    }
                },
                status=422,
            )
        await svc.transfer_ownership(
            self.match("id"),
            actor_username=ctx.username,
            to_user_id=new_owner,
        )
        return web.json_response(
            {"space_id": self.match("id"), "new_owner_user_id": new_owner},
        )


class SpaceJoinRequestCollectionView(BaseView):
    """``GET /api/spaces/{id}/join-requests`` + ``POST /api/spaces/{id}/join-requests``.

    * ``GET`` — admin/owner only: list pending requests.
    * ``POST`` — any authenticated user: request to join (if space
      allows request-to-join).
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        space = await svc._require_space(space_id)
        await svc._require_admin_or_owner(space, ctx.username)
        requests = await self.svc(space_repo_key).list_pending_join_requests(
            space_id,
        )
        return web.json_response([sanitise_for_api(r) for r in requests])

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        request_id = await svc.request_join(
            self.match("id"),
            user_id=ctx.user_id,
            message=body.get("message"),
        )
        return web.json_response({"request_id": request_id}, status=201)


class SpaceJoinRequestDetailView(BaseView):
    """``POST /api/spaces/{id}/join-requests/{request_id}/{action}``.

    ``action`` must be ``approve`` or ``deny``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        action = self.match("action")
        request_id = self.match("request_id")
        if action == "approve":
            member = await svc.approve_join_request(
                request_id,
                actor_username=ctx.username,
            )
            return web.json_response(
                {
                    "request_id": request_id,
                    "status": "approved",
                    "space_id": member.space_id,
                    "user_id": member.user_id,
                }
            )
        if action == "deny":
            await svc.deny_join_request(
                request_id,
                actor_username=ctx.username,
            )
            return web.json_response(
                {"request_id": request_id, "status": "denied"},
            )
        return web.json_response(
            {
                "error": {
                    "code": "UNPROCESSABLE",
                    "detail": "action must be 'approve' or 'deny'",
                }
            },
            status=422,
        )


class SpacePostReactionView(BaseView):
    """``POST /api/spaces/{id}/posts/{post_id}/reactions`` +
    ``DELETE /api/spaces/{id}/posts/{post_id}/reactions/{emoji}``.

    ``POST`` body: ``{emoji: "👍"}`` — adds a reaction.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        emoji = str(body.get("emoji") or "").strip()
        if not emoji:
            return web.json_response(
                {
                    "error": {
                        "code": "UNPROCESSABLE",
                        "detail": "emoji is required",
                    }
                },
                status=422,
            )
        await svc.add_reaction(
            self.match("post_id"),
            user_id=ctx.user_id,
            emoji=emoji,
        )
        return web.json_response(
            {"post_id": self.match("post_id"), "emoji": emoji},
            status=201,
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        await svc.remove_reaction(
            self.match("post_id"),
            user_id=ctx.user_id,
            emoji=self.match("emoji"),
        )
        return web.json_response({"ok": True})


class AdminSpaceCollectionView(BaseView):
    """``GET /api/admin/spaces`` — household-admin list of every space.

    Admin-only: shows active (non-dissolved) spaces across the household
    so operators can dissolve / transfer / inspect any of them from a
    central panel without joining each space.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or not ctx.is_admin:
            return web.json_response(
                {"error": {"code": "FORBIDDEN", "detail": "Admin only."}},
                status=403,
            )
        repo = self.svc(space_repo_key)
        spaces = await repo.list_all()
        out = []
        for s in spaces:
            members = await repo.list_members(s.id)
            out.append(
                sanitise_for_api(
                    {
                        "id": s.id,
                        "name": s.name,
                        "description": s.description,
                        "emoji": s.emoji,
                        "space_type": s.space_type.value,
                        "join_mode": s.join_mode.value,
                        "owner_username": s.owner_username,
                        "owner_instance_id": s.owner_instance_id,
                        "member_count": len(members),
                        "dissolved": bool(s.dissolved),
                    }
                )
            )
        return web.json_response(out)


def _serialise_comment(comment) -> dict:
    return {
        "id": comment.id,
        "post_id": comment.post_id,
        "author": comment.author,
        "content": comment.content,
        "parent_id": comment.parent_id,
        "deleted": bool(comment.deleted),
        "edited_at": comment.edited_at.isoformat() if comment.edited_at else None,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


class SpacePostCommentView(BaseView):
    """``POST /api/spaces/{id}/posts/{post_id}/comments`` — add a comment."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        content = str(body.get("content") or "").strip()
        if not content:
            return web.json_response(
                {
                    "error": {
                        "code": "UNPROCESSABLE",
                        "detail": "content is required",
                    }
                },
                status=422,
            )
        comment = await svc.add_comment(
            self.match("post_id"),
            author_user_id=ctx.user_id,
            content=content,
            parent_id=body.get("parent_id"),
        )
        return web.json_response(_serialise_comment(comment), status=201)


class SpacePostCommentDetailView(BaseView):
    """PATCH/DELETE /api/spaces/{id}/posts/{post_id}/comments/{cid}."""

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        new_content = body.get("content")
        if new_content is None:
            return web.json_response(
                {
                    "error": {
                        "code": "UNPROCESSABLE",
                        "detail": "content is required",
                    }
                },
                status=422,
            )
        comment = await svc.edit_comment(
            self.match("cid"),
            editor_user_id=ctx.user_id,
            new_content=new_content,
        )
        return web.json_response(_serialise_comment(comment))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        await svc.delete_comment(
            self.match("cid"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class SpaceSubscribeView(BaseView):
    """``POST /api/spaces/{id}/subscribe`` — subscribe to a public or
    global space as a read-only member (idempotent — double-subscribe
    is a no-op).

    ``DELETE /api/spaces/{id}/subscribe`` — unsubscribe. No-op for
    users who aren't subscribers (so this can't silently leave a real
    member's space). Both return ``{"subscribed": bool}``.

    Note on naming: "subscribe" is used here rather than "follow"
    because the product already uses "follow" for a different concept
    (the dashboard pin list, see ``corner_service``).
    """

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        svc = self.svc(space_service_key)
        await svc.subscribe_to_space(ctx.user_id, space_id)
        return web.json_response({"subscribed": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        svc = self.svc(space_service_key)
        await svc.unsubscribe_from_space(ctx.user_id, space_id)
        return web.json_response({"subscribed": False})


class MySubscriptionsView(BaseView):
    """``GET /api/me/subscriptions`` — the caller's read-only-member
    subscriptions.

    Returns ``{"subscriptions": [{space_id, subscribed_at}, ...]}``
    ordered newest first. The client hydrates metadata (name / cover)
    from its own public-space cache.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        return web.json_response(
            {"subscriptions": await svc.list_subscriptions(ctx.user_id)},
        )


# ─── Space sidebar links ────────────────────────────────────────────────


class SpaceLinkCollectionView(BaseView):
    """``GET /api/spaces/{id}/links`` — list configured links (members).

    ``POST /api/spaces/{id}/links`` — create a new link (admin/owner).
    Body: ``{label, url, position?}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        svc = self.svc(space_service_key)
        try:
            links = await svc.list_links(space_id, actor_user_id=ctx.user_id)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Space not found.")
        return web.json_response({"links": links})

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        body = await self.body()
        svc = self.svc(space_service_key)
        try:
            link = await svc.upsert_link(
                space_id=space_id,
                actor_username=ctx.username,
                link_id=None,
                label=str(body.get("label") or ""),
                url=str(body.get("url") or ""),
                position=int(body.get("position", 0) or 0),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(link, status=201)


class SpaceLinkDetailView(BaseView):
    """``PATCH /api/spaces/{id}/links/{link_id}`` — update an existing link.

    ``DELETE /api/spaces/{id}/links/{link_id}`` — remove the link.
    Both admin/owner-only.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        link_id = self.match("link_id")
        body = await self.body()
        repo = self.svc(space_repo_key)
        existing = await repo.get_link(link_id)
        if existing is None or existing["space_id"] != space_id:
            return error_response(404, "NOT_FOUND", "Link not found.")
        svc = self.svc(space_service_key)
        try:
            link = await svc.upsert_link(
                space_id=space_id,
                actor_username=ctx.username,
                link_id=link_id,
                label=str(body.get("label") or existing["label"]),
                url=str(body.get("url") or existing["url"]),
                position=int(
                    body.get("position", existing["position"]) or 0,
                ),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(link)

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        link_id = self.match("link_id")
        repo = self.svc(space_repo_key)
        existing = await repo.get_link(link_id)
        if existing is None or existing["space_id"] != space_id:
            return error_response(404, "NOT_FOUND", "Link not found.")
        svc = self.svc(space_service_key)
        await svc.delete_link(link_id=link_id, actor_username=ctx.username)
        return web.json_response({"ok": True}, status=200)


# ─── Per-space notification preferences ─────────────────────────────────


_NOTIF_LEVELS = frozenset({"all", "mentions", "muted"})


class SpaceNotifPrefsView(BaseView):
    """``GET /api/spaces/{id}/notif-prefs`` — caller's level for this space.

    ``PUT /api/spaces/{id}/notif-prefs`` — set level.
    Body: ``{level: 'all' | 'mentions' | 'muted'}``.
    Returns ``{level}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        space_repo = self.svc(space_repo_key)
        if await space_repo.get_member(space_id, ctx.user_id) is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        notif_repo = self.svc(notification_repo_key)
        level = await notif_repo.get_space_notif_level(
            user_id=ctx.user_id,
            space_id=space_id,
        )
        return web.json_response({"level": level})

    async def put(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        space_repo = self.svc(space_repo_key)
        if await space_repo.get_member(space_id, ctx.user_id) is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        level = str(body.get("level") or "").strip().lower()
        if level not in _NOTIF_LEVELS:
            return error_response(
                422,
                "UNPROCESSABLE",
                "level must be one of 'all','mentions','muted'.",
            )
        notif_repo = self.svc(notification_repo_key)
        await notif_repo.set_space_notif_level(
            user_id=ctx.user_id,
            space_id=space_id,
            level=level,
        )
        return web.json_response({"level": level})
