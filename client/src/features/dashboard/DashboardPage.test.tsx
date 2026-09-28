import { describe, it, expect, vi } from 'vitest'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const wsTypes: string[] = []
vi.mock('@/ws', () => ({
  ws: { on: (type: string) => { wsTypes.push(type); return () => {} }, send: vi.fn() },
}))

describe('DashboardPage', () => {
  it('refreshes on the frame names the server actually emits', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const { default: DashboardPage } = await import('./DashboardPage')
    const { api } = await import('@/api')
    // Keep every fetch pending — this test is about the subscriptions,
    // not the rendered bundle.
    vi.mocked(api.get).mockReturnValue(new Promise(() => {}))
    wsTypes.length = 0
    render(<DashboardPage />)
    await waitFor(() => expect(wsTypes).toContain('dm.message'))
    expect(wsTypes).toEqual(expect.arrayContaining([
      'notification.new', 'notification.unread_count',
      'calendar.created', 'calendar.updated', 'calendar.deleted',
    ]))
    expect(wsTypes).not.toContain('notification.created')
    expect(wsTypes).not.toContain('notification.read_changed')
    expect(wsTypes.filter(t => t.startsWith('calendar.event.'))).toEqual([])
  })

  it('module exports a default component', async () => {
    const mod = await import('./DashboardPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})
