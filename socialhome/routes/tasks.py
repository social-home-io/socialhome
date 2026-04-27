"""Task routes — /api/tasks/* (§5.2)."""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..app_keys import task_service_key
from ..security import error_response
from .base import BaseView


def _task_dict(task) -> dict:
    return {
        "id": task.id,
        "list_id": task.list_id,
        "title": task.title,
        "status": task.status.value,
        "position": task.position,
        "created_by": task.created_by,
        "description": task.description,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "assignees": list(task.assignees),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "archived_at": task.archived_at.isoformat() if task.archived_at else None,
    }


class TaskListCollectionView(BaseView):
    """``GET /api/tasks/lists`` — list all task lists.

    ``POST /api/tasks/lists`` — create a new task list.
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        svc = self.svc(task_service_key)
        lists = await svc.list_lists()
        return web.json_response(
            [
                {"id": lst.id, "name": lst.name, "created_by": lst.created_by}
                for lst in lists
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(task_service_key)
        task_list = await svc.create_list(
            name=body.get("name", ""),
            created_by=ctx.user_id,
        )
        return web.json_response(
            {
                "id": task_list.id,
                "name": task_list.name,
                "created_by": task_list.created_by,
            },
            status=201,
        )


class TaskListDetailView(BaseView):
    """``GET`` / ``PATCH`` / ``DELETE /api/tasks/lists/{id}``."""

    async def get(self) -> web.Response:
        self.user  # auth check
        list_id = self.match("id")
        svc = self.svc(task_service_key)
        task_list = await svc.get_list(list_id)
        return web.json_response(
            {
                "id": task_list.id,
                "name": task_list.name,
                "created_by": task_list.created_by,
            },
        )

    async def patch(self) -> web.Response:
        self.user  # auth check
        list_id = self.match("id")
        body = await self.body()
        svc = self.svc(task_service_key)
        name = str(body.get("name") or "")
        task_list = await svc.rename_list(list_id, name=name)
        return web.json_response(
            {
                "id": task_list.id,
                "name": task_list.name,
                "created_by": task_list.created_by,
            },
        )

    async def delete(self) -> web.Response:
        self.user  # auth check
        list_id = self.match("id")
        svc = self.svc(task_service_key)
        await svc.delete_list(list_id)
        return web.json_response({"ok": True})


class TaskListTasksView(BaseView):
    """``GET /api/tasks/lists/{id}/tasks`` — list tasks in a list.

    ``POST /api/tasks/lists/{id}/tasks`` — create a task in a list.
    """

    async def get(self) -> web.Response:
        self.user  # auth check
        list_id = self.match("id")
        q = self.request.query
        include_done = q.get("include_done", "true").lower() != "false"
        try:
            limit = int(q["limit"]) if "limit" in q else None
            offset = int(q.get("offset", "0"))
        except ValueError:
            return error_response(
                422,
                "UNPROCESSABLE",
                "limit/offset must be integers.",
            )
        svc = self.svc(task_service_key)
        tasks = await svc.list_tasks(
            list_id,
            include_done=include_done,
            status=q.get("status"),
            assignee=q.get("assignee"),
            due_from=q.get("due_from"),
            due_to=q.get("due_to"),
            limit=limit,
            offset=offset,
        )
        return web.json_response([_task_dict(t) for t in tasks])

    async def post(self) -> web.Response:
        ctx = self.user
        list_id = self.match("id")
        body = await self.body()
        svc = self.svc(task_service_key)
        task = await svc.create_task(
            list_id=list_id,
            title=body.get("title", ""),
            created_by=ctx.user_id,
            description=body.get("description"),
            due_date=body.get("due_date"),
            assignees=body.get("assignees"),
        )
        return web.json_response(_task_dict(task), status=201)


class TaskListReorderView(BaseView):
    """``POST /api/tasks/lists/{id}/reorder`` — bulk position update."""

    async def post(self) -> web.Response:
        self.user
        list_id = self.match("id")
        body = await self.body()
        ordered = body.get("order") or body.get("ordered_ids") or []
        if not isinstance(ordered, list):
            return error_response(
                422,
                "UNPROCESSABLE",
                "'order' must be an array of task ids.",
            )
        svc = self.svc(task_service_key)
        try:
            updated = await svc.reorder_tasks(
                list_id,
                ordered_ids=[str(x) for x in ordered],
            )
        except KeyError:
            return error_response(404, "NOT_FOUND", "Task list not found.")
        return web.json_response(
            {
                "ok": True,
                "count": len(updated),
            }
        )


class TaskDetailView(BaseView):
    """``PATCH /api/tasks/{id}`` — update a task.

    ``DELETE /api/tasks/{id}`` — delete a task.
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        task_id = self.match("id")
        body = await self.body()
        svc = self.svc(task_service_key)
        task = await svc.update_task(
            task_id,
            actor_user_id=ctx.user_id,
            title=body.get("title"),
            description=body.get("description"),
            status=body.get("status"),
            due_date=body.get("due_date"),
            assignees=body.get("assignees"),
            position=body.get("position"),
        )
        return web.json_response(_task_dict(task))

    async def delete(self) -> web.Response:
        ctx = self.user
        task_id = self.match("id")
        svc = self.svc(task_service_key)
        await svc.delete_task(task_id, actor_user_id=ctx.user_id)
        return web.json_response({"ok": True})


