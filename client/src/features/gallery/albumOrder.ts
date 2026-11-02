/**
 * Inside an album, photos sort by the day they were taken (owner
 * decision D1) — newest first, or oldest first when the person flips the
 * toggle. The choice is remembered per album and per person in this
 * browser; storage that is blocked or full just means newest first.
 *
 * The server lists in exactly this order (``?sort=taken`` /
 * ``taken_asc``) and pages on the last item's ``day`` + ``created_at`` +
 * ``id``, so no photo is skipped or repeated between pages.
 */

export type AlbumOrder = 'newest' | 'oldest'

const KEY = 'sh.gallery.order'

function storageKey(userId: string, albumId: string): string {
  return `${KEY}.${userId}.${albumId}`
}

/** The order this person last chose for this album (newest first unless
 *  they chose otherwise). */
export function readAlbumOrder(userId: string | undefined, albumId: string): AlbumOrder {
  if (!userId) return 'newest'
  try {
    return window.localStorage.getItem(storageKey(userId, albumId)) === 'oldest'
      ? 'oldest'
      : 'newest'
  } catch {
    return 'newest'
  }
}

/** Remember the order (only "oldest first" is stored — newest is the
 *  default, so nothing piles up). */
export function writeAlbumOrder(
  userId: string | undefined, albumId: string, order: AlbumOrder,
): void {
  if (!userId) return
  try {
    if (order === 'oldest') window.localStorage.setItem(storageKey(userId, albumId), 'oldest')
    else window.localStorage.removeItem(storageKey(userId, albumId))
  } catch {
    // Private mode / blocked storage: the choice lasts for this visit.
  }
}

/** ``?sort=`` for the items endpoint. */
export function sortParam(order: AlbumOrder): 'taken' | 'taken_asc' {
  return order === 'oldest' ? 'taken_asc' : 'taken'
}

export interface DatedItem {
  id: string
  day?: string
  taken_at?: string | null
  created_at?: string
}

/** The day an item sorts by: taken, else uploaded (the server's
 *  ``day``; older payloads fall back to the same rule). */
export function itemDay(i: DatedItem): string {
  return i.day || i.taken_at || (i.created_at ?? '').slice(0, 10)
}

/** The cursor for the page after ``last`` in a date order. */
export function takenCursor(last: DatedItem): string {
  return `&after_day=${encodeURIComponent(itemDay(last))}`
    + `&after=${encodeURIComponent(last.created_at ?? '')}`
    + `&after_id=${encodeURIComponent(last.id)}`
}

export interface DayGroup<T> {
  /** ``YYYY-MM-DD`` (``''`` when unknown). */
  day: string
  items: T[]
}

export interface MonthGroup<T> {
  /** ``YYYY-MM`` (``''`` when unknown). */
  month: string
  days: DayGroup<T>[]
}

/** Items already in date order → month → day groups, keeping the order. */
export function groupByMonthDay<T extends DatedItem>(items: T[]): MonthGroup<T>[] {
  const months: MonthGroup<T>[] = []
  for (const item of items) {
    const day = itemDay(item)
    const month = day.slice(0, 7)
    let m = months[months.length - 1]
    if (!m || m.month !== month) {
      m = { month, days: [] }
      months.push(m)
    }
    let d = m.days[m.days.length - 1]
    if (!d || d.day !== day) {
      d = { day, items: [] }
      m.days.push(d)
    }
    d.items.push(item)
  }
  return months
}
