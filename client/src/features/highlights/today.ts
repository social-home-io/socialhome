/**
 * today — who is on Highlights "Today", one entry per person.
 *
 * - **Today** is a person with a frame from the last 24 hours. Anything
 *   older lives in the Archive only (a 10-day-old highlight is not
 *   "today", whatever the author's retention).
 * - **One per person** (bug 15 / B12): an author who posted late
 *   yesterday and again this morning has two highlight days; they are
 *   one row (and one ring on Home). Unseen frames add up; the row opens
 *   the oldest day with something unseen, else the newest.
 * - **Your own** highlight is never New (#1163); it is the status card.
 *
 * Pure — the Today tab and the Home "today" card share it.
 */
import type { Highlight, HighlightFrame, HighlightInboxItem } from '@/types'
import { isOne, t } from '@/i18n/i18n'
import { normaliseTimestamp } from '@/utils/relativeTime'

/** A person is on Today while their newest frame is this recent. */
export const TODAY_WINDOW_MS = 24 * 3_600_000
/** "ends in 2 hours" shows once a highlight has less than this left. */
export const ENDS_SOON_MS = 6 * 3_600_000

export interface TodayPerson {
  authorId: string
  /** The newest highlight day with a frame today: who wrote it and its
   *  audience come from here. */
  highlight: Highlight
  /** The highlight the row opens. */
  openId: string
  /** Today's frames (last 24 h), newest first. */
  frames: HighlightFrame[]
  /** When the newest of them was added. */
  lastAt: string
  /** Frames this viewer hasn't watched (always 0 for your own). */
  unseen: number
  /** Your own only: how many people watched it. */
  seenBy: number | null
  /** ``expires_at`` of a day that ends within ``ENDS_SOON_MS``, else null. */
  endsAt: string | null
}

export interface TodayView {
  mine: TodayPerson | null
  /** People with unseen frames, newest first. */
  fresh: TodayPerson[]
  /** People you've caught up with, newest first. */
  seen: TodayPerson[]
}

function ms(iso: string): number {
  return Date.parse(normaliseTimestamp(iso))
}

export function todayView(
  items: readonly HighlightInboxItem[], me: string | null | undefined, now: number = Date.now(),
): TodayView {
  const since = now - TODAY_WINDOW_MS
  const byAuthor = new Map<string, { item: HighlightInboxItem; recent: HighlightFrame[] }[]>()
  for (const item of items) {
    const recent = item.frames.filter(f => ms(f.created_at) >= since)
    if (recent.length === 0) continue
    const author = item.highlight.author_user_id
    const list = byAuthor.get(author) ?? []
    list.push({ item, recent })
    byAuthor.set(author, list)
  }

  const people: TodayPerson[] = []
  for (const [authorId, days] of byAuthor) {
    const frames = days.flatMap(d => d.recent).sort((a, b) => ms(b.created_at) - ms(a.created_at))
    const lastAt = frames[0].created_at
    // Days newest-first by their newest frame.
    const ordered = [...days].sort((a, b) => ms(newest(b.recent)) - ms(newest(a.recent)))
    const newestDay = ordered[0].item
    const withUnseen = ordered.filter(d => d.item.unseen_count > 0)
    const openId = withUnseen.length > 0
      ? withUnseen[withUnseen.length - 1].item.highlight.id
      : newestDay.highlight.id
    const ends = days
      .map(d => d.item.highlight.expires_at)
      .filter(e => {
        const left = ms(e) - now
        return left > 0 && left < ENDS_SOON_MS
      })
      .sort((a, b) => ms(a) - ms(b))
    const seenBy = days.reduce<number | null>((acc, d) => (
      d.item.seen_by === undefined ? acc : Math.max(acc ?? 0, d.item.seen_by)
    ), null)
    people.push({
      authorId,
      highlight: newestDay.highlight,
      openId,
      frames,
      lastAt,
      unseen: days.reduce((n, d) => n + d.item.unseen_count, 0),
      seenBy,
      endsAt: ends[0] ?? null,
    })
  }
  people.sort((a, b) => ms(b.lastAt) - ms(a.lastAt))

  const mine = me ? people.find(p => p.authorId === me) ?? null : null
  const others = people.filter(p => p !== mine)
  return {
    mine,
    fresh: others.filter(p => p.unseen > 0),
    seen: others.filter(p => p.unseen === 0),
  }
}

function newest(frames: readonly HighlightFrame[]): string {
  return frames.reduce((a, f) => (ms(f.created_at) > ms(a) ? f.created_at : a), frames[0].created_at)
}

export type AudienceIcon = 'households' | 'home' | 'lock' | 'people'

/** The short audience chip on a row or the status card. Viewers get the
 *  coarse ``audience_label`` (#1165); the author, who knows the ids, gets
 *  how many households / people. */
export function audienceChip(h: Highlight, mine: boolean): { icon: AudienceIcon; text: string } {
  if (mine) {
    const n = h.audience?.length ?? 0
    switch (h.audience_kind) {
      case 'households':
        if (n === 0) return { icon: 'home', text: t('highlight.chip.your_household') }
        return {
          icon: 'households',
          text: t(isOne(n) ? 'highlight.chip.n_households_one' : 'highlight.chip.n_households', { n: String(n) }),
        }
      case 'users':
        return {
          icon: n === 0 ? 'lock' : 'people',
          text: t(isOne(n) ? 'highlight.chip.n_people_one' : 'highlight.chip.n_people', { n: String(n) }),
        }
      default:
        return { icon: 'households', text: t('highlight.chip.paired') }
    }
  }
  switch (h.audience_label) {
    case 'household': return { icon: 'home', text: t('highlight.chip.household') }
    case 'just_you': return { icon: 'lock', text: t('highlight.chip.just_you') }
    case 'people': return { icon: 'people', text: t('highlight.chip.people') }
    default: return { icon: 'households', text: t('highlight.chip.paired') }
  }
}
