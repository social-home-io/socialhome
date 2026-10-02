/**
 * TaskPage — the household's task lists, as the Organize "Tasks" tab
 * (§23.54 / §15); ``TasksView`` is the same page for any task scope (a
 * space's Tasks tab renders it under a space scope).
 *
 * The sidebar holds the list roster (pick, add, ⋯ rename / delete);
 * the main pane shows the active list as a **board** (``TaskBoard``:
 * To do · In progress · Done columns, drag or keyboard to move) or as
 * a compact **list** grouped by status, with the done archive under a
 * "Done (N) · Clear all" divider. Board | List is remembered per list.
 * Built on the shared Organize primitives so it reads like Shopping:
 *
 * - ``useLoad`` twice — the roster, then the active list's tasks (keyed
 *   by list id). Both render cached data at once on a revisit and
 *   revalidate behind it; a failed first load shows ``LoadErrorState``
 *   with Retry, never the empty state.
 * - List view: ``CheckToggle`` (unticked → done, ticked → to do); drag a
 *   row onto another status section to move it; the keyboard path is
 *   the detail dialog's status picker.
 * - Delete and "Clear all" are instant with Undo (deferred DELETE).
 *   Deleting a whole list asks first — it takes every task with it —
 *   and then also offers Undo.
 * - ``TaskDetailDialog`` (both views) edits every field, or shows the
 *   task read-only when the viewer may not change it.
 *
 * What it works on and who may change what come from the task scope
 * (``features/tasks/scope.ts``); the default is the household.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { CheckToggle } from '@/components/CheckToggle'
import { RowActionButton, ROW_REVEAL_HOST } from '@/components/RowActionButton'
import { QuickAddBar } from '@/components/QuickAddBar'
import { ListSkeleton } from '@/components/Skeleton'
import { LoadErrorState } from '@/components/LoadErrorState'
import { OverflowMenu } from '@/components/OverflowMenu'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { useTitle } from '@/store/pageTitle'
import { isOne, type TaskStatus } from '@/store/tasks'
import { locale, t } from '@/i18n/i18n'
import { useLoad } from '@/utils/useLoad'
import { pendingDeletes } from '@/utils/undoableDelete'
import { OrganizeSectionHeader } from '@/features/organize/shared/OrganizeSectionHeader'
import { ArchiveDivider } from '@/features/organize/shared/ArchiveDivider'
import { DropPad } from '@/features/organize/shared/DropPad'
import { useDragBuckets, type DragItemProps } from '@/features/organize/shared/useDragBuckets'
import type { TaskItem, TaskListEntry } from '@/types'
import { useNarrow } from '@/features/timetable/useNarrow'
import { dueLabel } from './dueLabel'
import { useTaskScope } from './scope'
import { TaskBoard } from './board/TaskBoard'
import { TaskDetailDialog } from './board/TaskDetailDialog'
import { joinNames, statusLabel } from './board/TaskCard'
import { PriorityIcon } from './board/priority'
import { collectLabels, labelColorClass } from './board/labels'
import { sortTasks } from './board/moves'
import { useTaskView, type TaskView } from './board/viewPrefs'

/** Marks a task-row drag on ``dataTransfer`` (see ``useDragBuckets``). */
const DRAG_TASK_MIME = 'application/x-sh-task'

const OPEN_STATUSES: TaskStatus[] = ['todo', 'in_progress']

/** The backend's list-name cap. */
const LIST_NAME_MAX = 100

/** Labels a list row shows before "+N". */
const ROW_LABELS_SHOWN = 2

/** ``t()`` with a count: the ``_one`` key for exactly one.
 *  For ``i18n:check``: t('tasks.lists_count') t('tasks.lists_count_one') */
function tn(key: string, n: number, params: Record<string, string> = {}): string {
  return t(isOne(n) ? `${key}_one` : key, { ...params, n: String(n) })
}

function errText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** Error toast for a failed task write: a 403 (not allowed to change
 *  this task) says so in the UI language; anything else is
 *  "<what failed>: <detail>". */
