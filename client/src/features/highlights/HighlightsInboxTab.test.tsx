import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent, within } from '@testing-library/preact'

const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import type { HighlightFrame, HighlightInboxItem } from '@/types'
import HighlightsInboxTab from './HighlightsInboxTab'

const H = 3_600_000

function ago(ms: number): string {
  return new Date(Date.now() - ms).toISOString()
}

function frame(id: string, hid: string, msAgo: number, extra: Partial<HighlightFrame> = {}): HighlightFrame {
  return {
    id, highlight_id: hid, sequence: 1, frame_type: 'image',
    media_url: `api/media/${id}.webp?sig=x`, caption_text: null, caption_emoji: null,
    duration_ms: null, created_at: ago(msAgo), ...extra,
  }
}

function item(
  id: string, author: string, name: string, frames: HighlightFrame[],
  extra: { unseen?: number; seen_by?: number; label?: string; expires?: string;
    kind?: string; audience?: string[] } = {},
): HighlightInboxItem {
  return {
    highlight: {
      id, author_user_id: author, highlight_date: '2026-10-26',
      audience_kind: extra.kind ?? 'all_paired', audience_label: extra.label ?? 'paired',
      ...(extra.audience ? { audience: extra.audience } : {}),
      created_at: ago(30 * H), expires_at: extra.expires ?? new Date(Date.now() + 20 * 24 * H).toISOString(),
      author: { display_name: name, picture_url: null },
    },
    frames,
    unseen_count: extra.unseen ?? 0,
    ...(extra.seen_by === undefined ? {} : { seen_by: extra.seen_by }),
  } as unknown as HighlightInboxItem
}

const amaka = item('h1', 'ru-amaka', 'Amaka Okafor', [frame('a1', 'h1', 1 * H)], { unseen: 1 })

function serve(rows: () => HighlightInboxItem[]) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path !== '/api/highlights') return []
    return rows()
  })
}

beforeEach(() => {
  wsHandlers.clear()
  vi.mocked(api.get).mockReset()
  currentUser.value = { user_id: 'me', username: 'me', display_name: 'Me' } as never
})
afterEach(() => { currentUser.value = null })

