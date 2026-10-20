"""Name, picture and household for bare user ids (§Privacy lists).

The viewer-private people lists — ``GET /api/blocks`` and
``GET /api/moments/follows`` — store only user ids. The SPA used to name
them from the *local* household roster, so every remote person rendered
as a raw ``ru-…`` id with initials and the viewer's nickname for them was
ignored. :class:`PersonSummaryService` resolves a whole list in a fixed
number of batched reads (local users, remote users, paired households,
own household, the viewer's aliases) — never one query per row.

The space admin lists (bans, join requests, reports, invite links) name
people the same way, scoped to the space (``space_id``): the per-space
name first, then the household's, then the space's roster seat — the only
record of a member of a household this one never paired with.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote

from ..domain.space import SpaceMember
from ..domain.user import PersonSummary, _picture_url
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .alias_service import AliasResolver
from .protection_gate import ProtectionGateMixin

#: Own-household label when the instance identity row has no name yet —
#: the same fallback ``GET /api/friends`` uses.
_OWN_HOUSEHOLD_FALLBACK = "Home"


class PersonSummaryService(ProtectionGateMixin):
    """Resolve user ids into :class:`PersonSummary` rows for one viewer.

    Someone a guardian block (§CP.F2) separates from the viewer, either
    way, comes back by name only — no picture.
    """

    __slots__ = (
        "_aliases",
        "_child_protection",
        "_federation",
        "_seats",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        user_repo: AbstractUserRepo,
        federation_repo: AbstractFederationRepo,
        alias_resolver: AliasResolver,
    ) -> None:
        self._users = user_repo
        self._federation = federation_repo
        self._aliases = alias_resolver
        self._child_protection = None
        self._spaces: AbstractSpaceRepo | None = None
        self._seats: AbstractSpaceRemoteMemberRepo | None = None

    def attach_space_rosters(
        self,
        space_repo: AbstractSpaceRepo,
        seat_repo: AbstractSpaceRemoteMemberRepo,
    ) -> None:
        """Wire the space rosters a ``space_id``-scoped summary reads."""
        self._spaces = space_repo
        self._seats = seat_repo

    async def summarize(
        self,
        viewer_user_id: str,
        user_ids: Iterable[str],
        *,
        space_id: str | None = None,
    ) -> dict[str, PersonSummary]:
        """``{user_id: PersonSummary}`` for every id in ``user_ids``.

        Local users are named after their own household; remote users after
        their paired household (``None`` once it is unpaired). Ids neither
        table knows come back with ``display_name=None`` so the caller
        never has to fall back to the raw id.

        With ``space_id``, the space's own record of a person wins and
        fills the gaps: their per-space name and picture (§4.1.6), and for
        anyone still unnamed the space's roster seat — kept after they left
        or were banned (tombstoned), and the only name a member of a
        household this one never paired with has here.
        """
        ids = set(user_ids)
        if not ids:
            return {}
        out = await self._summarize(viewer_user_id, ids)
        if space_id is None or self._spaces is None:
            return out
        return await self._in_space(viewer_user_id, space_id, out)

    async def _in_space(
        self,
        viewer_user_id: str,
        space_id: str,
        out: dict[str, PersonSummary],
    ) -> dict[str, PersonSummary]:
        assert self._spaces is not None
        members: dict[str, SpaceMember] = {
            m.user_id: m
            for m in await self._spaces.list_members(space_id)
            if m.user_id in out
        }
        withheld = await self._guardian_block_counterparts(viewer_user_id)
        scoped: dict[str, PersonSummary] = {}
        for uid, summary in out.items():
            name = summary.display_name
            picture = summary.picture_url
            if (m := members.get(uid)) is not None:
                name = m.space_display_name or name
                if m.picture_hash and uid not in withheld:
                    picture = (
                        f"api/spaces/{space_id}/members/{uid}/picture"
                        f"?v={quote(m.picture_hash, safe='')}"
                    )
            if not name and self._seats is not None:
                seat = await self._seats.get_including_tombstones(space_id, "", uid)
                name = seat.display_name if seat is not None else None
            scoped[uid] = PersonSummary(
                user_id=uid,
                display_name=name or None,
                personal_alias=summary.personal_alias,
                picture_url=picture,
                household=summary.household,
            )
        return scoped

    async def _summarize(
        self, viewer_user_id: str, ids: set[str]
    ) -> dict[str, PersonSummary]:
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
        withheld = await self._guardian_block_counterparts(viewer_user_id)
        out: dict[str, PersonSummary] = {}
        for uid in ids:
            alias = aliases.get(uid)
            if (user := local.get(uid)) is not None:
                out[uid] = PersonSummary(
                    user_id=uid,
                    display_name=user.display_name,
                    personal_alias=alias,
                    picture_url=(
                        None
                        if uid in withheld
                        else _picture_url(uid, user.picture_hash)
                    ),
                    household=own_household,
                )
            elif (ru := remote.get(uid)) is not None:
                out[uid] = PersonSummary(
                    user_id=uid,
                    display_name=ru.display_name,
                    personal_alias=alias,
                    picture_url=(
                        None if uid in withheld else _picture_url(uid, ru.picture_hash)
                    ),
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
        *,
        space_id: str | None = None,
    ) -> list[dict]:
        """``rows`` (each with a ``user_id``) merged with their summary.

        Keeps every existing key and order; adds ``display_name``,
        ``personal_alias``, ``picture_url`` (unsigned — the route signs)
        and ``household``. Returns new dicts — ``rows`` is never mutated.
        """
        people = await self.summarize(
            viewer_user_id, (r["user_id"] for r in rows), space_id=space_id
        )
        return [{**r, **people[r["user_id"]].to_dict()} for r in rows]
