/**
 * Tasks store: one ``createTaskStore(spaceId)`` per scope. Loads are
 * in-flight deduped (the Organize hub's count chip and the Tasks tab
 * mounting together fetch once), status changes are optimistic with a
 * rollback scoped to the one row, deletes are deferred behind an Undo
 * toast, and WS frames route by ``space_id``.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { TaskItem } from '@/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))

const handlers: Record<string, (e: { type: string; data: Record<string, unknown> }) => void> = {}
vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  return {
    connectionState: signal('open'),
    ws: {
      on: (type: string, h: (e: { type: string; data: Record<string, unknown> }) => void) => {
        handlers[type] = h
        return () => { delete handlers[type] }
      },
    },
  }
})

import { connectionState } from '@/ws'
import { toasts } from '@/components/Toast'
import { pendingDeletes, resetPendingDeletes } from '@/utils/undoableDelete'
import {
  createTaskStore, householdTaskStore, spaceTaskStore, wireTasksWs, resetTasks, canEditTask,
  type TaskStore,
} from './tasks'

function task(id: string, list: string, status: TaskItem['status'] = 'todo',
  extra: Partial<TaskItem> = {}): TaskItem {
  return {
    id, list_id: list, title: id, description: null, status, position: 0,
    due_date: null, assignees: [], created_by: 'u1', ...extra,
  }
}

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const flush = () => new Promise(r => setTimeout(r, 0))

/** Roster + every list's tasks (test setup — the app loads lazily). */
async function loadAll(s: TaskStore): Promise<void> {
  await s.ensureLists()
  await Promise.all(s.lists.value.map(l => s.ensureList(l.id)))
}

let store: TaskStore

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
  resetTasks()
  resetPendingDeletes()
  toasts.value = []
  store = createTaskStore(null)
})

afterEach(() => {
  vi.useRealTimers()
})

function serve(lists: { id: string; name: string; open_count?: number }[], byList: Record<string, TaskItem[]>) {
  apiGet.mockImplementation(async (url: string) => {
    if (url.endsWith('/tasks/lists')) return lists
    const m = /\/tasks\/lists\/([^/]+)\/tasks$/.exec(url)
    if (m) return byList[m[1]] ?? []
    throw new Error(`unexpected ${url}`)
  })
}

describe('scopes', () => {
  it('household store talks to /api/tasks, a space store to /api/spaces/{id}/tasks', async () => {
    serve([{ id: 'l1', name: 'A' }], { l1: [task('t1', 'l1')] })
    await store.loadLists()
    await store.loadList('l1')
    expect(apiGet).toHaveBeenCalledWith('/api/tasks/lists')
    expect(apiGet).toHaveBeenCalledWith('/api/tasks/lists/l1/tasks')

    const sp = createTaskStore('s 1')
    await sp.loadLists()
    await sp.loadList('l1')
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s%201/tasks/lists')
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s%201/tasks/lists/l1/tasks')
  })

  it('spaceTaskStore caches one store per space', () => {
    expect(spaceTaskStore('s1')).toBe(spaceTaskStore('s1'))
    expect(spaceTaskStore('s1')).not.toBe(spaceTaskStore('s2'))
    expect(householdTaskStore.spaceId).toBeNull()
  })
})

