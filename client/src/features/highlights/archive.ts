/**
 * archive — the Highlights Archive as a list of days (r3 design, H7).
 *
 * - **Local days.** Frames are grouped by the viewer's local calendar
 *   day of ``created_at`` (``localDayKey``), never by the UTC
 *   ``highlight_date`` (B23): a frame at 00:30 in Zurich belongs to the
 *   day it was taken there.
 * - **One row per person per day.** West of UTC one local day can span
 *   two highlight days; their frames join one row (oldest first) that
 *   opens the day's first highlight.
 * - **Newest day first**; in a day your own row comes first, then by
 *   the newest frame.
 *
 * Pure — the Archive tab renders what this returns.
 */
import type { Highlight, HighlightFrame, HighlightInboxItem } from '@/types'
import { addDaysToKey, formatDayKey } from '@/utils/calendar'
import { localDayKey, normaliseTimestamp } from '@/utils/relativeTime'
import { t } from '@/i18n/i18n'

export interface ArchiveRow {
  authorId: string
  /** The highlight of the row's newest frame (author, audience). */
  highlight: Highlight
  /** The highlight the row opens: the day's first. */
  openId: string
  /** The person's frames on this day, oldest first. */
  frames: HighlightFrame[]
  /** Your own only: how many people watched it (the most over the
   *  row's highlights). ``null`` on everyone else's. */
  seenBy: number | null
  /** When the row's first highlight is deleted (the author's setting). */
  endsAt: string
  mine: boolean
}

export interface ArchiveDay {
  /** ``YYYY-MM-DD``, the viewer's local day. */
  key: string
  rows: ArchiveRow[]
}

function ms(iso: string): number {
  return Date.parse(normaliseTimestamp(iso))
}

export function archiveDays(
  items: readonly HighlightInboxItem[],
  me: string | null | undefined,
  onlyMine: boolean,
): ArchiveDay[] {
  // day → author → the (item, frames-of-that-day) pairs.
  const byDay = new Map<string, Map<string, { item: HighlightInboxItem; frames: HighlightFrame[] }[]>>()
  for (const item of items) {
    const author = item.highlight.author_user_id
    if (onlyMine && author !== me) continue
    const perDay = new Map<string, HighlightFrame[]>()
    for (const f of item.frames) {
      const key = localDayKey(f.created_at) ?? item.highlight.highlight_date
      perDay.set(key, [...(perDay.get(key) ?? []), f])
    }
    for (const [key, frames] of perDay) {
      const authors = byDay.get(key) ?? new Map()
      authors.set(author, [...(authors.get(author) ?? []), { item, frames }])
      byDay.set(key, authors)
    }
  }

  const days: ArchiveDay[] = []
  for (const [key, authors] of byDay) {
    const rows: ArchiveRow[] = []
    for (const [authorId, parts] of authors) {
      const frames = parts.flatMap(p => p.frames).sort((a, b) => ms(a.created_at) - ms(b.created_at))
      const first = frames[0]
      const last = frames[frames.length - 1]
      const items = parts.map(p => p.item)
      const of = (f: HighlightFrame) => items.find(i => i.highlight.id === f.highlight_id) ?? items[0]
      const seenBy = items.reduce<number | null>((acc, i) => (
        i.seen_by === undefined ? acc : Math.max(acc ?? 0, i.seen_by)
      ), null)
      rows.push({
        authorId,
        highlight: of(last).highlight,
        openId: of(first).highlight.id,
        frames,
        seenBy,
        endsAt: items.map(i => i.highlight.expires_at).sort((a, b) => ms(a) - ms(b))[0],
        mine: !!me && authorId === me,
      })
    }
    rows.sort((a, b) => {
      if (a.mine !== b.mine) return a.mine ? -1 : 1
      return ms(b.frames[b.frames.length - 1].created_at) - ms(a.frames[a.frames.length - 1].created_at)
    })
    days.push({ key, rows })
  }
  days.sort((a, b) => (a.key < b.key ? 1 : a.key > b.key ? -1 : 0))
  return days
}

/** ``YYYY-MM`` of every month with a day in ``days``, newest first — the
 *  month select offers nothing else (C15: no paging into empty months). */
export function archiveMonths(days: readonly ArchiveDay[]): string[] {
  const out: string[] = []
  for (const d of days) {
    const m = d.key.slice(0, 7)
    if (!out.includes(m)) out.push(m)
  }
  return out
}

/** "October 2026" for ``2026-10``. */
export function monthLabel(month: string): string {
  return formatDayKey(`${month}-01`, { month: 'long', year: 'numeric' })
}

/** "Sunday, 25 October · today" — a day key in the UI's locale, with
 *  today / yesterday named, and the year only when it isn't this year. */
export function dayHeading(key: string, todayKey: string): string {
  const date = formatDayKey(key, {
    weekday: 'long', day: 'numeric', month: 'long',
    year: key.slice(0, 4) === todayKey.slice(0, 4) ? undefined : 'numeric',
  })
  if (key === todayKey) return t('highlight.archive.day_today', { date })
  if (key === addDaysToKey(todayKey, -1)) return t('highlight.archive.day_yesterday', { date })
  return date
}

export interface ArchiveFilter {
  /** ``YYYY-MM`` picked in the month select; ``null`` = the newest. */
  month: string | null
  onlyMine: boolean
}

/** The Archive's filter lives in its URL (``?month=2026-09&mine=1``), so
 *  Back from the viewer and a reload keep it. */
export function archiveFilterFromUrl(url: string): ArchiveFilter {
  const params = new URLSearchParams(url.split('?')[1]?.split('#')[0] ?? '')
  const month = params.get('month')
  return {
    month: month && /^\d{4}-\d{2}$/.test(month) ? month : null,
    onlyMine: params.get('mine') === '1',
  }
}

/** The Archive URL for ``filter`` (in-app path, before ``addBase``). */
export function archiveUrl(filter: ArchiveFilter): string {
  const params = new URLSearchParams()
  if (filter.month) params.set('month', filter.month)
  if (filter.onlyMine) params.set('mine', '1')
  const q = params.toString()
  return q ? `/highlights/archive?${q}` : '/highlights/archive'
}
