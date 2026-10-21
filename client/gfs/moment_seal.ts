/* Encrypted public-moment blobs — opened in the pinned viewer.
 *
 * Mirrors ``socialhome/moment_public_media.py``. A per-moment share link
 * carries the moment's ITEM KEY in its fragment (``#k=…``), never the
 * author's generation key. A blob is ``nonce(12) ‖ AES-256-GCM(ct ‖ tag)``
 * under ``HKDF-SHA256(item_key, salt = none, info "sh-moment-media:v1|" +
 * role)``, associated data ``"socialhome/moment-media/v1|" + suite + "|" +
 * author + "|" + moment + "|" + role``; the plaintext is
 * ``u32 length ‖ data ‖ zeros``. Its address is the SHA-256 of the blob —
 * checked here before anything is opened.
 */

export const MEDIA_SUITE = 'aesgcm-256-pad'

/** What a share copy names for the moment's file. */
export interface ShareMedia {
  blob_id: string
  media_suite: string
  content_type: string
  poster_blob_id?: string
}

/** The share copy: what the viewer shows. */
export interface ShareBody {
  content?: string
  created_at?: string
  expires_at?: string
  author_name?: string | null
  media_type?: string | null
  duration_ms?: number | null
  media?: ShareMedia | null
}

/** Content types the viewer shows (the household's allow-list). */
export const CONTENT_TYPES: Record<string, 'image' | 'video'> = {
  'image/webp': 'image',
  'image/jpeg': 'image',
  'image/png': 'image',
  'image/gif': 'image',
  'video/webm': 'video',
  'video/mp4': 'video',
}

/** A blob larger than the largest bucket + nonce + tag is not ours. */
export const MAX_BLOB_BYTES = 8 * 1024 * 1024 + 4 + 28

const AAD_PREFIX = 'socialhome/moment-media/v1|'
const INFO_PREFIX = 'sh-moment-media:v1|'

const enc = new TextEncoder()

export async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  return Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('')
}

async function mediaKey(itemKey: Uint8Array<ArrayBuffer>, role: string): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey('raw', itemKey, 'HKDF', false, ['deriveKey'])
  return crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: new Uint8Array(0),
      info: enc.encode(INFO_PREFIX + role),
    },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['decrypt'],
  )
}

/** The data inside a blob, after checking its address. Throws on any
 *  mismatch — a wrong address, key, author, moment or role. */
export async function openBlob(opts: {
  blob: Uint8Array<ArrayBuffer>
  blobId: string
  itemKey: Uint8Array<ArrayBuffer>
  author: string
  momentId: string
  role: 'media' | 'poster' | 'share'
}): Promise<Uint8Array<ArrayBuffer>> {
  const { blob, blobId, itemKey, author, momentId, role } = opts
  if (blob.length < 12 + 16 || blob.length > MAX_BLOB_BYTES) {
    throw new Error('blob size out of bounds')
  }
  if ((await sha256Hex(blob)) !== blobId) throw new Error('blob address mismatch')
  const key = await mediaKey(itemKey, role)
  const aad = enc.encode(`${AAD_PREFIX}${MEDIA_SUITE}|${author}|${momentId}|${role}`)
  const plain = new Uint8Array(
    await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: blob.slice(0, 12), additionalData: aad, tagLength: 128 },
      key,
      blob.slice(12),
    ),
  )
  const size = new DataView(plain.buffer).getUint32(0, false)
  if (size > plain.length - 4) throw new Error('length prefix out of bounds')
  return plain.slice(4, 4 + size)
}

/** The share copy a link opens. Throws when it isn't a JSON object or
 *  names media of a suite / type this page doesn't show. */
export async function openShare(opts: {
  blob: Uint8Array<ArrayBuffer>
  blobId: string
  itemKey: Uint8Array<ArrayBuffer>
  author: string
  momentId: string
}): Promise<ShareBody> {
  const data = await openBlob({ ...opts, role: 'share' })
  const body: unknown = JSON.parse(new TextDecoder().decode(data))
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('share copy is not an object')
  }
  const share = body as ShareBody
  const media = share.media
  if (media) {
    if (
      media.media_suite !== MEDIA_SUITE ||
      !(media.content_type in CONTENT_TYPES) ||
      !/^[0-9a-f]{64}$/.test(media.blob_id) ||
      (media.poster_blob_id !== undefined && !/^[0-9a-f]{64}$/.test(media.poster_blob_id))
    ) {
      share.media = null
    }
  }
  return share
}
