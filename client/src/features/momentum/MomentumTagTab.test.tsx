import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn(), url: '/momentum?tag=walk', path: '/momentum' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import type { Moment } from '@/types'
import MomentumTagTab from './MomentumTagTab'

function moment(id: string, createdAt: string): Moment {
  return {
    id, author_user_id: 'ru-amaka', content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: createdAt,
    expires_at: '2026-10-27T00:00:00+00:00',
    reaction_count: 0, reply_count: 0,
    author: { display_name: 'Amaka Okafor', picture_url: null },
  }
}

describe('MomentumTagTab times', () => {
  let prevTz: string | undefined
  beforeEach(() => {
    prevTz = process.env.TZ
    process.env.TZ = 'Europe/Zurich'
  })
  afterEach(() => {
    process.env.TZ = prevTz
  })

  // The archive printed ``created_at.slice(11, 16)`` — the UTC clock — and
  // grouped by the UTC date. A moment posted at 00:30 in Zurich (22:30 UTC
  // the day before) landed under yesterday at "22:30".
  it('groups by the viewer\'s local day and shows the local clock time', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/moments/archive') {
        return [
          moment('m-late', '2026-10-19T22:30:00+00:00'),
          moment('m-naive', '2026-10-19 08:05:00'),
        ]
      }
      return { hashtags: [] }
    })
    const { container } = render(<MomentumTagTab />)
    await waitFor(() => expect(container.querySelectorAll('h3').length).toBe(2))
    const days = [...container.querySelectorAll('h3')].map(h => h.textContent)
    expect(days).toEqual(['Tuesday, October 20, 2026', 'Monday, October 19, 2026'])
    const times = [...container.querySelectorAll('.sh-momentum-row-head time')]
      .map(e => e.textContent)
    // A naive SQLite stamp is UTC too: 08:05 UTC is 10:05 in Zurich.
    expect(times).toEqual(['12:30 AM', '10:05 AM'])
    expect(container.textContent).not.toContain('22:30')
  })

  it('keeps the author name and the time apart', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/archive' ? [moment('m1', '2026-10-19T10:00:00+00:00')] : { hashtags: [] })
    const { container } = render(<MomentumTagTab />)
    await waitFor(() => expect(container.querySelector('.sh-momentum-row-head')).toBeTruthy())
    const head = container.querySelector('.sh-momentum-row-head')!
    expect(head.querySelector('.sh-momentum-row-author')?.textContent).toBe('Amaka Okafor')
    const time = head.querySelector('time')!
    expect(time.getAttribute('datetime')).toBe('2026-10-19T10:00:00+00:00')
    expect(time.textContent).toBe('12:00 PM')
  })
})

describe('MomentumTagTab when a load fails', () => {
  beforeEach(() => { wsHandlers.clear(); vi.mocked(api.get).mockReset() })

  it('a failed first load is an alert with Try again, not the empty list', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments/archive') return { hashtags: [] }
      if (fail) throw new Error('offline')
      return [moment('m1', '2026-10-19T10:00:00+00:00')]
    })
    const { findByRole, container } = render(<MomentumTagTab />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load moments")
    expect(container.querySelector('.sh-empty-state:not(.sh-load-error)')).toBeNull()
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
  })

  it('a failed refresh keeps the list with a note', async () => {
    let fail = false
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments/archive') return { hashtags: [] }
      if (fail) throw new Error('offline')
      return [moment('m1', '2026-10-19T10:00:00+00:00')]
    })
    const { findByRole, container } = render(<MomentumTagTab />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    fail = true
    wsHandlers.get('moment.created')!()
    expect((await findByRole('status')).textContent).toContain("Couldn't refresh")
    expect(container.textContent).toContain('Amaka Okafor')
  })
})

describe('MomentumTagTab for a hashtag link', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    window.history.replaceState(null, '', '/momentum?tag=walk')
  })
  afterEach(() => { window.history.replaceState(null, '', '/') })

  it('names the tag, asks for it, and Clear goes back to every moment', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/archive' ? [moment('m1', '2026-10-19T10:00:00+00:00')] : { hashtags: [] })
    const { findByRole, getByRole } = render(<MomentumTagTab />)
    expect((await findByRole('heading', { level: 2 })).textContent).toBe('Moments tagged #walk')
    expect(api.get).toHaveBeenCalledWith('/api/moments/archive', { tag: 'walk' })
    expect(getByRole('link', { name: 'Clear' }).getAttribute('href')).toBe('/momentum')
  })
})
