import { describe, it, expect, vi } from 'vitest'
import type { DirectoryEntry } from '@/types'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: {
    value: {
      user_id: 'u1',
      username: 'a',
      display_name: 'A',
      is_admin: true,
      picture_url: null,
      picture_hash: null,
      bio: null,
      is_new_member: false,
    },
  },
  token: { value: 'tok' },
  isAuthed: { value: true },
}))

const localEntry: DirectoryEntry = {
  space_id: 's1', host_instance_id: 'local',
  host_display_name: 'Your household', host_is_paired: true,
  name: 'Book Club', description: null, emoji: null,
  member_count: 0, scope: 'public', join_mode: 'open',
  // The list endpoint ships no features block, so the mapper starts here.
  allow_subscribers: undefined,
  min_age: 0,
}

describe('SpaceBrowserPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SpaceBrowserPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)
})

// The "Your household" tab is built from `GET /api/spaces`, which does NOT
// ship the `features` block — only `GET /api/spaces/{id}` does. Coercing the
// missing flag to `false` made every local public space claim 🔒 "Content is
// private" (a lie whenever the owner had followers ON) and suppressed the
// 🔔 Subscribe button, which needs an explicit `true`.
describe('hydrateLocalReadability', () => {
  it('fills allow_subscribers in from the space detail endpoint', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: 's1', features: { allow_subscribers: true },
    })
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's1' },
    ])
    expect(out.allow_subscribers).toBe(true)
  }, 20000)

  it('leaves the flag unknown when the detail fetch fails', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('nope'),
    )
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's2' },
    ])
    // `undefined`, never `false` — "we don't know" must not render as
    // "the content is private".
    expect(out.allow_subscribers).toBeUndefined()
  }, 20000)

  it('does not fetch anything for a household-scope space', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockClear()
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's3', scope: 'household' },
    ])
    expect(api.get).not.toHaveBeenCalled()
    expect(out.allow_subscribers).toBeUndefined()
  }, 20000)
})
