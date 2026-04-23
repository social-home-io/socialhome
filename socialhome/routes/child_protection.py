"""Child Protection routes (§CP).

Admin-only configuration of minor accounts and per-space age gates.
Guardian operations are scoped to the caller's assigned minors.

* ``POST   /api/cp/users/{username}/protection``   — admin: enable/disable
* ``GET    /api/cp/users/{minor_id}/guardians``    — admin/guardian: list
* ``POST   /api/cp/users/{minor_id}/guardians/{guardian_id}``   — admin: add
* ``DELETE /api/cp/users/{minor_id}/guardians/{guardian_id}``   — admin: remove
* ``GET    /api/cp/minors``                                     — admin: list minors I'm a guardian of
* ``GET    /api/cp/minors/{minor_id}/blocks``                    — guardian: list blocks
* ``POST   /api/cp/minors/{minor_id}/blocks/{blocked_id}``      — guardian: block
* ``DELETE /api/cp/minors/{minor_id}/blocks/{blocked_id}``      — guardian: unblock
* ``GET    /api/cp/spaces/{space_id}/age-gate``                  — read gate
* ``PATCH  /api/cp/spaces/{space_id}/age-gate``                  — admin: set gate
* ``GET    /api/cp/minors/{minor_id}/audit-log``                 — guardian/admin: log
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..security import error_response
from .base import BaseView


class CPProtectionView(BaseView):
    """``POST /api/cp/users/{username}/protection``."""

    async def post(self) -> web.Response:
        ctx = self.user
        username = self.match("username")
        body = await self.body()
        enabled = bool(body.get("enabled"))
        svc = self.svc(K.child_protection_service_key)
        if enabled:
            declared_age = body.get("declared_age")
            if declared_age is None:
                return error_response(422, "UNPROCESSABLE", "declared_age required")
            await svc.enable_protection(
                minor_username=username,
                declared_age=int(declared_age),
                actor_user_id=ctx.user_id,
                date_of_birth=body.get("date_of_birth"),
            )
        else:
            await svc.disable_protection(
                minor_username=username,
                actor_user_id=ctx.user_id,
            )
        return web.Response(status=204)


class CPGuardiansView(BaseView):
    """``GET /api/cp/users/{minor_id}/guardians``
    + ``POST /api/cp/users/{minor_id}/guardians/{guardian_id}``
    + ``DELETE /api/cp/users/{minor_id}/guardians/{guardian_id}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        minor_id = self.match("minor_id")
        svc = self.svc(K.child_protection_service_key)
        is_admin = ctx.is_admin
        is_subject = ctx.user_id == minor_id
        is_guardian = await svc.is_guardian_of(ctx.user_id, minor_id)
        if not (is_admin or is_subject or is_guardian):
            return error_response(403, "FORBIDDEN", "forbidden")
        guardians = await svc.list_guardians(minor_id)
        return self._json({"guardians": guardians})

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.child_protection_service_key).add_guardian(
            minor_user_id=self.match("minor_id"),
            guardian_user_id=self.match("guardian_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.child_protection_service_key).remove_guardian(
            minor_user_id=self.match("minor_id"),
            guardian_user_id=self.match("guardian_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class CPBlockView(BaseView):
    """``POST /api/cp/minors/{minor_id}/blocks/{blocked_id}``
    + ``DELETE /api/cp/minors/{minor_id}/blocks/{blocked_id}``.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.child_protection_service_key).block_user_for_minor(
            minor_user_id=self.match("minor_id"),
            blocked_user_id=self.match("blocked_id"),
            guardian_user_id=ctx.user_id,
        )
        return web.Response(status=204)

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.child_protection_service_key).unblock_user_for_minor(
            minor_user_id=self.match("minor_id"),
            blocked_user_id=self.match("blocked_id"),
            guardian_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class CPBlockCollectionView(BaseView):
    """``GET /api/cp/minors/{minor_id}/blocks`` — guardian/admin: list blocks."""

    async def get(self) -> web.Response:
        ctx = self.user
        rows = await self.svc(
            K.child_protection_service_key,
        ).list_blocks_for_minor(
            minor_user_id=self.match("minor_id"),
            actor_user_id=ctx.user_id,
        )
        return self._json({"blocks": rows})


class CPKickView(BaseView):
    """``POST /api/cp/minors/{minor_id}/spaces/{space_id}/kick`` —
    guardian removes the minor from the space (spec §CP)."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(K.child_protection_service_key)
        removed = await svc.kick_from_space(
            minor_user_id=self.match("minor_id"),
            space_id=self.match("space_id"),
            guardian_user_id=ctx.user_id,
        )
        return self._json(
            {"removed": removed, "space_id": self.match("space_id")},
        )


class CPSpaceCollectionView(BaseView):
    """``GET /api/cp/minors/{minor_id}/spaces`` — guardian/admin: list
    every space the minor is currently a member of (spec §CP)."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(K.child_protection_service_key)
        spaces = await svc.list_spaces_for_minor(
            minor_user_id=self.match("minor_id"),
            actor_user_id=ctx.user_id,
        )
        return self._json({"spaces": spaces})


class CPConversationCollectionView(BaseView):
    """``GET /api/cp/minors/{minor_id}/conversations`` — guardian/admin:
    list every DM conversation the minor participates in (spec §CP)."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(K.child_protection_service_key)
        convs = await svc.list_conversations_for_minor(
            minor_user_id=self.match("minor_id"),
            actor_user_id=ctx.user_id,
        )
        return self._json({"conversations": convs})


class CPDmContactCollectionView(BaseView):
    """``GET /api/cp/minors/{minor_id}/dm-contacts`` — guardian/admin:
    list every peer the minor is currently DMing (spec §CP)."""

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(K.child_protection_service_key)
        contacts = await svc.list_dm_contacts_for_minor(
            minor_user_id=self.match("minor_id"),
            actor_user_id=ctx.user_id,
        )
        return self._json({"contacts": contacts})


class CPMinorsForGuardianView(BaseView):
    """``GET /api/cp/minors`` — every minor the caller is a guardian of.

    Guardians use this to populate the Parent Dashboard picker; admins
    see every minor they've been assigned by any dashboard session.
    Pass ``?guardian_id=X`` as an admin to see another guardian's list.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        guardian_id = self.request.query.get("guardian_id") or ctx.user_id
        if guardian_id != ctx.user_id and not ctx.is_admin:
            return error_response(403, "FORBIDDEN", "admin required")
        svc = self.svc(K.child_protection_service_key)
        minors = await svc.list_minors_for_guardian(guardian_id)
        return self._json({"minors": minors})


class CPAgeGateView(BaseView):
    """``GET /api/cp/spaces/{space_id}/age-gate``
    + ``PATCH /api/cp/spaces/{space_id}/age-gate``.
    """

    async def get(self) -> web.Response:
        self.user
        gate = await self.svc(K.child_protection_service_key).get_space_age_gate(
            self.match("space_id"),
        )
        return self._json(gate)

    async def patch(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        if body.get("min_age") is None or not body.get("target_audience"):
            return error_response(
                422,
                "UNPROCESSABLE",
                "min_age and target_audience are required",
            )
        await self.svc(K.child_protection_service_key).update_space_age_gate(
            self.match("space_id"),
            min_age=int(body["min_age"]),
            target_audience=str(body["target_audience"]),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class CPAuditLogView(BaseView):
    """``GET /api/cp/minors/{minor_id}/audit-log``."""

    async def get(self) -> web.Response:
        ctx = self.user
        minor_id = self.match("minor_id")
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        entries = await self.svc(K.child_protection_service_key).get_audit_log(
            minor_user_id=minor_id,
            requester_user_id=ctx.user_id,
            limit=max(1, min(limit, 200)),
        )
        return self._json({"entries": entries})


class CPMembershipAuditView(BaseView):
    """``GET /api/cp/minors/{minor_id}/membership-audit`` — append-only
    trail of space-membership changes that affected a minor
    (``joined`` / ``removed`` / ``blocked``).

    Guardian-or-admin gated via
    :meth:`ChildProtectionService.get_membership_audit`. Distinct from
    ``/audit-log`` (which tracks *guardian-driven* actions like
    enabling CP or toggling blocks); this surface tracks *system-
    driven* mutations like admin removing a minor from a space.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        minor_id = self.match("minor_id")
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        entries = await self.svc(
            K.child_protection_service_key,
        ).get_membership_audit(
            minor_user_id=minor_id,
            requester_user_id=ctx.user_id,
            limit=max(1, min(limit, 200)),
        )
        return self._json({"entries": entries})
