/**
 * The space itself says which of its GFSs this household isn't connected
 * to (``gfs_servers[].connected``): needed for a household standing in for
 * the host (``stands_in_for_host``), optional and dismissible otherwise.
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
  value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: true, preferences_json: '{}' } as Record<string, unknown>,
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

const SERVERS = [
  { display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true },
  { display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false },
]

function wire(over: Record<string, unknown>) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') {
      return {
        id: 's1', name: 'World', space_type: 'global', my_role: 'member', member_count: 2,
        owner_instance_id: 'inst-host',
        features: { chat: true },
        gfs_servers: SERVERS,
        ...over,
      }
    }
    if (url === '/api/spaces/s1/members') {
      return [{ user_id: 'u9', role: 'owner' }, { user_id: 'u1', role: 'member' }]
    }
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
  window.localStorage.clear()
})
afterEach(async () => { (await import('@testing-library/preact')).cleanup() })

describe('the space GFS notice', () => {
  it('a member household sees the optional notice with a one-click connect', async () => {
    wire({})
    const { tl, r } = await open()
    const row = await tl.waitFor(() => r.getByTestId('space-gfs-notice-optional'))
    expect(row.textContent).toContain('This space is also on Club GFS. Connect for a steadier connection.')
    expect(r.getByRole('link', { name: 'Connect to Club GFS' }).getAttribute('href'))
      .toBe('/connections?connect_gfs=https%3A%2F%2Fclub.example')
    expect(r.getByRole('button', { name: 'Hide the notice about Club GFS' })).toBeTruthy()
  }, 20000)

  it('a household standing in for the host sees it as needed, with no way to hide it', async () => {
    wire({ my_role: 'admin', stands_in_for_host: true })
    const { tl, r } = await open()
    const row = await tl.waitFor(() => r.getByTestId('space-gfs-notice-needed'))
    expect(row.textContent).toContain('Connect to Club GFS so your household can stand in for the host.')
    expect(r.queryByRole('button', { name: /Hide the notice/ })).toBeNull()
  }, 20000)

  it('a private space shows it only with the private GFS channel on', async () => {
    wire({ space_type: 'private' })
    let m = await open()
    await m.tl.waitFor(() => expect(m.r.container.querySelector('.sh-space-hero')).toBeTruthy())
    expect(m.r.queryByTestId('space-gfs-notice')).toBeNull()
    m.tl.cleanup()
    vi.resetModules()
    wire({ space_type: 'private', features: { chat: true, private_gfs: true } })
    m = await open()
    await m.tl.waitFor(() => expect(m.r.getByTestId('space-gfs-notice-optional')).toBeTruthy())
  }, 20000)
})

describe('the space sync chip', () => {
  it('shows only when posts may be missing — never for an away host', async () => {
    wire({ space_type: 'private', sync: { state: 'host_offline', last_ok_at: null, waiting_to_send: 2 } })
    let m = await open()
    await m.tl.waitFor(() => expect(m.r.container.querySelector('.sh-space-hero')).toBeTruthy())
    expect(m.r.queryByTestId('space-sync-chip')).toBeNull()
    m.tl.cleanup()
    vi.resetModules()
    wire({ space_type: 'private', sync: {
      state: 'behind', last_ok_at: null, waiting_to_send: 0, may_be_missing: true, unsent_posts: [],
    } })
    m = await open()
    const chip = await m.tl.waitFor(() => m.r.getByTestId('space-sync-chip'))
    expect(chip.textContent).toBe('⚠Some posts may be missing›')
    expect(chip.getAttribute('href')).toBe('/spaces/s1/settings/sync')
  }, 20000)
})