function failToast(key: string, err: unknown) {
  const forbidden = (err as { status?: unknown } | null)?.status === 403
  showToast(forbidden ? t('tasks.error.forbidden') : t(key, { error: errText(err) }), 'error')
}

/** No drag for a task the user may not change. */
const NO_DRAG = {
  draggable: false,
  onDragStart: (e: DragEvent) => e.preventDefault(),
  onDragEnd: () => {},
}

/** After a keyboard delete, focus the next row's tick (or the add field). */
function focusNeighbour(li: HTMLElement | null) {
  const next = (li?.nextElementSibling ?? li?.previousElementSibling) as HTMLElement | null
  requestAnimationFrame(() => {
    const target = next?.isConnected
      ? next.querySelector<HTMLElement>('.sh-check-toggle')
      : document.querySelector<HTMLElement>('.sh-tasks-add input')
    target?.focus()
  })
}

function focusTask(id: string) {
  requestAnimationFrame(() => {
    const row = Array.from(document.querySelectorAll<HTMLElement>('[data-task-id]'))
      .find(el => el.dataset.taskId === id)
    row?.querySelector<HTMLElement>('.sh-check-toggle')?.focus()
  })
}

export default function TaskPage() {
  useTitle(t('tasks.title'))
  return <TasksView />
}