describe('loading', () => {
  it('concurrent ensureLists / loadLists share one request', async () => {
    const d = deferred<unknown>()
    apiGet.mockReturnValue(d.promise)
    const a = store.ensureLists()
    const b = store.loadLists()
    const c = store.ensureLists()
    expect(apiGet).toHaveBeenCalledTimes(1)
    d.resolve([{ id: 'l1', name: 'A' }])
    await Promise.all([a, b, c])
    expect(store.lists.value).toEqual([{ id: 'l1', name: 'A' }])
    expect(store.listsLoaded.value).toBe(true)
  })

  it('a list fetched a moment ago is not fetched again; later it revalidates', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    serve([{ id: 'l1', name: 'A' }], { l1: [task('t1', 'l1')] })
    await store.ensureList('l1')
    await store.loadList('l1')
    expect(apiGet).toHaveBeenCalledTimes(1)
    vi.setSystemTime(Date.now() + 10_000)
    await store.loadList('l1')
    expect(apiGet).toHaveBeenCalledTimes(2)
    await store.loadList('l1', { force: true })
    expect(apiGet).toHaveBeenCalledTimes(3)
  })

  it('ensureLists / ensureList load the roster and every list exactly once', async () => {
    serve([{ id: 'l1', name: 'A' }, { id: 'l2', name: 'B' }], {
      l1: [task('t1', 'l1'), task('t2', 'l1', 'done')],
      l2: [task('t3', 'l2', 'in_progress')],
    })
    await Promise.all([loadAll(store), loadAll(store)])
    await loadAll(store)
    expect(apiGet.mock.calls.map(c => c[0]).sort()).toEqual([
      '/api/tasks/lists', '/api/tasks/lists/l1/tasks', '/api/tasks/lists/l2/tasks',
    ])
    expect(store.allTasks.value.map(t => t.id).sort()).toEqual(['t1', 't2', 't3'])
    expect(store.loadedListIds.value.has('l2')).toBe(true)
  })

  it('a failed load rejects and leaves the list unloaded', async () => {
    apiGet.mockRejectedValue(new Error('boom'))
    await expect(store.loadList('l1')).rejects.toThrow('boom')
    expect(store.loadedListIds.value.has('l1')).toBe(false)
  })

  it('a reload drops lists that are gone and their tasks', async () => {
    serve([{ id: 'l1', name: 'A' }, { id: 'l2', name: 'B' }], { l1: [], l2: [task('t3', 'l2')] })
    await loadAll(store)
    serve([{ id: 'l1', name: 'A' }], {})
    await store.loadLists({ force: true })
    expect(store.lists.value.map(l => l.id)).toEqual(['l1'])
    expect(store.tasksByList.value.l2).toBeUndefined()
    expect(store.loadedListIds.value.has('l2')).toBe(false)
  })
})

