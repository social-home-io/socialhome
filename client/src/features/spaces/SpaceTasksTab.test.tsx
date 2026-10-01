/**
 * SpaceTasksTab: the household board / list under a space scope — the
 * space routes, the space's members (incl. other households') as
 * people, collaborative edit rights, read-only subscribers and live
 * ``task.*`` frames for this space.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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

const handlers = vi.hoisted(() => ({} as Record<string, (e: { type: string; data: unknown }) => void>))
vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  return {
    connectionState: signal('open'),
    ws: {
      on: (type: string, h: (e: { type: string; data: unknown }) => void) => {
        handlers[type] = h
        return () => { delete handlers[type] }
      },
    },
  }
})

vi.mock('@/store/auth', () => ({
  currentUser: {
    value: {
      user_id: 'u1', username: 'admin', display_name: 'Admin',
      is_admin: true, picture_url: null, bio: null, is_new_member: false,
    },
  },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const MEMBERS = [
  { user_id: 'u1', role: 'owner', space_display_name: null, display_name: 'Admin', picture_url: null },
  // A member of another household: named by their space name, never a uid stub.
  { user_id: 'remote-user-0123456789', role: 'member', space_display_name: 'Lena Remote',
    display_name: null, picture_url: null, instance_id: 'inst-b', household_name: 'Millers' },
  { user_id: 'sub1', role: 'subscriber', space_display_name: 'Sam Sub', picture_url: null },
]

function task(id: string, status: TaskItem['status'], extra: Partial<TaskItem> = {}): TaskItem {
  return {
    id, list_id: 'l1', title: id, description: null, status, position: 0,
    due_date: null, assignees: [], created_by: 'u1', ...extra,
  }
}

function wire(rows: TaskItem[]) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1/members') return MEMBERS
    if (url === '/api/spaces/s1/tasks/lists') return [{ id: 'l1', name: 'Trip', created_by: 'u1' }]
    if (url === '/api/spaces/s1/tasks/lists/l1/tasks') return rows
    return []
  })
  apiPatch.mockImplementation(async (url: string, body: unknown) =>
    ({ ...rows.find(r => url.endsWith(r.id)), ...(body as object) }))
  apiPost.mockImplementation(async (url: string, body: { title?: string; status?: TaskItem['status'] }) =>
    url.endsWith('/tasks') ? task('new', body.status ?? 'todo', { title: body.title ?? '' }) : { ok: true })
}

async function setup(props: { writable: boolean; archived?: boolean }, rows: TaskItem[]) {
  wire(rows)
  const tl = await import('@testing-library/preact')
  const { SpaceTasksTab } = await import('./SpaceTasksTab')
  const { wireTasksWs } = await import('@/store/tasks')
  wireTasksWs()
  const r = tl.render(<SpaceTasksTab spaceId="s1" {...props} />)
  await tl.waitFor(() => expect(r.container.querySelector('.sh-board')).toBeTruthy())
  return { ...tl, ...r }
}

beforeEach(() => {
  vi.resetModules()
  for (const k of Object.keys(handlers)) delete handlers[k]
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
  localStorage.clear()
})

describe('SpaceTasksTab', () => {
  it('shows the space board, naming a remote member by their space name', async () => {
    const t = await setup({ writable: true }, [
      task('Book train', 'todo', { assignees: ['remote-user-0123456789'] }),
    ])
    await t.waitFor(() => expect(t.container.textContent).toContain('Assigned to Lena Remote'))
    expect(t.container.textContent).not.toContain('remote')
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/tasks/lists/l1/tasks')
  })

  it('a writable member adds into a column on the space route', async () => {
    const t = await setup({ writable: true }, [])
    t.fireEvent.click(t.getAllByRole('button', { name: /Add task.*In progress/ })[0])
    const input = await t.findByLabelText('New task in In progress') as HTMLInputElement
    t.fireEvent.input(input, { target: { value: 'Pack' } })
    t.fireEvent.submit(input.closest('form')!)
    await t.waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/spaces/s1/tasks/lists/l1/tasks', { title: 'Pack', status: 'in_progress' }))
  })

  it('a subscriber reads only: no add, no list actions, locked cards with the reason', async () => {
    const t = await setup({ writable: false }, [task('Book train', 'todo')])
    expect(t.queryByRole('button', { name: /Add task/ })).toBeNull()
    expect(t.queryByLabelText('New list')).toBeNull()
    expect(t.queryByRole('button', { name: /Card actions/ })).toBeNull()
    expect(t.container.querySelector('.sh-board-card--locked')).toBeTruthy()
    expect(t.getByRole('note').textContent).toContain('Subscribers can see')
    // Said once for the board, not as a lock on every card.
    expect(t.container.querySelector('.sh-board-card__lock')).toBeNull()
    expect(t.container.textContent).toContain('Subscribers can see this space’s tasks but not change them.')
  })

  it('while the viewer\'s role is loading it shows a skeleton, not locked cards', async () => {
    wire([task('Book train', 'todo')])
    const tl = await import('@testing-library/preact')
    const { SpaceTasksTab } = await import('./SpaceTasksTab')
    const r = tl.render(<SpaceTasksTab spaceId="s1" writable={undefined} />)
    expect(r.container.querySelector('[aria-busy="true"]')).toBeTruthy()
    expect(r.container.querySelector('.sh-board-card')).toBeNull()
    r.rerender(<SpaceTasksTab spaceId="s1" writable={true} />)
    await tl.waitFor(() => expect(r.container.querySelector('.sh-board-card--draggable')).toBeTruthy())
  })

  it('an archived space is read-only even for its owner', async () => {
    const t = await setup({ writable: true, archived: true }, [task('Book train', 'todo')])
    expect(t.queryByRole('button', { name: /Add task/ })).toBeNull()
    expect(t.container.textContent).toContain('This space is archived')
  })

  it('updates live from task.* frames for this space only', async () => {
    const t = await setup({ writable: true }, [task('Book train', 'todo')])
    handlers['task.created']?.({ type: 'task.created', data: { space_id: 's1', task: task('Hotel', 'in_progress') } })
    await t.waitFor(() => expect(t.getByRole('button', { name: 'Hotel' })).toBeTruthy())
    handlers['task.created']?.({ type: 'task.created', data: { space_id: 'other', task: task('Elsewhere', 'todo') } })
    handlers['task.created']?.({ type: 'task.created', data: { task: task('Household', 'todo') } })
    await new Promise(r => setTimeout(r, 0))
    expect(t.queryByRole('button', { name: 'Elsewhere' })).toBeNull()
    expect(t.queryByRole('button', { name: 'Household' })).toBeNull()
  })

  it('list view: the checkbox ticks to done and back to to do on the space route', async () => {
    localStorage.setItem('sh-tasks-view:l1', 'list')
    const rows = [task('A', 'todo'), task('B', 'in_progress'), task('C', 'done')]
    wire(rows)
    const tl = await import('@testing-library/preact')
    const { SpaceTasksTab } = await import('./SpaceTasksTab')
    const r = tl.render(<SpaceTasksTab spaceId="s1" writable={true} />)
    await tl.waitFor(() => expect(r.getByLabelText('Done: A')).toBeTruthy())
    tl.fireEvent.click(r.getByLabelText('Done: A'))
    tl.fireEvent.click(r.getByLabelText('Done: B'))
    tl.fireEvent.click(r.getByLabelText('Done: C'))
    await tl.waitFor(() => expect(apiPatch.mock.calls).toEqual([
      ['/api/spaces/s1/tasks/A', { status: 'done' }],
      ['/api/spaces/s1/tasks/B', { status: 'done' }],
      ['/api/spaces/s1/tasks/C', { status: 'todo' }],
    ]))
  })
})
