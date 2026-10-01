/**
 * The household Tasks tab's board view (the default): columns with
 * counts, per-column quick-add, "N done · Clear all", keyboard and ⋯
 * moves with an aria-live line, pointer drag, filters, the Board | List
 * toggle, read-only cards and the phone column switcher.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import type { TaskItem } from '@/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

const ADMIN = { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: false, picture_url: null, bio: null, is_new_member: false }
vi.mock('@/store/auth', () => ({
  currentUser: { value: ADMIN },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/householdUsers', () => ({
  householdUsers: {
    value: new Map([
      ['u1', { user_id: 'u1', username: 'admin', display_name: 'Admin', picture_url: null }],
      ['u2', { user_id: 'u2', username: 'bo', display_name: 'Bo', picture_url: null }],
    ]),
  },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
  householdDisplayName: (uid: string) => uid,
}))

function task(id: string, status: TaskItem['status'] = 'todo', extra: Partial<TaskItem> = {}): TaskItem {
  return {
    id, list_id: 'l1', title: id, description: null, status, position: 0,
    due_date: null, assignees: [], created_by: 'u1', priority: null, labels: [], ...extra,
  }
}

function wireApi(rows: TaskItem[]) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/tasks/lists') return [{ id: 'l1', name: 'House' }]
    if (url === '/api/tasks/lists/l1/tasks') return rows
    return []
  })
  apiPatch.mockImplementation(async (url: string, body: unknown) =>
    ({ ...rows.find(x => url.endsWith(`/${x.id}`)), ...(body as object) }))
  apiPost.mockImplementation(async (url: string, body: { title?: string; status?: TaskItem['status'] }) =>
    url.endsWith('/tasks')
      ? task(`new-${body.title}`, body.status ?? 'todo', { title: body.title ?? '', position: 99 })
      : { ok: true })
  apiDelete.mockResolvedValue(undefined)
}

async function setup(rows: TaskItem[]) {
  wireApi(rows)
  const tl = await import('@testing-library/preact')
  const toast = await import('@/components/Toast')
  toast.toasts.value = []
  const mod = await import('../TaskPage')
  const r = tl.render(<mod.default />)
  await tl.waitFor(() => expect(r.container.querySelector('.sh-board-column')).toBeTruthy())
  const col = (s: string) => r.container.querySelector<HTMLElement>(`.sh-board-column--${s}`)!
  const titles = (s: string) => Array.from(col(s).querySelectorAll('.sh-board-card__title')).map(b => b.textContent)
  const live = () => r.container.querySelector('.sh-board > p[aria-live]')!.textContent
  return { ...tl, ...r, toast, col, titles, live }
}

const BOARD = [
  task('Fix tap', 'todo', { position: 0 }),
  task('Paint', 'todo', { position: 1 }),
  task('Mow', 'in_progress', { position: 0 }),
  task('Pay bill', 'done', { position: 0 }),
  task('Theirs', 'done', { position: 1, created_by: 'u2' }),
]

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
})

afterEach(() => { vi.useRealTimers() })

describe('TaskBoard', () => {
  it('shows To do · In progress · Done with counts, cards in position order', async () => {
    const t = await setup(BOARD)
    const names = Array.from(t.container.querySelectorAll('.sh-board-column__name')).map(h => h.textContent)
    expect(names).toEqual(['To do', 'In progress', 'Done'])
    expect(t.titles('todo')).toEqual(['Fix tap', 'Paint'])
    expect(t.col('todo').querySelector('.sh-board-column__count')!.textContent).toBe('2 tasks2')
    expect(t.col('in_progress').querySelector('.sh-board-column__count')!.textContent).toBe('1 task1')
  })

  it('Done ends with "N done · Clear all" that only clears what you may delete', async () => {
    const t = await setup(BOARD)
    expect(t.col('done').querySelector('.sh-archive-divider')!.textContent).toContain('2 done')
    t.fireEvent.click(t.getByRole('button', { name: 'Clear all done tasks' }))
    await t.waitFor(() => expect(t.titles('done')).toEqual(['Theirs']))
    expect(t.toast.toasts.value[0]?.message).toBe('Cleared 1 done task')
  })

  it('"+ Add task" in a column creates the task in that column in one request', async () => {
    const t = await setup(BOARD)
    expect(t.col('done').querySelector('.sh-board-column__add')).toBeNull()
    t.fireEvent.click(t.getByRole('button', { name: /Add task.*In progress/ }))
    const input = await t.findByLabelText('New task in In progress') as HTMLInputElement
    t.fireEvent.input(input, { target: { value: 'Wash car' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => expect(t.titles('in_progress')).toEqual(['Mow', 'Wash car']))
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/tasks', { title: 'Wash car', status: 'in_progress' })
    // The field stays open for the next one; Escape closes it.
    const again = t.getByLabelText('New task in In progress')
    t.fireEvent.keyDown(again, { key: 'Escape' })
    await t.waitFor(() => expect(t.queryByLabelText('New task in In progress')).toBeNull())
  })

  it('Alt+→ moves a card to the next column: status PATCH, then reorder {order, moved_id}', async () => {
    const t = await setup(BOARD)
    const card = t.getByRole('button', { name: 'Paint' })
    card.focus()
    t.fireEvent.keyDown(card, { key: 'ArrowRight', altKey: true })
    await t.waitFor(() => expect(t.titles('in_progress')).toEqual(['Mow', 'Paint']))
    expect(t.live()).toBe('Moved “Paint” to In progress, 2 of 2')
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/reorder',
      { order: ['Mow', 'Paint'], moved_id: 'Paint' }))
    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/Paint', { status: 'in_progress' })
    // Focus follows the card into its new column.
    await t.waitFor(() => expect(document.activeElement?.textContent).toBe('Paint'))
  })

  it('Alt+↑ reorders within the column — no status change', async () => {
    const t = await setup(BOARD)
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Paint' }), { key: 'ArrowUp', altKey: true })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['Paint', 'Fix tap']))
    expect(t.live()).toBe('Moved “Paint” to To do, 1 of 2')
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/reorder',
      { order: ['Paint', 'Fix tap'], moved_id: 'Paint' }))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('the ⋯ menu moves to a column and up / down (disabled at the ends)', async () => {
    const t = await setup(BOARD)
    t.fireEvent.click(t.getByRole('button', { name: 'Card actions for Fix tap' }))
    const items = t.getAllByRole('menuitem').map(i => [i.textContent, i.getAttribute('aria-disabled')])
    expect(items).toEqual([
      ['Move to In progress', null], ['Move to Done', null],
      ['Move up', 'true'], ['Move down', null], ['Edit', null], ['Delete', null],
    ])
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Move to Done' }))
    await t.waitFor(() => expect(t.titles('done')).toEqual(['Pay bill', 'Theirs', 'Fix tap']))
    expect(t.live()).toBe('Moved “Fix tap” to Done, 3 of 3')
  })

  it('a refused move rolls back and says so', async () => {
    const t = await setup(BOARD)
    apiPatch.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Fix tap' }), { key: 'ArrowRight', altKey: true })
    await t.waitFor(() => expect(t.toast.toasts.value[0]?.message)
      .toBe('Only the creator, an assignee or an admin can change this task.'))
    expect(t.titles('todo')).toEqual(['Fix tap', 'Paint'])
    expect(t.live()).toBe('Couldn\'t move “Fix tap” — it\'s back where it was')
  })

  it('status saved but its place not: says so and reloads the list', async () => {
    const t = await setup(BOARD)
    apiPost.mockImplementation(async (url: string) => {
      if (url.endsWith('/reorder')) throw Object.assign(new Error('boom'), { status: 500 })
      return { ok: true }
    })
    const gets = () => apiGet.mock.calls.filter(c => c[0] === '/api/tasks/lists/l1/tasks').length
    const before = gets()
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Paint' }), { key: 'ArrowRight', altKey: true })
    await t.waitFor(() => expect(t.toast.toasts.value[0]?.message)
      .toBe('Moved to In progress, but couldn\'t set its place'))
    expect(t.live()).toBe('Moved to In progress, but couldn\'t set its place')
    await t.waitFor(() => expect(gets()).toBe(before + 1))
  })

  it('two quick moves run one after another, each planned from the last result', async () => {
    const t = await setup([task('a', 'todo', { position: 0 }), task('b', 'todo', { position: 1 }), task('c', 'todo', { position: 2 })])
    let release!: () => void
    apiPost.mockImplementationOnce(() => new Promise((res) => { release = () => res({ ok: true }) }))
    apiPost.mockResolvedValue({ ok: true })
    const a = t.getByRole('button', { name: 'a' })
    t.fireEvent.keyDown(a, { key: 'ArrowDown', altKey: true })
    t.fireEvent.keyDown(a, { key: 'ArrowDown', altKey: true })
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1))
    expect(apiPost.mock.calls[0]).toEqual(['/api/tasks/lists/l1/reorder', { order: ['b', 'a', 'c'], moved_id: 'a' }])
    await new Promise(r => setTimeout(r, 20))
    // The second waits for the first.
    expect(apiPost).toHaveBeenCalledTimes(1)
    release()
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    expect(apiPost.mock.calls[1]).toEqual(['/api/tasks/lists/l1/reorder', { order: ['b', 'c', 'a'], moved_id: 'a' }])
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['b', 'c', 'a']))
  })

  it('with a filter on, "n of total" counts the cards shown', async () => {
    const t = await setup([
      task('x1', 'todo', { position: 0, labels: ['Garden'] }),
      task('hidden', 'todo', { position: 1 }),
      task('x2', 'todo', { position: 2, labels: ['Garden'] }),
    ])
    t.fireEvent.change(t.getByLabelText('Label'), { target: { value: 'Garden' } })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['x1', 'x2']))
    t.fireEvent.keyDown(t.getByRole('button', { name: 'x1' }), { key: 'ArrowDown', altKey: true })
    await t.waitFor(() => expect(t.live()).toBe('Moved “x1” to To do, 2 of 2'))
  })

  it('a card you may not change: 🔒 with the reason, no menu, no keyboard move', async () => {
    const t = await setup(BOARD)
    const card = t.container.querySelector<HTMLElement>('[data-task-id="Theirs"]')!
    expect(card.classList.contains('sh-board-card--locked')).toBe(true)
    expect(card.querySelector('.sh-board-card__lock')!.getAttribute('title'))
      .toBe('Only the creator, an assignee or an admin can change this task.')
    expect(t.queryByRole('button', { name: 'Card actions for Theirs' })).toBeNull()
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Theirs' }), { key: 'ArrowUp', altKey: true })
    await new Promise(r => setTimeout(r, 0))
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('a mouse drag onto another column drops at the insertion point', async () => {
    const t = await setup(BOARD)
    const card = t.container.querySelector<HTMLElement>('[data-task-id="Fix tap"]')!
    const mow = t.container.querySelector<HTMLElement>('[data-task-id="Mow"]')!
    mow.getBoundingClientRect = () => ({ top: 100, height: 40, left: 0, width: 200, right: 200, bottom: 140, x: 0, y: 100, toJSON() {} }) as DOMRect
    const original = document.elementFromPoint
    document.elementFromPoint = (() => t.col('in_progress')) as typeof document.elementFromPoint
    onTestFinished(() => { document.elementFromPoint = original })
    t.fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 10, clientY: 10, pointerType: 'mouse' })
    t.fireEvent.pointerMove(window, { pointerId: 1, clientX: 30, clientY: 60 })
    // Mid-drag: a ghost follows and a line marks the drop point (above Mow).
    await t.waitFor(() => expect(t.container.querySelector('.sh-board-ghost')).toBeTruthy())
    expect(t.col('in_progress').querySelector('.sh-board-insert')).toBeTruthy()
    t.fireEvent.pointerUp(window, { pointerId: 1, clientX: 30, clientY: 60 })
    // The click a browser sends after the release doesn't open the card.
    t.fireEvent.click(t.getByRole('button', { name: 'Fix tap' }))
    expect(t.queryByRole('dialog')).toBeNull()
    await t.waitFor(() => expect(t.titles('in_progress')).toEqual(['Fix tap', 'Mow']))
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/reorder',
      { order: ['Fix tap', 'Mow'], moved_id: 'Fix tap' }))
    expect(t.container.querySelector('.sh-board-ghost')).toBeNull()
  })

  it('filters: text, Assigned to me, and Clear filters', async () => {
    const t = await setup([
      task('Water roses', 'todo', { labels: ['Garden'], assignees: ['u1'] }),
      task('Homework', 'todo', { labels: ['School'], priority: 'urgent' }),
      task('Rake', 'done', { labels: ['Garden'] }),
    ])
    t.fireEvent.input(t.getByLabelText('Search tasks'), { target: { value: 'garden' } })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['Water roses']))
    expect(t.titles('done')).toEqual(['Rake'])
    expect(t.getByText('Showing 2 of 3 tasks')).toBeTruthy()
    t.fireEvent.click(t.getByRole('button', { name: 'Assigned to me' }))
    await t.waitFor(() => expect(t.titles('done')).toEqual([]))
    expect(t.col('done').textContent).toContain('No matching tasks')
    t.fireEvent.click(t.getByRole('button', { name: 'Clear filters' }))
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['Water roses', 'Homework']))
  })

  it('the "Filters" button (shown on a narrow board) folds the other filters, open while one is on', async () => {
    const t = await setup(BOARD)
    const toggle = t.getByRole('button', { name: 'Filters' })
    const more = t.container.querySelector('.sh-board-filters__more')!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(more.getAttribute('data-open')).toBe('false')
    t.fireEvent.click(toggle)
    await t.waitFor(() => expect(more.getAttribute('data-open')).toBe('true'))
    t.fireEvent.click(t.getByRole('button', { name: 'Assigned to me' }))
    t.fireEvent.click(t.getByRole('button', { name: 'Filters · 1' }))
    // Still open: a filter is on.
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Filters · 1' }).getAttribute('aria-expanded')).toBe('true'))
  })

  it('filters by label and priority (incl. "None")', async () => {
    const t = await setup([
      task('A', 'todo', { labels: ['Garden'], priority: 'high' }),
      task('B', 'todo', { labels: ['School'] }),
    ])
    t.fireEvent.change(t.getByLabelText('Label'), { target: { value: 'School' } })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['B']))
    t.fireEvent.change(t.getByLabelText('Label'), { target: { value: '' } })
    t.fireEvent.change(t.getByLabelText('Priority'), { target: { value: 'none' } })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['B']))
    t.fireEvent.change(t.getByLabelText('Priority'), { target: { value: 'high' } })
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['A']))
  })

  it('Board | List is remembered per list', async () => {
    const t = await setup(BOARD)
    t.fireEvent.click(t.getByRole('radio', { name: 'List' }))
    await t.waitFor(() => expect(t.container.querySelector('.sh-task-group')).toBeTruthy())
    expect(localStorage.getItem('sh-tasks-view:l1')).toBe('list')
    t.unmount()
    const again = await import('@testing-library/preact')
    const mod = await import('../TaskPage')
    const r2 = again.render(<mod.default />)
    await again.waitFor(() => expect(r2.container.querySelector('.sh-task-group')).toBeTruthy())
    expect(r2.container.querySelector('.sh-board')).toBeNull()
  })

  it('the list view shows priority and labels on rows', async () => {
    localStorage.setItem('sh-tasks-view:l1', 'list')
    wireApi([task('Homework', 'todo', { labels: ['School', 'Urgent', 'Maths'], priority: 'urgent' })])
    const tl = await import('@testing-library/preact')
    const mod = await import('../TaskPage')
    const r = tl.render(<mod.default />)
    await tl.waitFor(() => expect(r.container.querySelector('.sh-task-item')).toBeTruthy())
    const row = r.container.querySelector('.sh-task-item')!
    expect(row.querySelector('.sh-task-priority--urgent')!.textContent).toBe('Priority: Urgent')
    expect(Array.from(row.querySelectorAll('.sh-task-label')).map(l => l.textContent)).toEqual(['School', 'Urgent', '+1'])
  })

  it('opens the detail dialog from a card', async () => {
    const t = await setup(BOARD)
    t.fireEvent.click(t.getByRole('button', { name: 'Paint' }))
    expect(await t.findByRole('dialog', { name: 'Edit task' })).toBeTruthy()
  })

  it('an empty list shows three empty columns', async () => {
    const t = await setup([])
    expect(Array.from(t.container.querySelectorAll('.sh-board-column__empty')).map(p => p.textContent))
      .toEqual(['No tasks', 'No tasks', 'No tasks'])
  })
})

describe('TaskBoard layout', () => {
  it('the board uses the compact list picker, not the sidebar; the list view keeps the sidebar', async () => {
    const t = await setup(BOARD)
    expect(t.container.querySelector('.sh-tasks-sidebar')).toBeNull()
    expect(t.getByRole('button', { name: 'List: House — switch or manage lists' })).toBeTruthy()
    t.fireEvent.click(t.getByRole('radio', { name: 'List' }))
    await t.waitFor(() => expect(t.container.querySelector('.sh-tasks-sidebar')).toBeTruthy())
    expect(t.queryByRole('button', { name: /switch or manage lists/ })).toBeNull()
  })

  it('the picker menu lists the lists and the list actions', async () => {
    const t = await setup(BOARD)
    t.fireEvent.click(t.getByRole('button', { name: 'List: House — switch or manage lists' }))
    expect(Array.from(t.getByRole('menu').querySelectorAll('[role^="menuitem"]')).map(i => i.textContent))
      .toEqual(['+ New list…', 'House✓'])
    t.fireEvent.keyDown(t.getByRole('menu'), { key: 'Escape' })
    t.fireEvent.click(t.getByRole('button', { name: 'List actions for House' }))
    expect(t.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Rename list', 'Delete list'])
  })

  describe('a board too narrow for three columns', () => {
    let RO: typeof globalThis.ResizeObserver | undefined
    beforeEach(() => {
      RO = globalThis.ResizeObserver
      globalThis.ResizeObserver = class {
        constructor(private cb: ResizeObserverCallback) {}
        observe() {
          this.cb([{ contentRect: { width: 500 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
        }
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    })
    afterEach(() => { globalThis.ResizeObserver = RO! })

    it('shows one column with the column switcher instead of clipping', async () => {
      const t = await setup(BOARD)
      await t.waitFor(() => expect(t.container.querySelector('.sh-board-switcher')).toBeTruthy())
      expect(t.container.querySelectorAll('.sh-board-column').length).toBe(1)
      expect(t.container.querySelector('.sh-board__columns--one')).toBeTruthy()
    })
  })

  it('a wide board shows all three columns and no switcher', async () => {
    const RO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(private cb: ResizeObserverCallback) {}
      observe() { this.cb([{ contentRect: { width: 900 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    onTestFinished(() => { globalThis.ResizeObserver = RO })
    const t = await setup(BOARD)
    expect(t.container.querySelectorAll('.sh-board-column').length).toBe(3)
    expect(t.container.querySelector('.sh-board-switcher')).toBeNull()
  })
})

describe('TaskBoard on a phone', () => {
  let mm: typeof window.matchMedia
  beforeEach(() => {
    mm = window.matchMedia
    window.matchMedia = ((q: string) => ({
      matches: q.includes('max-width'), media: q,
      addEventListener() {}, removeEventListener() {},
    })) as unknown as typeof window.matchMedia
  })
  afterEach(() => { window.matchMedia = mm })

  it('shows one column at a time, switched by chips with counts', async () => {
    const t = await setup(BOARD)
    expect(t.container.querySelectorAll('.sh-board-column').length).toBe(1)
    const chips = t.getAllByRole('radio').filter(r => r.closest('.sh-board-switcher'))
    expect(chips.map(c => c.textContent)).toEqual(['To do · 2', 'In progress · 1', 'Done · 2'])
    // The chips double as drop targets while dragging.
    expect(chips.map(c => c.getAttribute('data-board-drop'))).toEqual(['todo', 'in_progress', 'done'])
    t.fireEvent.click(chips[2])
    await t.waitFor(() => expect(t.titles('done')).toEqual(['Pay bill', 'Theirs']))
  })

  it('a refused move goes back to the column it came from, focus on the card', async () => {
    const t = await setup(BOARD)
    apiPatch.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    const card = t.getByRole('button', { name: 'Fix tap' })
    card.focus()
    t.fireEvent.keyDown(card, { key: 'ArrowRight', altKey: true })
    await t.waitFor(() => expect(t.toast.toasts.value[0]?.type).toBe('error'))
    await t.waitFor(() => expect(t.titles('todo')).toEqual(['Fix tap', 'Paint']))
    expect(t.getAllByRole('radio').find(r => r.closest('.sh-board-switcher') && r.getAttribute('aria-checked') === 'true')!.textContent)
      .toMatch(/^To do/)
    await t.waitFor(() => expect(document.activeElement?.textContent).toBe('Fix tap'))
  })

  it('a keyboard move follows the card to its new column', async () => {
    const t = await setup(BOARD)
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Fix tap' }), { key: 'ArrowRight', altKey: true })
    await t.waitFor(() => expect(t.titles('in_progress')).toEqual(['Mow', 'Fix tap']))
  })
})
