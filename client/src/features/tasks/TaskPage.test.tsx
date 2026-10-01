import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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

const ADMIN = { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false }
const auth = vi.hoisted(() => ({ currentUser: { value: null as unknown } }))
vi.mock('@/store/auth', () => ({
  currentUser: auth.currentUser,
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map([['u2', { user_id: 'u2', username: 'bo', display_name: 'Bo' }]]) },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
  householdDisplayName: (uid: string) => uid,
}))

vi.mock('@/components/confirm', () => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
}))

function task(id: string, status: TaskItem['status'] = 'todo', extra: Partial<TaskItem> = {}): TaskItem {
  return {
    id, list_id: 'l1', title: id, description: null, status, position: 0,
    due_date: null, assignees: [], created_by: 'u1', ...extra,
  }
}

interface Fixtures {
  lists: { id: string; name: string }[]
  tasks: Record<string, TaskItem[]> | TaskItem[]
}

function wireApi(f: Fixtures): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/tasks/lists') return f.lists
    const m = /^\/api\/tasks\/lists\/([^/]+)\/tasks$/.exec(url)
    if (m) return Array.isArray(f.tasks) ? f.tasks : f.tasks[m[1]] ?? []
    return []
  })
  apiPost.mockResolvedValue({})
  apiPatch.mockImplementation(async (url: string, body: unknown) => {
    const all = Array.isArray(f.tasks) ? f.tasks : Object.values(f.tasks).flat()
    return { ...all.find(x => url.endsWith(`/${x.id}`)), ...(body as object) }
  })
  apiDelete.mockResolvedValue(undefined)
}

function makeDataTransfer(): DataTransfer {
  const store = new Map<string, string>()
  const types: string[] = []
  return {
    types,
    effectAllowed: 'all',
    setData(type: string, val: string) {
      store.set(type, val)
      if (!types.includes(type)) types.push(type)
    },
    getData(type: string) { return store.get(type) ?? '' },
  } as unknown as DataTransfer
}

async function setup(f: Fixtures) {
  wireApi(f)
  const tl = await import('@testing-library/preact')
  const toast = await import('@/components/Toast')
  toast.toasts.value = []
  const mod = await import('./TaskPage')
  const r = tl.render(<mod.default />)
  return { ...tl, ...r, toast, mod }
}

const ONE_LIST = [{ id: 'l1', name: 'House' }]

