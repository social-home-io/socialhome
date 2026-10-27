import { describe, it, expect, afterEach } from 'vitest'
import type { Highlight, HighlightFrame, HighlightInboxItem } from '@/types'
import {
  archiveDays, archiveFilterFromUrl, archiveMonths, archiveUrl, dayHeading, monthLabel,
} from './archive'

describe('archive filter in the URL', () => {
  it('reads the month and Only mine, ignoring junk', () => {
    expect(archiveFilterFromUrl('/highlights/archive')).toEqual({ month: null, onlyMine: false })
    expect(archiveFilterFromUrl('/highlights/archive?month=2026-09&mine=1'))
      .toEqual({ month: '2026-09', onlyMine: true })
    expect(archiveFilterFromUrl('/highlights/archive?month=nope&mine=yes'))
      .toEqual({ month: null, onlyMine: false })
  })

  it('round-trips', () => {
    expect(archiveUrl({ month: null, onlyMine: false })).toBe('/highlights/archive')
    const url = archiveUrl({ month: '2026-09', onlyMine: true })
    expect(url).toBe('/highlights/archive?month=2026-09&mine=1')
    expect(archiveFilterFromUrl(url)).toEqual({ month: '2026-09', onlyMine: true })
  })
})

function frame(id: string, highlightId: string, at: string): HighlightFrame {
  return {
    id, highlight_id: highlightId, sequence: 1, frame_type: 'image',
    media_url: `api/media/${id}.webp`, caption_text: null, caption_emoji: null,
    duration_ms: null, created_at: at,
  }
}

function item(
  id: string, author: string, frames: HighlightFrame[],
  opts: Partial<Highlight> & { seen_by?: number } = {},
): HighlightInboxItem {
  const { seen_by, ...h } = opts
  return {
    highlight: {
      id, author_user_id: author, highlight_date: frames[0]?.created_at.slice(0, 10) ?? '2026-10-25',
      audience_kind: 'all_paired', audience_label: 'paired',
      created_at: frames[0]?.created_at ?? '2026-10-25T08:00:00Z',
      expires_at: '2026-11-24T08:00:00Z',
      ...h,
    },
    frames,
    unseen_count: 0,
    ...(seen_by === undefined ? {} : { seen_by }),
  }
}

describe('archiveDays', () => {
  const prevTz = process.env.TZ
  afterEach(() => { process.env.TZ = prevTz })

  it('groups frames by the viewer\'s local day, not the UTC highlight_date', () => {
    // 23:30 UTC on the 24th is already the 25th in Berlin (B23).
    process.env.TZ = 'Europe/Berlin'
    const days = archiveDays([
      item('h1', 'lena', [frame('f1', 'h1', '2026-10-24T23:30:00Z')], { highlight_date: '2026-10-24' }),
    ], 'me', false)
    expect(days.map(d => d.key)).toEqual(['2026-10-25'])
  })

  it('one row per person per day, even across two highlight days', () => {
    // Los Angeles: 10:00 and 20:00 PDT on the 24th are two UTC days.
    process.env.TZ = 'America/Los_Angeles'
    const days = archiveDays([
      item('late', 'thomas', [frame('t2', 'late', '2026-10-25T03:00:00Z')], { highlight_date: '2026-10-25' }),
      item('early', 'thomas', [frame('t1', 'early', '2026-10-24T17:00:00Z')], { highlight_date: '2026-10-24' }),
    ], 'me', false)
    expect(days).toHaveLength(1)
    expect(days[0].key).toBe('2026-10-24')
    expect(days[0].rows).toHaveLength(1)
    const row = days[0].rows[0]
    // Oldest frame first, and the row opens where the day starts.
    expect(row.frames.map(f => f.id)).toEqual(['t1', 't2'])
    expect(row.openId).toBe('early')
  })

  it('newest day first; your own row first, then by newest frame', () => {
    process.env.TZ = 'UTC'
    const days = archiveDays([
      item('a', 'anna', [frame('a1', 'a', '2026-10-25T09:00:00Z')]),
      item('m', 'me', [frame('m1', 'm', '2026-10-25T08:00:00Z')], { seen_by: 3 }),
      item('b', 'bert', [frame('b1', 'b', '2026-10-25T10:00:00Z')]),
      item('o', 'hilde', [frame('o1', 'o', '2026-10-15T10:00:00Z')]),
    ], 'me', false)
    expect(days.map(d => d.key)).toEqual(['2026-10-25', '2026-10-15'])
    expect(days[0].rows.map(r => r.authorId)).toEqual(['me', 'bert', 'anna'])
    expect(days[0].rows[0].mine).toBe(true)
    expect(days[0].rows[0].seenBy).toBe(3)
    expect(days[0].rows[1].seenBy).toBeNull()
  })

  it('"Only mine" keeps only your own rows', () => {
    process.env.TZ = 'UTC'
    const days = archiveDays([
      item('a', 'anna', [frame('a1', 'a', '2026-10-25T09:00:00Z')]),
      item('m', 'me', [frame('m1', 'm', '2026-10-20T08:00:00Z')]),
    ], 'me', true)
    expect(days.map(d => d.key)).toEqual(['2026-10-20'])
    expect(days[0].rows.map(r => r.authorId)).toEqual(['me'])
  })

  it('the row ends when its first highlight ends', () => {
    process.env.TZ = 'America/Los_Angeles'
    const days = archiveDays([
      item('late', 'thomas', [frame('t2', 'late', '2026-10-25T03:00:00Z')], { expires_at: '2026-10-28T03:00:00Z' }),
      item('early', 'thomas', [frame('t1', 'early', '2026-10-24T17:00:00Z')], { expires_at: '2026-10-27T17:00:00Z' }),
    ], 'me', false)
    expect(days[0].rows[0].endsAt).toBe('2026-10-27T17:00:00Z')
  })

  it('skips a highlight with no frames', () => {
    expect(archiveDays([item('e', 'anna', [])], 'me', false)).toEqual([])
  })
})

describe('archiveMonths', () => {
  it('lists only months that have highlights, newest first', () => {
    process.env.TZ = 'UTC'
    const days = archiveDays([
      item('a', 'anna', [frame('a1', 'a', '2026-10-02T09:00:00Z')]),
      item('b', 'anna', [frame('b1', 'b', '2026-10-25T09:00:00Z')]),
      item('c', 'anna', [frame('c1', 'c', '2026-08-30T09:00:00Z')]),
    ], 'me', false)
    expect(archiveMonths(days)).toEqual(['2026-10', '2026-08'])
    expect(monthLabel('2026-08')).toBe('August 2026')
  })
})

describe('dayHeading', () => {
  it('names today and yesterday next to the date', () => {
    expect(dayHeading('2026-10-25', '2026-10-25')).toBe('Sunday, October 25 · today')
    expect(dayHeading('2026-10-24', '2026-10-25')).toBe('Saturday, October 24 · yesterday')
    expect(dayHeading('2026-10-21', '2026-10-25')).toBe('Wednesday, October 21')
  })

  it('adds the year only when it is not this year', () => {
    expect(dayHeading('2025-12-30', '2026-01-02')).toBe('Tuesday, December 30, 2025')
  })
})
