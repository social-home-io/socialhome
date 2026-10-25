/**
 * loadStatus — what a list that refetches on WS frames knows about its
 * last loads, so a failure is never shown as "nothing here".
 *
 * - The first load fails (nothing loaded yet) → ``failed``: render
 *   ``LoadErrorState`` with a Try again.
 * - A later refresh fails → keep the list, ``staleSince`` = when it last
 *   loaded: render ``StaleNotice``.
 * - Any success clears both.
 *
 * Pure: callers keep the value in a signal.
 */
import { localClockTime } from './relativeTime'

export interface LoadStatus {
  /** The load failed and nothing is loaded to show instead. */
  failed: boolean
  /** ``Date.now()`` of the last successful load, ``null`` before one. */
  loadedAt: number | null
  /** A refresh failed: the list on screen is from this time. */
  staleSince: number | null
}

export const INITIAL_LOAD: LoadStatus = { failed: false, loadedAt: null, staleSince: null }

export function loadSucceeded(_prev: LoadStatus, now: number = Date.now()): LoadStatus {
  return { failed: false, loadedAt: now, staleSince: null }
}

/** "13:02" — the local clock time of a ``loadedAt`` / ``staleSince``. */
export function clockTime(at: number): string {
  return localClockTime(new Date(at).toISOString())
}

export function loadFailed(prev: LoadStatus): LoadStatus {
  if (prev.loadedAt === null) return { ...prev, failed: true, staleSince: null }
  return { ...prev, failed: false, staleSince: prev.loadedAt }
}