/** The tasks page for the current task scope. */
export function TasksView() {
  const scope = useTaskScope()
  const { store, canWrite } = scope
  const addRef = useRef<HTMLInputElement | null>(null)
  const newListRef = useRef<HTMLInputElement | null>(null)
  const [draft, setDraft] = useState('')
  const [listDraft, setListDraft] = useState('')
  const [editing, setEditing] = useState<TaskItem | null>(null)
  // Phones: a compact list picker replaces the lists card, so the tasks
  // start near the top instead of below the fold.
  const narrow = useNarrow()

  useEffect(() => { scope.loadPeople() }, [scope])

  // The store revalidates every loaded list itself after a WebSocket
  // reconnect, so neither load re-runs on one.
  const listsLoad = useLoad(() => store.loadLists(), {
    key: scope.key,
    cached: store.listsLoaded.value,
    revalidateOnReconnect: false,
  })

  const visibleLists = store.visibleLists.value
  const chosen = store.activeListId.value
  // The remembered list, else the first one (also after it was deleted).
  const activeId = visibleLists.some(l => l.id === chosen) ? chosen : visibleLists[0]?.id ?? null
  const activeList = visibleLists.find(l => l.id === activeId) ?? null
  const [view, setView] = useTaskView(activeId)

  const tasksLoad = useLoad(
    () => (activeId ? store.loadList(activeId) : Promise.resolve()),
    {
      key: `${scope.key}:${activeId ?? ''}`,
      cached: activeId ? store.loadedListIds.value.has(activeId) : true,
      revalidateOnReconnect: false,
      onRecovered: () => requestAnimationFrame(() => addRef.current?.focus()),
    },
  )

  const drag = useDragBuckets<TaskStatus>({
    mime: DRAG_TASK_MIME,
    onDrop: (id, status) => { void changeStatus(id, status) },
  })

  const hidden = pendingDeletes.value
  const visibleTasks = sortTasks((activeId ? store.tasksByList.value[activeId] ?? [] : [])
    .filter(x => !hidden.has(x.id)))
  const byStatus = (s: TaskStatus) => visibleTasks.filter(x => x.status === s)
  const doneTasks = byStatus('done')
  const openTasks = visibleTasks.length - doneTasks.length
  const people = scope.people()
  const userName = scope.nameOf

  async function changeStatus(id: string, status: TaskStatus) {
    try {
      await store.setStatus(id, status)
    } catch (err) {
      failToast('tasks.error.update', err)
    }
  }

  async function toggle(id: string) {
    try {
      await store.toggleDone(id)
    } catch (err) {
      failToast('tasks.error.update', err)
    }
  }

  const selectList = (id: string) => { store.activeListId.value = id }

  const addTask = async () => {
    const title = draft.trim()
    if (!title || !activeId) return
    try {
      await store.createTask(activeId, title)
      setDraft('')
    } catch (err) {
      showToast(t('tasks.error.add', { error: errText(err) }), 'error')
    }
  }

  /** Create a list and open it; ``false`` when it failed (toasted). */
  const addListNamed = async (raw: string, focusAdd = true): Promise<boolean> => {
    const name = raw.trim()
    if (!name) return false
    try {
      await store.createList(name)
      if (focusAdd) requestAnimationFrame(() => addRef.current?.focus())
      return true
    } catch (err) {
      showToast(t('tasks.error.add_list', { error: errText(err) }), 'error')
      return false
    }
  }

  const addList = async () => {
    if (await addListNamed(listDraft)) setListDraft('')
  }

  const renameList = async (list: TaskListEntry, name: string) => {
    const next = name.trim()
    if (!next || next === list.name) return
    try {
      await store.renameList(list.id, next)
    } catch (err) {
      showToast(t('tasks.error.rename', { error: errText(err) }), 'error')
    }
  }

  /** A whole list is many tasks at once — ask first, then still offer Undo. */
  const deleteList = async (list: TaskListEntry) => {
    const ok = await confirmDialog(t('tasks.list.delete_confirm', { name: list.name }), {
      destructive: true,
      confirmLabel: t('tasks.list.delete'),
    })
    if (ok) store.removeList(list)
  }

  const deleteTask = (task: TaskItem) => {
    store.removeTask(task, { onUndone: () => focusTask(task.id) })
  }

  /** Household tasks: creator, assignee or admin (the server's rule);
   *  space tasks: any writable member. */
  const editable = (x: TaskItem) => canWrite && scope.canEdit(x)
  const clearable = doneTasks.filter(editable)

  const clearDone = () => {
    if (!activeId) return
    const first = clearable[0]?.id
    store.clearDone(activeId, {
      canDelete: editable,
      onUndone: () => { if (first) focusTask(first) },
    })
    addRef.current?.focus()
  }

  const header = (
    <OrganizeSectionHeader
      title={activeList?.name}
      // The list picker (phones, and the board) already shows the name.
      hideTitle={narrow || view === 'board'}
      // No "0 open · 0 done" over an empty list's "All caught up".
      counts={activeList && tasksLoad.state === 'ready' && visibleTasks.length > 0 ? [
        { label: t('tasks.counts.open', { n: String(openTasks) }), tone: 'open' },
        { label: t('tasks.counts.done', { n: String(doneTasks.length) }), tone: 'done' },
      ] : []}
    >
      {activeList && (
        <ChipRadioGroup<TaskView>
          variant="segmented"
          class="sh-tasks-view-toggle"
          ariaLabel={t('tasks.board.view')}
          value={view}
          onChange={setView}
          options={[
            { value: 'board', label: t('tasks.board.view_board') },
            { value: 'list', label: t('tasks.board.view_list') },
          ]}
        />
      )}
    </OrganizeSectionHeader>
  )

  if (listsLoad.state !== 'ready') {
    return (
      <div class="sh-tasks-page">
        {listsLoad.state === 'error'
          ? <LoadErrorState message={t('tasks.load_failed')} onRetry={listsLoad.retry} />
          : <ListSkeleton variant="board" rows={6} label={t('tasks.loading')} />}
      </div>
    )
  }

  const rowProps = (task: TaskItem) => ({
    task,
    editable: editable(task),
    readOnlyReason: scope.readOnlyReason(),
    // A read-only board says so once, above it — no lock on every row.
    showLock: canWrite,
    dragging: drag.draggingId === task.id,
    dragProps: editable(task) ? drag.itemProps(task.id) : NO_DRAG,
    userName,
    onToggle: () => { void toggle(task.id) },
    onEdit: () => setEditing(task),
    onDelete: () => deleteTask(task),
  })

  const boardView = view === 'board'

  return (
    <div class={`sh-tasks${boardView ? ' sh-tasks--board' : ''}`}>
      {/* The board takes the full width: its list roster is the compact
        * picker at every size. The list view keeps the sidebar on
        * desktop. */}
      {narrow || boardView ? (
        <ListPicker
          lists={visibleLists}
          active={activeList}
          canWrite={canWrite}
          newListRef={newListRef}
          onSelect={selectList}
          onCreate={(name) => addListNamed(name, false)}
          onRename={(l, name) => void renameList(l, name)}
          onDelete={(l) => void deleteList(l)}
        />
      ) : (
        <aside class="sh-tasks-sidebar" aria-label={t('tasks.lists')}>
          <header class="sh-tasks-sidebar-header">
            <h3>{t('tasks.lists')}</h3>
            <span class="sh-tasks-sidebar-header__count">
              {tn('tasks.lists_count', visibleLists.length)}
            </span>
          </header>
          {visibleLists.map(l => (
            <ListRow
              key={l.id}
              list={l}
              active={l.id === activeId}
              canWrite={canWrite}
              onSelect={() => selectList(l.id)}
              onRename={(name) => void renameList(l, name)}
              onDelete={() => void deleteList(l)}
            />
          ))}
          {canWrite && <QuickAddBar
            class="sh-tasks-new-list"
            value={listDraft}
            inputRef={newListRef}
            placeholder={t('tasks.new_list_placeholder')}
            inputLabel={t('tasks.new_list')}
            submitLabel={t('tasks.add_list')}
            onValueChange={setListDraft}
            onSubmit={() => { void addList() }}
            inputProps={{ maxLength: LIST_NAME_MAX }}
          />}
        </aside>
      )}
      <div class="sh-tasks-content">
        {!activeList ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">📋</div>
            <h3>{t('tasks.no_lists.title')}</h3>
            <p>{canWrite ? t('tasks.no_lists.body') : t('tasks.board.no_lists_read_only')}</p>
            {canWrite && (
              <div class="sh-empty-state__cta-row">
                <Button onClick={() => newListRef.current?.focus()}>
                  {t('tasks.no_lists.cta')}
                </Button>
              </div>
            )}
          </div>
        ) : (
          <>
            {header}
            {!canWrite && (
              <p class="sh-task-edit__readonly sh-tasks-readonly" role="note">
                <span aria-hidden="true">🔒 </span>{scope.readOnlyReason()}
              </p>
            )}
            {/* After a failed load only the error and its Retry show. */}
            {!boardView && canWrite && tasksLoad.state !== 'error' && <QuickAddBar
              class="sh-tasks-add"
              value={draft}
              inputRef={addRef}
              placeholder={t('tasks.add_placeholder')}
              inputLabel={t('tasks.add_label', { name: activeList.name })}
              submitLabel={t('tasks.add')}
              onValueChange={setDraft}
              onSubmit={() => { void addTask() }}
              inputProps={{ maxLength: 200 }}
            />}
            {tasksLoad.stale && (
              <p class="sh-tasks-stale" role="status">
                {t('tasks.stale')}{' '}
                <button type="button" class="sh-link" onClick={tasksLoad.retry}>
                  {t('common.retry')}
                </button>
              </p>
            )}
            {tasksLoad.state === 'error' && (
              <LoadErrorState message={t('tasks.load_tasks_failed')} onRetry={tasksLoad.retry} />
            )}
            {tasksLoad.state === 'loading' && (
              <ListSkeleton variant={boardView ? 'board' : 'list'} rows={4} label={t('tasks.loading')} />
            )}
            {tasksLoad.state === 'ready' && boardView && (
              <TaskBoard key={activeList.id} listId={activeList.id} tasks={visibleTasks} onOpen={setEditing} />
            )}
            {tasksLoad.state === 'ready' && !boardView && visibleTasks.length === 0 && (
              <div class="sh-empty-state">
                <div aria-hidden="true">✅</div>
                <h3>{t('tasks.empty.title')}</h3>
                <p>{canWrite ? t('tasks.empty.body') : t('tasks.board.empty_read_only')}</p>
              </div>
            )}
            {tasksLoad.state === 'ready' && !boardView && visibleTasks.length > 0 && (
              <>
                {OPEN_STATUSES.map(status => {
                  const rows = byStatus(status)
                  // An empty open section only shows while dragging, as a target.
                  if (rows.length === 0 && drag.draggingId === null) return null
                  const over = drag.overBucket === status
                  return (
                    <section
                      key={status}
                      class={`sh-task-group sh-task-group--${status}${over ? ' sh-task-group--drop-target' : ''}`}
                      aria-labelledby={`sh-task-group-${status}`}
                      {...drag.bucketProps(status)}
                    >
                      <header class="sh-task-group__header">
                        <span class={`sh-task-group__dot sh-task-group__dot--${status}`} aria-hidden="true" />
                        <h3 class="sh-task-group__name" id={`sh-task-group-${status}`}>
                          {statusLabel(status)}
                        </h3>
                        <span class="sh-task-group__count">{rows.length}</span>
                      </header>
                      {rows.length === 0 ? (
                        <DropPad active={over} label={t('tasks.drop_into', { name: statusLabel(status) })} />
                      ) : (
                        <ul class="sh-task-items sh-list-card">
                          {rows.map(x => <TaskRow key={x.id} {...rowProps(x)} />)}
                        </ul>
                      )}
                    </section>
                  )
                })}
                {(doneTasks.length > 0 || drag.draggingId !== null) && (
                  <section
                    class={`sh-task-group sh-task-group--done sh-task-group--archive${drag.overBucket === 'done' ? ' sh-task-group--drop-target' : ''}`}
                    aria-label={statusLabel('done')}
                    {...drag.bucketProps('done')}
                  >
                    {doneTasks.length === 0 ? (
                      <DropPad active={drag.overBucket === 'done'} label={t('tasks.drop_done')} />
                    ) : (
                      <>
                        <ArchiveDivider
                          class="sh-tasks-done-divider"
                          label={t('tasks.done_heading', { n: String(doneTasks.length) })}
                          actionLabel={clearable.length > 0 ? t('tasks.clear_all') : undefined}
                          actionAriaLabel={t('tasks.clear_all_label')}
                          onAction={clearable.length > 0 ? clearDone : undefined}
                        />
                        <ul class="sh-task-items sh-list-card sh-list-card--moss">
                          {doneTasks.map(x => <TaskRow key={x.id} {...rowProps(x)} />)}
                        </ul>
                      </>
                    )}
                  </section>
                )}
              </>
            )}
          </>
        )}
      </div>

      {editing && (
        <TaskDetailDialog
          task={editing}
          editable={editable(store.findTask(editing.id) ?? editing)}
          readOnlyReason={scope.readOnlyReason()}
          people={people}
          nameOf={userName}
          labelSuggestions={collectLabels(visibleTasks, locale.value)}
          spaceId={scope.spaceId ?? null}
          onClose={() => setEditing(null)}
          onSave={async (patch) => {
            if (!store.findTask(editing.id)) {
              showToast(t('tasks.error.deleted'), 'info')
              setEditing(null)
              return
            }
            try {
              await store.patchTask(editing.id, patch)
              setEditing(null)
            } catch (err) {
              failToast('tasks.error.save', err)
            }
          }}
        />
      )}
    </div>
  )
}

