/* Boot context for the pinned public-highlight viewer (§highlights_public).
 *
 * The pinned viewer is a static, content-addressed page (``v/<hash>/``)
 * that no GFS serves, so a GFS can't rewrite the script that reads the
 * link secret. With no server-rendered ``<script id="boot">`` block, it
 * reads everything from the link's fragment, which browsers never send:
 *
 *   #g=<GFS base URL>&i=<instance>&h=<highlight>&t=<token>&k=<secret>
 *
 * ``g`` decides where every request goes, so it is refused unless it is a
 * plain ``https://`` URL (``http://`` only for localhost / 127.0.0.1, the
 * dev and demo setup) with no credentials, query or fragment. Every field
 * is length-bounded; the ids and the token keep to the URL-safe characters
 * the household and the GFS mint. ``k`` is checked by ``linkSecretFromHash``.
 */

export interface PinnedBoot {
  /** The GFS base URL, no trailing slash. */
  gfs: string
  instanceId: string
  highlightId: string
  token: string
}

/** Upper bounds per fragment field (characters). The household mints a
 *  43-char token and 32-char ids; the GFS refuses tokens over 256. */
export const MAX_FIELD_LENGTH = { g: 512, i: 128, h: 128, t: 256 } as const

/** A whole fragment longer than this is not a link we made. */
const MAX_HASH_LENGTH = 2048

const SAFE_ID = /^[A-Za-z0-9._~-]+$/

const DEV_HOSTS = new Set(['localhost', '127.0.0.1'])

/** A host both sides read alike: letters, digits, dots, dashes, a port. */
const PLAIN_HOST = /^[A-Za-z0-9.-]+(:[0-9]{1,5})?$/

/** The GFS base URL from ``g``, normalised (no trailing slash), or null. */
export function gfsBaseUrl(raw: string): string | null {
  if (!raw || raw.length > MAX_FIELD_LENGTH.g) return null
  // Absolute only: ``new URL`` without a base refuses ``//host`` and ``/path``.
  if (!/^https?:\/\//i.test(raw)) return null
  // A backslash or whitespace is read differently by URL parsers (the
  // browser takes "\" for "/"): refuse it rather than guess.
  if (/[\\\s]/.test(raw)) return null
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  if (url.protocol === 'http:') {
    if (!DEV_HOSTS.has(url.hostname)) return null
  } else if (url.protocol !== 'https:') {
    return null
  }
  if (url.username || url.password) return null
  if (!PLAIN_HOST.test(url.host)) return null
  if (url.search || url.hash || raw.includes('#') || raw.includes('?')) return null
  return (url.origin + url.pathname).replace(/\/+$/, '')
}

function boundedId(value: string | null, max: number): string | null {
  if (!value || value.length > max || !SAFE_ID.test(value)) return null
  return value
}

/** The pinned viewer's boot context from ``location.hash``, or null when
 *  any field is missing, overlong or malformed. */
export function pinnedBootFromHash(hash: string): PinnedBoot | null {
  if (hash.length > MAX_HASH_LENGTH) return null
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const gfs = gfsBaseUrl(params.get('g') ?? '')
  const instanceId = boundedId(params.get('i'), MAX_FIELD_LENGTH.i)
  const highlightId = boundedId(params.get('h'), MAX_FIELD_LENGTH.h)
  const token = boundedId(params.get('t'), MAX_FIELD_LENGTH.t)
  if (!gfs || !instanceId || !highlightId || !token) return null
  return { gfs, instanceId, highlightId, token }
}

/** The absolute URL of a GFS route (``path`` starts with ``/``). */
export function pinnedEndpoint(gfs: string, path: string): string {
  return gfs + path
}

/** Request options for every call the pinned page makes to the GFS:
 *  cross-origin, no cookies or auth, no ``Referer`` (the page URL carries
 *  nothing secret outside its fragment, but there is no reason to send it). */
export function pinnedRequestInit(init: RequestInit = {}): RequestInit {
  return {
    ...init,
    mode: 'cors',
    credentials: 'omit',
    referrerPolicy: 'no-referrer',
    // A GFS has no reason to redirect; following one would hand the token,
    // ``vk`` and SDP to whatever host it names.
    redirect: 'error',
  }
}
