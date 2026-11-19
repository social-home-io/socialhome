"""§25.6 sync of a space's tasks: ``task_lists``, ``tasks``,
``tasks_archived`` and their tombstones.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....domain.events import TaskDeleted, TaskListDeleted
from ....domain.federation import FederationEvent
from ....domain.task import task_from_wire_dict, task_list_from_wire_dict
from ...owner_bound_id import (
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
)
from .receiver_base import ReceiverBase, _log_sync_refusal
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship

log = logging.getLogger(__name__)


class TasksMixin(ReceiverBase):
    """The task resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_task_lists(
        self, space_id: str, records: list[dict[str, Any]]
    ) -> None:
        for r in records:
            lst = task_list_from_wire_dict(r)
            if lst is None or not lst.created_by:
                continue
            if await self._space_task_repo.is_list_deleted(lst.id, space_id=space_id):
                # Deleted here — a provider that missed the delete
                # still streams it; the tombstone wins (its id is never
                # reused), and its own tombstone stream will tell it.
                log.debug("sync: task list %s was deleted here — skipped", lst.id)
                continue
            if not await self._space_task_repo.save_list(lst, space_id=space_id):
                _log_sync_refusal("task list", lst.id, space_id)

    async def _persist_tasks(
        self, space_id: str, records: list[dict[str, Any]]
    ) -> None:
        for r in records:
            # A member household may only add ids not held here (see
            # ``_admit``), but the host's chunks are taken whole and
            # upsert over rows we hold — so merge onto the held row of
            # THIS space, or a v39 host's chunk (no ``priority``, the
            # fields its inbound lost sent as null) would wipe the
            # priority / labels / due date / archive every tick.
            rid = str(r.get("id") or r.get("task_id") or "")
            if rid and await self._space_task_repo.is_task_deleted(
                rid, space_id=space_id
            ):
                # Deleted here — a provider that missed the delete still
                # streams it; the tombstone wins (its id is never
                # reused), and its own tombstone stream will tell it.
                log.debug("sync: task %s was deleted here — skipped", rid)
                continue
            held = await self._space_task_repo.get(rid) if rid else None
            existing = held[1] if held is not None and held[0] == space_id else None
            task = task_from_wire_dict(r, existing=existing)
            if task is not None and await self._space_task_repo.is_list_deleted(
                task.list_id, space_id=space_id
            ):
                log.debug(
                    "sync: task %s is filed under list %s, deleted here — skipped",
                    task.id,
                    task.list_id,
                )
                continue
            if task is not None and not await self._space_task_repo.save(
                task, space_id=space_id
            ):
                _log_sync_refusal("task", task.id, space_id)

    async def _authored_task_list(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # A member household only adds owner-bound lists (minted by
        # every v_40 sender); a legacy (pre-v_40) list id is taken
        # from the host alone, whose chunks never reach here — so a
        # member can't squat a list id another space holds.
        if not rid or not is_owner_bound(rid):
            return False
        if await self._space_task_repo.get_list(rid) is not None:
            return False
        if await self._space_task_repo.is_list_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        return await self._may_author(
            held_back, event, space_id, str(r.get("created_by") or "")
        )

    async def _authored_task_list_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_TASK_LIST_DELETED rule
        # (:meth:`_writer_delete_admits`) for a list held live here
        # IN THIS SPACE.
        rid = rid or str(r.get("list_id") or "")
        held = await self._space_task_repo.get_list(rid) if rid else None
        if held is None or held[0] != space_id:
            return False
        return await self._writer_delete_admits(
            event, space_id, "tasks", r, row_owner=held[1].created_by
        )

    async def _authored_task_delete(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        # The live SPACE_TASK_DELETED rule for a task held live here
        # IN THIS SPACE.
        rid = rid or str(r.get("task_id") or "")
        held_task = await self._space_task_repo.get(rid) if rid else None
        if held_task is None or held_task[0] != space_id:
            return False
        return await self._writer_delete_admits(
            event, space_id, "tasks", r, row_owner=held_task[1].created_by
        )

    async def _authored_task(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        if not rid or await self._space_task_repo.get(rid) is not None:
            return False
        if await self._space_task_repo.is_task_deleted(rid, space_id=space_id):
            return False  # deleted here; the tombstone wins
        return await self._may_author(
            held_back, event, space_id, str(r.get("created_by") or "")
        )

    async def _persist_task_list_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed list deletes (``task_lists_deleted``).

        A list held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``); its tasks are tombstoned with
        it (the list-tombstone trigger, 0069 / 0071), and ``TaskListDeleted`` is published as
        for a live ``SPACE_TASK_LIST_DELETED``. A member household's records
        were admitted only for such lists (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams later cannot create it — but
        only when the id is owner-bound to its ``created_by`` in THIS
        space. List ids are global: a stub for another space's id would
        block that space's real list here forever. A legacy (unbound) or
        mismatched id is skipped; one already a tombstone is not rewritten.
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = await self._acts_as_host(space, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            list_id = str(r.get("id") or r.get("list_id") or "")
            if not list_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_task_repo.get_list(list_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(list_id)
                elif await self._space_task_repo.delete_list(
                    list_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        TaskListDeleted(
                            list_id=list_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._space_task_repo.is_list_deleted(
                list_id, space_id=space_id
            ):
                continue
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_TASK_LIST_KIND,
                    list_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            await self._space_task_repo.tombstone_list(
                list_id, space_id=space_id, created_by=created_by, deleted_by=deleted_by
            )
        if cross_space:
            log.warning(
                "space sync: %d task list tombstone(s) from %s for %s name a "
                "list held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d task list tombstone(s) from %s for %s name a "
                "list never held here whose id is not bound to this space — "
                "no stub recorded",
                unbound,
                provider,
                space_id,
            )

    async def _persist_task_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed single-task deletes (``tasks_deleted``).

        A task held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``) and ``TaskDeleted`` is
        published as for a live ``SPACE_TASK_DELETED``. A member
        household's records were admitted only for such tasks
        (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams later cannot create it — but
        only when the id is owner-bound to its ``created_by`` in THIS space
        (task ids are global: a stub for another space's id would block
        that space's real task here forever), and only under a list live
        here in this space (``space_tasks.list_id`` is a FK). A task under
        a list tombstoned here needs nothing: the list's delete already
        tombstoned every task of it (0071 trigger) and keeps new ones out. Refusals are summarised
        once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = await self._acts_as_host(space, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            task_id = str(r.get("id") or r.get("task_id") or "")
            if not task_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_task_repo.get(task_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(task_id)
                elif await self._space_task_repo.delete(
                    task_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        TaskDeleted(
                            task_id=task_id,
                            list_id=held[1].list_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._space_task_repo.is_task_deleted(
                task_id, space_id=space_id
            ):
                continue
            list_id = str(r.get("list_id") or "")
            if not list_id or await self._space_task_repo.is_list_deleted(
                list_id, space_id=space_id
            ):
                continue  # gone with its list; the list tombstone wins
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_TASK_KIND,
                    task_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            if not await self._space_task_repo.tombstone(
                task_id,
                space_id=space_id,
                list_id=list_id,
                created_by=created_by,
                deleted_by=deleted_by,
            ):
                unbound += 1  # its list is not held live in this space
        if cross_space:
            log.warning(
                "space sync: %d task tombstone(s) from %s for %s name a task "
                "held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d task tombstone(s) from %s for %s name a task "
                "never held here whose id is not bound to this space, or "
                "whose list is not held here — no stub recorded",
                unbound,
                provider,
                space_id,
            )