describe('status changes', () => {
  beforeEach(async () => {
    serve([{ id: 'l1', name: 'A' }], {
      l1: [task('t1', 'l1', 'todo'), task('t2', 'l1', 'in_progress'), task('t3', 'l1', 'done')],
    })
    await loadAll(store)
  })

  const statusOf = (id: string) => store.findTask(id)?.status

  it('toggleDone: todo → done, in_progress → done, done → todo', async () => {
    apiPatch.mockImplementation(async (url: string, body: Partial<TaskItem>) =>
      ({ ...store.findTask(url.split('/').pop()!)!, ...body }))
    await store.toggleDone('t1')
    await store.toggleDone('t2')
    await store.toggleDone('t3')
    expect(apiPatch.mock.calls).toEqual([
      ['/api/tasks/t1', { status: 'done' }],
      ['/api/tasks/t2', { status: 'done' }],
      ['/api/tasks/t3', { status: 'todo' }],
    ])
    expect([statusOf('t1'), statusOf('t2'), statusOf('t3')]).toEqual(['done', 'done', 'todo'])
  })

  it('setStatus applies at once and keeps the server answer', async () => {
    const d = deferred<TaskItem>()
    apiPatch.mockReturnValue(d.promise)
    const p = store.setStatus('t1', 'in_progress')
    expect(statusOf('t1')).toBe('in_progress')
    d.resolve(task('t1', 'l1', 'in_progress', { updated_at: 'now' }))
    await p
    expect(store.findTask('t1')?.updated_at).toBe('now')
  })

  it('setStatus to the current status is a no-op', async () => {
    await store.setStatus('t1', 'todo')
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('a failed change rolls back only that row, keeping a newer change to another', async () => {
    const d1 = deferred<TaskItem>()
    apiPatch.mockReturnValueOnce(d1.promise)
    apiPatch.mockResolvedValueOnce(task('t2', 'l1', 'done'))
    const p1 = store.setStatus('t1', 'done')
    await store.setStatus('t2', 'done')
    d1.reject(new Error('nope'))
    await expect(p1).rejects.toThrow('nope')
    expect(statusOf('t1')).toBe('todo')
    expect(statusOf('t2')).toBe('done')
  })

  it('a rollback leaves a field alone that a WS frame changed meanwhile', async () => {
    const d = deferred<TaskItem>()
    apiPatch.mockReturnValue(d.promise)
    const p = store.setStatus('t1', 'done')
    store.onTaskUpsert(task('t1', 'l1', 'in_progress'))
    d.reject(new Error('nope'))
    await expect(p).rejects.toThrow()
    expect(statusOf('t1')).toBe('in_progress')
  })

  it('an older PATCH response arriving after a newer one is ignored', async () => {
    const d1 = deferred<TaskItem>()
    const d2 = deferred<TaskItem>()
    apiPatch.mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise)
    const p1 = store.setStatus('t1', 'in_progress')
    const p2 = store.setStatus('t1', 'done')
    d2.resolve(task('t1', 'l1', 'done'))
    await p2
    d1.resolve(task('t1', 'l1', 'in_progress'))
    await p1
    expect(statusOf('t1')).toBe('done')
  })

  it('patchTask sends the fields and adopts the response', async () => {
    apiPatch.mockResolvedValue(task('t1', 'l1', 'todo', { title: 'New', due_date: '2026-10-03' }))
    await store.patchTask('t1', { title: 'New', due_date: '2026-10-03' })
    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/t1', { title: 'New', due_date: '2026-10-03' })
    expect(store.findTask('t1')?.title).toBe('New')
  })
})

describe('creating', () => {
  it('createTask appends the new task; a WS echo does not duplicate it', async () => {
    serve([{ id: 'l1', name: 'A' }], { l1: [] })
    await loadAll(store)
    apiPost.mockResolvedValue(task('t9', 'l1'))
    await store.createTask('l1', 'Buy milk')
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/tasks', { title: 'Buy milk' })
    store.onTaskUpsert(task('t9', 'l1'))
    expect(store.tasksByList.value.l1.map(t => t.id)).toEqual(['t9'])
  })

  it('createList adds the list once, selects it, and counts it as loaded', async () => {
    apiPost.mockResolvedValue({ id: 'l5', name: 'Garden' })
    await store.createList('Garden')
    store.onListUpsert('l5', 'Garden')
    expect(store.lists.value).toEqual([{ id: 'l5', name: 'Garden' }])
    expect(store.activeListId.value).toBe('l5')
    expect(store.loadedListIds.value.has('l5')).toBe(true)
  })

  it('renameList is optimistic and rolls back on failure', async () => {
    serve([{ id: 'l1', name: 'A' }], {})
    await store.ensureLists()
    apiPatch.mockRejectedValue(new Error('x'))
    const p = store.renameList('l1', 'B')
    expect(store.lists.value[0].name).toBe('B')
    await expect(p).rejects.toThrow()
    expect(store.lists.value[0].name).toBe('A')
  })
})

describe('deleting with Undo', () => {
  beforeEach(async () => {
    serve([{ id: 'l1', name: 'A' }], {
      l1: [task('t1', 'l1'), task('t2', 'l1', 'done'), task('t3', 'l1', 'done')],
    })
    await loadAll(store)
  })

  const toast = (msg: string) => toasts.value.find(x => x.message === msg)!

  it('removeTask hides the task and only DELETEs when the toast expires', async () => {
    apiDelete.mockResolvedValue(undefined)
    store.removeTask(store.findTask('t1')!)
    expect(pendingDeletes.value.has('t1')).toBe(true)
    expect(apiDelete).not.toHaveBeenCalled()
    expect(store.openCount.value).toBe(0)
    toast('Deleted t1').onExpire!()
    await flush()
    expect(apiDelete).toHaveBeenCalledWith('/api/tasks/t1')
    expect(store.findTask('t1')).toBeUndefined()
    expect(pendingDeletes.value.has('t1')).toBe(false)
  })

  it('Undo brings it back and never DELETEs', async () => {
    const onUndone = vi.fn()
    store.removeTask(store.findTask('t1')!, { onUndone })
    toast('Deleted t1').action!.onClick()
    expect(pendingDeletes.value.has('t1')).toBe(false)
    expect(store.findTask('t1')).toBeTruthy()
    expect(onUndone).toHaveBeenCalled()
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('a 404 on commit counts as gone', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('nf'), { status: 404 }))
    store.removeTask(store.findTask('t1')!)
    toast('Deleted t1').onExpire!()
    await flush()
    expect(store.findTask('t1')).toBeUndefined()
    expect(toasts.value.some(x => x.type === 'error')).toBe(false)
  })

  it('a failed commit brings the task back with an error toast', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error('down'), { status: 500 }))
    store.removeTask(store.findTask('t1')!)
    toast('Deleted t1').onExpire!()
    await flush()
    expect(store.findTask('t1')).toBeTruthy()
    expect(pendingDeletes.value.has('t1')).toBe(false)
    expect(toasts.value.some(x => x.type === 'error')).toBe(true)
  })

  it('a WS echo of a committed delete cannot bring the task back', async () => {
    apiDelete.mockResolvedValue(undefined)
    store.removeTask(store.findTask('t1')!)
    toast('Deleted t1').onExpire!()
    await flush()
    store.onTaskUpsert(task('t1', 'l1'))
    expect(store.findTask('t1')).toBeUndefined()
  })

  it('clearDone deletes exactly the done tasks hidden now (per id, allSettled)', async () => {
    apiDelete.mockImplementation(async (url: string) => {
      if (url.endsWith('/t3')) throw Object.assign(new Error('gone'), { status: 404 })
    })
    expect(store.clearDone('l1')).toBe(2)
    expect([...pendingDeletes.value].sort()).toEqual(['t2', 't3'])
    // Ticked off during the Undo window: not part of this clear.
    store.onTaskUpsert(task('t1', 'l1', 'done'))
    toast('Cleared 2 done tasks').onExpire!()
    await flush()
    expect(apiDelete.mock.calls.map(c => c[0]).sort()).toEqual(['/api/tasks/t2', '/api/tasks/t3'])
    expect(store.tasksByList.value.l1.map(t => t.id)).toEqual(['t1'])
  })

  it('clearDone only takes the tasks the predicate allows, and counts those', async () => {
    apiDelete.mockResolvedValue(undefined)
    expect(store.clearDone('l1', { canDelete: (x) => x.id === 't3' })).toBe(1)
    expect([...pendingDeletes.value]).toEqual(['t3'])
    toast('Cleared 1 done task').onExpire!()
    await flush()
    expect(apiDelete.mock.calls.map(c => c[0])).toEqual(['/api/tasks/t3'])
    expect(store.clearDone('l1', { canDelete: () => false })).toBe(0)
  })

  it('a 403 on a delete commit restores the task with a translated reason', async () => {
    apiDelete.mockRejectedValue(Object.assign(new Error("only the task's creator, an assignee or an admin can change it"), { status: 403 }))
    store.removeTask(store.findTask('t1')!)
    toast('Deleted t1').onExpire!()
    await flush()
    expect(store.findTask('t1')).toBeTruthy()
    expect(toasts.value.find(x => x.type === 'error')!.message)
      .toBe("Couldn't delete: Only the creator, an assignee or an admin can change this task.")
  })

  it('clearDone with one task uses the singular message; with none does nothing', () => {
    store.removeTask(store.findTask('t3')!)
    expect(store.clearDone('l1')).toBe(1)
    expect(toast('Cleared 1 done task')).toBeTruthy()
    expect(store.clearDone('l1')).toBe(0)
  })

  it('leaving the page commits pending deletes with keepalive', async () => {
    apiDelete.mockResolvedValue(undefined)
    store.removeTask(store.findTask('t1')!)
    window.dispatchEvent(new Event('pagehide'))
    await flush()
    expect(apiDelete).toHaveBeenCalledWith('/api/tasks/t1', { keepalive: true })
  })

  it('removeList hides the list and its tasks; the commit DELETEs the list', async () => {
    apiDelete.mockResolvedValue(undefined)
    store.activeListId.value = 'l1'
    store.removeList({ id: 'l1', name: 'A' })
    expect(store.visibleLists.value).toEqual([])
    expect(store.openCount.value).toBe(0)
    toast('Deleted list A').onExpire!()
    await flush()
    expect(apiDelete).toHaveBeenCalledWith('/api/tasks/lists/l1')
    expect(store.lists.value).toEqual([])
    expect(store.tasksByList.value.l1).toBeUndefined()
  })

  it('Undo of removeList restores the list and re-selects it', () => {
    store.activeListId.value = 'l1'
    store.removeList({ id: 'l1', name: 'A' })
    store.activeListId.value = null
    toast('Deleted list A').action!.onClick()
    expect(store.visibleLists.value.map(l => l.id)).toEqual(['l1'])
    expect(store.activeListId.value).toBe('l1')
  })
})

