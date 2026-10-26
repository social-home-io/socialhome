import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent, within } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const route = vi.hoisted(() => vi.fn())
const where = vi.hoisted(() => ({ url: '/momentum/yours' }))
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route, url: where.url, path: '/momentum' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

vi.mock('./PublicSharingRow', () => ({ PublicSharingRow: () => <a data-testid="sharing-row" /> }))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import type { Moment, User } from '@/types'
import MomentumYoursTab, { YOURS_PAGE, archiveStatus, yoursShowFromUrl } from './MomentumYoursTab'

const HOUR = 3_600_000
const DAY = 24 * HOUR

function mine(id: string, ageMs: number, extra: Partial<Moment> = {}): Moment {
  const created = Date.now() - ageMs
  return {
    id, author_user_id: 'u-me', content: `text ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'here',
    created_at: new Date(created).toISOString(),
    expires_at: new Date(created + 7 * DAY).toISOString(),
    reaction_count: 2, reply_count: 1, received_via: 'self',
    author: { display_name: 'Me', picture_url: null },
    ...extra,
  }
}

type Params = Record<string, string> | undefined
function serve(pages: (params: Params) => Moment[] | Promise<Moment[]>, usage: unknown = { used_bytes: 350 * 1024 ** 2, cap_bytes: 2 * 1024 ** 3 }) {
  vi.mocked(api.get).mockImplementation(async (path: string, params?: Params) => {
    if (path === '/api/moments/archive/usage') {
      if (usage instanceof Error) throw usage
      return usage
    }
    if (path === '/api/moments/archive') return pages(params)
    throw new Error(`unexpected ${path}`)
  })
}

describe('archiveStatus', () => {
  const now = Date.parse('2026-10-25T12:00:00Z')
  const at = (iso: string, days = 7) => ({
    created_at: iso,
    expires_at: new Date(Date.parse(iso) + days * DAY).toISOString(),
  })

  it('says who still sees a moment, and until when', () => {
    expect(archiveStatus(at('2026-10-25T08:00:00Z'), now)).toEqual({
      kind: 'others', until: Date.parse('2026-10-26T08:00:00Z'),
    })
    expect(archiveStatus(at('2026-10-22T08:00:00Z'), now)).toEqual({
      kind: 'followers', until: Date.parse('2026-10-29T08:00:00Z'),
    })
    expect(archiveStatus(at('2026-10-10T08:00:00Z'), now)).toEqual({ kind: 'kept' })
  })
})

describe('yoursShowFromUrl', () => {
  it('shows your posts unless the URL asks for the saved ones', () => {
    expect(yoursShowFromUrl('/momentum/yours')).toBe('posted')
    expect(yoursShowFromUrl('/momentum/yours?show=saved')).toBe('saved')
    expect(yoursShowFromUrl('/momentum/yours?show=nope')).toBe('posted')
  })
})

describe('MomentumYoursTab', () => {
  beforeEach(() => {
    wsHandlers.clear()
    vi.mocked(api.get).mockReset()
    route.mockReset()
    where.url = '/momentum/yours'
  })

  it('switches between your posts and the moments you saved', async () => {
    serve(() => [mine('m1', HOUR)])
    const { findByText, getByRole } = render(<MomentumYoursTab />)
    await findByText('text m1')
    const posted = getByRole('button', { name: 'Posted' })
    expect(posted.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByRole('button', { name: 'Saved' }))
    expect(route).toHaveBeenCalledWith('/momentum/yours?show=saved', true)
  })

  it('the Saved view lists scope=saved and the usage counts saved pictures too', async () => {
    where.url = '/momentum/yours?show=saved'
    serve((params) => (params?.scope === 'saved'
      ? [mine('m-saved', 3 * DAY, { author_user_id: 'ru-amaka', saved_by_me: true, saved_at: new Date().toISOString() })]
      : [mine('m-own', HOUR)]))
    const { findByText, getByRole, queryByText } = render(<MomentumYoursTab />)
    await findByText('text m-saved')
    expect(queryByText('text m-own')).toBeNull()
    const which = within(getByRole('group', { name: 'Which moments' }))
    expect(which.getByRole('button', { name: 'Saved' }).getAttribute('aria-pressed')).toBe('true')
    expect((await findByText(/the ones you saved/)).textContent).toContain('350 MB of 2 GB')
  })

  it('lists your own moments from scope=own with who sees them', async () => {
    serve(() => [
      mine('m-today', 2 * HOUR),
      mine('m-week', 3 * DAY, { is_public: true }),
      mine('m-old', 20 * DAY),
    ])
    const { container, findByText } = render(<MomentumYoursTab />)
    await findByText('text m-today')
    expect(api.get).toHaveBeenCalledWith('/api/moments/archive', { scope: 'own', limit: String(YOURS_PAGE) })
    const rows = [...container.querySelectorAll('.sh-momentum-yours-row')]
    expect(rows).toHaveLength(3)
    expect(rows[0].textContent).toContain('Others see it until')
    expect(rows[1].textContent).toContain('Your followers see it until')
    expect(rows[1].textContent).toContain('Public')
    expect(rows[2].textContent).toContain('Only you see it now')
    expect(rows[0].querySelector('[aria-label="1 reply"]')).toBeTruthy()
    fireEvent.click(rows[2].querySelector('a')!)
    expect(route).toHaveBeenCalledWith('/momentum/m-old')
  })

  it('the intro names how long your followers keep a moment (your choice)', async () => {
    currentUser.value = {
      user_id: 'u-me', username: 'me', display_name: 'Me',
      preferences_json: JSON.stringify({ moments: { follower_retention_days: 30 } }),
    } as unknown as User
    serve(() => [mine('m1', HOUR)])
    const { findByText } = render(<MomentumYoursTab />)
    expect((await findByText(/people who follow you keep it for/)).textContent)
      .toContain('keep it for 30\u00a0days.')
    currentUser.value = null
  })

  it('shows your archive size against the household limit', async () => {
    serve(() => [mine('m1', HOUR)])
    const { findByText } = render(<MomentumYoursTab />)
    expect((await findByText(/350 MB of 2 GB/)).textContent)
      .toContain('Oldest pictures go first when you pass 2 GB')
  })

  it('without a household limit it shows only the size', async () => {
    serve(() => [mine('m1', HOUR)], { used_bytes: 1024 ** 3, cap_bytes: 0 })
    const { findByText, container } = render(<MomentumYoursTab />)
    await findByText(/1 GB/)
    expect(container.textContent).not.toContain('Oldest pictures go first')
  })

  it('a picture removed to make room says so', async () => {
    serve(() => [mine('m-pic', 40 * DAY, { content: '' })])
    const { findByText } = render(<MomentumYoursTab />)
    await findByText('The picture was removed to make room.')
  })

  it('pages back with the before cursor', async () => {
    const first = Array.from({ length: YOURS_PAGE }, (_, i) => mine(`m${i}`, (i + 1) * HOUR))
    serve((params) => (params?.before ? [mine('m-older', 30 * DAY)] : first))
    const { findByRole, findByText, queryByRole } = render(<MomentumYoursTab />)
    fireEvent.click(await findByRole('button', { name: 'Load older' }))
    await findByText('text m-older')
    expect(api.get).toHaveBeenCalledWith('/api/moments/archive', {
      scope: 'own', limit: String(YOURS_PAGE), before: first[YOURS_PAGE - 1].created_at,
    })
    // A short page is the end of the archive.
    await waitFor(() => expect(queryByRole('button', { name: 'Load older' })).toBeNull())
  })

  it('an empty archive says what will show here', async () => {
    serve(() => [])
    const { findByText } = render(<MomentumYoursTab />)
    await findByText('Nothing here yet')
  })

  it('a failed first load is an alert with Try again, not the empty archive', async () => {
    let fail = true
    serve(() => { if (fail) throw new Error('offline'); return [mine('m1', HOUR)] })
    const { findByRole, container, findByText } = render(<MomentumYoursTab />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load your moments")
    expect(container.textContent).not.toContain('Nothing here yet')
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await findByText('text m1')
  })

  it('a failed refresh keeps the list with a note', async () => {
    let fail = false
    serve(() => { if (fail) throw new Error('offline'); return [mine('m1', HOUR)] })
    const { findByRole, findByText, container } = render(<MomentumYoursTab />)
    await findByText('text m1')
    fail = true
    wsHandlers.get('moment.deleted')!()
    expect((await findByRole('status')).textContent).toContain("Couldn't refresh")
    expect(container.textContent).toContain('text m1')
  })
})

describe('Yours › Public sharing', () => {
  it('links to Settings › Privacy › Public sharing from the posted list', async () => {
    where.url = '/momentum/yours'
    serve(() => [])
    const { findByTestId } = render(<MomentumYoursTab />)
    expect(await findByTestId('sharing-row')).toBeTruthy()
  })
})
