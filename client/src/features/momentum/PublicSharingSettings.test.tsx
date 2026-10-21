import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import PublicSharingSettings from './PublicSharingSettings'

const CONNS = [
  { id: 'g-new', display_name: 'Lakeside GFS', inbox_url: 'https://lake.example', status: 'active' },
  { id: 'g-old', display_name: 'Hilltop GFS', inbox_url: 'https://hill.example', status: 'active' },
  { id: 'g-off', display_name: 'Valley GFS', inbox_url: 'https://valley.example', status: 'active' },
]

describe('PublicSharingSettings', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return CONNS
      if (path === '/api/moments/public/registrations') {
        return [
          { user_id: 'u1', gfs_id: 'g-new', registered_at: '', default_share: true, delivery: 'encrypted', followers_unable: 2 },
          { user_id: 'u1', gfs_id: 'g-old', registered_at: '', default_share: true, delivery: 'unsupported_server' },
        ]
      }
      return []
    })
  })

  it('exports a default component', async () => {
    const m = await import('./PublicSharingSettings')
    expect(typeof m.default).toBe('function')
  })

  it('says a server that cannot carry encrypted moments shares none (v_62)', async () => {
    const { container } = render(<PublicSharingSettings />)
    await waitFor(() => expect(container.textContent).toContain('Hilltop GFS'))
    const warnings = [...container.querySelectorAll('.sh-public-sharing-warning')]
    // Only the registered server without the proof — not the encrypted one,
    // not the server the user hasn't signed up on.
    expect(warnings).toHaveLength(1)
    expect(warnings[0].getAttribute('role')).toBe('status')
    expect(warnings[0].closest('li')?.textContent).toContain('Hilltop GFS')
    expect(warnings[0].textContent).toMatch(/never sent unencrypted/)
  })

  it('counts follower households that cannot read encrypted moments yet', async () => {
    const { container } = render(<PublicSharingSettings />)
    await waitFor(() => expect(container.textContent).toContain('Lakeside GFS'))
    const notes = [...container.querySelectorAll('.sh-public-sharing-note')]
    expect(notes).toHaveLength(1)
    expect(notes[0].closest('li')?.textContent).toContain('Lakeside GFS')
    expect(notes[0].textContent).toContain(': 2.')
  })
})