describe('logout', () => {
  it('resetTasks also forgets the board filters', async () => {
    const f = await import('@/features/tasks/board/filters')
    f.setFilters('household:l1', { ...f.EMPTY_FILTERS, mine: true })
    resetTasks()
    expect(f.getFilters('household:l1')).toBe(f.EMPTY_FILTERS)
  })
})

describe('canEditTask', () => {
  const me = (user_id: string, is_admin = false) => ({ user_id, is_admin })
  it('household: creator, assignee or admin; nobody else', () => {
    const x = task('t', 'l', 'todo', { created_by: 'a', assignees: ['b'] })
    expect(canEditTask(x, me('a'), null)).toBe(true)
    expect(canEditTask(x, me('b'), null)).toBe(true)
    expect(canEditTask(x, me('z', true), null)).toBe(true)
    expect(canEditTask(x, me('z'), null)).toBe(false)
    expect(canEditTask(x, null, null)).toBe(false)
  })
  it('space tasks are collaborative', () => {
    expect(canEditTask(task('t', 'l', 'todo', { created_by: 'a' }), me('z'), 's1')).toBe(true)
  })
})

describe('counts', () => {
  it('openCount counts not-done, not-archived tasks across every loaded list', async () => {
    serve([{ id: 'l1', name: 'A' }, { id: 'l2', name: 'B' }], {
      l1: [task('t1', 'l1'), task('t2', 'l1', 'done')],
      l2: [task('t3', 'l2', 'in_progress'), task('t4', 'l2'),
        task('t5', 'l2', 'todo', { archived_at: '2026-01-01T00:00:00+00:00' })],
    })
    await loadAll(store)
    expect(store.openCount.value).toBe(3)
  })

  it('the roster alone gives the count — no list is fetched', async () => {
    serve([{ id: 'l1', name: 'A', open_count: 2 }, { id: 'l2', name: 'B', open_count: 5 }], {})
    await store.ensureLists()
    expect(apiGet.mock.calls.map(c => c[0])).toEqual(['/api/tasks/lists'])
    expect(store.openCount.value).toBe(7)
  })

  it('a loaded list counts its own rows (live); the others keep the roster count', async () => {
    serve([{ id: 'l1', name: 'A', open_count: 2 }, { id: 'l2', name: 'B', open_count: 5 }], {
      l1: [task('t1', 'l1'), task('t2', 'l1')],
    })
    await store.ensureLists()
    await store.ensureList('l1')
    expect(store.openCount.value).toBe(7)
    apiPatch.mockResolvedValue({ ...task('t1', 'l1'), status: 'done' })
    await store.toggleDone('t1')
    expect(store.openCount.value).toBe(6)
  })

  it('a list hidden behind Undo drops out of the count', async () => {
    serve([{ id: 'l1', name: 'A', open_count: 2 }, { id: 'l2', name: 'B', open_count: 5 }], {})
    await store.ensureLists()
    store.removeList(store.lists.value[1])
    expect(store.openCount.value).toBe(2)
  })
})

