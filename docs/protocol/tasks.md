# Tasks

Task lists and individual tasks inside a space. Each HFS keeps a local
copy; edits federate to every member instance.

## Scope

- **HFS**: both sides. Creates, assigns, completes, and deletes tasks;
  broadcasts each edit as a federation event.
- **GFS**: uninvolved.

## Event types

`SPACE_TASK_CREATED`, `SPACE_TASK_UPDATED`, `SPACE_TASK_DELETED`, and
(v_40) the list lifecycle `SPACE_TASK_LIST_CREATED`,
`SPACE_TASK_LIST_UPDATED` (rename) and `SPACE_TASK_LIST_DELETED`. A
receiver stores a task only under a list it already holds in that space,
so a list federates before (and independently of) its tasks; the
`task_lists` §25.6 sync resource streams ahead of `tasks`, and the
`SPACE_SYNC_RESUME` replay sends lists created since `since` before the
tasks. A task that overtakes its list's create is refused (WARNING) and
healed by the list's event or the sync stream — a task never creates its
list. Deleting a list cascades to its tasks on every household. All
events fan out with `broadcast_to_space_members` to member households
only, every field inside the sealed payload.

**Legacy list ids.** `SPACE_TASK_LIST_CREATED` is new in v_40 and every
v_40 household mints owner-bound list ids, so a live (or resume-replayed)
create of a legacy uuid4 list id is refused at WARNING, and a member
household's `task_lists` sync stream may only add bound ids. A pre-v_40
list reaches a member household only through its **host's** sync stream
(taken whole) — otherwise a household seated in two spaces could
first-come squat one space's legacy list id under the other.

**Replay and deletes (known gaps).** The resume replay sends lists
*created* at or after `since` (second precision, so `>=`); a rename is
not replayed — the next `task_lists` sync from the host heals it. There
are no list-delete tombstones yet: a household that missed a delete can
re-announce the list (the same holds for tasks today).

**Received text is sanitised** like REST input (control / bidi /
spoofing characters removed) and cut to fit — title ≤ 200, list name
≤ 100, description ≤ 5000, labels ≤ 10 × 32, assignees ≤ 10 strings; a
title or list name with nothing visible left is dropped at WARNING. A
`position` that is not a finite integer in the signed 64-bit range
keeps the held value.

List payload: `{id, space_id, name, created_by}` (`_UPDATED` carries
`{id, space_id, name}`, `_DELETED` `{id, space_id}`). New list ids are
owner-bound to `created_by` (kind `space-task-list`); writes are
collaborative like tasks — any writer household may rename or delete a
list, a new list's `created_by` must be a member seated on the sender,
and a rename keeps the list's original creator.

## Flow — create + assign

