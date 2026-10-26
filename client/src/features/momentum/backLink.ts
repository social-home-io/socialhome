/**
 * backLink — where the moment page's "‹ Moments" leads: the Momentum list
 * the person came from, with its tab and tag (``/momentum/yours``,
 * ``/momentum?tag=garten``, ``/momentum/yours?show=saved``).
 *
 * Kept in memory for the session only: a reload or a link from elsewhere
 * (a notification, a share) goes back to ``/momentum``.
 */
let lastList: string | null = null

/** The Momentum list calls this with its current URL. */
export function noteMomentumList(url: string): void {
  lastList = url === '/momentum' || url.startsWith('/momentum?') || url.startsWith('/momentum/yours')
    ? url
    : null
}

/** The list to go back to. */
export function momentumBackTarget(): string {
  return lastList ?? '/momentum'
}

/** The back target is the Yours tab (its link is named for it). */
export function backIsYours(target: string): boolean {
  return target.split('?', 1)[0].replace(/\/+$/, '') === '/momentum/yours'
}

/** Tests only. */
export function _resetMomentumListForTests(): void {
  lastList = null
}