describe('WebSocket routing', () => {
  beforeEach(() => { wireTasksWs() })

  const fire = (type: string, data: Record<string, unknown>) => handlers[type]({ type, data })

  it('household frames reach the household store only; space frames their space', () => {
    const sp = spaceTaskStore('s1')
    fire('task.created', { space_id: null, task: task('h1', 'l1') })
    fire('task.created', { space_id: 's1', task: task('s1t', 'sl') })
    fire('task.created', { space_id: 's-unknown', task: task('x', 'xl') })
    expect(householdTaskStore.findTask('h1')).toBeTruthy()
    expect(householdTaskStore.findTask('s1t')).toBeUndefined()
    expect(sp.findTask('s1t')).toBeTruthy()
    expect(sp.findTask('h1')).toBeUndefined()
  })

  it('updated / completed / deleted frames update the row', () => {
    fire('task.created', { task: task('h1', 'l1') })
    fire('task.updated', { space_id: null, task: task('h1', 'l1', 'in_progress', { title: 'T' }) })
    expect(householdTaskStore.findTask('h1')?.title).toBe('T')
    fire('task.completed', { space_id: null, task_id: 'h1' })
    expect(householdTaskStore.findTask('h1')?.status).toBe('done')
    fire('task.deleted', { space_id: null, task_id: 'h1', list_id: 'l1' })
    expect(householdTaskStore.findTask('h1')).toBeUndefined()
  })

  it('list frames add, rename and remove lists (and their tasks)', () => {
    fire('task_list.created', { space_id: null, list_id: 'l1', name: 'A' })
    fire('task_list.created', { space_id: null, list_id: 'l1', name: 'A' })
    fire('task_list.updated', { space_id: null, list_id: 'l1', name: 'B' })
    expect(householdTaskStore.lists.value).toEqual([{ id: 'l1', name: 'B' }])
    fire('task.created', { task: task('h1', 'l1') })
    fire('task_list.deleted', { space_id: null, list_id: 'l1' })
    expect(householdTaskStore.lists.value).toEqual([])
    expect(householdTaskStore.findTask('h1')).toBeUndefined()
  })

  it('frames on a list never loaded refetch the roster once (debounced)', async () => {
    vi.useFakeTimers()
    serve([{ id: 'l1', name: 'A', open_count: 1 }], {})
    await householdTaskStore.ensureLists()
    expect(householdTaskStore.openCount.value).toBe(1)
    apiGet.mockClear()
    serve([{ id: 'l1', name: 'A', open_count: 0 }], {})
    fire('task.completed', { space_id: null, task_id: 'gone-1' })
    fire('task.updated', { space_id: null, task: task('x', 'l1', 'done') })
    fire('task.deleted', { space_id: null, task_id: 'gone-2' })
    expect(apiGet).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(apiGet.mock.calls.map(c => c[0])).toEqual(['/api/tasks/lists'])
    expect(householdTaskStore.openCount.value).toBe(0)
  })

  it('frames on a loaded list update it in place — no roster refetch', async () => {
    vi.useFakeTimers()
    serve([{ id: 'l1', name: 'A', open_count: 1 }], { l1: [task('t1', 'l1')] })
    await householdTaskStore.ensureLists()
    await householdTaskStore.ensureList('l1')
    apiGet.mockClear()
    fire('task.completed', { space_id: null, task_id: 't1' })
    expect(householdTaskStore.openCount.value).toBe(0)
    fire('task.created', { space_id: null, task: task('t2', 'l1') })
    expect(householdTaskStore.openCount.value).toBe(1)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('a space store loading or getting frames leaves the household count alone', async () => {
    vi.useFakeTimers()
    serve([{ id: 'l1', name: 'A', open_count: 4 }], {})
    await householdTaskStore.ensureLists()
    const sp = spaceTaskStore('s1')
    await sp.ensureLists()
    apiGet.mockClear()
    fire('task.created', { space_id: 's1', task: task('st', 'l1') })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(apiGet.mock.calls.map(c => c[0])).toEqual(['/api/spaces/s1/tasks/lists'])
    expect(householdTaskStore.openCount.value).toBe(4)
  })

  it('a reconnect refetches the roster count', async () => {
    serve([{ id: 'l1', name: 'A', open_count: 1 }], {})
    await householdTaskStore.ensureLists()
    serve([{ id: 'l1', name: 'A', open_count: 3 }], {})
    connectionState.value = 'reconnecting'
    connectionState.value = 'open'
    await flush()
    await flush()
    expect(householdTaskStore.openCount.value).toBe(3)
  })

  it('a reconnect revalidates what was loaded', async () => {
    serve([{ id: 'l1', name: 'A' }], { l1: [task('t1', 'l1')] })
    await loadAll(householdTaskStore)
    apiGet.mockClear()
    serve([{ id: 'l1', name: 'A' }], { l1: [task('t1', 'l1'), task('t2', 'l1')] })
    connectionState.value = 'reconnecting'
    connectionState.value = 'open'
    await flush()
    await flush()
    expect(apiGet.mock.calls.map(c => c[0]).sort()).toEqual([
      '/api/tasks/lists', '/api/tasks/lists/l1/tasks',
    ])
    expect(householdTaskStore.findTask('t2')).toBeTruthy()
  })
})

describe('reset', () => {
  it('resetTasks forgets the household and every space store', async () => {
    serve([{ id: 'l1', name: 'A' }], { l1: [task('t1', 'l1')] })
    await loadAll(householdTaskStore)
    householdTaskStore.activeListId.value = 'l1'
    const sp = spaceTaskStore('s1')
    resetTasks()
    expect(householdTaskStore.lists.value).toEqual([])
    expect(householdTaskStore.allTasks.value).toEqual([])
    expect(householdTaskStore.listsLoaded.value).toBe(false)
    expect(householdTaskStore.activeListId.value).toBeNull()
    expect(spaceTaskStore('s1')).not.toBe(sp)
  })

  it('a load that was in flight at reset does not write afterwards', async () => {
    const d = deferred<unknown>()
    apiGet.mockReturnValue(d.promise)
    const p = householdTaskStore.loadLists()
    resetTasks()
    d.resolve([{ id: 'l1', name: 'A' }])
    await p
    expect(householdTaskStore.lists.value).toEqual([])
  })
})

describe('board moves', () => {
  async function seed(s: TaskStore, rows: TaskItem[]) {
    apiGet.mockResolvedValueOnce(rows)
    await s.loadList('l1', { force: true })
  }

  it('createTask sends the column status, priority and labels in one request', async () => {
    apiPost.mockResolvedValue(task('n', 'l1', 'in_progress', { priority: 'high', labels: ['Garden'] }))
    await store.createTask('l1', 'n', { status: 'in_progress', priority: 'high', labels: ['Garden'] })
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/tasks',
      { title: 'n', status: 'in_progress', priority: 'high', labels: ['Garden'] })
    expect(store.findTask('n')?.status).toBe('in_progress')
  })

  it('a move within a column only reorders, with {order, moved_id}', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 0 }), task('b', 'l1', 'todo', { position: 1 })])
    apiPost.mockResolvedValue({ ok: true, count: 2 })
    const p = store.moveTask('b', 'todo', ['b', 'a'])
    // Optimistic at once.
    expect(store.findTask('b')?.position).toBe(0)
    expect(store.findTask('a')?.position).toBe(1)
    await p
    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/reorder', { order: ['b', 'a'], moved_id: 'b' })
  })

  it('a move across columns PATCHes the status, then reorders the target column', async () => {
    await seed(store, [task('a', 'l1', 'todo'), task('c', 'l1', 'in_progress', { position: 0 })])
    apiPatch.mockImplementation(async (_u: string, body: object) => ({ ...task('a', 'l1'), ...body }))
    apiPost.mockResolvedValue({ ok: true })
    const p = store.moveTask('a', 'in_progress', ['c', 'a'])
    expect(store.findTask('a')?.status).toBe('in_progress')
    expect(store.findTask('a')?.position).toBe(1)
    await p
    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', { status: 'in_progress' })
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/reorder', { order: ['c', 'a'], moved_id: 'a' })
    expect(store.findTask('a')?.position).toBe(1)
  })

  it('a WS echo of the status change (old position) does not make the card jump', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 2 }), task('c', 'l1', 'in_progress', { position: 0 })])
    const reorder = deferred<unknown>()
    apiPatch.mockImplementation(async (_u: string, body: object) => ({ ...task('a', 'l1', 'todo', { position: 2 }), ...body }))
    apiPost.mockReturnValue(reorder.promise)
    const p = store.moveTask('a', 'in_progress', ['a', 'c'])
    store.onTaskUpsert(task('a', 'l1', 'in_progress', { position: 2 }))
    store.onTaskUpsert(task('c', 'l1', 'in_progress', { position: 0 }))
    await flush()
    expect(store.findTask('a')?.position).toBe(0)
    expect(store.findTask('c')?.position).toBe(1)
    reorder.resolve({ ok: true })
    await p
    // Confirmed: later frames are the truth again.
    store.onTaskUpsert(task('a', 'l1', 'in_progress', { position: 5 }))
    expect(store.findTask('a')?.position).toBe(5)
  })

  it('status saved but reorder failed: rejects with partial: true', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 2 }), task('c', 'l1', 'done', { position: 0 })])
    apiPatch.mockImplementation(async (_u: string, body: object) => ({ ...task('a', 'l1'), ...body }))
    apiPost.mockRejectedValue(Object.assign(new Error('bad'), { status: 500 }))
    await expect(store.moveTask('a', 'done', ['a', 'c'])).rejects.toMatchObject({ partial: true })
  })

  it('a reorder failure without a status change is not partial', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 0 }), task('b', 'l1', 'todo', { position: 1 })])
    apiPost.mockRejectedValue(Object.assign(new Error('bad'), { status: 500 }))
    const err = await store.moveTask('b', 'todo', ['b', 'a']).catch(e => e)
    expect(err.partial).toBeUndefined()
  })

  it('serial runs jobs of one list one after another', async () => {
    const order: string[] = []
    const first = deferred<void>()
    const p1 = store.serial('l1', async () => { order.push('1 start'); await first.promise; order.push('1 end') })
    const p2 = store.serial('l1', async () => { order.push('2') })
    await flush()
    expect(order).toEqual(['1 start'])
    first.resolve()
    await Promise.all([p1, p2])
    expect(order).toEqual(['1 start', '1 end', '2'])
  })

  it('a failed job does not block the next one', async () => {
    const p1 = store.serial('l1', async () => { throw new Error('x') })
    const p2 = store.serial('l1', async () => 'ok')
    await expect(p1).rejects.toThrow('x')
    await expect(p2).resolves.toBe('ok')
  })

  it('a frame overridden by a pin reloads the list once the pin goes', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 0 }), task('b', 'l1', 'todo', { position: 1 })])
    const reorder = deferred<unknown>()
    apiPost.mockReturnValue(reorder.promise)
    const p = store.moveTask('b', 'todo', ['b', 'a'])
    // Someone else's change lands meanwhile and is held back by the pin.
    store.onTaskUpsert(task('a', 'l1', 'todo', { position: 7 }))
    expect(store.findTask('a')?.position).toBe(1)
    apiGet.mockResolvedValueOnce([task('a', 'l1', 'todo', { position: 7 }), task('b', 'l1', 'todo', { position: 0 })])
    reorder.resolve({ ok: true })
    await p
    await flush()
    expect(apiGet).toHaveBeenLastCalledWith('/api/tasks/lists/l1/tasks')
    expect(store.findTask('a')?.position).toBe(7)
  })

  it('a space store reorders on the space route', async () => {
    const s = spaceTaskStore('s1')
    await seed(s, [task('a', 'l1', 'todo'), task('b', 'l1', 'todo', { position: 1 })])
    apiPost.mockResolvedValue({ ok: true })
    await s.moveTask('b', 'todo', ['b', 'a'])
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/tasks/lists/l1/reorder', { order: ['b', 'a'], moved_id: 'b' })
  })

  it('a failed status PATCH rolls the status and positions back and skips the reorder', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 3 }), task('c', 'l1', 'done', { position: 0 })])
    apiPatch.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    await expect(store.moveTask('a', 'done', ['a', 'c'])).rejects.toMatchObject({ status: 403 })
    expect(store.findTask('a')).toMatchObject({ status: 'todo', position: 3 })
    expect(store.findTask('c')?.position).toBe(0)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('a failed reorder puts the positions back but keeps the saved status', async () => {
    await seed(store, [task('a', 'l1', 'todo', { position: 2 }), task('c', 'l1', 'done', { position: 0 })])
    apiPatch.mockImplementation(async (_u: string, body: object) => ({ ...task('a', 'l1', 'todo', { position: 2 }), ...body }))
    apiPost.mockRejectedValue(Object.assign(new Error('bad'), { status: 422 }))
    await expect(store.moveTask('a', 'done', ['a', 'c'])).rejects.toMatchObject({ status: 422 })
    expect(store.findTask('a')).toMatchObject({ status: 'done', position: 2 })
    expect(store.findTask('c')?.position).toBe(0)
  })
})

