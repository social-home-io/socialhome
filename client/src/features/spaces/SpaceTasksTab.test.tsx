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
