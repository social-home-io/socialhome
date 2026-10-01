/**
 * Tasks store — task lists and their tasks, one store per scope.
 *
 * ``createTaskStore(spaceId)`` builds the store for the household
 * (``null`` → ``/api/tasks/...``) or for one space
 * (``/api/spaces/{id}/tasks/...``, the same endpoint shape). The
 * household store is ``householdTaskStore``; ``spaceTaskStore(id)``
 * makes a space's store on first use and keeps it (the space board
 * moves onto it next). Modelled on ``store/timetables.ts``.
 *
 * - **Loads** are deduped: a call while the same request is in flight
 *   shares it, and a list fetched a moment ago (``FRESH_MS``) is not
 *   fetched again — so the Organize hub's count chip and the Tasks tab
 *   mounting together fetch each URL once. ``force`` skips both.
 *   ``ensure*`` load only what was never loaded.
 * - **Edits** are optimistic. A failure rolls back only the fields of
 *   that one row that still hold our optimistic value, so it never
 *   reverts another row's change or a newer WS update.
 * - **Deletes** go through ``undoableDelete``: hidden at once (views
 *   filter on ``pendingDeletes``), the DELETE is sent when the Undo
 *   toast expires; a 404 counts as gone. Ids deleted here stay out of
 *   late WS echoes and list responses for a minute.
 * - **WS frames** route by ``space_id``: none → the household store, a
 *   space id → that space's store if one was made. A reconnect
 *   revalidates everything a store had loaded (frames sent while the
 *   socket was down are lost).
 */
import { computed, signal, type ReadonlySignal, type Signal } from '@preact/signals'
import { api } from '@/api'
import { connectionState, ws } from '@/ws'
import { locale, t } from '@/i18n/i18n'
import { pendingDeletes, undoableDelete, type CommitOptions } from '@/utils/undoableDelete'
import type { TaskItem, TaskListEntry, TaskPriority } from '@/types'
import { resetFilters } from '@/features/tasks/board/filters'

export type TaskStatus = TaskItem['status']

/** Fields a PATCH may change. ``null`` clears a nullable field. */
export type TaskPatch = Partial<Pick<TaskItem,
  'title' | 'description' | 'status' | 'due_date' | 'assignees' | 'position'
  | 'priority' | 'labels'>>

/** Optional fields of a new task (the server appends it at the bottom). */
export interface NewTaskFields {
  status?: TaskStatus
  priority?: TaskPriority | null
  labels?: string[]
  assignees?: string[]
  due_date?: string | null
  description?: string | null
}

export interface LoadOpts {
  /** Fetch even when a request is in flight or just finished. */
  force?: boolean
}

export interface UndoCallbacks {
  /** Runs after the user pressed Undo — e.g. to put focus back. */
  onUndone?: () => void
}

export interface ClearDoneOpts extends UndoCallbacks {
  /** Only these done tasks are cleared (e.g. the ones the user may edit). */
  canDelete?: (task: TaskItem) => boolean
}

/** Who may change a task. Household (``scope`` null): its creator, an
 *  assignee or an admin — the server answers 403 to anyone else. Space
 *  tasks are collaborative: every member may (the server gates them). */
export function canEditTask(
  task: Pick<TaskItem, 'created_by' | 'assignees'>,
  me: { user_id: string; is_admin?: boolean } | null | undefined,
  scope: string | null,
): boolean {
  if (scope !== null) return true
  if (!me) return false
  return !!me.is_admin || task.created_by === me.user_id
    || (task.assignees ?? []).includes(me.user_id)
}

/** A 403 from a task write, in the UI language (the backend's detail is
 *  English); other errors pass through. */
export function translateTaskError(err: unknown): unknown {
  if ((err as { status?: unknown } | null)?.status !== 403) return err
  return Object.assign(new Error(t('tasks.error.forbidden')), { status: 403 })
}