```mermaid
sequenceDiagram
    autonumber
    participant U as User (HFS A)
    participant A as HFS A
    participant B as HFS B (peer)
    U->>A: POST /api/spaces/{id}/tasks/lists<br/>(name)
    A->>A: persist list (owner-bound id),<br/>emit TaskListCreated
    A->>B: SPACE_TASK_LIST_CREATED<br/>(sealed {id, name, created_by})
    B->>B: persist list in the space<br/>emit TaskListCreated(origin_instance_id=A)
    U->>A: POST /api/spaces/{id}/tasks/lists/{lid}/tasks<br/>(title, assignees=[u1, u2])
    A->>A: persist Task row
    A->>A: emit TaskCreated<br/>+ TaskAssigned × (assignees − creator)
    A->>B: SPACE_TASK_CREATED<br/>(sealed wire dict, + assignee ids)
    B->>B: task_from_wire_dict(payload, existing=None)<br/>persist Task
    B->>B: emit TaskCreated(origin_instance_id=A)<br/>→ task.created WS frame
    Note over B: TaskFederationOutbound skips<br/>an event with origin_instance_id<br/>(no echo back to A)
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

`_CREATED` / `_UPDATED` carry the whole task in the wire form below;
`_DELETED` carries `id`, `list_id` and `space_id`. All are idempotent:
the receiver upserts / deletes by id (scoped to the path space). After
a write the receiver publishes `TaskCreated` / `TaskUpdated` /
`TaskDeleted` with `origin_instance_id` set to the sending household,
so its local members see the change live (`task.*` WS frame to the
space) and the outbound bridge never echoes it back. Reordering a list
(`POST /api/spaces/{id}/tasks/lists/{lid}/reorder`) federates as one
`SPACE_TASK_UPDATED` per moved task.

## Wire payload (v_40)

One codec — `task_to_wire_dict(task, space_id)` /
`task_from_wire_dict(payload, existing=)` in `socialhome/domain/task.py`
— serves the live `SPACE_TASK_*` events, the `tasks` /
`tasks_archived` §25.6 sync records and the `SPACE_SYNC_RESUME` replay,
so they cannot drift. Every field rides inside the sealed payload:

| Field | Type | Notes |
|---|---|---|
| `id`, `list_id`, `space_id` | string | required (`id`, `list_id`, `title`) |
| `title`, `description` | string / string or null | |
| `status` | `todo` / `in_progress` / `done` | |
| `position` | int | ordering key within the list |
| `created_by` | user id | owner-bound id input (v_36) |
| `created_at`, `updated_at` | ISO 8601 | |
| `due_date` | `YYYY-MM-DD` or null | |
| `assignees` | array of user ids | |
| `recurrence` | `{rrule, last_spawned_at}` or null | |
| `recurrence_parent_id` | string or null | |
| `archived_at` | ISO 8601 or null | |
| `priority` | `low` / `medium` / `high` / `urgent` or null | **always present** (v_40) |
| `labels` | array of ≤ 10 strings of ≤ 32 chars | v_40; normalised on receipt |

**Merge rule.** A key that is absent keeps the value already held
(the default for a new row); a key that is present is authoritative —
`null` / `[]` clears it. An unknown `priority` value (from a newer
build) keeps the held one.

**v_39 senders.** A payload without the `priority` key comes from a
v_39 household. Its inbound path stored remote tasks without
`due_date`, `archived_at` or the recurrence, so the values it sends
back in an edit are not its user's intent. For a task already held,
those fields — and `priority` / `labels`, which it never knew — count
as absent, so a v_39 edit (title, status, assignees, …) applies without
wiping them. For a new task there is nothing to wipe and its fields are
taken as sent.

## Attachments

Attachments federate as references, not blobs: the event carries a
`media_id` resolvable via the owning HFS's `/api/media/{id}` endpoint.
For private spaces, the media endpoint returns 404 to unauthenticated
requests; authenticated cross-HFS requests are bearer-authorised via
the space membership. This keeps large files off the federation
envelopes but still lets every member see them.

## Creator-bound ids (v_36)

A new space task's id is owner-bound to its `created_by` and space
(`federation/owner_bound_id.py`, kind `space-task`): the live create
handler (for a task not held yet) and the §25.6 sync receiver refuse
a bound id claimed for anybody else, so only the creator's household
can announce it. Tasks are collaborative, so only the
attribution was at stake; edits keep it. Legacy (uuid4) ids keep the
first-come rule. See [`spaces.md`](./spaces.md) and the v_36 row in
[`capabilities.md`](./capabilities.md).

## Implementation

- `socialhome/domain/task.py` — `Task`, `TaskPriority`,
  `normalize_labels`, `UNSET`, and the wire codec
  (`task_to_wire_dict` / `task_from_wire_dict`).
- `socialhome/services/task_service.py` — `TaskService` (household) and
  `SpaceTaskService` (space), including the minimal RRULE evaluator.
- `socialhome/services/task_federation_outbound.py` — `SPACE_TASK_*`
  fan-out via `broadcast_to_space_members`.
- `socialhome/services/federation_inbound/space_content.py` —
  `SPACE_TASK_*` inbound handlers.
- `socialhome/federation/sync/space/exporters/tasks.py`,
  `tasks_archived.py`, `receiver.py`, `resume.py` — §25.6 sync and the
  resume replay.
- `socialhome/repositories/task_repo.py` — `SqliteTaskRepo`,
  `SqliteSpaceTaskRepo`.
- `socialhome/routes/tasks.py` — REST endpoints.

## Spec references

§13.6 (space tasks),
§13.6.4 (recurrence).