describe('HighlightsInboxTab (Today)', () => {
  it('shows your status card: frames, seen by, who sees it, Watch and Add frames', async () => {
    serve(() => [
      item('mine', 'me', 'Me', [frame('m1', 'mine', 1 * H), frame('m2', 'mine', 2 * H)],
        { seen_by: 3, kind: 'households', audience: ['i1', 'i2'] }),
    ])
    const { findByRole, container } = render(<HighlightsInboxTab />)
    const card = await findByRole('region', { name: 'Your highlight' })
    expect(card.textContent).toContain('2 frames')
    expect(card.textContent).toContain('seen by 3')
    expect(card.textContent).toContain('2 paired households')
    expect(within(card).getByRole('link', { name: 'Watch' }).getAttribute('href')).toBe('/highlights/mine')
    expect(within(card).getByRole('link', { name: 'Add frames' }).getAttribute('href')).toBe('/highlights/new')
    // Your own highlight is never a New row.
    expect(container.querySelector('[data-section="new"]')).toBeNull()
  })

  it('offers Share a highlight when you shared nothing in the last 24 h', async () => {
    serve(() => [amaka])
    const { findByRole } = render(<HighlightsInboxTab />)
    const card = await findByRole('region', { name: 'Your highlight' })
    expect(card.textContent).toContain('Nothing shared today')
    expect(within(card).getByRole('link', { name: 'Share a highlight' }).getAttribute('href'))
      .toBe('/highlights/new')
  })

  it('lists each person once under New or Seen, never older than 24 h', async () => {
    serve(() => [
      item('t2', 'ru-thomas', 'Thomas Berger', [frame('t2a', 't2', 1.2 * H), frame('t2b', 't2', 1.5 * H)], { unseen: 2 }),
      item('t1', 'ru-thomas', 'Thomas Berger', [frame('t1a', 't1', 20 * H)], { unseen: 1 }),
      amaka,
      item('l', 'ru-lena', 'Lena', [frame('l1', 'l', 5 * H)], { label: 'just_you' }),
      item('o', 'ru-hilde', 'Oma Hilde', [frame('o1', 'o', 26 * H)], { unseen: 1 }),
    ])
    const { findByRole, container } = render(<HighlightsInboxTab />)
    const fresh = await findByRole('region', { name: 'New' })
    const rows = within(fresh).getAllByRole('link')
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('Amaka Okafor'),
      expect.stringContaining('Thomas Berger'),
    ])
    expect(fresh.textContent).toContain('2 people')
    // One row for Thomas, all his frames from today, his new count spoken.
    expect(rows[1].textContent).toContain('3 frames')
    expect(rows[1].textContent).toContain('3 new')
    expect(rows[1].getAttribute('href')).toBe('/highlights/t1')
    const seen = await findByRole('region', { name: 'Seen' })
    expect(seen.textContent).toContain('Lena')
    expect(seen.textContent).toContain('Just you')
    expect(container.textContent).not.toContain('Oma Hilde')
    const archive = container.querySelector('a[href="/highlights/archive"]')
    expect(archive?.textContent).toContain('Older highlights are in the Archive')
  })

  it('names are user text: dir=auto', async () => {
    serve(() => [amaka])
    const { findByText } = render(<HighlightsInboxTab />)
    expect((await findByText('Amaka Okafor')).getAttribute('dir')).toBe('auto')
  })

  it('warns when a highlight ends soon', async () => {
    serve(() => [item('l', 'ru-lena', 'Lena', [frame('l1', 'l', 20 * H)],
      { expires: new Date(Date.now() + 2 * H + 60_000).toISOString() })])
    const { findByRole } = render(<HighlightsInboxTab />)
    const seen = await findByRole('region', { name: 'Seen' })
    expect(seen.textContent).toMatch(/ends in 2 hours/)
  })

  it('shows a video frame as its poster, not a 🎬', async () => {
    serve(() => [item('v', 'ru-max', 'Max', [frame('v1', 'v', 1 * H, {
      frame_type: 'video', media_url: 'api/media/v1.webm?sig=x', thumbnail_url: 'api/media/v1.webp?sig=y',
    })], { unseen: 1 })])
    const { findByRole, container } = render(<HighlightsInboxTab />)
    const fresh = await findByRole('region', { name: 'New' })
    expect(fresh.textContent).toContain('1 video')
    expect(fresh.textContent).not.toContain('🎬')
    expect(container.querySelector('img[src="api/media/v1.webp?sig=y"]')).not.toBeNull()
  })

  it('an empty day says so and offers Share and the Archive', async () => {
    serve(() => [])
    const { findByRole, container } = render(<HighlightsInboxTab />)
    const empty = await findByRole('heading', { name: 'No highlights today' })
    expect(empty).toBeTruthy()
    expect(container.querySelector('.sh-empty-state a[href="/highlights/archive"]')).not.toBeNull()
  })

  it('shows row placeholders while loading', () => {
    vi.mocked(api.get).mockImplementation(() => new Promise(() => undefined))
    const { container } = render(<HighlightsInboxTab />)
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })
})

describe('HighlightsInboxTab when a load fails', () => {
  it('a failed first load is an alert with Try again, never "no highlights"', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/highlights') return []
      if (fail) throw new Error('offline')
      return [amaka]
    })
    const { findByRole, container } = render(<HighlightsInboxTab />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load highlights")
    expect(container.querySelector('.sh-empty-state:not(.sh-load-error)')).toBeNull()
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('a failed refresh keeps the list and says from when it is', async () => {
    let fail = false
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/highlights') return []
      if (fail) throw new Error('offline')
      return [amaka]
    })
    const { findByRole, container } = render(<HighlightsInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    fail = true
    wsHandlers.get('highlight.frame_added')!()
    expect((await findByRole('status')).textContent).toMatch(/Couldn't refresh\. Showing highlights from/)
    expect(container.textContent).toContain('Amaka Okafor')
  })
})
