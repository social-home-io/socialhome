/* Boot context for the viewer's public pages: a GFS's space page, invite
 * landing and Momentum profile ("one viewer for the outside world").
 *
 * The GFS answers ``/spaces/{id}``, ``/join/{token}`` and ``/moments/{user}``
 * with a 302 here (``socialhome/global_server/public_viewer.py``), all of it
 * in the fragment, which no browser sends:
 *
 *   #v=space&g=<GFS base URL>&s=<space id>
 *   #v=join&g=<GFS base URL>&t=<invite token>
 *   #v=person&g=<GFS base URL>&u=<user id>
 *
 * ``g`` decides where every request goes, so it passes ``gfsBaseUrl`` (the
 * same rule as a highlight link). Every id is bounded and URL-safe; any
 * other field, a repeated field or an overlong fragment is refused before
 * anything is fetched.
 */
import { gfsBaseUrl } from './pinned_link'

export type PageBoot =
  | { view: 'space'; gfs: string; spaceId: string }
  | { view: 'join'; gfs: string; token: string }
  | { view: 'person'; gfs: string; userId: string }

const VIEWS = { space: 's', join: 't', person: 'u' } as const
type View = keyof typeof VIEWS

/** Upper bounds per id (``public_viewer.py`` mirrors them). */
export const MAX_ID_LENGTH = { s: 128, t: 256, u: 128 } as const

/** A whole fragment longer than this is not a link the GFS made. */
const MAX_HASH_LENGTH = 1024

// URL-safe, and never only dots (``..`` would climb the request path).
const SAFE_ID = /^(?!\.+$)[A-Za-z0-9._~-]+$/

/** Whether ``hash`` names one of these pages (``v=``), right or wrong. */
export function isPageHash(hash: string): boolean {
  return new URLSearchParams(hash.replace(/^#/, '')).has('v')
}

/** The page's context from ``location.hash``, or null when anything about
 *  it is off. */
export function pageBootFromHash(hash: string): PageBoot | null {
  if (hash.length > MAX_HASH_LENGTH) return null
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const view = params.get('v') ?? ''
  if (!Object.hasOwn(VIEWS, view)) return null
  const key = VIEWS[view as View]
  // Exactly v, g and the view's id — each once.
  const keys = [...params.keys()]
  if (keys.length !== 3 || new Set(keys).size !== 3) return null
  if (!params.has('g') || !params.has(key)) return null
  const gfs = gfsBaseUrl(params.get('g') ?? '')
  const id = params.get(key) ?? ''
  if (!gfs || !id || id.length > MAX_ID_LENGTH[key] || !SAFE_ID.test(id)) return null
  switch (view as View) {
    case 'space':
      return { view: 'space', gfs, spaceId: id }
    case 'join':
      return { view: 'join', gfs, token: id }
    case 'person':
      return { view: 'person', gfs, userId: id }
  }
}
