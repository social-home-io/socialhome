/**
 * appReturn — whether an app was opened from another page of the SPA,
 * so the app host's Back leads back there (Needs you, a notification,
 * the Apps page) with one ``history.back()``, instead of pushing
 * ``/apps`` on top — which left the app one browser-Back away and
 * trapped the user between the two.
 *
 * The router reports every route change (``noteAppRoute``). A deep link
 * (the app is the first page) has nothing to go back to; Back then
 * replaces the entry with ``/apps``.
 */

let current: string | null = null
let openedFrom: string | null = null

/** ``/apps/<id>`` — the app host, not the Apps page. */
export function isAppHostPath(url: string): boolean {
  const path = url.split(/[?#]/, 1)[0]
  return /^\/apps\/[^/]+\/?$/.test(path)
}

/** The router's ``onRouteChange``. */
export function noteAppRoute(url: string): void {
  if (url === current) return
  if (isAppHostPath(url)) openedFrom = current
  current = url
}

/** The SPA page the open app was reached from, or ``null`` for a deep
 *  link (or when no app is open). */
export function appOpenedFrom(): string | null {
  return current !== null && isAppHostPath(current) ? openedFrom : null
}

/** Tests only. */
export function _resetAppReturnForTests(): void {
  current = null
  openedFrom = null
}