class TaskArchiveView(BaseView):
    """``POST /api/tasks/{id}/archive`` — archive (soft-hide) a task.

    ``DELETE /api/tasks/{id}/archive`` — unarchive.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        task_id = self.match("id")
        svc = self.svc(task_service_key)
        task = await svc.archive_task(task_id, actor_user_id=ctx.user_id)
        return web.json_response(_task_dict(task))

    async def delete(self) -> web.Response:
        ctx = self.user
        task_id = self.match("id")
        svc = self.svc(task_service_key)
        task = await svc.unarchive_task(task_id, actor_user_id=ctx.user_id)
        return web.json_response(_task_dict(task))


class TaskCommentCollectionView(BaseView):
    """``GET /api/tasks/{id}/comments`` + ``POST /api/tasks/{id}/comments`` (§23.68)."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        svc = self.svc(task_service_key)
        comments = await svc.list_comments(self.match("id"))
        return web.json_response(
            [
                {
                    "id": c.id,
                    "task_id": c.task_id,
                    "author": c.author,
                    "content": c.content,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in comments
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(task_service_key)
        comment = await svc.add_comment(
            self.match("id"),
            author_user_id=ctx.user_id,
            content=str(body.get("content") or ""),
        )
        return web.json_response(
            {
                "id": comment.id,
                "task_id": comment.task_id,
                "author": comment.author,
                "content": comment.content,
                "created_at": comment.created_at.isoformat(),
            },
            status=201,
        )


class TaskCommentDetailView(BaseView):
    """``DELETE /api/tasks/{id}/comments/{comment_id}`` — author/admin only."""

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(task_service_key)
        await svc.delete_comment(
            self.match("comment_id"),
            actor_user_id=ctx.user_id,
        )
        return web.json_response({"ok": True})


class TaskAttachmentCollectionView(BaseView):
    """``GET /api/tasks/{id}/attachments`` + ``POST /api/tasks/{id}/attachments``.

    Uploads are already handled by ``POST /api/media/upload``; this route
    just records the resulting ``url`` + metadata on the task row.
    """

    async def get(self) -> web.Response:
        self.user
        svc = self.svc(task_service_key)
        atts = await svc.list_attachments(self.match("id"))
        return web.json_response(
            [
                {
                    "id": a.id,
                    "task_id": a.task_id,
                    "uploaded_by": a.uploaded_by,
                    "url": a.url,
                    "filename": a.filename,
                    "mime": a.mime,
                    "size_bytes": a.size_bytes,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in atts
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        svc = self.svc(task_service_key)
        attachment = await svc.add_attachment(
            self.match("id"),
            uploaded_by=ctx.user_id,
            url=str(body.get("url") or ""),
            filename=str(body.get("filename") or ""),
            mime=str(body.get("mime") or "application/octet-stream"),
            size_bytes=int(body.get("size_bytes") or 0),
        )
        return web.json_response(
            {
                "id": attachment.id,
                "task_id": attachment.task_id,
                "uploaded_by": attachment.uploaded_by,
                "url": attachment.url,
                "filename": attachment.filename,
                "mime": attachment.mime,
                "size_bytes": attachment.size_bytes,
                "created_at": attachment.created_at.isoformat(),
            },
            status=201,
        )


class TaskAttachmentDetailView(BaseView):
    """``DELETE /api/tasks/{id}/attachments/{attachment_id}``."""

    async def delete(self) -> web.Response:
        self.user
        svc = self.svc(task_service_key)
        await svc.delete_attachment(self.match("attachment_id"))
        return web.json_response({"ok": True})


# ─── Space-scoped task routes (§15) ──────────────────────────────────────


class _SpaceTasksBase(BaseView):
    """Shared membership-check helper for every space-task view."""

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(K.space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None


class SpaceTaskListCollectionView(_SpaceTasksBase):
    """``GET`` / ``POST /api/spaces/{id}/tasks/lists``."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        lists = await svc.list_lists(space_id)
        return web.json_response(
            [
                {"id": lst.id, "name": lst.name, "created_by": lst.created_by}
                for lst in lists
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        svc = self.svc(K.space_task_service_key)
        lst = await svc.create_list(
            space_id=space_id,
            name=str(body.get("name") or ""),
            created_by=ctx.user_id,
        )
        return web.json_response(
            {"id": lst.id, "name": lst.name, "created_by": lst.created_by},
            status=201,
        )


class SpaceTaskListDetailView(_SpaceTasksBase):
    """``PATCH`` / ``DELETE /api/spaces/{id}/tasks/lists/{lid}``."""

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        svc = self.svc(K.space_task_service_key)
        lst = await svc.rename_list(
            self.match("lid"),
            name=str(body.get("name") or ""),
        )
        return web.json_response(
            {"id": lst.id, "name": lst.name, "created_by": lst.created_by},
        )

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        await svc.delete_list(self.match("lid"))
        return web.json_response({"ok": True})


class SpaceTaskListTasksView(_SpaceTasksBase):
    """``GET`` / ``POST /api/spaces/{id}/tasks/lists/{lid}/tasks``."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        rows = await svc.list_tasks_by_list(self.match("lid"))
        return web.json_response([_task_dict(t) for t in rows])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        svc = self.svc(K.space_task_service_key)
        task = await svc.create_task(
            space_id=space_id,
            list_id=self.match("lid"),
            title=str(body.get("title") or ""),
            created_by=ctx.user_id,
            description=body.get("description"),
            due_date=body.get("due_date"),
            assignees=body.get("assignees"),
        )
        return web.json_response(_task_dict(task), status=201)


class SpaceTaskDetailView(_SpaceTasksBase):
    """``PATCH`` / ``DELETE /api/spaces/{id}/tasks/{tid}``."""

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        svc = self.svc(K.space_task_service_key)
        try:
            task = await svc.update_task(
                self.match("tid"),
                actor_user_id=ctx.user_id,
                title=body.get("title"),
                description=body.get("description"),
                status=body.get("status"),
                due_date=body.get("due_date"),
                assignees=body.get("assignees"),
                position=body.get("position"),
            )
        except KeyError:
            return error_response(404, "NOT_FOUND", "Task not found.")
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_task_dict(task))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        try:
            await svc.delete_task(self.match("tid"))
        except KeyError:
            return error_response(404, "NOT_FOUND", "Task not found.")
        return web.json_response({"ok": True})


class SpaceTaskArchiveView(_SpaceTasksBase):
    """``POST`` / ``DELETE /api/spaces/{id}/tasks/{tid}/archive``."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        try:
            task = await svc.archive_task(self.match("tid"), actor_user_id=ctx.user_id)
        except KeyError:
            return error_response(404, "NOT_FOUND", "Task not found.")
        return web.json_response(_task_dict(task))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        svc = self.svc(K.space_task_service_key)
        try:
            task = await svc.unarchive_task(
                self.match("tid"), actor_user_id=ctx.user_id
            )
        except KeyError:
            return error_response(404, "NOT_FOUND", "Task not found.")
        return web.json_response(_task_dict(task))
