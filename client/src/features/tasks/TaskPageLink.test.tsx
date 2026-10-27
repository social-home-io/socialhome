/** ``?list={id}&task={id}`` — the link a task notification opens:
 *  that list, the task's detail open. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { TaskItem } from '@/types'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))
const where = vi.hoisted(() => ({ query: {} as Record<string, string> }))
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn(), url: '/organize', path: '/organize', query: where.query }),
  useRoute: () => ({ params: {}, query: where.query }),
}))
const auth = vi.hoisted(() => ({ currentUser: { value: null as unknown } }))
vi.mock('@/store/auth', () => ({
  currentUser: auth.currentUser,
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
  householdDisplayName: (uid: string) => uid,
}))

function task(id: string, list = 'l2'): TaskItem {
  return {
    id, list_id: list, title: `Task ${id}`, description: null, status: 'todo', position: 0,
    due_date: null, assignees: [], created_by: 'u1',
  }
}

async function setup() {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/tasks/lists') return [{ id: 'l1', name: 'House' }, { id: 'l2', name: 'Garden' }]
    if (url === '/api/tasks/lists/l1/tasks') return [task('a', 'l1')]
    if (url === '/api/tasks/lists/l2/tasks') return [task('mow'), task('rake')]
    return []
  })
  const tl = await import('@testing-library/preact')
  const toast = await import('@/components/Toast')
  toast.toasts.value = []
  const mod = await import('./TaskPage')
  return { ...tl, ...tl.render(<mod.default />), toast }
}

beforeEach(() => {
  auth.currentUser.value = { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true }
  localStorage.clear()
  for (const id of ['l1', 'l2']) localStorage.setItem(`sh-tasks-view:${id}`, 'list')
  vi.resetModules()
  apiGet.mockReset()
  where.query = {}
})

describe('TaskPage — a link to a task', () => {
  it('opens the linked list and the task', async () => {
    where.query = { list: 'l2', task: 'rake' }
    const t = await setup()
    await t.waitFor(() => expect(t.getByRole('heading', { level: 2, name: 'Garden' })).toBeTruthy())
    await t.waitFor(() => expect(t.getByRole('dialog')).toBeTruthy())
    expect((t.getByRole('dialog').querySelector('input') as HTMLInputElement | null)?.value
      ?? t.getByRole('dialog').textContent).toContain('Task rake')
  })

  it('a task that is gone says so and stays on the list', async () => {
    where.query = { list: 'l2', task: 'gone' }
    const t = await setup()
    await t.waitFor(() => expect(t.toast.toasts.value.map(x => x.message))
      .toContain('That task is no longer available.'))
    expect(t.queryByRole('dialog')).toBeNull()
  })

  it('a list that is gone says so', async () => {
    where.query = { list: 'l-gone', task: 'x' }
    const t = await setup()
    await t.waitFor(() => expect(t.toast.toasts.value.map(x => x.message))
      .toContain('That task is no longer available.'))
  })

  it('no link: the first list, no dialog', async () => {
    const t = await setup()
    await t.waitFor(() => expect(t.getByRole('heading', { level: 2, name: 'House' })).toBeTruthy())
    expect(t.queryByRole('dialog')).toBeNull()
  })
})
