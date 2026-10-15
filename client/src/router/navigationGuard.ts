/**
 * navigationGuard — one page-level "you have unsaved changes" hook the
 * router consults before it leaves the current page.
 *
 * A page with an editable form registers a guard while it is mounted
 * (``setNavigationGuard``). ``IngressLocationProvider`` asks
 * ``shouldConfirmLeave(target)`` before every in-app route change (link
 * click, ``route()`` call, Back / Forward) and, when it answers true,
 * waits for ``confirmLeave()`` — the page's own dialog — before it moves.
 * A hard reload / tab close is covered separately by the page's
 * ``beforeunload`` listener (``useBeforeUnload``).
 *
 * Only one guard is live at a time: the router renders one page.
 */
import { useEffect } from 'preact/hooks'

export interface NavigationGuard {
  /** True while the page holds changes that leaving would lose. */
  isDirty: () => boolean
  /** Ask the user; resolve ``true`` to leave (the page discards). */
  confirm: () => Promise<boolean>
  /** Stripped path the guard belongs to. A route change that stays on
   *  this path (e.g. ``/settings`` → ``/settings#privacy``) is in-page
   *  and never asks. */
  path: string
}

let current: NavigationGuard | null = null

/** Register *guard*; the returned function unregisters it (only if it
 *  is still the live one). */
export function setNavigationGuard(guard: NavigationGuard): () => void {
  current = guard
  return () => {
    if (current === guard) current = null
  }
}

function pathOf(url: string): string {
  const path = url.split(/[?#]/, 1)[0] ?? ''
  return path.replace(/\/+$/g, '') || '/'
}

/** Whether moving to *target* (stripped URL) needs the user's OK. */
export function shouldConfirmLeave(target: string): boolean {
  if (!current || !current.isDirty()) return false
  return pathOf(target) !== pathOf(current.path)
}

/** Ask the live guard. ``true`` when there is nothing to ask. */
export function confirmLeave(): Promise<boolean> {
  return current ? current.confirm() : Promise.resolve(true)
}

/** Warn on reload / tab close while *dirty* is true. Browsers show
 *  their own fixed copy; ``returnValue`` must be set for Chrome. */
export function useBeforeUnload(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])
}