export interface TaskStore {
  spaceId: string | null
  lists: Signal<TaskListEntry[]>
  tasksByList: Signal<Readonly<Record<string, TaskItem[]>>>
  /** The list roster has loaded at least once. */
  listsLoaded: Signal<boolean>
  /** Lists whose tasks have loaded at least once. */
  loadedListIds: Signal<ReadonlySet<string>>
  /** The list the Tasks page shows (kept across visits). */
  activeListId: Signal<string | null>
  /** ``lists`` minus lists hidden behind an Undo toast. */
  visibleLists: ReadonlySignal<TaskListEntry[]>
  /** Every loaded task. */
  allTasks: ReadonlySignal<TaskItem[]>
  /** Not-done tasks across the visible lists, minus pending deletes. */
  openCount: ReadonlySignal<number>

  loadLists(opts?: LoadOpts): Promise<void>
  ensureLists(): Promise<void>
  loadList(listId: string, opts?: LoadOpts): Promise<void>
  ensureList(listId: string): Promise<void>
  /** Roster + every list — for counts across all lists. */
  ensureAll(): Promise<void>
  /** Reload the roster and every loaded list (after a reconnect). */
  revalidate(): Promise<void>

  findTask(id: string): TaskItem | undefined
  createList(name: string): Promise<TaskListEntry>
  renameList(listId: string, name: string): Promise<void>
  removeList(list: TaskListEntry, cb?: UndoCallbacks): void
  createTask(listId: string, title: string, fields?: NewTaskFields): Promise<TaskItem>
  /** A board drop: ``id`` goes to ``status`` and ``order`` (that column's
   *  ids, top to bottom) becomes the column's order — at once, then a
   *  status PATCH (when it changed) and a reorder ``{order, moved_id}``.
   *  A failure rolls back what the server didn't take and rethrows; when
   *  the status was saved but the reorder failed, the error carries
   *  ``partial: true`` (the card did change column). */
  moveTask(id: string, status: TaskStatus, order: readonly string[]): Promise<void>
  /** Run ``job`` after every move queued before it on ``listId`` — so a
   *  move is planned from the state the previous one left. */
  serial<T>(listId: string, job: () => Promise<T>): Promise<T>
  patchTask(id: string, patch: TaskPatch): Promise<TaskItem | null>
  setStatus(id: string, status: TaskStatus): Promise<void>
  /** The checkbox: done → to do, anything else → done. */
  toggleDone(id: string): Promise<void>
  removeTask(task: TaskItem, cb?: UndoCallbacks): void
  /** "Clear all" done tasks of a list, with Undo. Returns how many. */
  clearDone(listId: string, opts?: ClearDoneOpts): number
  deleteTasks(ids: readonly string[], opts?: CommitOptions): Promise<void>

  onTaskUpsert(task: TaskItem): void
  onTaskDeleted(id: string): void
  onTaskCompleted(id: string): void
  onListUpsert(listId: string, name: string): void
  onListDeleted(listId: string): void
  reset(): void
}

/** A fetch this recent is reused instead of repeated. */
const FRESH_MS = 2_000
/** How long a deleted id stays out of late responses / WS echoes. */
const RECENTLY_GONE_MS = 60_000

/** ``t()`` with a count: the ``_one`` key for exactly one.
 *  For ``i18n:check``: t('tasks.cleared') t('tasks.cleared_one') */
function tn(key: string, n: number, params: Record<string, string> = {}): string {
  return t(isOne(n) ? `${key}_one` : key, { ...params, n: String(n) })
}

/** The UI language's "one" plural category (French counts 0 as one). */
export function isOne(n: number): boolean {
  try {
    return new Intl.PluralRules(locale.value || undefined).select(n) === 'one'
  } catch {
    return n === 1
  }
}

function isNotFound(err: unknown): boolean {
  return (err as { status?: unknown } | null)?.status === 404
}

