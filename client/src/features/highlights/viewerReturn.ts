/**
 * viewerReturn — where the highlight viewer was opened from (C8), so
 * Close / Esc / the end of the last person lead back there: a share card
 * in a feed, Home, Highlights, a notification.
 *
 * The router reports every route change (``noteViewerRoute``). Moving
 * from one person to the next inside the viewer replaces the history
 * entry, so the page before the viewer is one ``history.back()`` away.
 * A deep link (nothing before the viewer) has no such page; the viewer
 * closes to Highlights instead.
 */

let current: string | null = null
let openedFrom: string | null = null

/** ``/highlights/<id>`` — the viewer, not ``/highlights/new`` / ``archive``. */
export function isViewerPath(url: string): boolean {
  const path = url.split(/[?#]/, 1)[0]
  const m = /^\/highlights\/([^/]+)\/?$/.exec(path)
  return !!m && m[1] !== 'new' && m[1] !== 'archive'
}

/** The router's ``onRouteChange``. */
export function noteViewerRoute(url: string): void {
  if (url === current) return
  if (isViewerPath(url) && (current === null || !isViewerPath(current))) {
    openedFrom = current
  }
  current = url
}

/** The page the viewer was opened from, or ``null`` for a deep link. */
export function viewerOpenedFrom(): string | null {
  return current !== null && isViewerPath(current) ? openedFrom : null
}

/** Tests only. */
export function _resetViewerReturnForTests(): void {
  current = null
  openedFrom = null
}
