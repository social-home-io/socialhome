import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const loc = vi.hoisted(() => ({ url: '/highlights/archive', route: vi.fn() }))
vi.mock('preact-iso', () => ({ useLocation: () => loc }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import type { HighlightFrame, HighlightInboxItem } from '@/types'
import HighlightArchiveTab from './HighlightArchiveTab'

const NOW = new Date('2026-10-25T12:00:00Z')
const D = 24 * 3_600_000

function frame(id: string, hid: string, at: string, extra: Partial<HighlightFrame> = {}): HighlightFrame {
  return {
    id, highlight_id: hid, sequence: 1, frame_type: 'image',
    media_url: `api/media/${id}.webp?sig=x`, caption_text: null, caption_emoji: null,
    duration_ms: null, created_at: at, ...extra,
  }
}

function item(
  id: string, author: string, name: string, frames: HighlightFrame[],
  extra: { seen_by?: number; expires?: string; label?: string } = {},
): HighlightInboxItem {
  return {
    highlight: {
      id, author_user_id: author, highlight_date: frames[0].created_at.slice(0, 10),
      audience_kind: 'all_paired', audience_label: extra.label ?? 'paired',
      created_at: frames[0].created_at,
      expires_at: extra.expires ?? new Date(NOW.getTime() + 20 * D).toISOString(),
      author: { display_name: name, picture_url: null },
    },
    frames,
    unseen_count: 0,
    ...(extra.seen_by === undefined ? {} : { seen_by: extra.seen_by }),
  } as unknown as HighlightInboxItem
}

const mine = item('m1', 'me', 'Anna', [
  frame('a1', 'm1', '2026-10-25T08:00:00Z'), frame('a2', 'm1', '2026-10-25T09:00:00Z'),
], { seen_by: 3 })
const hilde = item('o1', 'ru-hilde', 'Oma Hilde', [frame('h1', 'o1', '2026-10-15T10:00:00Z')],
  { expires: new Date(NOW.getTime() + 4 * D).toISOString() })
const sept = item('s1', 'ru-thomas', 'Thomas', [frame('t1', 's1', '2026-09-30T10:00:00Z')],
  { label: 'just_you' })

function serve(rows: () => HighlightInboxItem[]) {
  vi.mocked(api.get).mockReset().mockImplementation(async (path: string) => {
    if (path !== '/api/highlights') return []
    return rows()
  })
}

beforeEach(() => {
  process.env.TZ = 'UTC'
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  loc.url = '/highlights/archive'
  loc.route.mockReset()
  currentUser.value = {
    user_id: 'me', username: 'anna', display_name: 'Anna',
    preferences_json: JSON.stringify({ highlights: { retention_days: 7 } }),
  } as unknown as typeof currentUser.value
})
afterEach(() => {
  vi.useRealTimers()
  currentUser.value = null
})

describe('HighlightArchiveTab', () => {
  it('lists days, not a month grid', async () => {
    serve(() => [hilde, mine])
    const { container, findByRole } = render(<HighlightArchiveTab />)
    await findByRole('heading', { name: 'Sunday, October 25 · today' })
    expect(container.querySelector('[role="grid"]')).toBeNull()
    const days = [...container.querySelectorAll('h2')].map(h => h.textContent)
    expect(days).toEqual(['Sunday, October 25 · today', 'Thursday, October 15'])
  })

  it('a row opens the viewer by its link alone — one history entry (bug 22)', async () => {
    serve(() => [mine])
    const { findByRole } = render(<HighlightArchiveTab />)
    const link = await findByRole('link', { name: /You/ })
    expect(link.getAttribute('href')).toBe('/highlights/m1')
    fireEvent.click(link)
    // The router handles the click; the row must not push a second entry.
    expect(loc.route).not.toHaveBeenCalled()
  })

  it('your row says how many frames and who saw it; others say when they end', async () => {
    serve(() => [hilde, mine])
    const { findByRole } = render(<HighlightArchiveTab />)
    const own = await findByRole('link', { name: /You/ })
    expect(own.textContent).toContain('2 frames')
    expect(own.textContent).toContain('seen by 3')
    expect(own.querySelectorAll('.sh-hl-thumb')).toHaveLength(2)
    const other = await findByRole('link', { name: /Oma Hilde/ })
    expect(other.textContent).toContain('ends in 4 days')
    expect(other.textContent).not.toContain('seen by')
  })

  it('offers only months that have highlights and keeps the pick in the URL', async () => {
    serve(() => [mine, sept])
    const { findByRole, queryByRole } = render(<HighlightArchiveTab />)
    const select = await findByRole('combobox', { name: 'Month' }) as HTMLSelectElement
    expect([...select.options].map(o => o.textContent)).toEqual(['October 2026', 'September 2026'])
    expect(queryByRole('link', { name: /Thomas/ })).toBeNull()
    fireEvent.change(select, { target: { value: '2026-09' } })
    expect(loc.route).toHaveBeenCalledWith('/highlights/archive?month=2026-09', true)
  })

  it('shows the month from the URL', async () => {
    loc.url = '/highlights/archive?month=2026-09'
    serve(() => [mine, sept])
    const { findByRole, queryByRole } = render(<HighlightArchiveTab />)
    expect(await findByRole('link', { name: /Thomas/ })).toBeTruthy()
    expect(queryByRole('link', { name: /You/ })).toBeNull()
  })

  it('Only mine is a toggle in the URL', async () => {
    serve(() => [mine, hilde])
    const { findByRole } = render(<HighlightArchiveTab />)
    const toggle = await findByRole('button', { name: 'Only mine' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(toggle)
    expect(loc.route).toHaveBeenCalledWith('/highlights/archive?mine=1', true)
  })

  it('Only mine with nothing of yours offers a way back', async () => {
    loc.url = '/highlights/archive?mine=1'
    serve(() => [hilde])
    const { findByRole } = render(<HighlightArchiveTab />)
    await findByRole('heading', { name: 'None of your highlights are in the archive' })
    expect((await findByRole('button', { name: 'Only mine' })).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(await findByRole('button', { name: "Show everyone's" }))
    expect(loc.route).toHaveBeenCalledWith('/highlights/archive', true)
  })

  it('says honestly how long highlights stay, with your own setting', async () => {
    serve(() => [mine])
    const { findByText } = render(<HighlightArchiveTab />)
    const note = await findByText(/Each person decides how long their highlights stay/)
    expect(note.textContent).toContain('Yours stay 7 days.')
    const link = within(note.closest('p')!).getByRole('link', { name: 'Change in Settings' })
    expect(link.getAttribute('href')).toBe('/settings#highlights')
  })

  it('groups by the viewer\'s local day (B23)', async () => {
    // 23:30 UTC on the 24th is Sunday the 25th in Berlin.
    process.env.TZ = 'Europe/Berlin'
    serve(() => [item('b1', 'ru-b', 'Bert', [frame('b1f', 'b1', '2026-10-24T23:30:00Z')])])
    const { findByRole } = render(<HighlightArchiveTab />)
    await findByRole('heading', { name: 'Sunday, October 25 · today' })
  })

  it('names a friend from another household by the server\'s author, with their picture', async () => {
    serve(() => [item('h1', 'ru-amaka', 'Amaka Okafor', [frame('f', 'h1', '2026-10-20T10:00:00Z')])])
    const { container, findByRole } = render(<HighlightArchiveTab />)
    const row = await findByRole('link', { name: /Amaka Okafor/ })
    expect(container.textContent).not.toContain('ru-amaka')
    expect(row.querySelector('img.sh-avatar, .sh-avatar img, img')).toBeTruthy()
  })

  it('nothing at all is the empty state, not an empty month', async () => {
    serve(() => [])
    const { findByRole, queryByRole } = render(<HighlightArchiveTab />)
    await findByRole('heading', { name: 'No highlights in the archive yet' })
    expect(queryByRole('combobox')).toBeNull()
  })

  it('a failed load is an alert with Try again instead of an empty list', async () => {
    let fail = true
    serve(() => { if (fail) throw new Error('offline'); return [mine] })
    const { findByRole, container } = render(<HighlightArchiveTab />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load highlights")
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await findByRole('link', { name: /You/ })
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('refreshes on a live highlight frame', async () => {
    let rows = [hilde]
    serve(() => rows)
    const { findByRole } = render(<HighlightArchiveTab />)
    await findByRole('link', { name: /Oma Hilde/ })
    rows = [hilde, mine]
    wsHandlers.get('highlight.frame_added')!()
    await waitFor(async () => expect(await findByRole('link', { name: /You/ })).toBeTruthy())
  })
})
