"""Friends overview — the payload behind ``GET /api/friends``.

A non-admin, social-shape view of "everyone we're connected to": the
local household members + every social remote household plus its
members, pre-grouped by household so the SPA can render the overview
without client-side joins.

Each member row carries the viewer's private nickname, the session
presence triple (``is_online`` / ``is_idle`` / ``last_seen_at``), the
emoji/text ``status`` and the ``bio``:

* Local rows read status + bio off the :class:`User` record — the same
  source ``GET /api/presence`` and ``GET /api/users`` use.
* Remote rows read the ``remote_users`` cache: ``bio`` from
  ``USER_UPDATED`` / ``USERS_SYNC``, ``status_json`` from
  ``USER_STATUS_UPDATED``.
* A status past its ``expires_at`` is dropped (``None``) even before the
  local expiry sweep clears it; remote statuses have no sweep, so the
  read-time check is the only gate.

Privacy:

* People the viewer personally blocked, and everyone a guardian block
  (§CP.F2) separates from the viewer — either way round — are left out
  entirely, the same symmetric rule DMs, calls and typing apply.
* Deprovisioned remote users (``USER_REMOVED``, including members a
  peer hid from us) and inactive local users never appear — the repo
  reads skip them.
* Whitelisted fields only — no routing keys, no session keys, no remote
  inbox URLs, no identity public keys.
* Household coordinates are 4dp-truncated at the schema level (§25).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.federation import PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..domain.user import RemoteUser, User, UserStatus
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from .alias_service import AliasResolver
    from .child_protection_service import ChildProtectionService
    from .online_status_service import OnlineStatusService


#: Own-household label when the instance identity row has no name yet.
_OWN_HOUSEHOLD_FALLBACK = "Home"


class FriendNotFoundError(LookupError):
    """``GET /api/friends/{user_id}`` — nobody the viewer may see.

    One error for unknown, blocked, guardian-separated, deprovisioned and
    not-socially-paired people alike, so the 404 never tells the viewer
    *why* someone is out of reach.
    """


class FriendsService:
    """Builds the ``GET /api/friends`` payload for one viewer.

    Reads are batched: one query for local users, one for every remote
    user across every household, one for the viewer's blocks, one for
    guardian-block counterparts and one for aliases — independent of the
    number of households or people.
    """

    __slots__ = (
        "_users",
        "_federation",
        "_child_protection",
        "_online",
        "_aliases",
    )

    def __init__(
        self,
        user_repo: AbstractUserRepo,
        federation_repo: AbstractFederationRepo,
        *,
        child_protection: "ChildProtectionService | None" = None,
        online_status: "OnlineStatusService | None" = None,
        alias_resolver: "AliasResolver | None" = None,
    ) -> None:
        self._users = user_repo
        self._federation = federation_repo
        self._child_protection = child_protection
        self._online = online_status
        self._aliases = alias_resolver

    async def overview(self, viewer_user_id: str) -> dict:
        """The Friends payload for *viewer_user_id* (unsigned picture URLs)."""
        hidden = await self._hidden_for(viewer_user_id)
        now = datetime.now(timezone.utc)

        local_id = await self._federation.get_local_identity()
        local_users = [
            u for u in await self._users.list_active() if u.user_id not in hidden
        ]

        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        instances = await self._federation.list_social_instances()
        remote_by_instance: dict[str, list[RemoteUser]] = defaultdict(list)
        for ru in await self._users.list_all_known_remote():
            if ru.user_id not in hidden:
                remote_by_instance[ru.instance_id].append(ru)

        target_ids = [u.user_id for u in local_users]
        for inst in instances:
            target_ids.extend(ru.user_id for ru in remote_by_instance[inst.id])
        aliases: dict[str, str] = {}
        if self._aliases is not None and target_ids:
            aliases = await self._aliases.resolve_users(viewer_user_id, target_ids)

        local_block = {
            "instance_id": local_id["instance_id"] if local_id else None,
            "display_name": (local_id or {}).get("display_name")
            or _OWN_HOUSEHOLD_FALLBACK,
            "home_lat": (local_id or {}).get("home_lat"),
            "home_lon": (local_id or {}).get("home_lon"),
            "members": [self._local_member(u, aliases, now) for u in local_users],
            "member_count": len(local_users),
        }
        households = [
            _instance_safe_dict(
                inst,
                members=[
                    self._remote_member(ru, aliases, now)
                    for ru in remote_by_instance[inst.id]
                ],
            )
            for inst in instances
        ]
        # Totals span every household — including ours — so the hero
        # reads "X people across Y households" without the SPA having to
        # add the local count separately.
        people = local_block["member_count"] + sum(
            h["member_count"] for h in households
        )
        return {
            "instance": local_block,
            "households": households,
            "totals": {"households": 1 + len(households), "people": people},
        }

    async def person(self, viewer_user_id: str, user_id: str) -> dict:
        """One member row of :meth:`overview`, plus where they live.

        Backs the SPA's person sheet when a surface only knows a user id
        (a highlight author, a momentum row). The visible set is exactly
        the one :meth:`overview` lists — active local users and the
        non-deprovisioned people of socially paired households, minus
        personal blocks and guardian-block counterparts — so this read
        never reveals someone the Friends page would hide.

        The row carries ``is_local`` and ``household`` —
        ``{instance_id, display_name, is_own}`` — on top of the overview's
        member keys (unsigned ``picture_url``; the route signs).

        Raises :class:`FriendNotFoundError` for everyone else.
        """
        if user_id in await self._hidden_for(viewer_user_id):
            raise FriendNotFoundError("person not found")
        now = datetime.now(timezone.utc)
        aliases: dict[str, str] = {}
        if self._aliases is not None:
            aliases = await self._aliases.resolve_users(viewer_user_id, [user_id])

        user = await self._users.get_by_user_id(user_id)
        if user is not None and user.state == "active" and user.deleted_at is None:
            local_id = await self._federation.get_local_identity() or {}
            return {
                **self._local_member(user, aliases, now),
                "is_local": True,
                "household": {
                    "instance_id": local_id.get("instance_id"),
                    "display_name": local_id.get("display_name")
                    or _OWN_HOUSEHOLD_FALLBACK,
                    "is_own": True,
                },
            }

        ru = await self._users.get_remote(user_id)
        if ru is None or ru.deprovisioned_at is not None:
            raise FriendNotFoundError("person not found")
        # §D2b: a household we only share a space with is not a social
        # peer — its people are not on the Friends page, so not here.
        inst = next(
            (
                i
                for i in await self._federation.list_social_instances()
                if i.id == ru.instance_id
            ),
            None,
        )
        if inst is None:
            raise FriendNotFoundError("person not found")
        return {
            **self._remote_member(ru, aliases, now),
            "is_local": False,
            "household": {
                "instance_id": inst.id,
                "display_name": inst.effective_display_name,
                "is_own": False,
            },
        }

    async def _hidden_for(self, viewer_user_id: str) -> set[str]:
        """Personal blocks + guardian-block counterparts of the viewer."""
        hidden = {uid for uid, _ in await self._users.list_blocked(viewer_user_id)}
        if self._child_protection is not None:
            hidden |= await self._child_protection.guardian_block_counterparts(
                viewer_user_id
            )
        return hidden

    def _presence(self, user_id: str, *, persisted: str | None, local: bool) -> dict:
        """The session presence triple from :class:`OnlineStatusService`.

        Local rows: an online user gets the live timestamp, an offline
        one the persisted ``users.last_seen_at``. Remote rows always read
        the service's remote cache (fed by ``USER_ONLINE`` /
        ``USER_OFFLINE``) — ``remote_users`` has no persisted column, so
        it is ``None`` until the first presence envelope lands.
        """
        online = self._online
        is_online = bool(online and online.is_online(user_id))
        is_idle = bool(online and online.is_idle(user_id))
        if online is not None and (is_online or not local):
            last_dt = online.last_seen(user_id)
            last_seen = last_dt.isoformat() if last_dt is not None else None
        else:
            last_seen = persisted
        return {"is_online": is_online, "is_idle": is_idle, "last_seen_at": last_seen}

    def _local_member(self, u: User, aliases: dict[str, str], now: datetime) -> dict:
        return {
            "user_id": u.user_id,
            "username": u.username,
            "display_name": u.display_name,
            "personal_alias": aliases.get(u.user_id),
            "picture_hash": u.picture_hash,
            "picture_url": _picture_url(u.user_id, u.picture_hash),
            **self._presence(u.user_id, persisted=u.last_seen_at, local=True),
            "status": u.status.visible_dict(now),
            "bio": u.bio or None,
        }

    def _remote_member(
        self, ru: RemoteUser, aliases: dict[str, str], now: datetime
    ) -> dict:
        return {
            "user_id": ru.user_id,
            "instance_id": ru.instance_id,
            "remote_username": ru.remote_username,
            "display_name": ru.display_name,
            "personal_alias": aliases.get(ru.user_id),
            "picture_hash": ru.picture_hash,
            "picture_url": _picture_url(ru.user_id, ru.picture_hash),
            **self._presence(ru.user_id, persisted=None, local=False),
            "status": UserStatus.from_json(ru.status_json).visible_dict(now),
            "bio": ru.bio or None,
        }


def _picture_url(user_id: str, picture_hash: str | None) -> str | None:
    """Same cache-busting URL the rest of the SPA uses for avatars."""
    if not picture_hash:
        return None
    return f"api/users/{user_id}/picture?v={picture_hash}"


def _instance_safe_dict(inst, *, members: list[dict]) -> dict:
    """Whitelisted view of a :class:`RemoteInstance` for the Friends
    surface. Mirrors ``_instance_dict`` from ``routes/pairing.py`` but
    folds in the member list + count so the SPA renders one card without
    a follow-up fetch per household.

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
        # The displayed household name. When the admin set a local alias
        # ("Brother's house") it wins over whatever the peer advertised
        # via the federation handshake.
        "display_name": effective,
        "federated_display_name": inst.display_name,
        "local_alias": getattr(inst, "local_alias", None),
        "home_lat": getattr(inst, "home_lat", None),
        "home_lon": getattr(inst, "home_lon", None),
        "status": status,
        "reachable": reachable,
        # When the household stopped answering (UTC, naive SQLite shape)
        # — the Friends card reads "Unreachable since Wed". ``None``
        # while reachable.
        "unreachable_since": getattr(inst, "unreachable_since", None),
        "paired_at": getattr(inst, "paired_at", None),
        "source": (inst.source.value if hasattr(inst.source, "value") else inst.source),
        # Whether this household's people can be put into a group chat
        # (v_37+). The new-group picker greys out the others and says why.
        "supports_group_dm": int(getattr(inst, "proto_version", 1) or 1)
        >= FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
        "members": members,
        "member_count": len(members),
    }
