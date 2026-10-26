import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import type { Moment } from '@/types'
import MomentumInboxTab from './MomentumInboxTab'

function moment(id: string, author: string, extra: Partial<Moment> = {}): Moment {
  return {
    id, author_user_id: author, content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

describe('MomentumInboxTab authors', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/moments') {
        return [
          moment('m-amaka', 'ru-amaka', {
            author: {
              display_name: 'Amaka Okafor',
              picture_url: 'api/users/ru-amaka/picture?v=1&sig=s',
            },
          }),
          moment('m-kid', 'uid-kid', {
            author: { display_name: 'Kid', picture_url: null },
          }),
          moment('m-ghost', 'ru-forgotten', {
            author: { display_name: null, picture_url: null },
          }),
        ]
      }
      return []
    })
  })

  it('names a friend from another household by the server\'s author', async () => {
    const { container } = render(<MomentumInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    const authors = [...container.querySelectorAll('.sh-momentum-row-author')]
      .map(e => e.textContent)
    expect(authors).toEqual(['Amaka Okafor', 'Kid', 'Someone'])
    expect(container.textContent).not.toContain('ru-amaka')
    expect(container.textContent).not.toContain('ru-forgotten')
    const pictures = [...container.querySelectorAll('.sh-momentum-row > img')]
      .map(i => i.getAttribute('src'))
    // Only Amaka's picture: the guardian-blocked kid's is withheld.
    expect(pictures).toEqual(['api/users/ru-amaka/picture?v=1&sig=s'])
  })

  it('names a GFS-followed author and labels the row as arriving via GFS', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments'
        ? [moment('m-noor', 'u-gfs-noor', {
            received_via: 'gfs',
            received_via_gfs_id: 'g1',
            author: {
              display_name: 'Noor Haddad',
              picture_url: 'api/gfs/g1/moments/users/u-gfs-noor/picture?v=0123&sig=s',
              household: null,
              bio: 'Beekeeper.',
            },
          })]
        : [])
    const { container } = render(<MomentumInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Noor Haddad'))
    expect(container.querySelector('.sh-momentum-row-via-gfs')?.textContent)
      .toContain('via GFS')
    expect(container.textContent).not.toContain('u-gfs-noor')
  })
})

describe('MomentumInboxTab when a load fails', () => {
  beforeEach(() => { wsHandlers.clear(); vi.mocked(api.get).mockReset() })

  it('a failed first load is an alert with Try again, never "No moments yet"', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments') return []
      if (fail) throw new Error('offline')
      return [moment('m-1', 'uid-bob', { author: { display_name: 'Bob', picture_url: null } })]
    })
    const { findByRole, container } = render(<MomentumInboxTab />)
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain("Couldn't load moments")
    expect(container.textContent).not.toContain('No moments yet')
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Bob'))
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('a failed refresh keeps the list and says from when it is', async () => {
    let fail = false
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments') return []
      if (fail) throw new Error('offline')
      return [moment('m-1', 'uid-bob', { author: { display_name: 'Bob', picture_url: null } })]
    })
    const { findByRole, container } = render(<MomentumInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Bob'))
    fail = true
    wsHandlers.get('moment.created')!()
    const note = await findByRole('status')
    expect(note.textContent).toMatch(/Couldn't refresh\. Showing moments from \d/)
    expect(container.textContent).toContain('Bob')
  })
})

describe('MomentumInboxTab time without unit formatting', () => {
  it('falls back to the translated short form, never a hard-coded "5m"', async () => {
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('de')
    const RealNF = Intl.NumberFormat
    const spy = vi.spyOn(Intl, 'NumberFormat').mockImplementation(((loc: string, opts?: Intl.NumberFormatOptions) => {
      if (opts?.style === 'unit') throw new RangeError('unsupported')
      return new RealNF(loc, opts)
    }) as unknown as typeof Intl.NumberFormat)
    try {
      vi.mocked(api.get).mockReset().mockImplementation(async (path: string) => path === '/api/moments'
        ? [moment('m-5', 'uid-bob', {
          created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
          author: { display_name: 'Bob', picture_url: null },
        })]
        : [])
      const { container } = render(<MomentumInboxTab />)
      await waitFor(() => expect(container.textContent).toContain('Bob'))
      expect(container.textContent).toContain('vor 5 Min.')
    } finally {
      spy.mockRestore()
      await setLocale('en')
    }
  })
})

describe('MomentumInboxTab save chip', () => {
  it('offers Save where the server says so, never on your own or unsavable moments', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === '/api/moments'
      ? [
          moment('m-ok', 'ru-amaka', { can_save: true }),
          moment('m-saved', 'ru-amaka', { saved_by_me: true, can_save: true }),
          moment('m-old-peer', 'ru-chidi', { can_save: false }),
        ]
      : []))
    vi.mocked(api.put).mockResolvedValue({ moment_id: 'm-ok', saved: true })
    const { container, getAllByRole } = render(<MomentumInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('hello from m-ok'))
    const rows = [...container.querySelectorAll('.sh-momentum-row')]
    const toggle = (row: Element) => row.querySelector('button[aria-pressed]')
    expect(toggle(rows[0])?.getAttribute('aria-pressed')).toBe('false')
    expect(toggle(rows[1])?.getAttribute('aria-pressed')).toBe('true')
    expect(toggle(rows[2])).toBeNull()
    fireEvent.click(getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/moments/m-ok/save', {}))
  })
})
