/**
 * A new space's Feed (owner decision 3): its owner is asked "What is it
 * for?" while the space is in their ``space_setup_pending`` preference —
 * never a member, never once chosen — and a space where the owner is
 * still alone offers "Invite your first member".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))

const user = vi.hoisted(() => ({
  value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, preferences_json: '{}' } as Record<string, unknown>,
}))
vi.mock('@/store/auth', () => ({
  currentUser: user,
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1', section: undefined }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

const openInvite = vi.fn()
vi.mock('@/components/RemoteInviteDialog', () => ({
  openRemoteInviteDialog: (...a: unknown[]) => openInvite(...a),
}))

function wire(role: string, members: { user_id: string; role: string }[]) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') {
      return {
        id: 's1', name: 'Book club', space_type: 'private', my_role: role, member_count: members.length,
        features: { chat: true, calendar: true, todo: false, pages: false, gallery: false,
          stickies: false, bazaar: false, location: false, timetable: false },
      }
    }
    if (url === '/api/spaces/s1/members') return members
    if (url === '/api/spaces/s1/links') return { links: [] }
    return []
  })
}

async function open() {
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  openInvite.mockReset()
  user.value = { ...user.value, preferences_json: JSON.stringify({ space_setup_pending: ['s1'] }) }
})
afterEach(async () => { (await import('@testing-library/preact')).cleanup() })

describe('a new space', () => {
  it('asks its owner "What’s Book club for?" with Club or group guessed', async () => {
    wire('owner', [{ user_id: 'u1', role: 'owner' }])
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.getByRole('heading', { name: 'What’s Book club for?' })).toBeTruthy())
    expect((r.getByRole('radio', { name: /Club or group/ }) as HTMLInputElement).checked).toBe(true)
  }, 20000)

  it('never asks a member, nor once the owner chose', async () => {
    wire('member', [{ user_id: 'u9', role: 'owner' }, { user_id: 'u1', role: 'member' }])
    let m = await open()
    await m.tl.waitFor(() => expect(m.r.container.querySelector('.sh-space-hero')).toBeTruthy())
    expect(m.r.queryByText(/for\?$/)).toBeNull()
    m.tl.cleanup()
    vi.resetModules()
    user.value = { ...user.value, preferences_json: '{}' }
    wire('owner', [{ user_id: 'u1', role: 'owner' }])
    m = await open()
    await m.tl.waitFor(() => expect(m.r.container.querySelector('.sh-space-hero')).toBeTruthy())
    expect(m.r.queryByText(/for\?$/)).toBeNull()
  }, 20000)

  it('an owner alone gets "Invite your first member" — the one Invite door', async () => {
    wire('owner', [{ user_id: 'u1', role: 'owner' }])
    const { tl, r } = await open()
    const btn = await tl.waitFor(() => r.getByRole('button', { name: 'Invite people' }))
    expect(r.getByRole('heading', { name: 'Invite your first member' })).toBeTruthy()
    tl.fireEvent.click(btn)
    expect(openInvite).toHaveBeenCalledWith('s1', 'owner')
  }, 20000)

  it('no invite card once someone else is in', async () => {
    user.value = { ...user.value, preferences_json: '{}' }
    wire('owner', [{ user_id: 'u1', role: 'owner' }, { user_id: 'u2', role: 'member' }])
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.container.querySelector('.sh-space-hero')).toBeTruthy())
    expect(r.queryByRole('button', { name: 'Invite people' })).toBeNull()
  }, 20000)
})
