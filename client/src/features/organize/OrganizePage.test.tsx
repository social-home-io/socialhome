/**
 * OrganizePage — the hub's ``?tab=`` routing, its translated tab strip
 * and the count chips: open tasks across EVERY list (not just the one
 * the Tasks tab has open), household stickies only (never a space
 * board's that happen to sit in the shared sticky signal), shopping
 * minus items hidden behind an Undo toast. Each source is fetched once.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { TaskItem } from '@/types'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn(),
  },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true } },
  token: { value: 'tok' },
  isAuthed: { value: true },
  onLogout: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
}))

function task(id: string, list: string, status: TaskItem['status'] = 'todo'): TaskItem {
  return { id, list_id: list, title: id, description: null, status, position: 0, due_date: null, assignees: [], created_by: 'u1' }
}

const TASKS: Record<string, TaskItem[]> = {
  l1: [task('a', 'l1'), task('b', 'l1', 'done')],
  l2: [task('c', 'l2', 'in_progress'), task('d', 'l2')],
}

function serve() {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/tasks/lists') return [{ id: 'l1', name: 'House' }, { id: 'l2', name: 'Garden' }]
    const m = /^\/api\/tasks\/lists\/([^/]+)\/tasks$/.exec(url)
    if (m) return TASKS[m[1]] ?? []
    if (url === '/api/stickies') return [{ id: 's1', space_id: null }, { id: 's2', space_id: null }]
    if (url.startsWith('/api/shopping/stores')) return []
    if (url.startsWith('/api/shopping')) return [
      { id: 'i1', text: 'Milk', completed: false },
      { id: 'i2', text: 'Eggs', completed: false },
      { id: 'i3', text: 'Old', completed: true },
    ]
    return []
  })
}

/** Stub the three tab bodies — these tests are about the hub. */
function stubTabs() {
  vi.doMock('@/features/tasks/TaskPage', () => ({ default: () => <p>tasks-tab</p> }))
  vi.doMock('@/features/shopping/ShoppingPage', () => ({ default: () => <p>shopping-tab</p> }))
  vi.doMock('@/features/stickies/StickyBoardPage', () => ({ default: () => <p>stickies-tab</p> }))
}

async function mount(url: string) {
  history.replaceState(null, '', url)
  const tl = await import('@testing-library/preact')
  const { LocationProvider } = await import('preact-iso')
  const { default: OrganizePage } = await import('./OrganizePage')
  const r = tl.render(<LocationProvider><OrganizePage /></LocationProvider>)
  const tabs = () => r.getAllByRole('tab').map(b => b.textContent)
  return { ...tl, ...r, tabs }
}

beforeEach(() => {
  vi.resetModules()
  vi.doUnmock('@/features/tasks/TaskPage')
  vi.doUnmock('@/features/shopping/ShoppingPage')
  vi.doUnmock('@/features/stickies/StickyBoardPage')
  apiGet.mockReset()
  serve()
})

describe('OrganizePage', () => {
  it('opens the tab named in ?tab= and defaults to Tasks', async () => {
    stubTabs()
    let t = await mount('/organize?tab=shopping')
    expect(t.getByText('shopping-tab')).toBeTruthy()
    t.unmount()
    t = await mount('/organize?tab=stickies')
    expect(t.getByText('stickies-tab')).toBeTruthy()
    t.unmount()
    t = await mount('/organize?tab=bogus')
    expect(t.getByText('tasks-tab')).toBeTruthy()
  })

  it('picking a tab routes to it (Tasks drops the query)', async () => {
    stubTabs()
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.tabs()[1]).toMatch(/^Shopping/))
    t.fireEvent.click(t.getAllByRole('tab')[1])
    await t.waitFor(() => expect(location.pathname + location.search).toBe('/organize?tab=shopping'))
    expect(t.getByText('shopping-tab')).toBeTruthy()
    t.fireEvent.click(t.getAllByRole('tab')[0])
    await t.waitFor(() => expect(location.pathname + location.search).toBe('/organize'))
  })

  it('counts open tasks across every list, household stickies and unbought items', async () => {
    stubTabs()
    const t = await mount('/organize?tab=shopping')
    await t.waitFor(() => expect(t.tabs()).toEqual(['Tasks · 3', 'Shopping · 2', 'Stickies · 2']))
    expect(t.getByRole('tablist').getAttribute('aria-label')).toBe('Organize sections')
  })

  it('fetches each source once', async () => {
    stubTabs()
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.tabs()[0]).toBe('Tasks · 3'))
    const urls = apiGet.mock.calls.map(c => c[0] as string).sort()
    expect(urls.filter(u => u.startsWith('/api/tasks'))).toEqual([
      '/api/tasks/lists', '/api/tasks/lists/l1/tasks', '/api/tasks/lists/l2/tasks',
    ])
    expect(urls.filter(u => u === '/api/stickies')).toHaveLength(1)
  })

  it('the stickies chip ignores a space board left in the shared sticky signal', async () => {
    stubTabs()
    const stickies = await import('@/store/stickies')
    stickies.stickies.value = Array.from({ length: 5 }, (_, i) => ({
      id: `sp${i}`, author: 'u1', content: '', color: '#fff', position_x: 0, position_y: 0,
      created_at: '', updated_at: '', space_id: 'space-1',
    }))
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.tabs()[2]).toBe('Stickies · 2'))
  })

  it('a shopping item hidden behind Undo drops out of the chip', async () => {
    stubTabs()
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.tabs()[1]).toBe('Shopping · 2'))
    const { undoableDelete } = await import('@/utils/undoableDelete')
    undoableDelete({ ids: ['i1'], message: 'Deleted Milk', commit: async () => {} })
    await t.waitFor(() => expect(t.tabs()[1]).toBe('Shopping · 1'))
  })

  it('tab labels follow the UI language', async () => {
    stubTabs()
    const i18n = await import('@/i18n/i18n')
    const de = (await import('@/i18n/locales/de.json')).default as Record<string, string>
    await i18n.setLocale('de')
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.tabs()).toEqual(['Aufgaben · 3', 'Einkauf · 2', 'Notizen · 2']))
    expect(t.getByRole('tablist').getAttribute('aria-label')).toBe(de['organize.label'])
  })

  it('with the real Tasks tab mounted, the tasks are still fetched once each', async () => {
    vi.doMock('@/features/shopping/ShoppingPage', () => ({ default: () => <p>shopping-tab</p> }))
    vi.doMock('@/features/stickies/StickyBoardPage', () => ({ default: () => <p>stickies-tab</p> }))
    const t = await mount('/organize')
    await t.waitFor(() => expect(t.getByRole('heading', { level: 2, name: 'House' })).toBeTruthy())
    await t.waitFor(() => expect(t.tabs()[0]).toBe('Tasks · 3'))
    const urls = apiGet.mock.calls.map(c => c[0] as string).filter(u => u.startsWith('/api/tasks')).sort()
    expect(urls).toEqual(['/api/tasks/lists', '/api/tasks/lists/l1/tasks', '/api/tasks/lists/l2/tasks'])
  })
})
