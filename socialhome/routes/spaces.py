"""Space routes — /api/spaces/* (section 23.48-23.73).

Thin handlers delegating to :class:`SpaceService`.
"""

from __future__ import annotations

from aiohttp import web

from aiohttp.multipart import BodyPartReader

import math

from ..app_keys import (
    alias_resolver_key,
    child_protection_service_key,
    event_bus_key,
    federation_repo_key,
    media_signer_key,
    media_transcode_repo_key,
    notification_repo_key,
    online_status_service_key,
    presence_service_key,
    profile_picture_repo_key,
    space_bot_repo_key,
    space_cover_repo_key,
    space_icon_repo_key,
    space_remote_location_repo_key,
    space_approval_service_key,
    space_remote_member_repo_key,
    space_repo_key,
    space_service_key,
    space_sync_scheduler_key,
    space_zone_repo_key,
    user_repo_key,
)
from ..domain.events import SpaceMemberLocationOptedIn
from ..domain.post import LocationData, PostType
from ..domain.space import (
    PUBLIC_SPACE_TIERS,
    SpaceFeatures,
    SpacePermissionError,
    SpaceZone,
)
from ..domain.space_proposal import ProposalAction
from ..domain.user import SYSTEM_AUTHOR
from ..domain.federation import PairingStatus
from ..domain.media_constraints import PROFILE_PICTURE_MAX_UPLOAD_BYTES
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..security import error_response, sanitise_for_api
from ..services.space_service import _UNSET_MEMBER_PROFILE, normalize_category
from .base import BaseView
from .media_status import READY, media_filename, video_poster_path

_PROFILE_PICTURE_MAX_UPLOAD_BYTES = PROFILE_PICTURE_MAX_UPLOAD_BYTES