/** A store for one scope: ``null`` = the household, else a space id. */
export function createTaskStore(spaceId: string | null): TaskStore {
  const enc = encodeURIComponent
  const base = spaceId === null ? '/api/tasks' : `/api/spaces/${enc(spaceId)}/tasks`
  const listsPath = `${base}/lists`
  const listPath = (id: string) => `${listsPath}/${enc(id)}`
  const listTasksPath = (id: string) => `${listPath(id)}/tasks`
  const taskPath = (id: string) => `${base}/${enc(id)}`
  const reorderPath = (id: string) => `${listPath(id)}/reorder`

  const lists = signal<TaskListEntry[]>([])
  const tasksByList = signal<Readonly<Record<string, TaskItem[]>>>({})
  const listsLoaded = signal(false)
  const loadedListIds = signal<ReadonlySet<string>>(new Set())
  const activeListId = signal<string | null>(null)

  const visibleLists = computed(() => {
    const hidden = pendingDeletes.value
    return lists.value.filter(l => !hidden.has(l.id))
  })
  const allTasks = computed(() => Object.values(tasksByList.value).flat())
  const openCount = computed(() => {
    const hidden = pendingDeletes.value
    let n = 0
    for (const l of visibleLists.value) {
      for (const task of tasksByList.value[l.id] ?? []) {
        if (task.status !== 'done' && !hidden.has(task.id)) n++
      }
    }
    return n
  })

  /** Bumped by ``reset`` so a request in flight then never writes. */
  let epoch = 0
  let listsInflight: Promise<void> | null = null
  let listsFetchedAt = 0
  let listsGen = 0
  const listInflight = new Map<string, Promise<void>>()
  const listFetchedAt = new Map<string, number>()
  const listGen = new Map<string, number>()
  const recentlyGone = new Map<string, number>()
  /** Per-row PATCH sequence: only the newest request's answer is kept. */
  const patchSeq = new Map<string, number>()
  /** Positions a board move set and the server hasn't confirmed yet:
   *  a WS echo of the status PATCH (still the old position) mustn't
   *  make the card jump back and forth. */
  const pinnedPos = new Map<string, number>()
  /** Pinned ids whose latest frame said another position: the frame
   *  was overridden, so the list reloads once the pin goes. */
  const pinOverridden = new Set<string>()
  /** Per-list tail of the move queue. */
  const moveQueue = new Map<string, Promise<unknown>>()

  function markGone(ids: Iterable<string>) {
    const now = Date.now()
    for (const id of ids) recentlyGone.set(id, now)
  }
  function isGone(id: string): boolean {
    const at = recentlyGone.get(id)
    if (at === undefined) return false
    if (Date.now() - at > RECENTLY_GONE_MS) {
      recentlyGone.delete(id)
      return false
    }
    return true
  }

  function setList(listId: string, rows: TaskItem[]) {
    tasksByList.value = { ...tasksByList.value, [listId]: rows }
  }
  function markLoaded(listId: string) {
    if (loadedListIds.value.has(listId)) return
    loadedListIds.value = new Set([...loadedListIds.value, listId])
  }
  function forgetList(listId: string) {
    if (listId in tasksByList.value) {
      const next = { ...tasksByList.value }
      delete next[listId]
      tasksByList.value = next
    }
    if (loadedListIds.value.has(listId)) {
      const next = new Set(loadedListIds.value)
      next.delete(listId)
      loadedListIds.value = next
    }
    listFetchedAt.delete(listId)
  }

  function findTask(id: string): TaskItem | undefined {
    for (const rows of Object.values(tasksByList.value)) {
      const hit = rows.find(x => x.id === id)
      if (hit) return hit
    }
    return undefined
  }

  /** Map one row in place (wherever it lives). */
  function mapTask(id: string, fn: (x: TaskItem) => TaskItem) {
    const row = findTask(id)
    if (!row) return
    setList(row.list_id, (tasksByList.value[row.list_id] ?? []).map(x => x.id === id ? fn(x) : x))
  }

  function upsert(incoming: TaskItem) {
    if (!incoming?.id || isGone(incoming.id)) return
    const pin = pinnedPos.get(incoming.id)
    if (pin !== undefined) {
      if (incoming.position !== pin) pinOverridden.add(incoming.id)
      else pinOverridden.delete(incoming.id)
    }
    const task = pin === undefined ? incoming : { ...incoming, position: pin }
    const prev = findTask(task.id)
    if (prev && prev.list_id !== task.list_id) {
      setList(prev.list_id, (tasksByList.value[prev.list_id] ?? []).filter(x => x.id !== task.id))
    }
    const rows = tasksByList.value[task.list_id] ?? []
    setList(task.list_id, rows.some(x => x.id === task.id)
      ? rows.map(x => x.id === task.id ? { ...x, ...task } : x)
      : [...rows, task])
  }

  function dropTasks(ids: ReadonlySet<string>) {
    const next: Record<string, TaskItem[]> = {}
    let changed = false
    for (const [listId, rows] of Object.entries(tasksByList.value)) {
      const kept = rows.filter(x => !ids.has(x.id))
      if (kept.length !== rows.length) changed = true
      next[listId] = kept
    }
    if (changed) tasksByList.value = next
  }

  // ─── Loading ──────────────────────────────────────────────────────

  function loadLists({ force = false }: LoadOpts = {}): Promise<void> {
    if (!force && listsInflight) return listsInflight
    if (!force && listsLoaded.value && Date.now() - listsFetchedAt < FRESH_MS) {
      return Promise.resolve()
    }
    const gen = ++listsGen
    const myEpoch = epoch
    const run: Promise<void> = (async () => {
      const rows = await api.get(listsPath) as TaskListEntry[]
      if (myEpoch !== epoch || gen !== listsGen) return
      lists.value = rows
      const keep = new Set(rows.map(l => l.id))
      for (const id of Object.keys(tasksByList.value)) if (!keep.has(id)) forgetList(id)
      for (const id of loadedListIds.value) if (!keep.has(id)) forgetList(id)
      listsLoaded.value = true
      listsFetchedAt = Date.now()
    })().finally(() => {
      if (listsInflight === run) listsInflight = null
    })
    listsInflight = run
    return run
  }

  function ensureLists(): Promise<void> {
    if (listsLoaded.value) return Promise.resolve()
    return listsInflight ?? loadLists()
  }

  function loadList(listId: string, { force = false }: LoadOpts = {}): Promise<void> {
    const inflight = listInflight.get(listId)
    if (!force && inflight) return inflight
    const at = listFetchedAt.get(listId)
    if (!force && at !== undefined && Date.now() - at < FRESH_MS) return Promise.resolve()
    const gen = (listGen.get(listId) ?? 0) + 1
    listGen.set(listId, gen)
    const myEpoch = epoch
    const run: Promise<void> = (async () => {
      const rows = await api.get(listTasksPath(listId)) as TaskItem[]
      if (myEpoch !== epoch || listGen.get(listId) !== gen) return
      setList(listId, rows.filter(x => !isGone(x.id)))
      markLoaded(listId)
      listFetchedAt.set(listId, Date.now())
    })().finally(() => {
      if (listInflight.get(listId) === run) listInflight.delete(listId)
    })
    listInflight.set(listId, run)
    return run
  }

  function ensureList(listId: string): Promise<void> {
    if (loadedListIds.value.has(listId)) return Promise.resolve()
    return listInflight.get(listId) ?? loadList(listId)
  }

  async function ensureAll(): Promise<void> {
    await ensureLists()
    await Promise.all(lists.value.map(l => ensureList(l.id)))
  }

  async function revalidate(): Promise<void> {
    const jobs: Promise<void>[] = []
    if (listsLoaded.value) jobs.push(loadLists({ force: true }))
    for (const id of loadedListIds.value) jobs.push(loadList(id, { force: true }))
    await Promise.allSettled(jobs)
  }

  // ─── Lists ────────────────────────────────────────────────────────

  function onListUpsert(listId: string, name: string) {
    if (!listId) return
    if (lists.value.some(l => l.id === listId)) {
      lists.value = lists.value.map(l => l.id === listId ? { ...l, name } : l)
    } else {
      lists.value = [...lists.value, { id: listId, name }]
    }
  }

  function onListDeleted(listId: string) {
    lists.value = lists.value.filter(l => l.id !== listId)
    forgetList(listId)
    if (activeListId.value === listId) activeListId.value = null
  }

  async function createList(name: string): Promise<TaskListEntry> {
    const list = await api.post(listsPath, { name }) as TaskListEntry
    if (!lists.value.some(l => l.id === list.id)) lists.value = [...lists.value, list]
    // A brand-new list has no tasks — nothing to fetch.
    if (!(list.id in tasksByList.value)) setList(list.id, [])
    markLoaded(list.id)
    activeListId.value = list.id
    return list
  }

  async function renameList(listId: string, name: string): Promise<void> {
    const before = lists.value.find(l => l.id === listId)
    if (!before) return
    lists.value = lists.value.map(l => l.id === listId ? { ...l, name } : l)
    try {
      const fresh = await api.patch(listPath(listId), { name }) as TaskListEntry
      lists.value = lists.value.map(l => l.id === listId ? { ...l, ...fresh } : l)
    } catch (err) {
      lists.value = lists.value.map(l =>
        l.id === listId && l.name === name ? { ...l, name: before.name } : l)
      throw err
    }
  }

  function removeList(list: TaskListEntry, cb: UndoCallbacks = {}): void {
    const wasActive = activeListId.value === list.id
    undoableDelete({
      ids: [list.id],
      message: t('tasks.list_deleted', { name: list.name }),
      commit: async ({ keepalive }) => {
        try {
          await (keepalive
            ? api.delete(listPath(list.id), { keepalive: true })
            : api.delete(listPath(list.id)))
        } catch (err) {
          if (!isNotFound(err)) throw err
        }
        onListDeleted(list.id)
      },
      onUndone: () => {
        if (wasActive) activeListId.value = list.id
        cb.onUndone?.()
      },
    })
  }

  // ─── Tasks ────────────────────────────────────────────────────────

  async function createTask(
    listId: string, title: string, fields: NewTaskFields = {},
  ): Promise<TaskItem> {
    const task = await api.post(listTasksPath(listId), { title, ...fields }) as TaskItem
    upsert(task)
    return task
  }

  async function moveTask(id: string, status: TaskStatus, order: readonly string[]): Promise<void> {
    const row = findTask(id)
    if (!row) return
    const listId = row.list_id
    const statusChanged = row.status !== status
    const beforeStatus = row.status
    const wanted = new Map(order.map((tid, i) => [tid, i]))
    const beforePos = new Map<string, number>()
    for (const x of tasksByList.value[listId] ?? []) {
      if (wanted.has(x.id)) beforePos.set(x.id, x.position)
    }
    setList(listId, (tasksByList.value[listId] ?? []).map((x) => {
      const pos = wanted.get(x.id)
      if (pos === undefined && x.id !== id) return x
      return { ...x, ...(pos !== undefined ? { position: pos } : {}),
        ...(x.id === id ? { status } : {}) }
    }))
    // A late answer to an earlier PATCH of this row must not undo the move.
    const seq = (patchSeq.get(id) ?? 0) + 1
    patchSeq.set(id, seq)
    for (const [tid, pos] of wanted) pinnedPos.set(tid, pos)
    const unpin = () => {
      let heal = false
      for (const [tid, pos] of wanted) {
        if (pinnedPos.get(tid) !== pos) continue
        pinnedPos.delete(tid)
        if (pinOverridden.delete(tid)) heal = true
      }
      // A frame we overrode may have been the server's last word.
      if (heal) void loadList(listId, { force: true }).catch(() => {})
    }
    const undoPositions = () => {
      setList(listId, (tasksByList.value[listId] ?? []).map((x) => {
        const was = beforePos.get(x.id)
        // Only where our optimistic position still stands.
        return was !== undefined && x.position === wanted.get(x.id) ? { ...x, position: was } : x
      }))
    }
    if (statusChanged) {
      try {
        const fresh = await api.patch(taskPath(id), { status }) as TaskItem
        // (``upsert`` keeps the pinned position until the reorder answers.)
        if (fresh?.id && patchSeq.get(id) === seq) upsert(fresh)
      } catch (err) {
        unpin()
        undoPositions()
        mapTask(id, x => x.status === status ? { ...x, status: beforeStatus } : x)
        throw translateTaskError(err)
      }
    }
    try {
      await api.post(reorderPath(listId), { order: [...order], moved_id: id })
      unpin()
    } catch (err) {
      unpin()
      undoPositions()
      const e = translateTaskError(err)
      // The status went through: the card did move, only its place didn't.
      throw statusChanged ? Object.assign(e instanceof Error ? e : new Error(String(e)), { partial: true }) : e
    }
  }

  function serial<T>(listId: string, job: () => Promise<T>): Promise<T> {
    const prev = moveQueue.get(listId) ?? Promise.resolve()
    const run = prev.catch(() => {}).then(job)
    const tail = run.catch(() => {})
    moveQueue.set(listId, tail)
    void tail.then(() => { if (moveQueue.get(listId) === tail) moveQueue.delete(listId) })
    return run
  }

  /** Put back the fields we changed on ONE row, only where they still
   *  hold our optimistic value. Never re-creates a removed row. */
  function rollback(id: string, before: TaskPatch, optimistic: TaskPatch) {
    mapTask(id, (row) => {
      const next = { ...row }
      for (const k of Object.keys(optimistic) as (keyof TaskPatch)[]) {
        if (row[k] === optimistic[k]) (next as Record<string, unknown>)[k] = before[k]
      }
      return next
    })
  }

  async function patchTask(id: string, patch: TaskPatch): Promise<TaskItem | null> {
    const row = findTask(id)
    if (!row) return null
    const before: TaskPatch = {}
    for (const k of Object.keys(patch) as (keyof TaskPatch)[]) {
      (before as Record<string, unknown>)[k] = row[k]
    }
    mapTask(id, x => ({ ...x, ...patch }))
    const seq = (patchSeq.get(id) ?? 0) + 1
    patchSeq.set(id, seq)
    try {
      const fresh = await api.patch(taskPath(id), patch) as TaskItem
      // An older request answering late must not undo a newer one.
      if (fresh?.id && patchSeq.get(id) === seq) upsert(fresh)
      return fresh
    } catch (err) {
      rollback(id, before, patch)
      throw translateTaskError(err)
    }
  }

  async function setStatus(id: string, status: TaskStatus): Promise<void> {
    const row = findTask(id)
    if (!row || row.status === status) return
    await patchTask(id, { status })
  }

  async function toggleDone(id: string): Promise<void> {
    const row = findTask(id)
    if (!row) return
    await setStatus(id, row.status === 'done' ? 'todo' : 'done')
  }

  async function deleteTasks(
    ids: readonly string[], { keepalive = false }: CommitOptions = {},
  ): Promise<void> {
    const results = await Promise.allSettled(ids.map(id => keepalive
      ? api.delete(taskPath(id), { keepalive: true })
      : api.delete(taskPath(id))))
    const gone = new Set<string>()
    let firstError: unknown = null
    results.forEach((r, i) => {
      if (r.status === 'fulfilled' || isNotFound(r.reason)) gone.add(ids[i])
      else if (firstError === null) firstError = translateTaskError(r.reason)
    })
    markGone(gone)
    dropTasks(gone)
    if (firstError !== null) throw firstError
  }

  function removeTask(task: TaskItem, cb: UndoCallbacks = {}): void {
    undoableDelete({
      ids: [task.id],
      message: t('tasks.deleted', { title: task.title }),
      commit: (opts) => deleteTasks([task.id], opts),
      onUndone: cb.onUndone,
    })
  }

  function clearDone(listId: string, cb: ClearDoneOpts = {}): number {
    const hidden = pendingDeletes.value
    const ids = (tasksByList.value[listId] ?? [])
      .filter(x => x.status === 'done' && !hidden.has(x.id) && (cb.canDelete?.(x) ?? true))
      .map(x => x.id)
    if (ids.length === 0) return 0
    undoableDelete({
      ids,
      message: tn('tasks.cleared', ids.length),
      commit: (opts) => deleteTasks(ids, opts),
      onUndone: cb.onUndone,
    })
    return ids.length
  }

  // ─── WS hooks ─────────────────────────────────────────────────────

  function onTaskDeleted(id: string) {
    markGone([id])
    dropTasks(new Set([id]))
  }

  function onTaskCompleted(id: string) {
    mapTask(id, x => ({ ...x, status: 'done' }))
  }

  function reset() {
    epoch++
    listsInflight = null
    listsFetchedAt = 0
    listInflight.clear()
    listFetchedAt.clear()
    recentlyGone.clear()
    patchSeq.clear()
    pinnedPos.clear()
    pinOverridden.clear()
    moveQueue.clear()
    lists.value = []
    tasksByList.value = {}
    listsLoaded.value = false
    loadedListIds.value = new Set()
    activeListId.value = null
  }

  return {
    spaceId, lists, tasksByList, listsLoaded, loadedListIds, activeListId,
    visibleLists, allTasks, openCount,
    loadLists, ensureLists, loadList, ensureList, ensureAll, revalidate,
    findTask, createList, renameList, removeList,
    createTask, moveTask, serial, patchTask, setStatus, toggleDone, removeTask, clearDone, deleteTasks,
    onTaskUpsert: upsert, onTaskDeleted, onTaskCompleted, onListUpsert, onListDeleted,
    reset,
  }
}

