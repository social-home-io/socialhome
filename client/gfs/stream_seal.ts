/* End-to-end sealed public highlight stream — opened in the viewer.
 *
 * Mirrors ``socialhome/services/highlight_public_seal.py``. The share link
 * carries a per-link secret in its fragment (``#k=…``), which browsers
 * never send to a server. The author's household answers this view's
 * ephemeral key (``vk``) with one plaintext ``stream_seal`` frame
 * (``seal_suite`` + its ephemeral ``epk``); every following frame is
 * ``sealed``: AES-256-GCM under HKDF-SHA256(ikm = ECDH(epk, vk),
 * salt = link secret, info ``sh-highlight-public-stream:v1``), a 96-bit
 * big-endian frame counter as the nonce, the highlight id as associated
 * data. Frames must be opened in arrival order.
 */

export const STREAM_SEAL_SUITE_P256_LINK = 'p256+link-hkdf-sha256-aes256gcm'
const SUPPORTED_SUITES = new Set([STREAM_SEAL_SUITE_P256_LINK])
const INFO = new TextEncoder().encode('sh-highlight-public-stream:v1')

/** The link secret from a location hash (``#k=…``), or null. */
export function linkSecretFromHash(hash: string): Uint8Array<ArrayBuffer> | null {
  const params = new URLSearchParams(hash.replace(/^#/, ''))
  const k = params.get('k')
  if (!k || !/^[A-Za-z0-9_-]{43}$/.test(k)) return null
  try {
    const out = b64urlDecode(k)
    return out.length === 32 ? out : null
  } catch {
    return null
  }
}

export function b64urlDecode(text: string): Uint8Array<ArrayBuffer> {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/')
  const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function nonce(counter: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(12)
  const view = new DataView(out.buffer)
  view.setUint32(4, Math.floor(counter / 2 ** 32))
  view.setUint32(8, counter >>> 0)
  return out
}

export interface StreamOpener {
  /** Open the next ``sealed`` frame's payload, in order. Throws on a
   *  tampered, reordered or foreign frame. */
  open(ciphertext: Uint8Array): Promise<Uint8Array>
}

/** The opener for this view, or null for an unknown suite / bad header. */
export async function makeStreamOpener(
  privateKey: CryptoKey,
  header: { seal_suite?: string; epk?: string },
  secret: Uint8Array<ArrayBuffer>,
  highlightId: string,
): Promise<StreamOpener | null> {
  const s = globalThis.crypto?.subtle
  if (!s || !header.seal_suite || !SUPPORTED_SUITES.has(header.seal_suite) || !header.epk) {
    return null
  }
  try {
    const epk = await s.importKey(
      'raw', b64urlDecode(header.epk), { name: 'ECDH', namedCurve: 'P-256' }, false, [],
    )
    const shared = await s.deriveBits({ name: 'ECDH', public: epk }, privateKey, 256)
    const hkdf = await s.importKey('raw', shared, 'HKDF', false, ['deriveKey'])
    const key = await s.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: secret, info: INFO },
      hkdf,
      { name: 'AES-GCM', length: 256 },
      false,
      ['decrypt'],
    )
    const aad = new TextEncoder().encode(highlightId)
    let counter = 0
    return {
      async open(ciphertext: Uint8Array): Promise<Uint8Array> {
        const iv = nonce(counter)
        counter += 1
        const plain = await s.decrypt(
          { name: 'AES-GCM', iv, additionalData: aad }, key, ciphertext.slice(),
        )
        return new Uint8Array(plain)
      },
    }
  } catch {
    return null
  }
}
