/**
 * viewerQueue — who comes before and after the person you are watching
 * (C8).
 *
 * The order is the Today list's (:func:`todayView`): New first, then
 * Seen, newest first in each. **Up next** is the next person with frames
 * you haven't watched — after the current one, else wrapping to an
 * earlier one — skipping people you watched in this session (their
 * views may still be on their way to the server). At the end of a
 * person the viewer moves to Up next; with nobody left it closes.
 * ``prev`` / ``next`` are the desktop ‹ › person buttons; ``next``
 * prefers Up next, then simply the next row.
 */
import type { HighlightInboxItem } from '@/types'
import { todayView, type TodayPerson } from './today'

export interface ViewerNeighbours {
  prev: TodayPerson | null
  next: TodayPerson | null
  upNext: TodayPerson | null
}

export function neighbours(
  items: readonly HighlightInboxItem[],
  me: string | null | undefined,
  currentAuthorId: string,
  watched: ReadonlySet<string>,
  now: number = Date.now(),
): ViewerNeighbours {
  const view = todayView(items, me, now)
  const queue = [...view.fresh, ...view.seen]
  const at = queue.findIndex(p => p.authorId === currentAuthorId)
  const isUnseen = (p: TodayPerson) => (
    p.unseen > 0 && p.authorId !== currentAuthorId && !watched.has(p.authorId)
  )
  const after = at >= 0 ? queue.slice(at + 1) : queue
  const before = at >= 0 ? queue.slice(0, at) : []
  const upNext = after.find(isUnseen) ?? before.find(isUnseen) ?? null
  return {
    prev: at > 0 ? queue[at - 1] : null,
    next: upNext ?? (at >= 0 ? queue[at + 1] ?? null : null),
    upNext,
  }
}