beforeEach(() => {
  auth.currentUser.value = ADMIN
  // These tests cover the List view (the board has its own suite).
  localStorage.clear()
  for (const id of ['l1', 'l2', 'l9']) localStorage.setItem(`sh-tasks-view:${id}`, 'list')
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('TaskPage', () => {
  it('renders the open status sections and a "Done (n) · Clear all" archive', async () => {
    const t = await setup({
      lists: ONE_LIST,
      tasks: [task('Fix tap'), task('Paint', 'in_progress'), task('Pay bill', 'done')],
    })
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-task-group').length).toBe(3))
    const headers = Array.from(t.container.querySelectorAll('.sh-task-group__name')).map(h => h.textContent)
    expect(headers).toEqual(['To do', 'In progress'])
    expect(t.getByText('Done (1)')).toBeTruthy()
    expect(t.getByRole('button', { name: 'Clear all done tasks' })).toBeTruthy()
    // The list name is the section heading; the counts sit beside it.
    expect(t.getByRole('heading', { level: 2, name: 'House' })).toBeTruthy()
    expect(t.container.querySelector('.sh-organize-header__counts')!.textContent).toBe('2 open·1 done')
  })

  it('the checkbox marks to do / in progress done and done back to to do', async () => {
    const t = await setup({
      lists: ONE_LIST,
      tasks: [task('a', 'todo', { position: 1 }), task('b', 'in_progress', { position: 2 }), task('c', 'done', { position: 3 })],
    })
    await t.waitFor(() => expect(t.getAllByRole('checkbox').length).toBe(3))
    fireAll(t.getAllByRole('checkbox'), t.fireEvent)
    await t.waitFor(() => expect(apiPatch.mock.calls).toEqual([
      ['/api/tasks/a', { status: 'done' }],
      ['/api/tasks/b', { status: 'done' }],
      ['/api/tasks/c', { status: 'todo' }],
    ]))
    expect(t.getByRole('checkbox', { name: 'Done: a' }).getAttribute('aria-checked')).toBe('true')
  })

  it('a failed roster load shows an error with Retry — not "No task lists yet"', async () => {
    apiGet.mockRejectedValueOnce(new Error('down'))
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    await t.waitFor(() => expect(t.getByRole('alert').textContent).toContain("Couldn't load your task lists."))
    expect(t.queryByText('No task lists yet')).toBeNull()
    wireApi({ lists: ONE_LIST, tasks: [task('a')] })
    t.fireEvent.click(t.getByRole('button', { name: 'Retry' }))
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy())
  })

  it("a failed tasks load shows an error in the list pane, not an empty list", async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/tasks/lists') return ONE_LIST
      throw new Error('boom')
    })
    await t.waitFor(() => expect(t.getByRole('alert').textContent).toContain("Couldn't load this list's tasks."))
    expect(t.queryByText('All caught up')).toBeNull()
  })

  it('no lists: the empty state focuses the new-list field', async () => {
    const t = await setup({ lists: [], tasks: [] })
    await t.waitFor(() => expect(t.getByText('No task lists yet')).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Create your first list' }))
    expect(document.activeElement).toBe(t.getByRole('textbox', { name: 'New list' }))
  })

  it('an empty list says so', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    await t.waitFor(() => expect(t.getByText('All caught up')).toBeTruthy())
  })

  it('returning to the tab shows the cached list at once and revalidates it', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy())
    t.unmount()
    vi.setSystemTime(Date.now() + 60_000)
    apiGet.mockClear()
    wireApi({ lists: ONE_LIST, tasks: [task('a'), task('b')] })
    const r = t.render(<t.mod.default />)
    // Cached rows straight away, no skeleton.
    expect(r.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy()
    await t.waitFor(() => expect(r.getByRole('checkbox', { name: 'Done: b' })).toBeTruthy())
    expect(apiGet.mock.calls.map(c => c[0])).toEqual(['/api/tasks/lists', '/api/tasks/lists/l1/tasks'])
  })

  it('switching lists loads the other list', async () => {
    const t = await setup({
      lists: [{ id: 'l1', name: 'House' }, { id: 'l2', name: 'Garden' }],
      tasks: { l1: [task('a')], l2: [task('g', 'todo', { list_id: 'l2' })] },
    })
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Garden' }))
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: g' })).toBeTruthy())
    expect(t.queryByRole('checkbox', { name: 'Done: a' })).toBeNull()
    expect(t.getByRole('heading', { level: 2, name: 'Garden' })).toBeTruthy()
  })

  it('delete hides the task at once — no confirm — and DELETEs when the Undo toast expires', async () => {
    const { confirmDialog } = await import('@/components/confirm')
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Delete Fix tap' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Fix tap' }))
    expect(confirmDialog).not.toHaveBeenCalled()
    expect(t.queryByRole('checkbox', { name: 'Done: Fix tap' })).toBeNull()
    expect(apiDelete).not.toHaveBeenCalled()
    const row = t.toast.toasts.value.find(x => x.message === 'Deleted Fix tap')!
    expect(row.action?.label).toBe('Undo')
    row.onExpire!()
    await t.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/tasks/Fix%20tap'))
  })

  it('Undo brings the task back without a DELETE', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Delete Fix tap' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Delete Fix tap' }))
    t.toast.toasts.value.find(x => x.message === 'Deleted Fix tap')!.action!.onClick()
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: Fix tap' })).toBeTruthy())
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('"Clear all" hides the done tasks with Undo — no confirm — and deletes exactly those', async () => {
    const { confirmDialog } = await import('@/components/confirm')
    const t = await setup({
      lists: ONE_LIST,
      tasks: [task('t1'), task('t2', 'done'), task('t3', 'done')],
    })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Clear all done tasks' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Clear all done tasks' }))
    expect(confirmDialog).not.toHaveBeenCalled()
    expect(t.queryByText('Done (2)')).toBeNull()
    t.toast.toasts.value.find(x => x.message === 'Cleared 2 done tasks')!.onExpire!()
    await t.waitFor(() => expect(apiDelete.mock.calls.map(c => c[0]).sort()).toEqual(['/api/tasks/t2', '/api/tasks/t3']))
  })

  it('shows ONE due chip: "Overdue · <date>" for a past date, "Today" for today', async () => {
    const pad = (n: number) => String(n).padStart(2, '0')
    const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    const past = new Date(); past.setDate(past.getDate() - 3)
    const t = await setup({
      lists: ONE_LIST,
      tasks: [
        task('late', 'todo', { due_date: iso(past), position: 1 }),
        task('now', 'todo', { due_date: iso(new Date()), position: 2 }),
      ],
    })
    await t.waitFor(() => expect(t.container.querySelectorAll('.sh-task-due').length).toBe(2))
    const late = t.container.querySelector('[data-task-id="late"]')!
    expect(late.querySelectorAll('.sh-task-due').length).toBe(1)
    expect(late.querySelector('.sh-task-due--overdue .sh-task-due__full')!.textContent).toMatch(/^Overdue · /)
    expect(late.querySelector('.sh-task-due__short')!.getAttribute('aria-hidden')).toBe('true')
    expect(late.textContent!.match(/overdue/gi)!.length).toBe(1)
    expect(t.container.querySelector('[data-task-id="now"] .sh-task-due--today')!.textContent).toBe('Today')
  })

  it('says who a task is assigned to in plain words; no cryptic "+ creator"', async () => {
    const t = await setup({
      lists: ONE_LIST,
      tasks: [
        task('a', 'todo', { assignees: ['u1', 'u2'], description: 'notes' }),
        task('b', 'todo', { assignees: ['u2'] }),
        task('c', 'todo', { created_by: 'u2' }),
      ],
    })
    await t.waitFor(() => expect(t.container.querySelector('[data-task-id="a"]')).not.toBeNull())
    const meta = (id: string) => t.container.querySelector(`[data-task-id="${id}"] .sh-task-item__meta`)
    expect(meta('a')!.querySelector('.sh-task-item__people')!.textContent).toBe('Assigned to You & Bo')
    expect(meta('a')!.querySelector('.sr-only')!.textContent).toBe('Assigned to ')
    expect(meta('b')!.querySelector('.sh-task-item__people')!.textContent).toBe('Assigned to Bo')
    // Nobody assigned: no byline at all (the creator shows in the dialog).
    expect(meta('c')).toBeNull()
    const notes = meta('a')!.querySelector('.sh-task-item__notes')!
    expect(notes.getAttribute('title')).toBe('Has notes')
    expect(notes.textContent).toContain('Has notes')
    // The edit button keeps a short name and is described by the byline.
    const btn = t.getByRole('button', { name: 'Edit a' })
    expect(btn.getAttribute('aria-describedby')).toBe(meta('a')!.id)
  })

  it('the edit dialog says who added the task', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a', 'todo', { created_by: 'u2' })] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit a' }))
    expect(await t.findByText('Added by Bo')).toBeTruthy()
  })

  it('the edit dialog says "Added by you" for your own task', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit a' }))
    expect(await t.findByText('Added by you')).toBeTruthy()
  })

  it('an empty list shows no "0 open · 0 done" counts', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    await t.waitFor(() => expect(t.getByText('All caught up')).toBeTruthy())
    expect(t.container.querySelector('.sh-organize-header__counts')).toBeNull()
  })

  it('a failed tasks load hides the quick-add bar — only the error and Retry show', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/tasks/lists') return ONE_LIST
      throw new Error('boom')
    })
    await t.waitFor(() => expect(t.getByRole('alert')).toBeTruthy())
    expect(t.queryByRole('textbox', { name: 'New task in House' })).toBeNull()
  })

  it('drop pads name the status ("Drop into “In progress”")', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    await t.waitFor(() => expect(t.container.querySelector('.sh-task-item')).not.toBeNull())
    t.fireEvent.dragStart(t.container.querySelector('.sh-task-item')!, { dataTransfer: makeDataTransfer() })
    expect(t.container.querySelector('.sh-task-group--in_progress .sh-drop-pad')!.textContent)
      .toBe('Drop into “In progress”')
  })

  it('drag onto another section is optimistic, then PATCHes the status', async () => {
    let resolve: ((v: unknown) => void) | null = null
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    apiPatch.mockImplementation(() => new Promise<unknown>((r) => { resolve = r }))
    await t.waitFor(() => expect(t.container.querySelector('.sh-task-item')).not.toBeNull())
    const row = t.container.querySelector('.sh-task-item') as HTMLElement
    expect(row.getAttribute('draggable')).toBe('true')
    const dt = makeDataTransfer()
    t.fireEvent.dragStart(row, { dataTransfer: dt })
    const ip = t.container.querySelector('.sh-task-group--in_progress') as HTMLElement
    // The empty section shows a drop pad while dragging.
    expect(ip.querySelector('.sh-drop-pad')).not.toBeNull()
    t.fireEvent.dragOver(ip, { dataTransfer: dt })
    t.fireEvent.drop(ip, { dataTransfer: dt })
    await t.waitFor(() => {
      const rows = t.container.querySelectorAll('.sh-task-group--in_progress .sh-task-item')
      expect(rows.length).toBe(1)
    })
    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/Fix%20tap', { status: 'in_progress' })
    resolve!(task('Fix tap', 'in_progress'))
  })

  it('a failed status change rolls back and says so', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    apiPatch.mockRejectedValue(new Error('nope'))
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('checkbox', { name: 'Done: a' }))
    await t.waitFor(() => expect(t.toast.toasts.value.some(x => x.type === 'error')).toBe(true))
    expect(t.getByRole('checkbox', { name: 'Done: a' }).getAttribute('aria-checked')).toBe('false')
  })

  it('edit dialog: the status picker is a radiogroup and Save sends only what changed', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Edit Fix tap' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Edit Fix tap' }))
    const group = await t.findByRole('radiogroup', { name: 'Status' })
    const done = t.within(group).getByRole('radio', { name: 'Done' })
    t.fireEvent.click(done)
    await t.waitFor(() => expect(done.getAttribute('aria-checked')).toBe('true'))
    t.fireEvent.click(t.getByRole('button', { name: 'Save' }))
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/tasks/Fix%20tap', { status: 'done' }))
    await t.waitFor(() => expect(t.queryByRole('radiogroup', { name: 'Status' })).toBeNull())
  })

  it('edit dialog: focus lands on the Name field (fine pointer)', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit Fix tap' }))
    const name = await t.findByLabelText('Name')
    await t.waitFor(() => expect(document.activeElement).toBe(name))
  })

  it('edit dialog: an empty name is refused inline', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('Fix tap')] })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Edit Fix tap' })).toBeTruthy())
    t.fireEvent.click(t.getByRole('button', { name: 'Edit Fix tap' }))
    const name = await t.findByLabelText('Name')
    t.fireEvent.input(name, { target: { value: '  ' } })
    t.fireEvent.submit(name.closest('form')!)
    expect(t.getByRole('alert').textContent).toBe('A task needs a name.')
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('quick-add posts the task to the active list', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiPost.mockResolvedValue(task('Water plants'))
    const input = await t.findByRole('textbox', { name: 'New task in House' })
    t.fireEvent.input(input, { target: { value: 'Water plants' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists/l1/tasks', { title: 'Water plants' }))
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: Water plants' })).toBeTruthy())
    expect((input as HTMLInputElement).value).toBe('')
  })

  it('the new-list field creates and opens the list', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiPost.mockResolvedValue({ id: 'l9', name: 'Garden' })
    const input = await t.findByRole('textbox', { name: 'New list' })
    t.fireEvent.input(input, { target: { value: 'Garden' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => expect(t.getByRole('heading', { level: 2, name: 'Garden' })).toBeTruthy())
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists', { name: 'Garden' })
  })

  it('the list ⋯ menu renames inline', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiPatch.mockResolvedValue({ id: 'l1', name: 'Home' })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Rename' }))
    const input = t.getByRole('textbox', { name: 'Rename House' })
    t.fireEvent.input(input, { target: { value: 'Home' } })
    t.fireEvent.keyDown(input, { key: 'Enter' })
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/tasks/lists/l1', { name: 'Home' }))
    expect(t.getByRole('heading', { level: 2, name: 'Home' })).toBeTruthy()
  })

  it('deleting a list asks first, then hides it with Undo', async () => {
    const { confirmDialog } = await import('@/components/confirm')
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Delete list' }))
    await t.waitFor(() => expect(t.getByText('No task lists yet')).toBeTruthy())
    expect(confirmDialog).toHaveBeenCalled()
    expect(apiDelete).not.toHaveBeenCalled()
    t.toast.toasts.value.find(x => x.message === 'Deleted list House')!.action!.onClick()
    await t.waitFor(() => expect(t.getByRole('heading', { level: 2, name: 'House' })).toBeTruthy())
  })
})