// ─── Task row ─────────────────────────────────────────────────────────

interface TaskRowProps {
  task: TaskItem
  /** The user may change it (else: inert tick with the reason, no ✕). */
  editable: boolean
  readOnlyReason: string
  /** 🔒 on a row the user can't change (off on a read-only board). */
  showLock: boolean
  dragging: boolean
  dragProps: DragItemProps
  userName: (uid: string) => string
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
}

function TaskRow({
  task, editable, readOnlyReason: reason, showLock, dragging, dragProps, userName, onToggle, onEdit, onDelete,
}: TaskRowProps) {
  const rowRef = useRef<HTMLLIElement | null>(null)
  const done = task.status === 'done'
  const due = task.due_date ? dueLabel(task.due_date) : null
  const people = (task.assignees ?? []).map(userName)
  const hasNotes = !!task.description?.trim()
  const metaId = `sh-task-meta-${task.id}`
  const dueId = `sh-task-due-${task.id}`
  const labels = task.labels ?? []
  const lock = !editable && showLock
  const showMeta = people.length > 0 || hasNotes || lock || labels.length > 0
  const describedBy = [showMeta ? metaId : '', due && !done ? dueId : ''].filter(Boolean).join(' ')
  const readOnlyReason = editable ? undefined : reason
  return (
    <li
      ref={rowRef}
      data-task-id={task.id}
      class={'sh-task-item ' + ROW_REVEAL_HOST
        + (done ? ' sh-task-item--done' : '')
        + (dragging ? ' sh-task-item--dragging' : '')}
      {...dragProps}
    >
      <CheckToggle
        checked={done}
        label={t('tasks.check_label', { title: task.title })}
        disabledReason={readOnlyReason}
        onChange={onToggle}
      />
      {/* The title + byline together are the edit button, so it fills
        * the row height (a 44 px target on touch). */}
      <button
        type="button"
        class="sh-task-item__body"
        aria-label={t('tasks.edit_label', { title: task.title })}
        title={t('tasks.edit_title')}
        aria-describedby={describedBy || undefined}
        onClick={onEdit}
      >
        <span class="sh-task-item__title">
          <PriorityIcon priority={task.priority} />
          <span class="sh-task-item__title-text">{task.title}</span>
        </span>
        {showMeta && (
          <span class="sh-task-item__meta" id={metaId}>
            {labels.length > 0 && (
              <span class="sh-task-item__labels">
                <span class="sr-only">{t('tasks.labels.sr', { names: joinNames(labels, locale.value) })}</span>
                {labels.slice(0, ROW_LABELS_SHOWN).map(l => (
                  <span key={l} class={`sh-task-label ${labelColorClass(l)}`} aria-hidden="true">{l}</span>
                ))}
                {labels.length > ROW_LABELS_SHOWN && (
                  <span class="sh-task-label sh-task-label--more" aria-hidden="true"
                        title={labels.slice(ROW_LABELS_SHOWN).join(', ')}>
                    +{labels.length - ROW_LABELS_SHOWN}
                  </span>
                )}
              </span>
            )}
            {lock && (
              <span class="sh-task-item__lock" title={readOnlyReason}>
                <span aria-hidden="true">🔒</span>
                <span class="sr-only">{t('tasks.read_only')}</span>
              </span>
            )}
            {people.length > 0 && (
              <>
                <span class="sh-task-item__icon" aria-hidden="true">👤</span>
                <span class="sh-task-item__people">
                  <span class="sr-only">{t('tasks.assigned_to')} </span>
                  {joinNames(people, locale.value)}
                </span>
              </>
            )}
            {hasNotes && (
              <span class="sh-task-item__notes" title={t('tasks.has_notes')}>
                <span aria-hidden="true">≡</span>
                <span class="sr-only">{t('tasks.has_notes')}</span>
              </span>
            )}
          </span>
        )}
      </button>
      {/* One due chip; a finished task's due date no longer matters. */}
      {due && !done && (
        <span id={dueId} class={`sh-task-due${due.tone ? ` sh-task-due--${due.tone}` : ''}`}
              title={due.title}>
          {due.short === due.text ? due.text : (
            <>
              {/* Narrow rows show "! Sep 28"; the full text stays for
                * screen readers. */}
              <span class="sh-task-due__full">{due.text}</span>
              <span class="sh-task-due__short" aria-hidden="true">! {due.short}</span>
            </>
          )}
        </span>
      )}
      {editable ? (
        <RowActionButton
          class="sh-task-item__delete"
          label={t('tasks.delete_label', { title: task.title })}
          icon="✕"
          danger
          reveal
          onClick={(e) => {
            const li = rowRef.current
            onDelete()
            if (e.detail === 0) focusNeighbour(li)
          }}
        />
      ) : (
        // Keeps the due chips of all rows in one column.
        <span class="sh-task-item__delete-spacer" aria-hidden="true" />
      )}
    </li>
  )
}

