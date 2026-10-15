"""Name, picture and household for bare user ids (§Privacy lists).

The viewer-private people lists — ``GET /api/blocks`` and
``GET /api/moments/follows`` — store only user ids. The SPA used to name
them from the *local* household roster, so every remote person rendered
as a raw ``ru-…`` id with initials and the viewer's nickname for them was
ignored. :class:`PersonSummaryService` resolves a whole list in a fixed
number of batched reads (local users, remote users, paired households,
own household, the viewer's aliases) — never one query per row.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..domain.user import PersonSummary, _picture_url
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.user_repo import AbstractUserRepo
from .alias_service import AliasResolver

#: Own-household label when the instance identity row has no name yet —
#: the same fallback ``GET /api/friends`` uses.
_OWN_HOUSEHOLD_FALLBACK = "Home"


class PersonSummaryService:
    """Resolve user ids into :class:`PersonSummary` rows for one viewer."""

    __slots__ = ("_users", "_federation", "_aliases")

    def __init__(
        self,
        user_repo: AbstractUserRepo,
        federation_repo: AbstractFederationRepo,
        alias_resolver: AliasResolver,
    ) -> None:
        self._users = user_repo
        self._federation = federation_repo
        self._aliases = alias_resolver

    async def summarize(
        self,
        viewer_user_id: str,
        user_ids: Iterable[str],
    ) -> dict[str, PersonSummary]:
        """``{user_id: PersonSummary}`` for every id in ``user_ids``.

        Local users are named after their own household; remote users after
        their paired household (``None`` once it is unpaired). Ids neither
        table knows come back with ``display_name=None`` so the caller
        never has to fall back to the raw id.
        """
        ids = set(user_ids)
        if not ids:
            return {}
        local = {u.user_id: u for u in await self._users.list_by_ids(ids)}
        remote = {
            r.user_id: r
            for r in await self._users.list_remote_by_ids(ids - local.keys())
        }
        aliases = await self._aliases.resolve_users(viewer_user_id, ids)
        own_household = _OWN_HOUSEHOLD_FALLBACK
        if local:
            identity = await self._federation.get_local_identity()
            own_household = (identity or {}).get(
                "display_name"
            ) or _OWN_HOUSEHOLD_FALLBACK
        households: dict[str, str] = {}
        if remote:
            # Unfiltered = every pairing except unpair tombstones, so a
            # household mid-unpair already reads as "no longer paired".
            households = {
                inst.id: inst.effective_display_name
                for inst in await self._federation.list_instances()
            }
        out: dict[str, PersonSummary] = {}
        for uid in ids:
            alias = aliases.get(uid)
            if (user := local.get(uid)) is not None:
                out[uid] = PersonSummary(
                    user_id=uid,
                    display_name=user.display_name,
                    personal_alias=alias,
                    picture_url=_picture_url(uid, user.picture_hash),
                    household=own_household,
                )
            elif (ru := remote.get(uid)) is not None:
                out[uid] = PersonSummary(
                    user_id=uid,
                    display_name=ru.display_name,
                    personal_alias=alias,
                    picture_url=_picture_url(uid, ru.picture_hash),
                    household=households.get(ru.instance_id),
                )
            else:
                out[uid] = PersonSummary(
                    user_id=uid,
                    display_name=None,
                    personal_alias=alias,
                    picture_url=None,
                    household=None,
                )
        return out

    async def enrich(
        self,
        viewer_user_id: str,
        rows: list[dict],
    ) -> list[dict]:
        """``rows`` (each with a ``user_id``) merged with their summary.

        Keeps every existing key and order; adds ``display_name``,
        ``personal_alias``, ``picture_url`` (unsigned — the route signs)
        and ``household``.
        """
        people = await self.summarize(viewer_user_id, (r["user_id"] for r in rows))
        return [{**r, **people[r["user_id"]].to_dict()} for r in rows]