describe('TaskPage edit rights (household)', () => {
  // Lena (u3) is neither creator nor assignee of "theirs", and not an admin.
  const LENA = { ...ADMIN, user_id: 'u3', username: 'lena', display_name: 'Lena', is_admin: false }
  const theirs = () => task('theirs', 'todo', { created_by: 'u2', due_date: '2099-01-02', position: 1 })
  const mine = () => task('mine', 'todo', { created_by: 'u3', position: 2 })

  beforeEach(() => { auth.currentUser.value = LENA })

  it("someone else's task: the tick is inert with a reason, no ✕, not draggable", async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [theirs(), mine()] })
    const box = await t.findByRole('checkbox', { name: 'Done: theirs' })
    expect(box.getAttribute('aria-disabled')).toBe('true')
    expect(box.getAttribute('title')).toBe('Only the creator, an assignee or an admin can change this task.')
    t.fireEvent.click(box)
    expect(apiPatch).not.toHaveBeenCalled()
    expect(t.queryByRole('button', { name: 'Delete theirs' })).toBeNull()
    expect(t.container.querySelector('[data-task-id="theirs"]')!.getAttribute('draggable')).toBe('false')
    // Your own task stays fully editable.
    expect(t.getByRole('checkbox', { name: 'Done: mine' }).getAttribute('aria-disabled')).toBeNull()
    expect(t.getByRole('button', { name: 'Delete mine' })).toBeTruthy()
    expect(t.container.querySelector('[data-task-id="mine"]')!.getAttribute('draggable')).toBe('true')
  })

  it('the dialog opens read-only: titled "Task details", static text, the reason, no Save', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [
      task('theirs', 'in_progress', { created_by: 'u2', description: 'Line one\nLine two', due_date: '2099-01-02' }),
    ] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit theirs' }))
    const dialog = await t.findByRole('dialog', { name: 'Task details' })
    // No form controls at all — nothing that looks editable.
    expect(dialog.querySelectorAll('input, textarea, select, [role=radiogroup]').length).toBe(0)
    expect(t.getByText('Only the creator, an assignee or an admin can change this task.')).toBeTruthy()
    const value = (label: string) => {
      const dt = Array.from(dialog.querySelectorAll('dt')).find(d => d.textContent === label)!
      return dt.nextElementSibling as HTMLElement
    }
    expect(value('Status').textContent).toBe('In progress')
    expect(value('Name').textContent).toBe('theirs')
    expect(value('Description').textContent).toBe('Line one\nLine two')
    expect(value('Description').className).toContain('sh-task-edit__notes')
    expect(value('Due date').textContent).toBe(new Date(2099, 0, 2).toLocaleDateString('en', { dateStyle: 'full' }))
    expect(t.getByText('Added by Bo')).toBeTruthy()
    expect(t.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(t.getByRole('button', { name: 'Close' })).toBeTruthy()
  })

  it('read-only: no notes / no due date read as muted placeholders', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('theirs', 'todo', { created_by: 'u2' })] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit theirs' }))
    const dialog = await t.findByRole('dialog', { name: 'Task details' })
    const none = Array.from(dialog.querySelectorAll('dd.sh-task-edit__empty')).map(d => d.textContent)
    expect(none).toEqual(['No notes', 'No due date', 'None', 'No labels', 'Nobody assigned'])
  })

  it('an editable task keeps the "Edit task" title and form fields', async () => {
    auth.currentUser.value = ADMIN
    const t = await setup({ lists: ONE_LIST, tasks: [theirs()] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit theirs' }))
    const dialog = await t.findByRole('dialog', { name: 'Edit task' })
    expect(dialog.querySelector('input[type=text]')).not.toBeNull()
  })

  it('"Clear all" only clears the done tasks you may delete, and counts those', async () => {
    const t = await setup({
      lists: ONE_LIST,
      tasks: [task('d1', 'done', { created_by: 'u2' }), task('d2', 'done', { created_by: 'u3' })],
    })
    t.fireEvent.click(await t.findByRole('button', { name: 'Clear all done tasks' }))
    const row = t.toast.toasts.value.find(x => x.message === 'Cleared 1 done task')!
    expect(row).toBeTruthy()
    expect(t.getByRole('checkbox', { name: 'Done: d1' })).toBeTruthy()
    row.onExpire!()
    await t.waitFor(() => expect(apiDelete.mock.calls.map(c => c[0])).toEqual(['/api/tasks/d2']))
  })

  it('no clearable done tasks: no "Clear all"', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('d1', 'done', { created_by: 'u2' })] })
    await t.findByText('Done (1)')
    expect(t.queryByRole('button', { name: 'Clear all done tasks' })).toBeNull()
  })

  it('a 403 from the server is a translated toast, not the backend English', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [mine()] })
    apiPatch.mockRejectedValue(Object.assign(new Error("only the task's creator, an assignee or an admin can change it"), { status: 403 }))
    t.fireEvent.click(await t.findByRole('checkbox', { name: 'Done: mine' }))
    await t.waitFor(() => expect(t.toast.toasts.value.map(x => x.message))
      .toContain('Only the creator, an assignee or an admin can change this task.'))
  })
})