// ─── List roster row ──────────────────────────────────────────────────

function ListRow({
  list, active, canWrite, onSelect, onRename, onDelete,
}: {
  list: TaskListEntry
  active: boolean
  canWrite: boolean
  onSelect: () => void
  onRename: (next: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(list.name)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const btnRef = useRef<HTMLButtonElement | null>(null)
  /** Set once Enter / Escape / blur ended the edit, so the blur that
   *  follows Enter or Escape can't save (again). */
  const doneRef = useRef(false)

  useEffect(() => {
    if (!editing) return
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [editing])

  const start = () => {
    doneRef.current = false
    setDraft(list.name)
    setEditing(true)
  }
  const finish = (save: boolean, refocus: boolean) => {
    if (doneRef.current) return
    doneRef.current = true
    if (save) onRename(draft)
    setEditing(false)
    if (refocus) requestAnimationFrame(() => btnRef.current?.focus())
  }

  if (editing) {
    return (
      <div class="sh-task-list-row sh-task-list-row--editing">
        <input
          ref={inputRef}
          type="text"
          value={draft}
          maxLength={LIST_NAME_MAX}
          aria-label={t('tasks.list.rename_label', { name: list.name })}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true, true) }
            if (e.key === 'Escape') { e.preventDefault(); finish(false, true) }
          }}
          onBlur={() => finish(true, false)}
        />
      </div>
    )
  }

  return (
    <div class={`sh-task-list-row ${ROW_REVEAL_HOST}`}>
      <button
        ref={btnRef}
        type="button"
        class={`sh-task-list-btn${active ? ' sh-task-list-btn--active' : ''}`}
        aria-current={active ? 'true' : undefined}
        onClick={onSelect}
        onDblClick={canWrite ? start : undefined}
      >
        {list.name}
      </button>
      {canWrite && <OverflowMenu
        label={t('tasks.list.menu', { name: list.name })}
        bareTrigger
        triggerClass="sh-icon-btn sh-row-action sh-row-action--reveal"
        items={[
          { label: t('tasks.list.rename'), onSelect: start },
          { label: t('tasks.list.delete'), danger: true, onSelect: onDelete },
        ]}
      >
        <span aria-hidden="true">⋯</span>
      </OverflowMenu>}
    </div>
  )
}