def _features_from_body(
    raw: object,
    *,
    defaults: SpaceFeatures | None = None,
) -> SpaceFeatures | None:
    """Rehydrate :class:`SpaceFeatures` from a PATCH body.

    The wire shape mirrors :meth:`SpaceFeatures.to_wire_dict` —
    boolean flags + access-level enums + an ``allowed_post_types``
    list.  Missing keys fall back to *defaults*.
    Returns ``None`` when the caller didn't include a ``features``
    block, so the service layer treats it as "leave unchanged".

    Callers pass the space's CURRENT features as *defaults*, so a partial
    body means "change these keys, leave the rest". The SPA happens to PATCH
    the full dict, but the API accepts (and a script will send) a partial one
    — and with class defaults a partial ``{"bazaar": false}`` would reset
    ``allow_subscribers`` to OFF, silently withdrawing the space's public
    readability and evicting its followers.
    """
    if not isinstance(raw, dict):
        return None
    return SpaceFeatures.from_wire_dict(raw, defaults=defaults)


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
                        # The list-card is the SPA's first read of a
                        # space — surfacing the description here avoids
                        # the per-space ``GET /api/spaces/{id}`` round-
                        # trip the SpaceList page would otherwise need
                        # just to render the subtitle on each card.
                        "description": s.description,
                        "space_type": s.space_type.value,
                        "join_mode": s.join_mode.value,
                        # Read-only archive state — the SPA groups
                        # archived spaces out of the active sidebar list.
                        "archived": s.archived,
                        # Why archived: NULL = normal reversible admin
                        # archive; 'dissolved'/'removed' = remote-terminated
                        # (read-only, not unarchivable). The SPA picks the
                        # banner copy + hides Unarchive off this.
                        "archived_reason": s.archived_reason,
                        # §D1b — the creator's instance. When it
                        # differs from our own, the row is a remote
                        # stub: the SPA gates the Settings link + any
                        # admin gestures on this field so a member
                        # never tries to mutate state the host owns.
                        "owner_instance_id": s.owner_instance_id,
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
            category=body.get("category"),
        )
        # Discoverable tiers (public / global) may carry an age gate set at
        # creation time; the gate lives in ChildProtectionService, not on the
        # space row. Private spaces gate on invite, so a min_age is ignored.
        min_age = body.get("min_age")
        if min_age is not None and space.space_type in PUBLIC_SPACE_TIERS:
            await self.svc(child_protection_service_key).update_space_age_gate(
                space.id,
                min_age=int(min_age),
                actor_user_id=ctx.user_id,
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
        icon_url = (
            f"/api/spaces/{space.id}/icon?v={space.icon_hash}"
            if space.icon_hash
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
                "category": normalize_category(space.category),
                "features": space.features.to_wire_dict(),
                "retention_days": space.retention_days,
                "retention_exempt_types": list(space.retention_exempt_types),
                "about_markdown": space.about_markdown,
                "cover_hash": space.cover_hash,
                "cover_url": cover_url,
                "icon_hash": space.icon_hash,
                "icon_url": icon_url,
                "bot_enabled": space.bot_enabled,
                # Read-only archive state — the SPA disables write
                # affordances and shows a read-only banner when true.
                "archived": space.archived,
                # Why archived: NULL = normal reversible admin archive;
                # 'dissolved'/'removed' = remote-terminated (read-only, not
                # unarchivable). Drives the reason-aware banner + hides the
                # Unarchive button in Settings.
                "archived_reason": space.archived_reason,
                # §D1b — see ``SpaceCollectionView.get`` for the
                # complement; the SPA reads this on the SpaceFeedPage
                # to suppress local-only admin gestures on a remote
                # stub.
                "owner_instance_id": space.owner_instance_id,
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
        # ``features`` arrives in the same wire shape the GET response
        # ships (``to_wire_dict``).  The previous handler silently
        # dropped this key, so the SpaceSettings UI's location toggle
        # appeared to save (200 OK) but never actually flipped the
        # feature.  Rehydrate into a ``SpaceFeatures`` instance so the
        # service layer can persist it.
        # Merge onto the space's CURRENT features so a partial body edits only
        # the keys it names (see ``_features_from_body``). ``_require_space``
        # raises the mapped ``SpaceNotFoundError`` for an unknown id, exactly
        # as ``update_config`` would a moment later.
        current = await svc._require_space(space_id)
        features = _features_from_body(
            body.get("features"),
            defaults=current.features,
        )
        # ``space_type`` (publication tier) is NOT applied here — it is gated
        # behind multi-admin approval (v_16). The SPA proposes a tier change
        # via ``POST /api/spaces/{id}/proposals``; the generic config PATCH
        # ignores any ``space_type`` it is sent.
        updated = await svc.update_config(
            space_id,
            actor_username=ctx.username,
            name=body.get("name"),
            description=body.get("description"),
            emoji=body.get("emoji"),
            features=features,
            join_mode=body.get("join_mode"),
            retention_days=body.get("retention_days"),
            retention_exempt_types=body.get("retention_exempt_types"),
            about_markdown=(
                body["about_markdown"]
                if "about_markdown" in body
                else _UNSET_MEMBER_PROFILE
            ),
            bot_enabled=body.get("bot_enabled"),
            category=body.get("category"),
        )
        return web.json_response(
            {
                "id": updated.id,
                "name": updated.name,
                "about_markdown": updated.about_markdown,
            }
        )

    async def delete(self) -> web.Response:
        """Dissolving a space is a critical action — gated behind
        multi-admin approval (v_16). This opens a ``dissolve`` proposal
        (and executes immediately for a solo-admin space). Returns the
        proposal view so the SPA can show the pending state."""
        ctx = self.user
        approvals = self.svc(space_approval_service_key)
        space_id = self.match("id")
        view = await approvals.propose(
            space_id,
            actor_username=ctx.username,
            action=ProposalAction.DISSOLVE,
        )
        return web.json_response({"ok": True, "proposal": view})


class SpaceProposalsView(BaseView):
    """``GET`` / ``POST /api/spaces/{id}/proposals`` — multi-admin approval
    (quorum) for critical space actions (v_16).

    * ``GET`` lists open proposals (with tally) for the space.
    * ``POST {action, params}`` opens a proposal (``dissolve`` or
      ``set_public_tier``). Any admin may propose; it executes only once a
      majority of the space's admins approve (the proposer auto-counts, so
      a solo-admin space executes immediately).
    """

    async def get(self) -> web.Response:
        approvals = self.svc(space_approval_service_key)
        proposals = await approvals.list_for_space(self.match("id"))
        return web.json_response({"proposals": proposals})

    async def post(self) -> web.Response:
        ctx = self.user
        approvals = self.svc(space_approval_service_key)
        body = await self.body()
        try:
            action = ProposalAction(str(body.get("action") or ""))
        except ValueError:
            return error_response(400, "BAD_ACTION", "unknown proposal action")
        params: dict = {}
        if action is ProposalAction.SET_PUBLIC_TIER:
            params["space_type"] = str(body.get("space_type") or "")
        view = await approvals.propose(
            self.match("id"),
            actor_username=ctx.username,
            action=action,
            params=params,
        )
        return web.json_response({"proposal": view})


class SpaceProposalVoteView(BaseView):
    """``POST /api/spaces/{id}/proposals/{pid}/vote {approve}`` — an admin
    approves or rejects an open proposal. A reject cancels it; reaching a
    majority of approvals executes it (v_16)."""

    async def post(self) -> web.Response:
        ctx = self.user
        approvals = self.svc(space_approval_service_key)
        body = await self.body()
        view = await approvals.vote(
            self.match("id"),
            self.match("pid"),
            actor_username=ctx.username,
            approve=bool(body.get("approve", True)),
        )
        return web.json_response({"proposal": view})


class SpaceArchiveView(BaseView):
    """``POST`` / ``DELETE /api/spaces/{id}/archive`` — archive (make
    read-only + drop from active lists) or restore a space. Owner / admin
    only; reversible. Distinct from ``DELETE /api/spaces/{id}`` (dissolve),
    which is a permanent hard delete.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        await svc.archive_space(self.match("id"), actor_username=ctx.username)
        return web.json_response({"ok": True, "archived": True})

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        await svc.unarchive_space(self.match("id"), actor_username=ctx.username)
        return web.json_response({"ok": True, "archived": False})


class SpaceCompatView(BaseView):
    """``GET /api/spaces/{id}/compat`` — per-space protocol-version
    compatibility of member households (#319 ¶5). Owner / admin only.

    Surfaces which member households lag behind this build's advertised
    ``proto_version`` and which shared-space features that breaks, so a
    space-admin banner can warn "these features won't work until member
    household X upgrades." Households mid-first-handshake (no advertised
    capabilities yet) are excluded — they aren't genuinely behind.
    """

    async def get(self) -> web.Response:
        svc = self.svc(space_service_key)
        c = await svc.space_version_compat(
            self.match("id"), actor_username=self.user.username
        )
        return web.json_response(
            {
                "ours": c.ours,
                "min_member_proto_version": c.min_member_proto_version,
                "lagging_features": list(c.lagging_features),
                "behind_members": [
                    {
                        "instance_id": b.instance_id,
                        "display_name": b.display_name,
                        "proto_version": b.proto_version,
                        "lacking_features": list(b.lacking_features),
                    }
                    for b in c.behind_members
                ],
            }
        )


def _member_to_dict(
    m,
    space_id: str,
    *,
    display_name: str | None = None,
    personal_alias: str | None = None,
    is_online: bool = False,
    is_idle: bool = False,
    last_seen_at: str | None = None,
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
        # Session-presence indicators (orthogonal to physical presence).
        "is_online": is_online,
        "is_idle": is_idle,
        "last_seen_at": last_seen_at,
        # §23.8.8 — per-space location-share opt-in. The map tab's
        # "sharing chip" reads this off the member row to decide whether
        # to render "You are sharing your location" vs. "Your location
        # is private here". Without it, the SPA always renders the OFF
        # state on a cold load even though the user IS sharing — the
        # backend keeps the opt-in across sessions.
        "location_share_enabled": bool(m.location_share_enabled),
    }


def _remote_member_to_dict(
    rm,
    *,
    display_name_override: str | None = None,
    personal_alias: str | None = None,
    picture_hash: str | None = None,
    is_online: bool = False,
    is_idle: bool = False,
    last_seen_at: str | None = None,
) -> dict:
    """Serialize a :class:`SpaceRemoteMember` (§D1b) into the same row
    shape ``SpaceMembersView`` returns for local members, so the SPA's
    ``SpaceMemberList`` can render both in one list without branching.

    ``display_name_override`` — when provided, replaces
    ``rm.display_name`` on the rendered row. The route passes the
    freshest value it found in ``users`` / ``remote_users`` (kept up
    to date by ``USER_UPDATED`` fan-out), so a rename on the remote
    household propagates here without re-inviting. ``rm.display_name``
    stays as the §D1b accept-time snapshot in the row and is used as
    the fallback if the user-table lookup misses (the user has been
    deprovisioned, the row was seeded before USERS_SYNC arrived,
    etc.).

    ``personal_alias`` — viewer-private nickname from
    ``personal_aliases`` (set via :class:`AliasItemView`). The alias
    service stores the row keyed on ``(viewer, target)`` regardless
    of whether the target is local or remote, so this surfaces
    naturally here — the only reason it used to be hardcoded ``None``
    is that the caller wasn't passing it through.

    ``picture_hash`` — when set, builds the same
    ``api/users/{user_id}/picture?v=<hash>`` URL the local row uses.
    The bytes are federated to this household via ``USERS_SYNC`` and
    stored in the shared ``user_profile_pictures`` table keyed by
    ``user_id``, so :class:`UserPictureView` serves them without
    caring whether the target is local or remote.

    Differences from a local row:

    - ``role`` reads ``rm.role`` from ``space_remote_members.role``,
      which can be ``'admin'`` after a cross-household promotion
      (PR #434). Earlier this was hardcoded to ``"member"``, which
      hid promotions in the rendered roster.
    - ``instance_id`` is set so the UI can render a "from {household}"
      affordance and route role / kick PATCH+DELETE to the
      ``/remote-members/{instance}/{user}`` endpoint.
    - ``space_display_name`` is still ``None`` — ``space_remote_members``
      has no per-space override column; admins who want a different
      label per space need a schema bump first (deferred).
    - presence fields default to ``false`` / ``null`` — we have no
      session presence signal from the peer's instance.
    """
    return {
        "user_id": rm.user_id,
        "role": rm.role or "member",
        "joined_at": rm.joined_at or "",
        "display_name": display_name_override or rm.display_name,
        "space_display_name": None,
        "personal_alias": personal_alias,
        "picture_hash": picture_hash,
        "picture_url": (
            f"api/users/{rm.user_id}/picture?v={picture_hash}" if picture_hash else None
        ),
        "is_online": is_online,
        "is_idle": is_idle,
        "last_seen_at": last_seen_at,
        "location_share_enabled": False,
        # New field — the SPA branches on this to render the
        # "from another household" badge + suppress local-only
        # admin gestures.
        "instance_id": rm.instance_id,
    }


def _remote_member_to_dict_signed(
    request: web.Request,
    rm,
    *,
    display_name_override: str | None = None,
    personal_alias: str | None = None,
    picture_hash: str | None = None,
    is_online: bool = False,
    is_idle: bool = False,
    last_seen_at: str | None = None,
) -> dict:
    """:func:`_remote_member_to_dict` + sign the picture URL when set."""
    payload = _remote_member_to_dict(
        rm,
        display_name_override=display_name_override,
        personal_alias=personal_alias,
        picture_hash=picture_hash,
        is_online=is_online,
        is_idle=is_idle,
        last_seen_at=last_seen_at,
    )
    signer = request.app.get(media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


def _member_to_dict_signed(
    request: web.Request,
    m,
    space_id: str,
    *,
    display_name: str | None = None,
    personal_alias: str | None = None,
    is_online: bool = False,
    is_idle: bool = False,
    last_seen_at: str | None = None,
) -> dict:
    """:func:`_member_to_dict` + sign ``picture_url`` for the SPA."""
    payload = _member_to_dict(
        m,
        space_id,
        display_name=display_name,
        personal_alias=personal_alias,
        is_online=is_online,
        is_idle=is_idle,
        last_seen_at=last_seen_at,
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
        # §D1b — federated peers who accepted a cross-household invite
        # are seated in ``space_remote_members``, NOT ``space_members``.
        # Merge them in here or the admin who invited them will look at
        # their member list and not see the person they just invited
        # (the friend joined "into thin air" from Pascal's perspective).
        remote_repo = self.request.app.get(space_remote_member_repo_key)
        remote_members = (
            await remote_repo.list_for_space(space_id) if remote_repo else []
        )
        # Resolve global display_name + viewer's personal alias for each
        # member in two bulk calls (no N+1).
        user_repo = self.svc(user_repo_key)
        member_ids = {m.user_id for m in members}
        local_users = await user_repo.list_by_ids(member_ids) if member_ids else []
        display_names: dict[str, str] = {u.user_id: u.display_name for u in local_users}
        last_seen_persisted: dict[str, str | None] = {
            u.user_id: u.last_seen_at for u in local_users
        }
        # Remote members fall back to one-by-one — list_by_ids only
        # reads ``users``, not ``remote_users``.
        for m in members:
            if m.user_id in display_names:
                continue
            remote = await user_repo.get_remote(m.user_id)
            if remote is not None:
                display_names[m.user_id] = remote.display_name
        viewer = self.user
        viewer_id = viewer.user_id if viewer is not None else ""
        # Resolve viewer-private aliases for BOTH local and remote
        # members in one bulk call. ``personal_aliases`` keys on
        # ``(viewer, target_user_id)`` regardless of whether the
        # target is local or remote, so the same lookup covers both.
        # Before this, the remote roster never surfaced the alias
        # the user already set via the friends list (the wire field
        # was hardcoded to ``None``).
        aliases = await self.svc(alias_resolver_key).resolve_users(
            viewer_id,
            [m.user_id for m in members] + [rm.user_id for rm in remote_members],
        )
        # Session-presence snapshot — single call, applied per-row below.
        online_svc = self.request.app.get(online_status_service_key)
        online_ids = online_svc.online_user_ids() if online_svc else set()
        idle_ids = online_svc.idle_user_ids() if online_svc else set()
        local_rows = [
            _member_to_dict_signed(
                self.request,
                m,
                space_id,
                display_name=display_names.get(m.user_id),
                personal_alias=aliases.get(m.user_id),
                is_online=m.user_id in online_ids,
                is_idle=m.user_id in idle_ids,
                last_seen_at=(
                    online_svc.last_seen(m.user_id).isoformat()
                    if online_svc
                    and m.user_id in online_ids
                    and online_svc.last_seen(m.user_id) is not None
                    else last_seen_persisted.get(m.user_id)
                ),
            )
            for m in members
        ]
        # Per-row freshness for the remote roster:
        # - display_name comes from ``remote_users`` (kept fresh by
        #   USER_UPDATED) with the §D1b accept-time snapshot as
        #   fallback.
        # - is_online / is_idle / last_seen come from the SAME
        #   ``online_status_service`` set the local-member loop uses:
        #   :meth:`OnlineStatusService.online_user_ids` already
        #   merges local sessions AND the ``_remote`` cache fed by
        #   inbound USER_ONLINE / USER_OFFLINE federation events, so
        #   the lookup just works without an extra repo call.
        remote_rows = []
        for rm in remote_members:
            remote_user = await user_repo.get_remote(rm.user_id)
            override = (
                remote_user.display_name
                if remote_user is not None and remote_user.display_name
                else None
            )
            # ``remote_users.picture_hash`` is kept fresh by USERS_SYNC
            # inbound; the bytes land in the shared
            # ``user_profile_pictures`` table keyed by user_id, so the
            # local picture endpoint serves them without any extra
            # routing.
            picture_hash = remote_user.picture_hash if remote_user is not None else None
            is_online = rm.user_id in online_ids
            is_idle = rm.user_id in idle_ids
            last_seen_at: str | None = None
            if online_svc is not None:
                ls = online_svc.last_seen(rm.user_id)
                last_seen_at = ls.isoformat() if ls is not None else None
            remote_rows.append(
                _remote_member_to_dict_signed(
                    self.request,
                    rm,
                    display_name_override=override,
                    personal_alias=aliases.get(rm.user_id),
                    picture_hash=picture_hash,
                    is_online=is_online,
                    is_idle=is_idle,
                    last_seen_at=last_seen_at,
                ),
            )
        return web.json_response(local_rows + remote_rows)

    async def post(self) -> web.Response:
        """Issue a pending invitation for a same-household user.

        Used to seat directly via :meth:`SpaceService.add_member`,
        which surprised invitees ("I'm suddenly in this space — no
        idea who put me here"). Now mirrors the cross-household
        flow: a row in ``space_invitations`` is created, the user
        sees it on ``GET /api/local_invites``, and they accept or
        decline via ``POST /api/local_invites/{id}/{decision}``.
        Returns 202 + ``{invitation_id}`` instead of the old 201 +
        ``{user_id, role}`` so the SPA can branch on the response.
        """
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        invitation_id = await svc.invite_local_user(
            space_id,
            actor_username=ctx.username,
            user_id=body["user_id"],
        )
        return web.json_response(
            {
                "invitation_id": invitation_id,
                "invited_user_id": body["user_id"],
                "status": "pending",
            },
            status=202,
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
        # On opt-in, fire a bus event so SpaceLocationOutbound can
        # refire the member's current presence for this one space —
        # the user sees their pin appear immediately instead of
        # waiting for the next HA push (could be minutes).
        if body["enabled"]:
            bus = self.svc(event_bus_key)
            await bus.publish(
                SpaceMemberLocationOptedIn(
                    space_id=space_id,
                    user_id=ctx.user_id,
                ),
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


class SpaceIconView(BaseView):
    """Space icon (avatar) image — distinct from the cover banner (§23).

    * ``GET    /api/spaces/{id}/icon`` — stream the WebP bytes.
    * ``POST   /api/spaces/{id}/icon`` — multipart upload; owner/admin only.
    * ``DELETE /api/spaces/{id}/icon`` — remove the icon (falls back to emoji).
    """

    async def get(self) -> web.Response:
        space_id = self.match("id")
        repo = self.svc(space_icon_repo_key)
        got = await repo.get(space_id)
        if got is None:
            return error_response(404, "NOT_FOUND", "This space has no icon set.")
        bytes_webp, _hash = got
        return web.Response(
            body=bytes_webp,
            content_type="image/webp",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
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
            updated = await svc.set_icon(
                space_id,
                actor_username=ctx.username,
                raw_bytes=raw,
            )
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        payload = {
            "icon_hash": updated.icon_hash,
            "icon_url": f"/api/spaces/{space_id}/icon?v={updated.icon_hash}",
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
            await svc.clear_icon(space_id, actor_username=ctx.username)
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
        if token == "":
            # Forwarded to the host for owner approval (delegation OFF).
            return web.json_response({"status": "pending_owner_approval"}, status=202)
        return web.json_response({"token": token}, status=201)


class SpaceRemoteMemberRoleView(BaseView):
    """``PATCH`` / ``DELETE /api/spaces/{id}/remote-members/{instance_id}/{user_id}``
    — set a remote member's role (#114) or kick them (#114 phase 2).

    ``PATCH`` body ``{"role": "admin"|"member"}`` — owner-only;
    the change federates to every member household via
    ``SPACE_MEMBER_ROLE_CHANGED``.

    ``DELETE`` — admin/owner only; routes through
    :meth:`SpaceService.remove_remote_member` which broadcasts
    ``SPACE_REMOTE_MEMBER_REMOVED`` to the kicked household and
    rotates the space epoch.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        instance_id = self.match("instance_id")
        user_id = self.match("user_id")
        body = await self.body()
        role = str(body.get("role") or "").strip()
        if role not in ("admin", "member"):
            return error_response(
                422,
                "UNPROCESSABLE",
                "role must be 'admin' or 'member'",
            )
        await svc.set_remote_member_role(
            space_id,
            actor_username=ctx.username,
            instance_id=instance_id,
            user_id=user_id,
            role=role,
        )
        return web.json_response(
            {"instance_id": instance_id, "user_id": user_id, "role": role}
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        instance_id = self.match("instance_id")
        user_id = self.match("user_id")
        await svc.remove_remote_member(
            space_id,
            actor_username=ctx.username,
            instance_id=instance_id,
            user_id=user_id,
        )
        return web.json_response({"ok": True})


class LocalInviteCollectionView(BaseView):
    """``GET /api/local_invites`` — same-household invitations
    pending for the caller. Symmetric to
    :class:`RemoteInviteCollectionView` (which covers the §D1b
    cross-household case); both list endpoints feed
    ``RemoteInviteInboxBanner`` so a member sees pending invites
    regardless of who issued them.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        repo = self.svc(space_repo_key)
        rows = await repo.list_pending_local_invites_for(ctx.user_id)
        return web.json_response(
            [
                {
                    "invitation_id": r["id"],
                    "space_id": r["space_id"],
                    "invited_by": r.get("invited_by"),
                    "expires_at": r.get("expires_at"),
                    "created_at": r.get("created_at"),
                }
                for r in rows
            ]
        )


class LocalInviteDecisionView(BaseView):
    """``POST /api/local_invites/{id}/accept`` or ``/decline``."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return error_response(401, "UNAUTHENTICATED", "Login required.")
        invitation_id = self.match("id")
        decision = self.match("decision")
        svc = self.svc(space_service_key)
        if decision == "accept":
            try:
                member = await svc.accept_local_invite(
                    invitation_id,
                    user_id=ctx.user_id,
                )
            except KeyError:
                return error_response(404, "NOT_FOUND", "Invitation not found.")
            return web.json_response(
                {"user_id": member.user_id, "role": member.role},
                status=200,
            )
        if decision == "decline":
            try:
                await svc.decline_local_invite(
                    invitation_id,
                    user_id=ctx.user_id,
                )
            except KeyError:
                return error_response(404, "NOT_FOUND", "Invitation not found.")
            return web.Response(status=204)
        return error_response(
            404,
            "NOT_FOUND",
            f"unknown decision {decision!r}",
        )


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
    """POST /api/spaces/join — accept an invite token.

    Accepts an optional ``issuer_instance_id`` body field. When present
    and not equal to our own instance, the service runs a §D2
    cross-instance handshake with the issuer; the issuer validates the
    token, seats us as a remote member, and ACKs back. Errors map to:

    * 422 — issuer not paired / token denied (``SpacePermissionError``)
    * 504 — issuer didn't respond within the timeout (``TimeoutError``)
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        body = await self.body()
        issuer_raw = body.get("issuer_instance_id") if isinstance(body, dict) else None
        issuer_instance_id = str(issuer_raw).strip() if issuer_raw else None
        try:
            result = await svc.redeem_invite_token(
                body["token"],
                user_id=ctx.user_id,
                issuer_instance_id=issuer_instance_id or None,
            )
        except TimeoutError as exc:
            return error_response(504, "ISSUER_TIMEOUT", str(exc))
        # ``SpacePermissionError`` from the cross-instance path means
        # the issuer wouldn't / couldn't honour the token — map to 422
        # so the SPA can render the issuer's reason verbatim. The local
        # path raises the same exception on a ban (banned=True), which
        # ``BaseView._iter`` already maps to 403; only catch and remap
        # here when the redeem actually went out over the wire.
        except SpacePermissionError as exc:
            if issuer_instance_id and not exc.banned:
                return error_response(422, "REDEEM_DENIED", str(exc))
            raise
        return web.json_response(result)


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

        # Match the household-feed shape — every type-specific field
        # the SPA needs to render rich posts (image_urls, location,
        # file_meta, reactions, linked_event_id, linked_highlight_id,
        # edited_at) needs to ride along.  The previous shape only
        # carried ``content`` + ``media_url`` so location / image /
        # file / event posts in space feeds rendered as bare text.
        signer = self.request.app.get(media_signer_key)
        # Video posts transcode in the background — surface a live
        # ``media_status`` so the SPA renders a "Processing…" placeholder
        # until the ``.webm`` exists. One batched repo read per request.
        video_fns = [
            fn
            for p in posts
            if p.type is PostType.VIDEO
            and (fn := media_filename(p.media_url)) is not None
        ]
        statuses = await self.svc(media_transcode_repo_key).status_for(video_fns)
        out: list[dict] = []
        for p in posts:
            payload = {
                "id": p.id,
                "author": p.author,
                "type": p.type.value,
                "content": p.content,
                "media_url": p.media_url,
                "image_urls": list(p.image_urls or ()),
                "file_meta": (
                    {
                        "url": p.file_meta.url,
                        "mime_type": p.file_meta.mime_type,
                        "original_name": p.file_meta.original_name,
                        "size_bytes": p.file_meta.size_bytes,
                    }
                    if p.file_meta is not None
                    else None
                ),
                "location": (
                    {
                        "lat": p.location.lat,
                        "lon": p.location.lon,
                        "label": p.location.label,
                    }
                    if p.location is not None
                    else None
                ),
                "reactions": {
                    emoji: sorted(uids) for emoji, uids in (p.reactions or {}).items()
                },
                "comment_count": p.comment_count,
                "pinned": p.pinned,
                "edited_at": p.edited_at.isoformat() if p.edited_at else None,
                "linked_event_id": p.linked_event_id,
                "linked_highlight_id": p.linked_highlight_id,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "bot": _bot_view(p.bot_id, bots_by_id, creators_by_user_id)
                if p.author == SYSTEM_AUTHOR
                else None,
            }
            if p.type is PostType.VIDEO:
                fn = media_filename(p.media_url)
                payload["media_status"] = statuses.get(fn, READY) if fn else READY
                # Signed poster (the ``.webp`` sibling of the ``.webm``
                # ``media_url``) — set the *unsigned* path so the
                # ``sign_media_urls_in`` pass below signs it alongside
                # ``media_url`` (``media_thumbnail_url`` is signable).
                poster = video_poster_path(p.media_url)
                if poster is not None:
                    payload["media_thumbnail_url"] = poster
            if signer is not None:
                sign_media_urls_in(payload, signer)
            out.append(sanitise_for_api(payload))
        return web.json_response(out)


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


async def _remote_member_pin_entries(
    request: web.Request,
    space_id: str,
    *,
    mode_filter: str = "gps",
) -> list[dict]:
    """Return location entries for remote members of a space.

    Reads the ``space_remote_member_locations`` table populated by
    :class:`PrivateSpaceInviteHandler._on_space_location_updated` and
    enriches each row with the member's display name from
    ``space_remote_members`` (with a fallback to ``remote_users`` if a
    rename arrived since the §D1b accept). ``mode_filter`` picks which
    rows to surface — the host's space privacy tier (gps / zone_only)
    is the authority; rows whose stored mode disagrees are dropped so
    the response matches the host's chosen tier.
    """
    repo = request.app.get(space_remote_location_repo_key)
    member_repo = request.app.get(space_remote_member_repo_key)
    if repo is None or member_repo is None:
        return []
    signer = request.app.get(media_signer_key)
    locs = await repo.list_for_space(space_id)
    if not locs:
        return []
    members = {
        (m.instance_id, m.user_id): m
        for m in await member_repo.list_for_space(space_id)
    }
    user_repo = request.app[user_repo_key]
    out: list[dict] = []
    for loc in locs:
        if loc.mode != mode_filter:
            continue
        rm = members.get((loc.instance_id, loc.user_id))
        if rm is None:
            # Location row outlived the member row — skip it. A
            # SPACE_REMOTE_MEMBER_REMOVED should have cascaded the
            # delete; this is defence-in-depth.
            continue
        # Display name: prefer the fresh ``users``/``remote_users`` row
        # over the §D1b accept-time snapshot. Same rule the members
        # endpoint uses.
        display_name = rm.display_name
        remote_user = await user_repo.get_remote(loc.user_id)
        if remote_user is not None and remote_user.display_name:
            display_name = remote_user.display_name
        # Picture URL: ``USERS_SYNC`` lands the WebP bytes in the
        # shared ``user_profile_pictures`` table keyed by user_id;
        # ``UserPictureView`` serves them whether the target is
        # local or remote. Mirror what ``_remote_member_to_dict``
        # does so the map pin shows the member's avatar instead
        # of just initials. Was previously hardcoded ``None``
        # (Pascal's report: "the space/map does not render image
        # profile on the map, only for myself - not for someone
        # on another household").
        picture_hash = remote_user.picture_hash if remote_user is not None else None
        picture_url = (
            f"api/users/{loc.user_id}/picture?v={picture_hash}"
            if picture_hash
            else None
        )
        entry: dict = {
            "user_id": loc.user_id,
            "username": None,
            "display_name": display_name,
            "state": "home",
            "picture_url": picture_url,
            "instance_id": loc.instance_id,
        }
        if mode_filter == "gps":
            entry["latitude"] = loc.latitude
            entry["longitude"] = loc.longitude
            entry["gps_accuracy_m"] = loc.accuracy_m
        else:  # zone_only
            entry["zone_id"] = loc.zone_id
            entry["zone_name"] = loc.zone_name
        if signer is not None and entry.get("picture_url"):
            sign_media_urls_in(entry, signer)
        out.append(entry)
    return out


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
                    "total_members": 0,
                    "sharing_count": 0,
                }
            )
        members = await repo.list_members(space_id)
        opted_in = {m.user_id for m in members if m.location_share_enabled}
        presence_svc = self.svc(presence_service_key)
        entries = await presence_svc.list_presence_for_members(opted_in)
        mode = space.features.location_mode
        # SPA header reads "X sharing / Y members" — Y is the full
        # space roster including remote members from other households
        # (federated via #426 member-list mirror + chunked sync), not
        # just the locals or the active-GPS subset.
        remote_member_repo = self.request.app.get(space_remote_member_repo_key)
        remote_member_count = 0
        if remote_member_repo is not None:
            remote_member_count = len(
                await remote_member_repo.list_for_space(space_id),
            )
        total_members = len(members) + remote_member_count
        # X is the number of members who have OPTED IN to sharing. For
        # locals that's ``location_share_enabled=1`` on their row. For
        # remotes we count anyone with a current pin row (the existence
        # of the row is the cross-household proxy for "opted in to share
        # with this space" — the row only lands when their outbound
        # actually fired with sharing on).
        remote_loc_repo = self.request.app.get(space_remote_location_repo_key)
        remote_sharing = 0
        if remote_loc_repo is not None:
            remote_locs = await remote_loc_repo.list_for_space(space_id)
            remote_sharing = sum(1 for loc in remote_locs if loc.mode == mode)
        sharing_count = len(opted_in) + remote_sharing

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
            # Remote member pins are added as ``zone_only`` entries
            # only — the sender already applied the zone-only filter
            # before federating, so we trust the ``mode`` field on
            # the stored row.
            response_entries.extend(
                await _remote_member_pin_entries(
                    self.request,
                    space_id,
                    mode_filter="zone_only",
                )
            )
            return web.json_response(
                {
                    "feature_enabled": True,
                    "location_mode": "zone_only",
                    "entries": response_entries,
                    "total_members": total_members,
                    "sharing_count": sharing_count,
                },
            )

        # gps mode (default)
        local_entries = [
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
        ]
        remote_entries = await _remote_member_pin_entries(self.request, space_id)
        return web.json_response(
            {
                "feature_enabled": True,
                "location_mode": "gps",
                "entries": local_entries + remote_entries,
                "total_members": total_members,
                "sharing_count": sharing_count,
            }
        )


class SpacePostItemView(BaseView):
    """PATCH/DELETE /api/spaces/{id}/posts/{post_id} — edit or delete a
    single space post.

    The household-feed twin lives at ``/api/feed/posts/{id}`` and
    routes through ``feed_service``; this view delegates to
    ``space_service`` so the soft-delete + ``SpacePostDeleted``
    federation broadcast fire (the household feed_service can't reach
    the ``space_posts`` table).

    Without this view, the SPA's ``api.delete('/api/spaces/.../posts/...')``
    hit aiohttp's routing-layer 404 (no handler invoked, no server log),
    and ``handleDelete``'s async-fn rejection was silently swallowed.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        post_id = self.match("post_id")
        body = await self.body()
        new_content = body.get("content")
        if new_content is None:
            return error_response(
                422,
                "VALIDATION_ERROR",
                "content is required.",
            )
        try:
            post = await svc.edit_post(
                post_id,
                editor_user_id=ctx.user_id,
                new_content=new_content,
            )
        except KeyError:
            return error_response(404, "NOT_FOUND", "Post not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.json_response(
            {
                "id": post.id,
                "content": post.content,
                "edited_at": (post.edited_at.isoformat() if post.edited_at else None),
            },
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        post_id = self.match("post_id")
        try:
            await svc.delete_post(post_id, actor_user_id=ctx.user_id)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Post not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        return web.Response(status=204)


class SpacePostCollectionView(BaseView):
    """POST /api/spaces/{id}/posts — create a post in a space."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        space_id = self.match("id")
        body = await self.body()
        raw_images = body.get("image_urls") or []
        if not isinstance(raw_images, list):
            return error_response(
                422,
                "VALIDATION_ERROR",
                "image_urls must be a list of strings.",
            )
        post = await svc.create_post(
            space_id,
            author_user_id=ctx.user_id,
            type=body.get("type", "text"),
            content=body.get("content"),
            media_url=strip_signature_query(body.get("media_url")),
            image_urls=tuple(strip_signature_query(str(u)) for u in raw_images),
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
    """``POST/GET /api/spaces/{id}/posts/{post_id}/comments`` — add or
    list comments.

    The household-feed twin at ``/api/feed/posts/{id}/comments`` had
    both verbs since forever; the space twin was POST-only. The SPA
    at ``CommentOverlay.tsx`` fires ``api.get(commentsUrl)`` to render
    the thread on every overlay open + after every POST. Without a
    GET handler aiohttp returned 405, the SPA's promise rejected, and
    the overlay rendered "no comments" even though ``post.comment_count``
    correctly incremented — Pascal's symptom: "I see '1 comment' but
    no comment body".
    """

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

    async def get(self) -> web.Response:
        self.user  # auth gate
        svc = self.svc(space_service_key)
        post_id = self.match("post_id")
        try:
            comments = await svc.list_comments(post_id)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Post not found.")
        return web.json_response(
            [_serialise_comment(c) for c in comments],
        )


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


class MyJoinRequestsView(BaseView):
    """``GET /api/me/join-requests`` — the caller's own outstanding
    (``pending``) join-requests, as a list of space ids.

    Returns ``{"pending_space_ids": [...]}``. The SPA merges these into
    its directory entries so a "Request pending" card survives a reload
    (the optimistic in-memory flag is otherwise lost on the next mount).
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(space_service_key)
        return web.json_response(
            {
                "pending_space_ids": await svc.list_pending_join_request_space_ids(
                    ctx.user_id
                ),
            },
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
    """``GET /api/spaces/{id}/notif-prefs`` — caller's per-space prefs.

    ``PUT /api/spaces/{id}/notif-prefs`` — set notification level.
    Body: ``{level: 'all' | 'mentions' | 'muted'}``.
    Returns ``{level}``.

    GET piggy-backs the caller's location-sharing state so the SPA's
    per-space "@" menu can render the toggle without a second round-trip:
    ``feature_location`` mirrors the admin-controlled space flag,
    ``location_share_enabled`` mirrors this member's opt-in.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        space_repo = self.svc(space_repo_key)
        member = await space_repo.get_member(space_id, ctx.user_id)
        if member is None:
            return error_response(403, "FORBIDDEN", "Not a space member.")
        space = await space_repo.get(space_id)
        feature_location = bool(space and space.features.location)
        notif_repo = self.svc(notification_repo_key)
        level = await notif_repo.get_space_notif_level(
            user_id=ctx.user_id,
            space_id=space_id,
        )
        return web.json_response(
            {
                "level": level,
                "feature_location": feature_location,
                "location_share_enabled": bool(member.location_share_enabled),
            }
        )

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