describe('TaskPage edit dialog vs concurrent changes', () => {
  it('saves only what you changed — a field someone else changed meanwhile survives', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a', 'todo', { description: 'old' })] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit a' }))
    const group = await t.findByRole('radiogroup', { name: 'Status' })
    // Someone else edits the description while the dialog is open.
    const { householdTaskStore } = await import('@/store/tasks')
    householdTaskStore.onTaskUpsert(task('a', 'todo', { description: 'theirs' }))
    t.fireEvent.click(t.within(group).getByRole('radio', { name: 'Done' }))
    t.fireEvent.click(t.getByRole('button', { name: 'Save' }))
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', { status: 'done' }))
  })

  it('a task deleted while the dialog is open: "This task was deleted" and it closes', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a')] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Edit a' }))
    const group = await t.findByRole('radiogroup', { name: 'Status' })
    const { householdTaskStore } = await import('@/store/tasks')
    householdTaskStore.onTaskDeleted('a')
    t.fireEvent.click(t.within(group).getByRole('radio', { name: 'Done' }))
    t.fireEvent.click(t.getByRole('button', { name: 'Save' }))
    await t.waitFor(() => expect(t.toast.toasts.value.map(x => x.message)).toContain('This task was deleted'))
    expect(apiPatch).not.toHaveBeenCalled()
    await t.waitFor(() => expect(t.queryByRole('radiogroup', { name: 'Status' })).toBeNull())
  })
})