// ─── Phone list picker ────────────────────────────────────────────────

/** Below 640 px: the current list as one button that opens a sheet of
 *  lists (✓ on the active one) plus New / Rename / Delete — instead of
 *  the lists card, which would push the tasks below the fold. With no
 *  lists yet it is just the new-list field.
 *
 *  While the sheet is open a real scrim covers the page: it takes the
 *  tap that closes the sheet, so that tap never lands on the row
 *  underneath. It stays until the press's click arrives (the menu
 *  itself already closed on pointerdown). After Enter / Escape in the
 *  rename or new-list field, focus goes back to the picker. */
function ListPicker({
  lists, active, canWrite, newListRef, onSelect, onCreate, onRename, onDelete,
}: {
  lists: TaskListEntry[]
  active: TaskListEntry | null
  canWrite: boolean
  newListRef: { current: HTMLInputElement | null }
  onSelect: (id: string) => void
  onCreate: (name: string) => Promise<boolean>
  onRename: (list: TaskListEntry, name: string) => void
  onDelete: (list: TaskListEntry) => void
}) {
  const [mode, setMode] = useState<'idle' | 'new' | 'rename'>('idle')
  const [draft, setDraft] = useState('')
  const [open, setOpen] = useState(false)
  const [scrim, setScrim] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const renameRef = useRef<HTMLInputElement | null>(null)
  /** The rename / new-list edit already ended (Enter, Escape or blur). */
  const doneRef = useRef(false)
  /** A press on the scrim is in progress — keep it for its click. */
  const pressRef = useRef(false)
  /** Focus the trigger once we are back in ``idle``. */
  const refocusRef = useRef(false)
  const savingRef = useRef(false)
  const creating = canWrite && (mode === 'new' || lists.length === 0)

  useEffect(() => {
    if (mode === 'new') newListRef.current?.focus()
    if (mode === 'rename') { renameRef.current?.focus(); renameRef.current?.select() }
    if (mode === 'idle' && refocusRef.current) {
      refocusRef.current = false
      rootRef.current?.querySelector<HTMLElement>('.sh-tasks-picker__trigger')?.focus()
    }
  }, [mode, newListRef])

  const enter = (next: 'new' | 'rename', value: string) => {
    doneRef.current = false
    setDraft(value)
    setMode(next)
  }
  const leave = (refocus: boolean) => {
    doneRef.current = true
    refocusRef.current = refocus
    setMode('idle')
  }
  const finishRename = (save: boolean, refocus: boolean) => {
    if (doneRef.current) return
    if (save && active) onRename(active, draft)
    leave(refocus)
  }
  const create = () => {
    if (savingRef.current) return
    savingRef.current = true
    void onCreate(draft).then((ok) => {
      savingRef.current = false
      if (ok) { setDraft(''); leave(true) }
    })
  }

  const onOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) setScrim(true)
    else if (!pressRef.current) setScrim(false)
  }

  return (
    <div class="sh-tasks-picker" ref={rootRef}>
      {scrim && (
        <div
          class="sh-tasks-picker__scrim"
          aria-hidden="true"
          onPointerDown={(e) => {
            e.preventDefault()
            pressRef.current = true
          }}
          onPointerCancel={() => { pressRef.current = false; setScrim(false) }}
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            pressRef.current = false
            setScrim(false)
            setOpen(false)
          }}
        />
      )}
      {mode === 'rename' && active ? (
        <input
          ref={renameRef}
          class="sh-tasks-picker__rename"
          type="text"
          value={draft}
          maxLength={LIST_NAME_MAX}
          aria-label={t('tasks.list.rename_label', { name: active.name })}
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); finishRename(true, true) }
            if (e.key === 'Escape') { e.preventDefault(); finishRename(false, true) }
          }}
          onBlur={() => finishRename(true, false)}
        />
      ) : active && (
        <div class="sh-tasks-picker__row">
        <OverflowMenu
          label={t('tasks.picker.label', { name: active.name })}
          bareTrigger
          open={open}
          onOpenChange={onOpenChange}
          triggerClass="sh-tasks-picker__trigger"
          wrapClass="sh-tasks-picker__wrap"
          menuClass="sh-tasks-picker__menu"
          items={[
            // The picker only picks: a new list first, then the lists.
            // Rename / Delete of the current list live in its own ⋯.
            ...(canWrite ? [
              { key: '__new', label: t('tasks.picker.new'), class: 'sh-tasks-picker__new',
                onSelect: () => enter('new', '') },
            ] : []),
            ...lists.map(l => ({
              key: l.id, label: l.name, checked: l.id === active.id, onSelect: () => onSelect(l.id),
            })),
          ]}
        >
          <span class="sh-tasks-picker__name">{active.name}</span>
          <span class="sh-tasks-picker__caret" aria-hidden="true">▾</span>
        </OverflowMenu>
        {canWrite && (
          <OverflowMenu
            label={t('tasks.list.menu', { name: active.name })}
            bareTrigger
            triggerClass="sh-icon-btn sh-row-action sh-tasks-picker__actions"
            wrapClass="sh-tasks-picker__actions-wrap"
            items={[
              { key: '__rename', label: t('tasks.list.rename_current'),
                onSelect: () => enter('rename', active.name) },
              { key: '__delete', label: t('tasks.list.delete'), danger: true, onSelect: () => onDelete(active) },
            ]}
          >
            <span aria-hidden="true">⋯</span>
          </OverflowMenu>
        )}
        </div>
      )}
      {creating && (
        <QuickAddBar
          class="sh-tasks-new-list"
          value={draft}
          inputRef={newListRef}
          placeholder={t('tasks.new_list_placeholder')}
          inputLabel={t('tasks.new_list')}
          submitLabel={t('tasks.add_list')}
          onValueChange={setDraft}
          onSubmit={create}
          inputProps={{
            maxLength: LIST_NAME_MAX,
            onKeyDown: (e) => {
              if (e.key === 'Escape' && lists.length > 0) { e.preventDefault(); leave(true) }
            },
          }}
        />
      )}
    </div>
  )
}
