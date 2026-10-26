/**
 * momentumSeen — when this person last had Momentum open, so the Home
 * "today" card can say "Momentum · 3 new" and mean new to *them*.
 *
 * A per-viewer convenience in ``localStorage`` (keyed by user id), not
 * server state: moments carry no seen-state, and a fresh browser simply
 * counts the last 24 hours. Recorded from the router (``noteRoute`` on
 * every route change): opening Momentum marks it seen, and so does
 * leaving it — moments that arrived while the page was open were seen.
 * Every storage access is guarded; a private window just never remembers.
 */
const KEY = 'sh.momentum.seen_at:'

let lastPath: string | null = null

function isMomentum(url: string): boolean {
  const path = url.split(/[?#]/, 1)[0]
  return path === '/momentum' || path.startsWith('/momentum/')
}

export function momentumSeenAt(userId: string | null | undefined): number | null {
  if (!userId) return null
  try {
    const raw = window.localStorage.getItem(KEY + userId)
    const at = raw === null ? NaN : Number(raw)
    return Number.isFinite(at) ? at : null
  } catch {
    return null
  }
}

export function markMomentumSeen(userId: string | null | undefined, at: number = Date.now()): void {
  if (!userId) return
  try {
    window.localStorage.setItem(KEY + userId, String(at))
  } catch {
    // Storage blocked (private window, quota): nothing to remember.
  }
}

/** The router's ``onRouteChange``: mark Momentum seen when it opens or
 *  closes. */
export function noteRoute(url: string, userId: string | null | undefined): void {
  if (isMomentum(url) || (lastPath !== null && isMomentum(lastPath))) {
    markMomentumSeen(userId)
  }
  lastPath = url
}

/** Tests only: forget the previous route. */
export function _resetRouteForTests(): void {
  lastPath = null
}