describe('TaskPage rename fields', () => {
  it('Escape never saves, and the blur that follows is a no-op', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Rename' }))
    const input = t.getByRole('textbox', { name: 'Rename House' })
    expect(input.getAttribute('maxlength')).toBe('100')
    t.fireEvent.input(input, { target: { value: 'Nope' } })
    t.fireEvent.keyDown(input, { key: 'Escape' })
    t.fireEvent.blur(input)
    await new Promise(r => setTimeout(r, 20))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('Enter saves once, even with the blur that follows', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    apiPatch.mockResolvedValue({ id: 'l1', name: 'Home' })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Rename' }))
    const input = t.getByRole('textbox', { name: 'Rename House' })
    t.fireEvent.input(input, { target: { value: 'Home' } })
    t.fireEvent.keyDown(input, { key: 'Enter' })
    t.fireEvent.blur(input)
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(1))
    await new Promise(r => setTimeout(r, 20))
    expect(apiPatch).toHaveBeenCalledTimes(1)
  })

  it('the new-list field caps names at the backend limit', async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [] })
    expect((await t.findByRole('textbox', { name: 'New list' })).getAttribute('maxlength')).toBe('100')
  })

  it("the edit button is also described by the due chip", async () => {
    const t = await setup({ lists: ONE_LIST, tasks: [task('a', 'todo', { due_date: '2099-03-04' })] })
    const btn = await t.findByRole('button', { name: 'Edit a' })
    const due = t.container.querySelector('[data-task-id="a"] .sh-task-due')!
    expect(btn.getAttribute('aria-describedby')!.split(' ')).toContain(due.id)
  })
})

