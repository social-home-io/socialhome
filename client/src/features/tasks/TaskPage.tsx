/**
 * TaskPage — the household's task lists, as the Organize "Tasks" tab
 * (§23.54 / §15).
 *
 * The sidebar holds the list roster (pick, add, ⋯ rename / delete);
 * the main pane shows the active list grouped by status: "To do" and
 * "In progress" sections, then the done archive under a
 * "Done (N) · Clear all" divider. Built on the shared Organize
 * primitives so it reads like Shopping:
 *
 * - ``useLoad`` twice — the roster, then the active list's tasks (keyed
 *   by list id). Both render cached data at once on a revisit and
 *   revalidate behind it; a failed first load shows ``LoadErrorState``
 *   with Retry, never the empty state.
 * - ``CheckToggle``: unticked → done, ticked → to do (from in progress
 *   → done). Drag a row onto another status section to move it; the
 *   keyboard path is the edit dialog's status picker.
 * - Delete and "Clear all" are instant with Undo (deferred DELETE).
 *   Deleting a whole list asks first — it takes every task with it —
 *   and then also offers Undo.
 *
 * Data lives in ``householdTaskStore`` (``store/tasks.ts``).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
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
import {
  householdTaskStore, canEditTask, isOne, type TaskPatch, type TaskStatus,
} from '@/store/tasks'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import { locale, t } from '@/i18n/i18n'
import { useLoad } from '@/utils/useLoad'
import { pendingDeletes } from '@/utils/undoableDelete'
import { OrganizeSectionHeader } from '@/features/organize/shared/OrganizeSectionHeader'
import { ArchiveDivider } from '@/features/organize/shared/ArchiveDivider'
import { DropPad } from '@/features/organize/shared/DropPad'
import { useDragBuckets, type DragItemProps } from '@/features/organize/shared/useDragBuckets'
import type { TaskItem, TaskListEntry } from '@/types'
import { useNarrow } from '@/features/timetable/useNarrow'
import { useAutofocus } from '@/features/timetable/useAutofocus'
import { dueLabel, parseDueDate } from './dueLabel'

const store = householdTaskStore

/** Marks a task-row drag on ``dataTransfer`` (see ``useDragBuckets``). */
const DRAG_TASK_MIME = 'application/x-sh-task'

const OPEN_STATUSES: TaskStatus[] = ['todo', 'in_progress']

/** The backend's list-name cap. */
const LIST_NAME_MAX = 100

/** Status names. Keys for ``i18n:check``:
 *  t('tasks.status.todo') t('tasks.status.in_progress') t('tasks.status.done') */
function statusLabel(s: TaskStatus): string {
  return t(`tasks.status.${s}`)
}

/** ``t()`` with a count: the ``_one`` key for exactly one.
 *  For ``i18n:check``: t('tasks.lists_count') t('tasks.lists_count_one') */
function tn(key: string, n: number, params: Record<string, string> = {}): string {
  return t(isOne(n) ? `${key}_one` : key, { ...params, n: String(n) })
}

