/* A per-moment share link's fragment, read on the pinned page:
 *
 *   #g=<GFS base URL>&a=<author>&m=<moment>&b=<share blob>&k=<item key>
 *
 * ``g`` is validated exactly like a highlight link's (``gfsBaseUrl``);
 * ``a`` / ``m`` keep to the URL-safe ids the household mints; ``b`` is a
 * 64-hex blob address; ``k`` the moment's 32-byte item key (base64url).
 * The fragment never reaches a server.
 */
import { gfsBaseUrl } from './pinned_link'
import { b64urlDecode } from './stream_seal'

export interface MomentBoot {
  gfs: string
  author: string
  momentId: string
  shareId: string
  itemKey: Uint8Array<ArrayBuffer>
}

const MAX_HASH_LENGTH = 2048
const SAFE_ID = /^[A-Za-z0-9._~:-]+$/

function boundedId(value: string | null, max: number): string | null {
  if (!value || value.length > max || !SAFE_ID.test(value)) return null
  return value
}

/** Whether the fragment is a moment link (it names a moment). */
export function isMomentHash(hash: string): boolean {
  return new URLSearchParams(hash.replace(/^#/, '')).has('m')
}

/** The moment link's context, or null when any part is missing, overlong
 *  or malformed. */
export function momentBootFromHash(hash: string): MomentBoot | null {
  if (hash.length > MAX_HASH_LENGTH) return null
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const gfs = gfsBaseUrl(params.get('g') ?? '')
  const author = boundedId(params.get('a'), 128)
  const momentId = boundedId(params.get('m'), 200)
  const shareId = params.get('b') ?? ''
  const k = params.get('k') ?? ''
  if (!gfs || !author || !momentId) return null
  if (!/^[0-9a-f]{64}$/.test(shareId)) return null
  if (!/^[A-Za-z0-9_-]{43}$/.test(k)) return null
  let itemKey: Uint8Array<ArrayBuffer>
  try {
    itemKey = b64urlDecode(k)
  } catch {
    return null
  }
  if (itemKey.length !== 32) return null
  return { gfs, author, momentId, shareId, itemKey }
}
