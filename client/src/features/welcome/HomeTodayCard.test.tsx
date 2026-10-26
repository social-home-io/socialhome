import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent, within } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { userPreferences } from '@/store/userPreferences'
import { markMomentumSeen, momentumSeenAt } from '@/store/momentumSeen'
import type { HighlightInboxItem, Moment } from '@/types'
import { HomeTodayCard } from './HomeTodayCard'

const H = 3_600_000
const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
const later = (ms: number) => new Date(Date.now() + ms).toISOString()

function hl(id: string, author: string, name: string, msAgo: number, unseen: number): HighlightInboxItem {
  return {
    highlight: {
      id, author_user_id: author, highlight_date: '2026-10-26', audience_kind: 'all_paired',
      audience_label: 'paired', created_at: ago(msAgo), expires_at: later(30 * 24 * H),
      author: { display_name: name, picture_url: null },
    },
    frames: [{
      id: `${id}-f`, highlight_id: id, sequence: 1, frame_type: 'image', media_url: 'api/media/x.webp',
      caption_text: null, caption_emoji: null, duration_ms: null, created_at: ago(msAgo),
    }],
    unseen_count: unseen,
  } as unknown as HighlightInboxItem
}

function moment(id: string, author: string, name: string, msAgo: number): Moment {
  return {
    id, author_user_id: author, content: 'hi', media_url: null, media_type: null, duration_ms: null,
    parent_moment_id: null, origin_instance_id: 'i', created_at: ago(msAgo), expires_at: later(H),
    reaction_count: 0, reply_count: 0, author: { display_name: name, picture_url: null },
  } as unknown as Moment
}

let highlights: HighlightInboxItem[] = []
let moments: Moment[] = []

beforeEach(() => {
  window.localStorage.clear()
  highlights = []
  moments = []
  currentUser.value = { user_id: 'me', username: 'me', display_name: 'Me' } as never
  userPreferences.value = { ...userPreferences.value, hide_highlights: false, hide_momentum: false }
  vi.mocked(api.get).mockReset()
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === '/api/highlights') return highlights
    if (path === '/api/moments') return moments
    throw new Error(`unexpected ${path}`)
  })
})
afterEach(() => { currentUser.value = null })

describe('HomeTodayCard', () => {
  it('stays hidden when nothing is new', async () => {
    highlights = [hl('seen', 'ru-a', 'Anna', 1 * H, 0), hl('mine', 'me', 'Me', 1 * H, 0)]
    moments = [moment('m0', 'me', 'Me', 1 * H)]
    const { container } = render(<HomeTodayCard />)
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    expect(container.innerHTML).toBe('')
  })

  it('rings: You adds, then one ring per person with something new, marked with a count', async () => {
    highlights = [
      hl('t2', 'ru-t', 'Thomas Berger', 1 * H, 2),
      hl('t1', 'ru-t', 'Thomas Berger', 20 * H, 1),
      hl('a', 'ru-a', 'Anna', 2 * H, 0),
      hl('old', 'ru-o', 'Oma Hilde', 30 * H, 1),
    ]
    const { findByRole } = render(<HomeTodayCard />)
    const card = await findByRole('region', { name: 'New today' })
    const links = within(card).getAllByRole('link')
    expect(links[0].getAttribute('href')).toBe('/highlights')
    expect(links[0].getAttribute('aria-label')).toBe('All highlights')
    expect(links[1].getAttribute('href')).toBe('/highlights/new')
    expect(links[1].getAttribute('aria-label')).toBe('Share a highlight')
    expect(links[1].textContent).toContain('You')
    expect(links[2].getAttribute('href')).toBe('/highlights/t1')
    expect(links[2].getAttribute('aria-label')).toBe('Thomas Berger’s highlight, 3 new')
    // A visible marker, not a glow only (C16).
    expect(links[2].querySelector('.sh-home-ring__badge')?.textContent).toBe('3')
    expect(links).toHaveLength(3)
    expect(card.textContent).not.toContain('Momentum')
  })

  it('Momentum row counts moments by others since you last opened Momentum', async () => {
    markMomentumSeen('me', Date.now() - 3 * H)
    moments = [
      moment('m1', 'ru-a', 'Anna', 1 * H),
      moment('m2', 'ru-t', 'Thomas', 2 * H),
      moment('m3', 'ru-t', 'Thomas', 2.5 * H),
      moment('m4', 'ru-s', 'Sophie Keller', 4 * H),
      moment('m5', 'me', 'Me', 1 * H),
    ]
    const { findByRole } = render(<HomeTodayCard />)
    const row = await findByRole('link', { name: /Momentum · 3 new/ })
    expect(row.getAttribute('href')).toBe('/momentum')
    expect(row.textContent).toContain('Anna and Thomas')
    expect(row.textContent).not.toContain('Sophie')
    fireEvent.click(row)
    expect(momentumSeenAt('me')).toBeGreaterThan(Date.now() - 1000)
  })

  it('without a visit on record, the last 24 hours are new; long name lists are shortened', async () => {
    moments = ['A', 'B', 'C', 'D'].map((n, i) => moment(`m${i}`, `ru-${n}`, n, (i + 1) * H))
    moments.push(moment('old', 'ru-x', 'X', 26 * H))
    const { findByRole } = render(<HomeTodayCard />)
    const row = await findByRole('link', { name: /Momentum · 4 new/ })
    expect(row.textContent).toContain('A, B and 2 more')
  })

  it('hidden sections stay hidden: no fetch for what the person turned off', async () => {
    userPreferences.value = { ...userPreferences.value, hide_highlights: true, hide_momentum: true }
    const { container } = render(<HomeTodayCard />)
    await new Promise(r => setTimeout(r, 0))
    expect(api.get).not.toHaveBeenCalled()
    expect(container.innerHTML).toBe('')
  })

  it('a failed load hides the card instead of showing an error on Home', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const { container } = render(<HomeTodayCard />)
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })
})