/** "You & Lena" / "Lena, Max & you" in the UI language. */
function joinNames(names: string[]): string {
  try {
    return new Intl.ListFormat(locale.value || undefined, { style: 'short', type: 'conjunction' })
      .format(names)
  } catch {
    return names.join(', ')
  }
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
  const addRef = useRef<HTMLInputElement | null>(null)
  const newListRef = useRef<HTMLInputElement | null>(null)
  const [draft, setDraft] = useState('')
  const [listDraft, setListDraft] = useState('')
  const [editing, setEditing] = useState<TaskItem | null>(null)
  // Phones: a compact list picker replaces the lists card, so the tasks
  // start near the top instead of below the fold.
  const narrow = useNarrow()

  useEffect(() => { void loadHouseholdUsers() }, [])

  // The store revalidates every loaded list itself after a WebSocket
  // reconnect, so neither load re-runs on one.
  const listsLoad = useLoad(() => store.loadLists(), {
    cached: store.listsLoaded.value,
    revalidateOnReconnect: false,
  })

  const visibleLists = store.visibleLists.value
  const chosen = store.activeListId.value
  // The remembered list, else the first one (also after it was deleted).
  const activeId = visibleLists.some(l => l.id === chosen) ? chosen : visibleLists[0]?.id ?? null
  const activeList = visibleLists.find(l => l.id === activeId) ?? null

  const tasksLoad = useLoad(
    () => (activeId ? store.loadList(activeId) : Promise.resolve()),
    {
      key: activeId ?? '',
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
  const visibleTasks = (activeId ? store.tasksByList.value[activeId] ?? [] : [])
    .filter(x => !hidden.has(x.id))
    .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
  const byStatus = (s: TaskStatus) => visibleTasks.filter(x => x.status === s)
  const doneTasks = byStatus('done')
  const openTasks = visibleTasks.length - doneTasks.length

  const me = currentUser.value
  const userName = (uid: string): string => {
    if (me?.user_id === uid) return t('tasks.you')
    const found = householdUsers.value.get(uid)
    return found?.display_name || found?.username || uid.slice(0, 6)
  }

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

  /** Household tasks: creator, assignee or admin (the server's rule). */
  const editable = (x: TaskItem) => canEditTask(x, me, store.spaceId)
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
      // On phones the list picker already shows the name.
      hideTitle={narrow}
      // No "0 open · 0 done" over an empty list's "All caught up".
      counts={activeList && tasksLoad.state === 'ready' && visibleTasks.length > 0 ? [
        { label: t('tasks.counts.open', { n: String(openTasks) }), tone: 'open' },
        { label: t('tasks.counts.done', { n: String(doneTasks.length) }), tone: 'done' },
      ] : []}
    />
  )

  if (listsLoad.state !== 'ready') {
    return (
      <div class="sh-tasks-page">
        {listsLoad.state === 'error'
          ? <LoadErrorState message={t('tasks.load_failed')} onRetry={listsLoad.retry} />
          : <ListSkeleton variant="list" rows={6} label={t('tasks.loading')} />}
      </div>
    )
  }

  const rowProps = (task: TaskItem) => ({
    task,
    editable: editable(task),
    dragging: drag.draggingId === task.id,
    dragProps: editable(task) ? drag.itemProps(task.id) : NO_DRAG,
    userName,
    onToggle: () => { void toggle(task.id) },
    onEdit: () => setEditing(task),
    onDelete: () => deleteTask(task),
  })

  return (
    <div class="sh-tasks">
      {narrow ? (
        <ListPicker
          lists={visibleLists}
          active={activeList}
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
              onSelect={() => selectList(l.id)}
              onRename={(name) => void renameList(l, name)}
              onDelete={() => void deleteList(l)}
            />
          ))}
          <QuickAddBar
            class="sh-tasks-new-list"
            value={listDraft}
            inputRef={newListRef}
            placeholder={t('tasks.new_list_placeholder')}
            inputLabel={t('tasks.new_list')}
            submitLabel={t('tasks.add_list')}
            onValueChange={setListDraft}
            onSubmit={() => { void addList() }}
            inputProps={{ maxLength: LIST_NAME_MAX }}
          />
        </aside>
      )}
      <div class="sh-tasks-content">
        {!activeList ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">📋</div>
            <h3>{t('tasks.no_lists.title')}</h3>
            <p>{t('tasks.no_lists.body')}</p>
            <div class="sh-empty-state__cta-row">
              <Button onClick={() => newListRef.current?.focus()}>
                {t('tasks.no_lists.cta')}
              </Button>
            </div>
          </div>
        ) : (
          <>
            {header}
            {/* After a failed load only the error and its Retry show. */}
            {tasksLoad.state !== 'error' && <QuickAddBar
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
              <ListSkeleton variant="list" rows={4} label={t('tasks.loading')} />
            )}
            {tasksLoad.state === 'ready' && visibleTasks.length === 0 && (
              <div class="sh-empty-state">
                <div aria-hidden="true">✅</div>
                <h3>{t('tasks.empty.title')}</h3>
                <p>{t('tasks.empty.body')}</p>
              </div>
            )}
            {tasksLoad.state === 'ready' && visibleTasks.length > 0 && (
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
        <TaskEditDialog
          task={editing}
          editable={editable(store.findTask(editing.id) ?? editing)}
          userName={userName}
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
  dragging: boolean
  dragProps: DragItemProps
  userName: (uid: string) => string
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
}

function TaskRow({
  task, editable, dragging, dragProps, userName, onToggle, onEdit, onDelete,
}: TaskRowProps) {
  const rowRef = useRef<HTMLLIElement | null>(null)
  const done = task.status === 'done'
  const due = task.due_date ? dueLabel(task.due_date) : null
  const people = (task.assignees ?? []).map(userName)
  const hasNotes = !!task.description?.trim()
  const metaId = `sh-task-meta-${task.id}`
  const dueId = `sh-task-due-${task.id}`
  const showMeta = people.length > 0 || hasNotes || !editable
  const describedBy = [showMeta ? metaId : '', due && !done ? dueId : ''].filter(Boolean).join(' ')
  const readOnlyReason = editable ? undefined : t('tasks.error.forbidden')
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
        <span class="sh-task-item__title">{task.title}</span>
        {showMeta && (
          <span class="sh-task-item__meta" id={metaId}>
            {!editable && (
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
                  {joinNames(people)}
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
  list, active, onSelect, onRename, onDelete,
}: {
  list: TaskListEntry
  active: boolean
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
        onDblClick={start}
      >
        {list.name}
      </button>
      <OverflowMenu
        label={t('tasks.list.menu', { name: list.name })}
        bareTrigger
        triggerClass="sh-icon-btn sh-row-action sh-row-action--reveal"
        items={[
          { label: t('tasks.list.rename'), onSelect: start },
          { label: t('tasks.list.delete'), danger: true, onSelect: onDelete },
        ]}
      >
        <span aria-hidden="true">⋯</span>
      </OverflowMenu>
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
  lists, active, newListRef, onSelect, onCreate, onRename, onDelete,
}: {
  lists: TaskListEntry[]
  active: TaskListEntry | null
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
  const creating = mode === 'new' || lists.length === 0

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
        <OverflowMenu
          label={t('tasks.picker.label', { name: active.name })}
          bareTrigger
          open={open}
          onOpenChange={onOpenChange}
          triggerClass="sh-tasks-picker__trigger"
          wrapClass="sh-tasks-picker__wrap"
          menuClass="sh-tasks-picker__menu"
          items={[
            ...lists.map(l => ({
              key: l.id, label: l.name, checked: l.id === active.id, onSelect: () => onSelect(l.id),
            })),
            { key: '__new', label: t('tasks.picker.new'), class: 'sh-tasks-picker__first-action',
              onSelect: () => enter('new', '') },
            { key: '__rename', label: t('tasks.list.rename_current'),
              onSelect: () => enter('rename', active.name) },
            { key: '__delete', label: t('tasks.list.delete'), danger: true, onSelect: () => onDelete(active) },
          ]}
        >
          <span class="sh-tasks-picker__name">{active.name}</span>
          <span class="sh-tasks-picker__caret" aria-hidden="true">▾</span>
        </OverflowMenu>
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

// ─── Edit dialog ──────────────────────────────────────────────────────

function TaskEditDialog({
  task, editable, userName, onClose, onSave,
}: {
  /** The row as it was when the dialog opened. */
  task: TaskItem
  /** ``false``: show the task read-only, with the reason and no Save. */
  editable: boolean
  userName: (uid: string) => string
  onClose: () => void
  onSave: (patch: TaskPatch) => Promise<void>
}) {
  // What the dialog opened with: Save diffs against THIS, not the live
  // row, so a field someone else changed meanwhile (and this user left
  // alone) isn't sent back with its old value.
  const snap = useRef(task)
  const base = snap.current
  // ``useState`` (not ``signal()``) for the fields — a signal created in
  // the render body is replaced on every render and resets the value.
  const [title, setTitle] = useState(base.title)
  const [description, setDescription] = useState(base.description ?? '')
  const [dueDate, setDueDate] = useState(base.due_date ?? '')
  const [status, setStatus] = useState<TaskStatus>(base.status)
  const [saving, setSaving] = useState(false)
  const [titleError, setTitleError] = useState(false)
  const nameRef = useRef<HTMLInputElement | null>(null)
  // The Name field takes focus — except on touch, where (as Modal does)
  // the soft keyboard stays down until the user taps a field.
  useAutofocus(nameRef, editable)

  const save = async (e: Event) => {
    e.preventDefault()
    if (!editable) return
    if (!title.trim()) {
      setTitleError(true)
      return
    }
    const patch: TaskPatch = {}
    if (title.trim() !== base.title) patch.title = title.trim()
    if ((description.trim() || null) !== (base.description || null)) {
      patch.description = description.trim() || null
    }
    if ((dueDate || null) !== (base.due_date || null)) patch.due_date = dueDate || null
    if (status !== base.status) patch.status = status
    if (Object.keys(patch).length === 0) {
      onClose()
      return
    }
    setSaving(true)
    try {
      await onSave(patch)
    } finally {
      setSaving(false)
    }
  }

  const readOnly = !editable
  const addedBy = base.created_by && (
    <p class="sh-task-edit__added">
      {base.created_by === currentUser.value?.user_id
        ? t('tasks.edit.added_by_you')
        : t('tasks.edit.added_by', { name: userName(base.created_by) })}
    </p>
  )

  if (readOnly) {
    // Read-only: plain labelled text, no form controls — nothing that
    // looks like it could be typed into.
    const due = base.due_date ? parseDueDate(base.due_date) : null
    const notes = base.description?.trim()
    return (
      <Modal open={true} onClose={onClose} title={t('tasks.edit.title_readonly')}>
        <div class="sh-task-edit">
          <p class="sh-task-edit__readonly">
            <span aria-hidden="true">🔒 </span>{t('tasks.error.forbidden')}
          </p>
          <dl class="sh-task-view">
            <dt>{t('tasks.edit.status')}</dt>
            <dd>
              <span class={`sh-task-edit__status-text sh-task-edit__status-text--${base.status}`}>
                {statusLabel(base.status)}
              </span>
            </dd>
            <dt>{t('tasks.edit.name')}</dt>
            <dd class="sh-task-view__name">{base.title}</dd>
            <dt>{t('tasks.edit.description')}</dt>
            {notes
              ? <dd class="sh-task-edit__notes">{base.description}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.edit.no_notes')}</dd>}
            <dt>{t('tasks.edit.due')}</dt>
            {base.due_date
              ? <dd>{due ? due.toLocaleDateString(locale.value || undefined, { dateStyle: 'full' }) : base.due_date}</dd>
              : <dd class="sh-task-edit__empty">{t('tasks.edit.no_due')}</dd>}
          </dl>
          {addedBy}
          <div class="sh-form-actions">
            <Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
          </div>
        </div>
      </Modal>
    )
  }

  return (
    <Modal open={true} onClose={onClose} title={t('tasks.edit.title')}>
      <form onSubmit={save} class="sh-task-edit">
        {/* Status first — the most common edit, and the keyboard way to
          * do what dragging a row does. */}
        <span class="sh-form-label" id="sh-task-edit-status">{t('tasks.edit.status')}</span>
        <ChipRadioGroup<TaskStatus>
          variant="segmented"
          class="sh-task-edit__status"
          labelledBy="sh-task-edit-status"
          value={status}
          onChange={setStatus}
          options={(['todo', 'in_progress', 'done'] as const).map(s => ({ value: s, label: statusLabel(s) }))}
        />
        <label>
          {t('tasks.edit.name')}
          <input ref={nameRef} type="text" value={title} maxLength={200} required
            aria-invalid={titleError ? 'true' : undefined}
            aria-describedby={titleError ? 'sh-task-edit-title-err' : undefined}
            onInput={(e) => { setTitle((e.target as HTMLInputElement).value); setTitleError(false) }} />
        </label>
        {titleError && (
          <p class="sh-form-error" id="sh-task-edit-title-err" role="alert">
            {t('tasks.edit.name_required')}
          </p>
        )}
        <label>
          {t('tasks.edit.description')}
          <textarea value={description} maxLength={2000} rows={3}
            onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)} />
        </label>
        <label for="sh-task-edit-due">{t('tasks.edit.due')}</label>
        <span class="sh-task-edit__due">
          <input id="sh-task-edit-due" type="date" value={dueDate}
            onInput={(e) => setDueDate((e.target as HTMLInputElement).value)} />
          {dueDate && (
            <button type="button" class="sh-link" onClick={() => setDueDate('')}>
              {t('tasks.edit.clear_due')}
            </button>
          )}
        </span>
        {addedBy}
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={saving}>{t('common.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}
