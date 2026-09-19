"""Friends route — /api/friends.

A non-admin, social-shape view of "everyone we're connected to":
the local household members + every confirmed remote household plus
its members. Same data the admin Connections page surfaces, but
reframed for regular members and pre-grouped by household so the SPA
can render a "constellation of households" overview without
client-side joins.

Privacy:
* Whitelisted fields only — no routing keys, no session keys, no
  remote inbox URLs (those are SENSITIVE_FIELDS-equivalent for
  ``remote_instances`` even though the table itself isn't on the
  central list, since they're material a peer could use to
  impersonate the relay).
* Household coordinates are 4dp-truncated at the schema level (§25).
  They identify households, not users, and already round-trip with
  pairing — exposing them on this endpoint adds no new information
  the SPA didn't already have.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    alias_resolver_key,
    federation_repo_key,
    media_signer_key,
    online_status_service_key,
    user_repo_key,
)
from ..domain.federation import PairingStatus
from ..media_signer import sign_media_urls_in
from .base import BaseView


def _local_user_to_dict(
    u,
    *,
    online_svc=None,
    aliases: dict[str, str] | None = None,
) -> dict:
    """Public-shape view of a local :class:`User`. Mirrors
    ``GET /api/users`` (same fields the SPA already renders) plus the
    online-status triple from :class:`OnlineStatusService` when
    available — saves the Friends page a follow-up fetch.

    ``aliases`` is the viewer's ``{target_user_id: alias}`` map from
    :class:`AliasResolver`; when present, the row carries the viewer's
    private nickname so the SPA can render it without a second fetch.
    """
    is_online = bool(online_svc and online_svc.is_online(u.user_id))
    is_idle = bool(online_svc and online_svc.is_idle(u.user_id))
    if is_online and online_svc is not None:
        last_dt = online_svc.last_seen(u.user_id)
        last_seen = last_dt.isoformat() if last_dt is not None else None
    else:
        last_seen = u.last_seen_at
    return {
        "user_id": u.user_id,
        "username": u.username,
        "display_name": u.display_name,
        "personal_alias": (aliases or {}).get(u.user_id),
        "picture_hash": u.picture_hash,
        "picture_url": (
            f"api/users/{u.user_id}/picture?v={u.picture_hash}"
            if u.picture_hash
            else None
        ),
        "is_online": is_online,
        "is_idle": is_idle,
        "last_seen_at": last_seen,
    }


def _remote_user_to_dict(
    ru,
    *,
    online_svc=None,
    aliases: dict[str, str] | None = None,
) -> dict:
    """Public-shape view of a :class:`RemoteUser`. Picture is served
    via the existing per-user route; same cache-busting hash convention
    as the local users. The presence triple matches
    :func:`_local_user_to_dict` — federation ``USER_ONLINE`` /
    ``USER_OFFLINE`` envelopes already populate
    :class:`OnlineStatusService`'s remote cache keyed on ``user_id``, so
    the same calls used for local rows surface remote presence
    transparently. ``remote_users`` has no persisted ``last_seen_at``
    column, so both online + offline branches read the service's
    remote cache (returns ``None`` until the first presence envelope
    lands)."""
    is_online = bool(online_svc and online_svc.is_online(ru.user_id))
    is_idle = bool(online_svc and online_svc.is_idle(ru.user_id))
    last_dt = online_svc.last_seen(ru.user_id) if online_svc else None
    last_seen = last_dt.isoformat() if last_dt is not None else None
    return {
        "user_id": ru.user_id,
        "instance_id": ru.instance_id,
        "remote_username": ru.remote_username,
        "display_name": ru.display_name,
        "personal_alias": (aliases or {}).get(ru.user_id),
        "picture_hash": ru.picture_hash,
        "picture_url": (
            f"api/users/{ru.user_id}/picture?v={ru.picture_hash}"
            if ru.picture_hash
            else None
        ),
        "is_online": is_online,
        "is_idle": is_idle,
        "last_seen_at": last_seen,
    }


def _instance_safe_dict(inst, *, members: list[dict]) -> dict:
    """Whitelisted view of a :class:`RemoteInstance` for the Friends
    surface. Mirrors ``_instance_dict`` from ``routes/pairing.py`` but
    folds in the member list + count so the SPA renders one card
    without a follow-up fetch per household.

    *Never* serialise via ``asdict(inst)`` — that would leak
    ``remote_inbox_url``, ``key_self_to_remote``, ``key_remote_to_self``,
    and the identity public keys.
    """
    status = (
        inst.status.value if isinstance(inst.status, PairingStatus) else inst.status
    )
    reachable = inst.is_reachable() if hasattr(inst, "is_reachable") else True
    effective = (
        inst.effective_display_name
        if hasattr(inst, "effective_display_name")
        else inst.display_name
    )
    return {
        "instance_id": inst.id,
        # The displayed household name. When the admin set a local
        # alias ("Brother's house") it wins over whatever the peer
        # advertised via the federation handshake — keeps the cryptic
        # ``z7k63zfi``-style names out of the dashboard.
        "display_name": effective,
        "federated_display_name": inst.display_name,
        "local_alias": getattr(inst, "local_alias", None),
        "home_lat": getattr(inst, "home_lat", None),
        "home_lon": getattr(inst, "home_lon", None),
        "status": status,
        "reachable": reachable,
        "paired_at": getattr(inst, "paired_at", None),
        "source": (inst.source.value if hasattr(inst.source, "value") else inst.source),
        "members": members,
        "member_count": len(members),
    }


class FriendsView(BaseView):
    """``GET /api/friends`` — the connected-people dashboard payload.

    Open to any authenticated household member. The data is purely
    informational — every field already rides the Connections admin
    surface (with the addition of the per-household member list and
    household coordinates), and contains no routing or session keys.
    """

    async def get(self) -> web.Response:
        ctx = self.user  # auth check; raises 401 for anonymous
        fed_repo = self.svc(federation_repo_key)
        user_repo = self.svc(user_repo_key)
        online_svc = self.request.app.get(online_status_service_key)
        alias_resolver = self.request.app.get(alias_resolver_key)

        # Personal blocks (§Privacy) — drop blocked household members so
        # the constellation doesn't surface someone the viewer hid.
        blocked_ids = {uid for uid, _ in await user_repo.list_blocked(ctx.user_id)}

        # Local block — own household first.
        local_id = await fed_repo.get_local_identity()
        local_users = [
            u for u in await user_repo.list_active() if u.user_id not in blocked_ids
        ]

        # Confirmed remote households + their members.
        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        instances = await fed_repo.list_social_instances()
        remote_members_by_instance: list[tuple] = []
        for inst in instances:
            members = [
                ru
                for ru in await user_repo.list_remote_for_instance(inst.id)
                if ru.user_id not in blocked_ids
            ]
            remote_members_by_instance.append((inst, members))

        # Bulk-fetch the viewer's personal aliases for every user we're
        # about to render — one round-trip instead of N. Resolver is
        # optional (older deployments without it degrade to no aliases).
        all_target_ids: list[str] = [u.user_id for u in local_users]
        for _, members in remote_members_by_instance:
            all_target_ids.extend(ru.user_id for ru in members)
        aliases: dict[str, str] = {}
        if alias_resolver is not None and all_target_ids:
            aliases = await alias_resolver.resolve_users(
                ctx.user_id,
                all_target_ids,
            )

        local_block = {
            "instance_id": local_id["instance_id"] if local_id else None,
            "display_name": (local_id or {}).get("display_name") or "Home",
            "home_lat": (local_id or {}).get("home_lat"),
            "home_lon": (local_id or {}).get("home_lon"),
            "members": [
                _local_user_to_dict(u, online_svc=online_svc, aliases=aliases)
                for u in local_users
            ],
            "member_count": len(local_users),
        }

        households: list[dict] = []
        for inst, remote_members in remote_members_by_instance:
            households.append(
                _instance_safe_dict(
                    inst,
                    members=[
                        _remote_user_to_dict(
                            ru,
                            online_svc=online_svc,
                            aliases=aliases,
                        )
                        for ru in remote_members
                    ],
                ),
            )

        # Totals span every household — including ours — so the hero
        # reads "X people across Y households" without the SPA having
        # to add the local count separately.
        people = local_block["member_count"] + sum(
            h["member_count"] for h in households
        )
        payload = {
            "instance": local_block,
            "households": households,
            "totals": {
                "households": 1 + len(households),
                "people": people,
            },
        }
        # Sign nested ``picture_url`` so the Friends grid avatars load
        # in plain ``<img src>`` without a Bearer token attached.
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(payload, signer)
        return self._json(payload)
