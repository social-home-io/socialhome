/**
 * SpaceSettingsPage × GFS listing — a plain member never sees the General
 * tab, so where a global space is listed is shown to them read-only, with a
 * hint for each GFS their household isn't connected to.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 'sp-1' } }),
  useLocation: () => ({ route: vi.fn() }),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'anna', is_admin: false } },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

let detail: Record<string, unknown>
let role: string
beforeEach(() => {
  apiGet.mockReset()
  role = 'member'
  detail = {
    id: 'sp-1', name: 'Garden', features: {}, owner_instance_id: null,
    space_type: 'global',
    gfs_servers: [
      { display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true },
      { display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false },
    ],
    gfs_unlisted: false,
  }
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/sp-1') return detail
    if (url === '/api/spaces/sp-1/members') return [{ user_id: 'u1', role }]
    return []
  })
})

describe('SpaceSettingsPage — a member sees the GFS list', () => {
  it('lists the GFSs read-only, with a connect hint for the missing one', async () => {
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    const section = await view.findByTestId('member-gfs-servers')
    expect(section.textContent).toContain('Town GFS')
    expect(section.textContent).toContain('Club GFS')
    expect(section.querySelectorAll('button')).toHaveLength(0)
    expect(view.getByTestId('gfs-connect-hint').textContent).toBe(
      'Ask a household admin to connect to Club GFS so you keep up while the host is offline.',
    )
  })

  it('shows the "not listed yet" banner', async () => {
    detail = { ...detail, gfs_servers: [], gfs_unlisted: true }
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    expect((await view.findByTestId('gfs-unlisted-banner')).textContent)
      .toContain('Global, but not listed on any GFS yet')
  })

  it('a public space shows no GFS section to a member', async () => {
    detail = { ...detail, space_type: 'public', gfs_servers: null }
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    await view.findByRole('heading', { name: /Garden — settings/ })
    expect(view.queryByTestId('member-gfs-servers')).toBeNull()
  })
})
