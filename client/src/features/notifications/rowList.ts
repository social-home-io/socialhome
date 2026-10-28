/**
 * List helpers shared by the Notifications page and the bell panel: day
 * headers, the All / Unread filter, and the polite live-region announcer.
 */
import { formatLocale, isOne, t } from '@/i18n/i18n'
import type { Notification } from '@/types'

/** The local calendar day of *d* (``YYYY-M-D``) — headers follow the
 *  viewer's clock, while the server stores UTC. */
function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** "Today", "Yesterday", or the date ("Mon 26 Oct"; the year only when
 *  it isn't this one) in the viewer's locale. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  if (dayKey(d) === dayKey(now)) return t('notifications.day.today')
  const y = new Date(now)
  y.setDate(now.getDate() - 1)
  if (dayKey(d) === dayKey(y)) return t('notifications.day.yesterday')
  const opts: Intl.DateTimeFormatOptions = { weekday: 'short', day: 'numeric', month: 'short' }
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric'
  return d.toLocaleDateString(formatLocale(), opts)
}

export interface DayGroup {
  key: string
  label: string
  rows: Notification[]
}

/** Consecutive rows of one local day under one header, in list order. */
export function groupByDay(rows: Notification[], now: Date = new Date()): DayGroup[] {
  const out: DayGroup[] = []
  for (const n of rows) {
    const d = new Date(n.created_at)
    const key = Number.isNaN(d.getTime()) ? 'unknown' : dayKey(d)
    const last = out[out.length - 1]
    if (last && last.key === key) last.rows.push(n)
    else out.push({ key, label: dayLabel(n.created_at, now), rows: [n] })
  }
  return out
}

export type NotifFilter = 'all' | 'unread'

/** One announcement per this many ms, at most. */
export const ANNOUNCE_INTERVAL_MS = 10_000

/** "2 new notifications" — a count, never what they say: a shared screen
 *  or a screen reader in company must not read out private content. */
export function liveText(n: number): string {
  return t(isOne(n) ? 'notifications.live_new_one' : 'notifications.live_new', { n: String(n) })
}

/**
 * A debounced announcer for a polite live region: the first arrival is
 * spoken at once, later ones within :data:`ANNOUNCE_INTERVAL_MS` are
 * counted and spoken together when the window ends. *say* receives the
 * text to put in the region.
 */
export function createAnnouncer(say: (text: string) => void, intervalMs = ANNOUNCE_INTERVAL_MS) {
  let pending = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  const tick = () => {
    timer = null
    if (pending === 0) return
    say(liveText(pending))
    pending = 0
    timer = setTimeout(tick, intervalMs)
  }
  return {
    arrived(n = 1) {
      pending += n
      if (timer === null) tick()
    },
    dispose() {
      if (timer !== null) clearTimeout(timer)
      timer = null
      pending = 0
    },
  }
}
