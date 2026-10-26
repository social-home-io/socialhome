import { describe, it, expect } from 'vitest'
import type { Highlight, HighlightFrame, HighlightInboxItem } from '@/types'
import { audienceChip, todayView, ENDS_SOON_MS } from './today'

const NOW = Date.parse('2026-10-26T12:00:00Z')
const H = 3_600_000

function iso(msAgo: number): string {
  return new Date(NOW - msAgo).toISOString()
}

function frame(id: string, highlightId: string, msAgo: number, type: 'image' | 'video' = 'image'): HighlightFrame {
  return {
    id, highlight_id: highlightId, sequence: 1, frame_type: type,
    media_url: `api/media/${id}.webp`, caption_text: null, caption_emoji: null,
    duration_ms: null, created_at: iso(msAgo),
  }
}

function item(
  id: string, author: string, frames: HighlightFrame[],
  opts: Partial<Highlight> & { unseen?: number; seen_by?: number } = {},
): HighlightInboxItem {
  const { unseen = 0, seen_by, ...h } = opts
  return {
    highlight: {
      id, author_user_id: author, highlight_date: '2026-10-26',
      audience_kind: 'all_paired', audience_label: 'paired',
      created_at: iso(30 * H), expires_at: new Date(NOW + 30 * 24 * H).toISOString(),
      ...h,
    },
    frames,
    unseen_count: unseen,
    ...(seen_by === undefined ? {} : { seen_by }),
  }
}

describe('todayView', () => {
  it('puts each person on Today once, however many highlight days they have', () => {
    // Thomas posted late yesterday and again this morning: one row, not
    // two (bug 15 / B12). Unseen frames add up across both days.
    const view = todayView([
      item('t-today', 'thomas', [frame('t2', 't-today', 2 * H)], { unseen: 1 }),
      item('t-yday', 'thomas', [frame('t1', 't-yday', 20 * H)], { unseen: 2, highlight_date: '2026-10-25' }),
    ], 'me', NOW)
    expect(view.fresh).toHaveLength(1)
    expect(view.fresh[0].authorId).toBe('thomas')
    expect(view.fresh[0].unseen).toBe(3)
    expect(view.fresh[0].frames.map(f => f.id)).toEqual(['t2', 't1'])
    // It opens where the unseen frames start: the older day.
    expect(view.fresh[0].openId).toBe('t-yday')
    expect(view.fresh[0].lastAt).toBe(iso(2 * H))
  })

  it('leaves people whose newest frame is older than 24 h to the Archive', () => {
    const view = todayView([
      item('hilde', 'hilde', [frame('h1', 'hilde', 25 * H)], { unseen: 1 }),
      item('lena', 'lena', [frame('l1', 'lena', 23 * H)]),
    ], 'me', NOW)
    expect(view.fresh).toEqual([])
    expect(view.seen.map(p => p.authorId)).toEqual(['lena'])
  })

  it('counts only frames from the last 24 h on a row', () => {
    const view = todayView([
      item('a', 'anna', [frame('old', 'a', 30 * H), frame('new', 'a', 1 * H)]),
    ], 'me', NOW)
    expect(view.seen[0].frames.map(f => f.id)).toEqual(['new'])
  })

  it('splits New from Seen and keeps your own highlight out of both', () => {
    const view = todayView([
      item('mine', 'me', [frame('m1', 'mine', 1 * H)], { seen_by: 3 }),
      item('a', 'anna', [frame('a1', 'a', 3 * H)], { unseen: 1 }),
      item('b', 'bert', [frame('b1', 'b', 1 * H)], { unseen: 2 }),
      item('c', 'carl', [frame('c1', 'c', 2 * H)]),
    ], 'me', NOW)
    expect(view.mine?.authorId).toBe('me')
    expect(view.mine?.seenBy).toBe(3)
    expect(view.mine?.openId).toBe('mine')
    // Newest first in each section.
    expect(view.fresh.map(p => p.authorId)).toEqual(['bert', 'anna'])
    expect(view.seen.map(p => p.authorId)).toEqual(['carl'])
  })

  it('has no status for you when you shared nothing in the last 24 h', () => {
    const view = todayView([
      item('mine', 'me', [frame('m1', 'mine', 26 * H)], { seen_by: 3 }),
    ], 'me', NOW)
    expect(view.mine).toBeNull()
  })

  it('opens the newest day when everything is seen', () => {
    const view = todayView([
      item('new', 'anna', [frame('n1', 'new', 1 * H)]),
      item('old', 'anna', [frame('o1', 'old', 20 * H)]),
    ], 'me', NOW)
    expect(view.seen[0].openId).toBe('new')
  })

  it('warns only when a highlight ends soon', () => {
    const soon = new Date(NOW + 2 * H).toISOString()
    const view = todayView([
      item('l', 'lena', [frame('l1', 'l', 19 * H)], { expires_at: soon }),
      item('a', 'anna', [frame('a1', 'a', 1 * H)]),
    ], 'me', NOW)
    const lena = view.seen.find(p => p.authorId === 'lena')!
    const anna = view.seen.find(p => p.authorId === 'anna')!
    expect(lena.endsAt).toBe(soon)
    expect(anna.endsAt).toBeNull()
    expect(ENDS_SOON_MS).toBeGreaterThanOrEqual(2 * H)
  })

  it('reads naive SQLite timestamps as UTC', () => {
    const f = { ...frame('x', 'x', 0), created_at: '2026-10-26 11:00:00' }
    const view = todayView([item('x', 'anna', [f])], 'me', NOW)
    expect(view.seen).toHaveLength(1)
  })
})

describe('audienceChip', () => {
  const base = item('h', 'anna', []).highlight

  it('gives viewers the short coarse label', () => {
    expect(audienceChip({ ...base, audience_label: 'paired' }, false).text).toBe('Paired households')
    expect(audienceChip({ ...base, audience_label: 'household' }, false).text).toBe('Household only')
    expect(audienceChip({ ...base, audience_label: 'just_you' }, false).text).toBe('Just you')
    expect(audienceChip({ ...base, audience_label: 'people' }, false).text).toBe('Picked people')
  })

  it('tells the author exactly how wide their own highlight goes', () => {
    expect(audienceChip({ ...base, audience_kind: 'all_paired', audience: [] }, true).text)
      .toBe('Paired households')
    expect(audienceChip({ ...base, audience_kind: 'households', audience: ['i1', 'i2'] }, true).text)
      .toBe('2 paired households')
    expect(audienceChip({ ...base, audience_kind: 'households', audience: [] }, true).text)
      .toBe('Your household')
    expect(audienceChip({ ...base, audience_kind: 'users', audience: ['u1'] }, true).text)
      .toBe('1 person')
    expect(audienceChip({ ...base, audience_kind: 'users', audience: ['u1', 'u2', 'u3'] }, true).text)
      .toBe('3 people')
  })
})