describe('held for review (202 {queued: true}, §4.3)', () => {
  const QUEUED = { queued: true, item_id: 'q1', feature: 'tasks', action: 'edit', entity: 'task', target_id: 'a' }
  let sp: TaskStore

  beforeEach(async () => {
    sp = createTaskStore('sp')
    apiGet.mockResolvedValueOnce([task('a', 'l1', 'todo', { position: 0 }), task('c', 'l1', 'in_progress', { position: 0 })])
    await sp.loadList('l1', { force: true })
  })

  it('a queued cross-column move puts the card back and skips the reorder', async () => {
    apiPatch.mockResolvedValueOnce(QUEUED)
    const p = sp.moveTask('a', 'in_progress', ['c', 'a'])
    expect(sp.findTask('a')?.status).toBe('in_progress')
    await expect(p).resolves.toBe('queued')
    expect(sp.findTask('a')).toMatchObject({ status: 'todo', position: 0 })
    expect(sp.findTask('c')?.position).toBe(0)
    expect(apiPost).not.toHaveBeenCalledWith(expect.stringContaining('/reorder'), expect.anything())
    expect(toasts.value.some(x => x.message === 'Submitted for review — a moderator will look at it')).toBe(true)
  })

  it('an applied move resolves "applied"', async () => {
    apiPatch.mockImplementation(async (_u: string, body: object) => ({ ...task('a', 'l1'), ...body }))
    apiPost.mockResolvedValue({ ok: true })
    await expect(sp.moveTask('a', 'in_progress', ['c', 'a'])).resolves.toBe('applied')
  })

  it('a queued create adds no row and resolves null', async () => {
    apiPost.mockResolvedValueOnce({ ...QUEUED, action: 'create', target_id: 'new' })
    await expect(sp.createTask('l1', 'New one')).resolves.toBeNull()
    expect(sp.tasksByList.value.l1.map(x => x.id)).toEqual(['a', 'c'])
  })

  it('a queued edit rolls the fields back', async () => {
    apiPatch.mockResolvedValueOnce(QUEUED)
    await expect(sp.patchTask('a', { title: 'Renamed' })).resolves.toBeNull()
    expect(sp.findTask('a')?.title).toBe('a')
  })

  it('a queued delete keeps the task', async () => {
    apiDelete.mockResolvedValueOnce({ ...QUEUED, action: 'delete' })
    await sp.deleteTasks(['a'])
    expect(sp.findTask('a')).toBeTruthy()
  })

  it('a queued list create resolves null and adds no list', async () => {
    apiPost.mockResolvedValueOnce({ ...QUEUED, entity: 'list', action: 'create' })
    await expect(sp.createList('Groceries')).resolves.toBeNull()
    expect(sp.lists.value).toEqual([])
  })
})