/** The household's task lists. */
export const householdTaskStore = createTaskStore(null)

const spaceStores = new Map<string, TaskStore>()

/** The store of one space's task lists (made on first use, then kept). */
export function spaceTaskStore(spaceId: string): TaskStore {
  let s = spaceStores.get(spaceId)
  if (!s) {
    s = createTaskStore(spaceId)
    spaceStores.set(spaceId, s)
  }
  return s
}

/** Logout: forget every scope's tasks. */
export function resetTasks(): void {
  resetFilters()
  householdTaskStore.reset()
  for (const s of spaceStores.values()) s.reset()
  spaceStores.clear()
}

// ─── WebSocket ─────────────────────────────────────────────────────

let _wired = false

/** Route ``task.*`` / ``task_list.*`` frames to their scope's store:
 *  no ``space_id`` → the household; a space id → that space's store if
 *  one exists (a space never opened has nothing to update). Idempotent.
 *  ``task.assigned`` (sent alongside ``task.updated``) and
 *  ``task.deadline_due`` (the notification covers it) need nothing. */
export function wireTasksWs(): void {
  if (_wired) return
  _wired = true
  type Frame = { space_id?: string | null }
  const target = (d: Frame): TaskStore | undefined =>
    d.space_id == null ? householdTaskStore : spaceStores.get(d.space_id)

  ws.on('task.created', (e) => {
    const d = e.data as Frame & { task?: TaskItem }
    if (d.task) target(d)?.onTaskUpsert(d.task)
  })
  ws.on('task.updated', (e) => {
    const d = e.data as Frame & { task?: TaskItem }
    if (d.task) target(d)?.onTaskUpsert(d.task)
  })
  ws.on('task.deleted', (e) => {
    const d = e.data as Frame & { task_id?: string }
    if (d.task_id) target(d)?.onTaskDeleted(d.task_id)
  })
  ws.on('task.completed', (e) => {
    const d = e.data as Frame & { task_id?: string }
    if (d.task_id) target(d)?.onTaskCompleted(d.task_id)
  })
  ws.on('task_list.created', (e) => {
    const d = e.data as Frame & { list_id?: string; name?: string }
    if (d.list_id) target(d)?.onListUpsert(d.list_id, d.name ?? '')
  })
  ws.on('task_list.updated', (e) => {
    const d = e.data as Frame & { list_id?: string; name?: string }
    if (d.list_id) target(d)?.onListUpsert(d.list_id, d.name ?? '')
  })
  ws.on('task_list.deleted', (e) => {
    const d = e.data as Frame & { list_id?: string }
    if (d.list_id) target(d)?.onListDeleted(d.list_id)
  })

  let prev = connectionState.value
  connectionState.subscribe((next) => {
    if (prev === 'reconnecting' && next === 'open') {
      void householdTaskStore.revalidate()
      for (const s of spaceStores.values()) void s.revalidate()
    }
    prev = next
  })
}
