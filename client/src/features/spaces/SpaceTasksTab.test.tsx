import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

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

describe('SpaceTasksTab', () => {
  it('exports the component + the resetSpaceTasks helper', async () => {
    const mod = await import('./SpaceTasksTab')
    expect(typeof mod.SpaceTasksTab).toBe('function')
    expect(typeof mod.resetSpaceTasks).toBe('function')
  })
})

describe('SpaceTasksTab checkbox', () => {
  it('ticks to done from to do / in progress and back to to do from done', async () => {
    const { api } = await import('@/api')
    const rows = [
      { id: 'a', list_id: 'l1', title: 'A', status: 'todo', position: 1, assignees: [], created_by: 'u1' },
      { id: 'b', list_id: 'l1', title: 'B', status: 'in_progress', position: 2, assignees: [], created_by: 'u1' },
      { id: 'c', list_id: 'l1', title: 'C', status: 'done', position: 3, assignees: [], created_by: 'u1' },
    ]
    vi.mocked(api.get).mockImplementation(async (url: string) =>
      url.endsWith('/tasks/lists') ? [{ id: 'l1', name: 'L', created_by: 'u1' }] : rows)
    vi.mocked(api.patch).mockImplementation(async (url: string, body: unknown) =>
      ({ ...rows.find(r => url.endsWith(r.id)), ...(body as object) }))
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const { SpaceTasksTab, resetSpaceTasks } = await import('./SpaceTasksTab')
    resetSpaceTasks()
    const { getByLabelText } = render(<SpaceTasksTab spaceId="s1" />)
    await waitFor(() => expect(getByLabelText('Toggle A')).toBeTruthy())
    fireEvent.click(getByLabelText('Toggle A'))
    fireEvent.click(getByLabelText('Toggle B'))
    fireEvent.click(getByLabelText('Toggle C'))
    await waitFor(() => expect(vi.mocked(api.patch).mock.calls).toEqual([
      ['/api/spaces/s1/tasks/a', { status: 'done' }],
      ['/api/spaces/s1/tasks/b', { status: 'done' }],
      ['/api/spaces/s1/tasks/c', { status: 'todo' }],
    ]))
  })
})
