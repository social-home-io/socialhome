/* Who shared a public highlight — opened in the viewer's browser.
 *
 * Mirrors ``socialhome/services/highlight_public_profile.py``: the viewer
 * makes an ephemeral P-256 ECDH key pair for this one view and sends the
 * raw public key as ``vk``; the author's household seals the profile to it
 * (ECDH → HKDF-SHA256, info ``sh-highlight-public-profile:v1`` → AES-256-GCM,
 * the highlight id as associated data) in one ``author_profile`` frame.
 * The GFS relays only ciphertext.
 *
 * Without WebCrypto (an insecure context, an old browser) no key is sent
 * and the page shows the highlight without the author — never an error.
 */

export const PROFILE_SEAL_SUITE_P256 = 'p256-hkdf-sha256-aes256gcm'
const SUPPORTED_SUITES = new Set([PROFILE_SEAL_SUITE_P256])
const INFO = new TextEncoder().encode('sh-highlight-public-profile:v1')

export interface AuthorProfile {
  display_name: string
  bio: string | null
  /** Base64 WebP, or null. */
  picture: string | null
}

export interface SealedHeader {
  seal_suite?: string
  epk?: string
  nonce?: string
}

export interface ViewerKey {
  /** Base64url raw uncompressed point — sent as ``vk``. */
  vk: string
  privateKey: CryptoKey
}

function b64urlEncode(bytes: Uint8Array): string {
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function b64urlDecode(text: string): Uint8Array<ArrayBuffer> {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/')
  const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function subtle(): SubtleCrypto | null {
  return globalThis.crypto?.subtle ?? null
}

/** A fresh viewer key, or null when this browser can't do it. */
export async function makeViewerKey(): Promise<ViewerKey | null> {
  const s = subtle()
  if (!s) return null
  try {
    const pair = await s.generateKey(
      { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'],
    ) as CryptoKeyPair
    const raw = new Uint8Array(await s.exportKey('raw', pair.publicKey))
    return { vk: b64urlEncode(raw), privateKey: pair.privateKey }
  } catch {
    return null
  }
}

/** Open a sealed ``author_profile`` frame. Null for anything that doesn't
 *  open or doesn't look like a profile; an unknown suite is refused. */
export async function openProfile(
  privateKey: CryptoKey,
  header: SealedHeader,
  ciphertext: Uint8Array,
  highlightId: string,
): Promise<AuthorProfile | null> {
  const s = subtle()
  if (!s || !header.seal_suite || !SUPPORTED_SUITES.has(header.seal_suite)) return null
  if (!header.epk || !header.nonce) return null
  try {
    const epk = await s.importKey(
      'raw', b64urlDecode(header.epk), { name: 'ECDH', namedCurve: 'P-256' }, false, [],
    )
    const shared = await s.deriveBits({ name: 'ECDH', public: epk }, privateKey, 256)
    const hkdf = await s.importKey('raw', shared, 'HKDF', false, ['deriveKey'])
    const key = await s.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: INFO },
      hkdf,
      { name: 'AES-GCM', length: 256 },
      false,
      ['decrypt'],
    )
    const plain = await s.decrypt(
      {
        name: 'AES-GCM',
        iv: b64urlDecode(header.nonce),
        additionalData: new TextEncoder().encode(highlightId),
      },
      key,
      ciphertext.slice(),
    )
    const body = JSON.parse(new TextDecoder().decode(plain)) as Partial<AuthorProfile>
    if (typeof body.display_name !== 'string' || !body.display_name.trim()) return null
    return {
      display_name: body.display_name.trim().slice(0, 64),
      bio: typeof body.bio === 'string' && body.bio.trim() ? body.bio.trim().slice(0, 300) : null,
      picture: typeof body.picture === 'string' && body.picture ? body.picture : null,
    }
  } catch {
    return null
  }
}

/** A blob URL for the profile's WebP picture, or null. */
export function pictureUrl(profile: AuthorProfile | null): string | null {
  if (!profile?.picture) return null
  try {
    const bin = atob(profile.picture)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    if (bytes[0] !== 0x52 || bytes[8] !== 0x57) return null // RIFF … WEBP
    return URL.createObjectURL(new Blob([bytes], { type: 'image/webp' }))
  } catch {
    return null
  }
}

/** For tests: a base64 PKCS#8 viewer private key. */
export async function importViewerPrivateKey(pkcs8Base64: string): Promise<CryptoKey> {
  const s = subtle()!
  const bin = atob(pkcs8Base64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return s.importKey('pkcs8', bytes, { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'])
}
