import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const confirmSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/confirm', () => ({ confirmDialog: confirmSpy }))
const me = vi.hoisted(() => ({ value: { user_id: 'u1', restrictions: [] as string[] } }))
vi.mock('@/store/auth', () => ({ currentUser: me }))

import { api } from '@/api'
import { _resetGfsConnectionsForTests, registrations } from '@/store/momentPublic'
import { PublicSharingPanel } from './PublicSharingSettings'

const CONNS = [
  { id: 'g-new', display_name: 'Lakeside GFS', inbox_url: 'https://lake.example', status: 'active' },
  { id: 'g-old', display_name: 'Hilltop GFS', inbox_url: 'https://hill.example', status: 'active' },
  { id: 'g-off', display_name: 'Valley GFS', inbox_url: 'https://valley.example', status: 'active' },
]

const row = (container: Element, name: string) =>
  [...container.querySelectorAll('.sh-public-sharing-row')]
    .find(r => r.textContent!.includes(name)) as HTMLElement

beforeEach(() => {
  _resetGfsConnectionsForTests()
  registrations.value = []
  me.value = { user_id: 'u1', restrictions: [] }
  confirmSpy.mockReset()
  for (const f of Object.values(api)) vi.mocked(f).mockReset()
})

describe('Settings › Privacy › Public sharing', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return CONNS
      if (path === '/api/moments/public/registrations') {
        return [
          { user_id: 'u1', gfs_id: 'g-new', registered_at: '', default_share: false, delivery: 'encrypted', followers_unable: 2 },
          { user_id: 'u1', gfs_id: 'g-old', registered_at: '', default_share: true, delivery: 'unsupported_server' },
        ]
      }
      return []
    })
  })

  it('is a section with its own anchor and one row per GFS: listed or not', async () => {
    const { container, getByRole } = render(<PublicSharingPanel />)
    expect(container.querySelector('#public-sharing')).not.toBeNull()
    expect(getByRole('heading', { name: 'Public sharing' })).toBeTruthy()
    await waitFor(() => expect(container.textContent).toContain('Valley GFS'))
    expect(row(container, 'Lakeside GFS').textContent).toContain('You’re listed')
    expect(row(container, 'Valley GFS').textContent).toContain('Not listed')
    expect(row(container, 'Valley GFS').querySelector('[role="switch"]')).toBeNull()
  })

  it('"Make new moments public" is a switch per listed GFS, off unless chosen', async () => {
    vi.mocked(api.patch).mockResolvedValue({})
    const { container } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Lakeside GFS'))
    const sw = row(container, 'Lakeside GFS').querySelector('[role="switch"]') as HTMLInputElement
    expect(sw.checked).toBe(false)
    expect(row(container, 'Lakeside GFS').textContent)
      .toContain('Off: you switch it on per moment. Replies are never public.')
    expect(sw.getAttribute('aria-describedby')).toBeTruthy()
    fireEvent.click(sw)
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      '/api/moments/public/registrations/g-new', { default_share: true }))
    await waitFor(() => expect(sw.checked).toBe(true))
  })

  it('says a server that cannot carry encrypted moments shares none (v_62)', async () => {
    const { container } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Hilltop GFS'))
    const warnings = [...container.querySelectorAll('.sh-public-sharing-warning')]
    expect(warnings).toHaveLength(1)
    expect(warnings[0].getAttribute('role')).toBe('status')
    expect(warnings[0].closest('.sh-public-sharing-row')?.textContent).toContain('Hilltop GFS')
    expect(warnings[0].textContent).toMatch(/never sent unencrypted/)
  })

  it('counts follower households that cannot read encrypted moments yet', async () => {
    const { container } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Lakeside GFS'))
    const notes = [...container.querySelectorAll('.sh-public-sharing-note')]
    expect(notes).toHaveLength(1)
    expect(notes[0].closest('.sh-public-sharing-row')?.textContent).toContain('Lakeside GFS')
    expect(notes[0].textContent).toContain(': 2.')
  })

  it('Leave asks first, then deregisters', async () => {
    vi.mocked(api.delete).mockResolvedValue({})
    confirmSpy.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const { container, getByRole } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Lakeside GFS'))
    const leave = getByRole('button', { name: 'Leave Lakeside GFS' })
    fireEvent.click(leave)
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(api.delete).not.toHaveBeenCalled()
    fireEvent.click(leave)
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/public/registrations/g-new'))
    await waitFor(() => expect(row(container, 'Lakeside GFS').textContent).toContain('Not listed'))
  })
})

describe('Public sharing — signing up (owner decision 4)', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return CONNS
      return []
    })
    vi.mocked(api.post).mockResolvedValue({
      user_id: 'u1', gfs_id: 'g-new', registered_at: '', default_share: false, delivery: 'encrypted',
    })
  })

  it('signing up leaves "make new moments public" to the server (off for a new registration)', async () => {
    const { container, getByRole } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Lakeside GFS'))
    fireEvent.click(getByRole('button', { name: 'Sign up on Lakeside GFS' }))
    await waitFor(() => expect(api.post).toHaveBeenCalled())
    expect(api.post).toHaveBeenCalledWith('/api/moments/public/registrations', { gfs_id: 'g-new' })
    await waitFor(() => expect(container.textContent).toContain('Make new moments public'))
    const sw = row(container, 'Lakeside GFS').querySelector('[role="switch"]') as HTMLInputElement
    expect(sw.checked).toBe(false)
  })
})

describe('Public sharing — states', () => {
  it('no GFS: says how to get one, no rows', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/gfs/connections' ? [] : [])
    const { container } = render(<PublicSharingPanel />)
    await waitFor(() => expect(container.textContent).toContain('Connect to a GFS'))
    expect(container.querySelector('.sh-public-sharing-row')).toBeNull()
  })

  it('a failed load is an error with Try again, not "no GFS"', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') {
        if (fail) throw new Error('offline')
        return CONNS
      }
      return []
    })
    const { container, findByRole } = render(<PublicSharingPanel />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load your GFS connections")
    expect(container.textContent).not.toContain('Connect to a GFS')
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Valley GFS'))
  })

  it('a protected account sees why, and no rows', async () => {
    me.value = { user_id: 'u-kid', restrictions: ['public_moments'] }
    const { container } = render(<PublicSharingPanel />)
    expect(container.querySelector('#public-sharing')).not.toBeNull()
    expect(container.querySelector('.sh-public-sharing-row')).toBeNull()
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/connections')
  })
})