describe('TaskPage on a phone', () => {
  let mm: typeof window.matchMedia
  beforeEach(() => {
    mm = window.matchMedia
    window.matchMedia = ((q: string) => ({
      matches: q === '(max-width: 639px)', media: q,
      addEventListener() {}, removeEventListener() {},
    })) as unknown as typeof window.matchMedia
  })
  afterEach(() => { window.matchMedia = mm })

  const LISTS = [{ id: 'l1', name: 'House' }, { id: 'l2', name: 'Garden' }]
  const TASKS = { l1: [task('a')], l2: [task('g', 'todo', { list_id: 'l2' })] }

  it('shows a compact list selector instead of the lists card', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    const trigger = await t.findByRole('button', { name: 'List: House — switch or manage lists' })
    expect(trigger.textContent).toContain('House')
    expect(t.container.querySelector('.sh-tasks-sidebar')).toBeNull()
    // The name isn't repeated as a visible heading under the picker.
    expect(t.getByRole('heading', { level: 2, name: 'House' }).className).toBe('sr-only')
    expect(t.queryByRole('textbox', { name: 'New list' })).toBeNull()
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: a' })).toBeTruthy())
  })

  it('the picker switches lists and ticks the active one', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    t.fireEvent.click(await t.findByRole('button', { name: 'List: House — switch or manage lists' }))
    expect(t.getByRole('menuitemradio', { name: /House/ }).getAttribute('aria-checked')).toBe('true')
    t.fireEvent.click(t.getByRole('menuitemradio', { name: /Garden/ }))
    await t.waitFor(() => expect(t.getByRole('checkbox', { name: 'Done: g' })).toBeTruthy())
    expect(t.getByRole('button', { name: 'List: Garden — switch or manage lists' })).toBeTruthy()
  })

  it('"+ New list…" opens a name field that creates and opens the list', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    apiPost.mockResolvedValue({ id: 'l9', name: 'Errands' })
    t.fireEvent.click(await t.findByRole('button', { name: 'List: House — switch or manage lists' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: '+ New list…' }))
    const input = t.getByRole('textbox', { name: 'New list' })
    t.fireEvent.input(input, { target: { value: 'Errands' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => expect(t.getByRole('button', { name: 'List: Errands — switch or manage lists' })).toBeTruthy())
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/lists', { name: 'Errands' })
  })

  it('the picker only picks: "+ New list…" first, then the lists', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    t.fireEvent.click(await t.findByRole('button', { name: 'List: House — switch or manage lists' }))
    const items = Array.from(t.getByRole('menu').querySelectorAll('[role^="menuitem"]')).map(i => i.textContent)
    expect(items).toEqual(['+ New list…', 'House✓', 'Garden'])
  })

  it("the current list's ⋯ renames and deletes it", async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    apiPatch.mockResolvedValue({ id: 'l1', name: 'Home' })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    expect(t.getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Rename list', 'Delete list'])
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Rename list' }))
    const input = t.getByRole('textbox', { name: 'Rename House' })
    t.fireEvent.input(input, { target: { value: 'Home' } })
    t.fireEvent.keyDown(input, { key: 'Enter' })
    await t.waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/tasks/lists/l1', { name: 'Home' }))
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for Home' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Delete list' }))
    await t.waitFor(() => expect(t.getByRole('button', { name: 'List: Garden — switch or manage lists' })).toBeTruthy())
  })

  it('a tap on the sheet scrim closes it and never reaches what is underneath', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    t.fireEvent.click(await t.findByRole('button', { name: 'List: House — switch or manage lists' }))
    const scrim = t.container.ownerDocument.querySelector('.sh-tasks-picker__scrim') as HTMLElement
    expect(scrim).not.toBeNull()
    const below = vi.fn()
    document.addEventListener('click', below)
    t.fireEvent.pointerDown(scrim)
    // Still there for the click that follows the press.
    expect(document.querySelector('.sh-tasks-picker__scrim')).not.toBeNull()
    t.fireEvent.click(scrim)
    document.removeEventListener('click', below)
    expect(below).not.toHaveBeenCalled()
    await t.waitFor(() => expect(document.querySelector('.sh-tasks-picker__scrim')).toBeNull())
    expect(t.queryByRole('menu')).toBeNull()
  })

  it('Escape in the picker rename returns focus to the picker and saves nothing', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    t.fireEvent.click(await t.findByRole('button', { name: 'List actions for House' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: 'Rename list' }))
    const input = t.getByRole('textbox', { name: 'Rename House' })
    expect(input.getAttribute('maxlength')).toBe('100')
    t.fireEvent.input(input, { target: { value: 'Nope' } })
    t.fireEvent.keyDown(input, { key: 'Escape' })
    t.fireEvent.blur(input)
    await t.waitFor(() => expect(document.activeElement)
      .toBe(t.getByRole('button', { name: 'List: House — switch or manage lists' })))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('Escape in the picker new-list field closes it and returns focus to the picker', async () => {
    const t = await setup({ lists: LISTS, tasks: TASKS })
    t.fireEvent.click(await t.findByRole('button', { name: 'List: House — switch or manage lists' }))
    t.fireEvent.click(t.getByRole('menuitem', { name: '+ New list…' }))
    const input = t.getByRole('textbox', { name: 'New list' })
    expect(input.getAttribute('maxlength')).toBe('100')
    t.fireEvent.keyDown(input, { key: 'Escape' })
    await t.waitFor(() => expect(t.queryByRole('textbox', { name: 'New list' })).toBeNull())
    await t.waitFor(() => expect(document.activeElement)
      .toBe(t.getByRole('button', { name: 'List: House — switch or manage lists' })))
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('with no lists, the empty state still offers creating one', async () => {
    const t = await setup({ lists: [], tasks: [] })
    t.fireEvent.click(await t.findByRole('button', { name: 'Create your first list' }))
    expect(document.activeElement).toBe(t.getByRole('textbox', { name: 'New list' }))
  })
})

function fireAll(els: HTMLElement[], fireEvent: { click: (el: HTMLElement) => boolean }) {
  for (const el of els) fireEvent.click(el)
}
