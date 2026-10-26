import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const me = vi.hoisted(() => ({ value: { user_id: 'u1', restrictions: [] as string[] } }))
vi.mock('@/store/auth', () => ({ currentUser: me }))

import { api } from '@/api'
import { _resetGfsConnectionsForTests, registrations } from '@/store/momentPublic'
import { PublicSharingRow } from './PublicSharingRow'

const DEMO = { id: 'g1', display_name: 'Demo GFS', inbox_url: 'https://demo.example', status: 'active' }

function serve(conns: unknown[], regs: unknown[]) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === '/api/gfs/connections') return conns
    if (path === '/api/moments/public/registrations') return regs
    return []
  })
}

beforeEach(() => {
  _resetGfsConnectionsForTests()
  registrations.value = []
  me.value = { user_id: 'u1', restrictions: [] }
  vi.mocked(api.get).mockReset()
})

describe('PublicSharingRow (Momentum › Yours)', () => {
  it('links to Settings › Privacy › Public sharing and says how new moments go', async () => {
    serve([DEMO], [{ user_id: 'u1', gfs_id: 'g1', registered_at: '', default_share: false }])
    const { findByRole } = render(<PublicSharingRow />)
    const link = await findByRole('link', { name: /Public sharing · Demo GFS/ })
    expect(link.getAttribute('href')).toBe('/settings#public-sharing')
    await waitFor(() => expect(link.textContent).toContain('New moments aren’t public — you switch it on per moment'))
  })

  it('says when new moments are public by default', async () => {
    serve([DEMO], [{ user_id: 'u1', gfs_id: 'g1', registered_at: '', default_share: true }])
    const { findByRole } = render(<PublicSharingRow />)
    const link = await findByRole('link', { name: /Public sharing/ })
    await waitFor(() => expect(link.textContent).toContain('New moments are public'))
  })

  it('not listed yet: says what signing up does', async () => {
    serve([DEMO], [])
    const { findByRole } = render(<PublicSharingRow />)
    const link = await findByRole('link', { name: /Public sharing/ })
    await waitFor(() => expect(link.textContent)
      .toContain('Not listed — let people outside your paired households follow you'))
  })

  it('nothing without a GFS, and nothing for a protected account', async () => {
    serve([], [])
    const a = render(<PublicSharingRow />)
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/gfs/connections'))
    expect(a.container.querySelector('a')).toBeNull()
    a.unmount()
    _resetGfsConnectionsForTests()
    me.value = { user_id: 'u-kid', restrictions: ['public_moments'] }
    serve([DEMO], [])
    const b = render(<PublicSharingRow />)
    await new Promise(r => setTimeout(r, 0))
    expect(b.container.querySelector('a')).toBeNull()
  })
})
