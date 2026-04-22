# Tasks

Task lists and individual tasks inside a space. Each HFS keeps a local
copy; edits federate to every member instance.

## Scope

- **HFS**: both sides. Creates, assigns, completes, and deletes tasks;
  broadcasts each edit as a federation event.
- **GFS**: uninvolved.

## Event types

`SPACE_TASK_CREATED`, `SPACE_TASK_UPDATED`, `SPACE_TASK_DELETED`,
plus the task-list wrappers carried implicitly inside the task's
`list_id` field (lists are created locally and their creation is
announced via a wrapper update).

## Flow — create + assign

```mermaid
sequenceDiagram
    autonumber
    participant U as User (HFS A)
    participant A as HFS A
    participant B as HFS B (peer)
    U->>A: POST /api/spaces/{id}/tasks/lists/{lid}/tasks<br/>(title, assignees=[u1, u2])
    A->>A: persist Task row
    A->>A: emit TaskCreated<br/>+ TaskAssigned × (assignees − creator)
    A->>B: SPACE_TASK_CREATED<br/>(+ assignee ids)
    B->>B: persist Task,<br/>push to connected clients
```

## Flow — complete a recurring task

A task with a non-empty `rrule` re-spawns on completion: the original
task flips to `done`, and a fresh successor is inserted with the next
due date computed from the rule.

```mermaid
sequenceDiagram
    autonumber
    participant U as User (HFS A)
    participant A as HFS A
    participant B as HFS B
    U->>A: PATCH /api/tasks/{id}<br/>(status=done)
    A->>A: mark done,<br/>compute next occurrence
    A->>A: insert successor task
    A->>B: SPACE_TASK_UPDATED (status=done)
    A->>B: SPACE_TASK_CREATED (successor)
    Note over B: assignees on the new<br/>successor get TaskAssigned<br/>locally on B
```

## Edit & delete

`_UPDATED` carries the whole Task row. `_DELETED` carries the
`task_id` only. Both are idempotent: the receiver upserts by ID.

Priority, due date, checklist items, and attachments ride on the
`task_updated` envelope as a diff on the full payload — pragmatic,
not minimal-diff, because tasks are small.

## Attachments

Attachments federate as references, not blobs: the event carries a
`media_id` resolvable via the owning HFS's `/api/media/{id}` endpoint.
For private spaces, the media endpoint returns 404 to unauthenticated
requests; authenticated cross-HFS requests are bearer-authorised via
the space membership. This keeps large files off the federation
envelopes but still lets every member see them.

## Implementation

- `socialhome/services/task_service.py`,
  `socialhome/services/space_task_service.py`.
- `socialhome/services/federation_inbound/space_content.py` —
  `SPACE_TASK_*` handlers.
- `socialhome/repositories/task_repo.py` — `SqliteTaskRepo`,
  `SqliteSpaceTaskRepo`.
- `socialhome/utils/rrule.py` — recurrence rule parser.
- `socialhome/routes/task_routes.py` — REST endpoints.

## Spec references

§13.6 (space tasks),
§13.6.4 (recurrence).
