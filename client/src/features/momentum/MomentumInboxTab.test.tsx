import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/momentum' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))
import { api } from '@/api'
import type { Moment, User } from '@/types'
import { currentUser } from '@/store/auth'
import MomentumInboxTab, { INBOX_PAGE } from './MomentumInboxTab'

function signIn(isProtected = false) {
  currentUser.value = { user_id: 'uid-me', username: 'me', display_name: 'Me', protected: isProtected } as User
}

function moment(id: string, author: string, extra: Partial<Moment> = {}): Moment {
  return {
    id, author_user_id: author, content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

type Get = (path: string, params?: Record<string, string>) => Promise<unknown>
const onGet = (fn: Get) => vi.mocked(api.get).mockImplementation(fn as never)

describe('MomentumInboxTab list', () => {
  beforeEach(() => { wsHandlers.clear(); vi.mocked(api.get).mockReset(); signIn() })

  it('asks for top-level moments only and names authors from the server', async () => {
    onGet(async (path) => path === '/api/moments'
      ? [
          moment('m-amaka', 'ru-amaka', { author: { display_name: 'Amaka Okafor', picture_url: null } }),
          moment('m-ghost', 'ru-forgotten', { author: { display_name: null, picture_url: null } }),
        ]
      : [])
    const { container } = render(<MomentumInboxTab tag={null} />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(api.get).toHaveBeenCalledWith('/api/moments', { top_level: '1', limit: String(INBOX_PAGE) })
    const names = [...container.querySelectorAll('.sh-moment-card__name')].map(e => e.textContent)
    expect(names).toEqual(['Amaka Okafor', 'Someone'])
    expect(container.textContent).not.toContain('ru-forgotten')
  })

  it('a tag filter goes to the server with the list', async () => {
    onGet(async () => [moment('m-1', 'uid-bob')])
    const { container } = render(<MomentumInboxTab tag="herbst" />)
    await waitFor(() => expect(container.textContent).toContain('hello from m-1'))
    expect(api.get).toHaveBeenCalledWith('/api/moments', { top_level: '1', limit: String(INBOX_PAGE), tag: 'herbst' })
  })

  it('Load older pages with the before cursor; a short page ends with the honest footer', async () => {
    const full = Array.from({ length: INBOX_PAGE }, (_, i) =>
      moment(`m-${i}`, 'uid-bob', { created_at: new Date(Date.now() - i * 60_000).toISOString() }))
    onGet(async (_path, params) => params?.before ? [moment('m-old', 'uid-bob')] : full)
    const { container, findByRole } = render(<MomentumInboxTab tag={null} />)
    const older = await findByRole('button', { name: 'Load older' })
    expect(container.textContent).not.toContain('kept for as long as')
    fireEvent.click(older)
    await waitFor(() => expect(container.textContent).toContain('hello from m-old'))
    expect(api.get).toHaveBeenLastCalledWith('/api/moments', {
      top_level: '1', limit: String(INBOX_PAGE), before: full[full.length - 1].created_at,
    })
    expect(container.querySelector('.sh-momentum-end')?.textContent).toBe(
      "That's everything from the last 24 hours. Moments from people you follow are kept for as long as their authors chose.")
  })

  it('the composer entry says where a moment goes — narrower for a protected account', async () => {
    onGet(async () => [])
    const first = render(<MomentumInboxTab tag={null} />)
    await waitFor(() => expect(first.container.textContent).toContain('No moments yet'))
    const entry = first.getByRole('button', { name: /Share a moment/ })
    expect(entry.textContent).toContain('Paired households and theirs, up to 3 hops · 24 h')
    first.unmount()
    signIn(true)
    const kid = render(<MomentumInboxTab tag={null} />)
    await waitFor(() => expect(kid.container.textContent).toContain('No moments yet'))
    expect(kid.getByRole('button', { name: /Share a moment/ }).textContent)
      .toContain('Your household and paired households')
    // The empty state doesn't promise a 3-step reach a protected account doesn't have.
    expect(kid.container.textContent).not.toContain('3 steps')
    expect(kid.container.textContent).toContain(
      'A moment is a short post for your household and the households you’re paired with.')
  })
})

describe('MomentumInboxTab when a load fails', () => {
  beforeEach(() => { wsHandlers.clear(); vi.mocked(api.get).mockReset(); signIn() })

  it('a failed first load is an alert with Try again, never "No moments yet"', async () => {
    let fail = true
    onGet(async (path) => {
      if (path !== '/api/moments') return []
      if (fail) throw new Error('offline')
      return [moment('m-1', 'uid-bob', { author: { display_name: 'Bob', picture_url: null } })]
    })
    const { findByRole, container } = render(<MomentumInboxTab tag={null} />)
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
    onGet(async (path) => {
      if (path !== '/api/moments') return []
      if (fail) throw new Error('offline')
      return [moment('m-1', 'uid-bob', { author: { display_name: 'Bob', picture_url: null } })]
    })
    const { findByRole, container } = render(<MomentumInboxTab tag={null} />)
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
      onGet(async (path) => path === '/api/moments'
        ? [moment('m-5', 'uid-bob', {
          created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
          author: { display_name: 'Bob', picture_url: null },
        })]
        : [])
      const { container } = render(<MomentumInboxTab tag={null} />)
      await waitFor(() => expect(container.textContent).toContain('Bob'))
      expect(container.textContent).toContain('vor 5 Min.')
    } finally {
      spy.mockRestore()
      await setLocale('en')
    }
  })
})
