/**
 * Interop with ``socialhome/services/highlight_public_seal.py``: a stream
 * the author's household sealed (golden vector made by ``StreamSealer``
 * with a fixed viewer key and link secret) opens in the browser — and
 * nothing else does.
 */
import { describe, expect, it } from 'vitest'
import { importViewerPrivateKey } from './sealed_profile'
import { b64urlDecode, linkSecretFromHash, makeStreamOpener } from './stream_seal'

const PKCS8 =
  'MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgAfLj1MW2p5iAEjRWeJq83vASNFZ4mrze8BI0VniavN6hRANCAATl+yFTYtWIFIxUaNIG3kCcRISvjwFavRp6Dq/OKcrF76I74YcTYxYnuX0nqlAkVa1ezQF2yxtvhuzBgXrMR9sE'
const SECRET = '0htX0WWeIJnofu_HTifPGX_9pz5oTvHMWAG9Ys-9JyQ'
const SEAL = {
  seal_suite: 'p256+link-hkdf-sha256-aes256gcm',
  epk: 'BOgtWCjARBpVAKk1dzrqXrtYxunHZoSrvvooQCnECMp0d4QBJW7baBZ6NsczZ2fTaqoVKqYlswgsYU1BqFH5xZ8',
}
const b64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0))
const CT = [
  b64('ZtSPKL+KUHJb7UMsg+9SrWSbROMCya6fFvVnIkQN+2W9nyGqHhnybBP5JoQ+xWrErU77c/mr7VnKCMzw+n5iueSA9x1vwDaCQf65ybJloY9U9dy4B88U09KQXcU='),
  b64('h4p0Pk5NdO/Fud9h7rje1STIeKqnM/fQrU5Dkq73dr34kkWgPvUhsR7xPa30iLbVcPVyyvWuX97uhax5JOftwwUWj+cTxMMWtW3RC9gGoKTJr04GEBOT2shS/3yQPJa6PJDiZb7AoLp3J2TdG+0zJHrA2Yxqp0rS3JooSjRl20Jf/Yt9aw=='),
]

function headerOf(frame: Uint8Array): { kind: string } {
  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength)
  const len = view.getUint32(0)
  return JSON.parse(new TextDecoder().decode(frame.subarray(4, 4 + len)))
}

describe('sealed highlight stream', () => {
  it('opens what the author household sealed, in order', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    const opener = await makeStreamOpener(sk, SEAL, b64urlDecode(SECRET), 'hl-golden')
    expect(opener).not.toBeNull()
    expect(headerOf(await opener!.open(CT[0]!)).kind).toBe('highlight_meta')
    const chunk = await opener!.open(CT[1]!)
    expect(headerOf(chunk).kind).toBe('frame_chunk')
    expect(new TextDecoder().decode(chunk.subarray(chunk.length - 5))).toBe('hello')
  })

  it('does not open without the link secret (what the GFS lacks)', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    const opener = await makeStreamOpener(sk, SEAL, new Uint8Array(32), 'hl-golden')
    await expect(opener!.open(CT[0]!)).rejects.toBeTruthy()
  })

  it('refuses frames out of order', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    const opener = await makeStreamOpener(sk, SEAL, b64urlDecode(SECRET), 'hl-golden')
    await expect(opener!.open(CT[1]!)).rejects.toBeTruthy()
  })

  it('is bound to its highlight', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    const opener = await makeStreamOpener(sk, SEAL, b64urlDecode(SECRET), 'another')
    await expect(opener!.open(CT[0]!)).rejects.toBeTruthy()
  })

  it('refuses an unknown suite rather than guessing', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    expect(
      await makeStreamOpener(sk, { ...SEAL, seal_suite: 'none' }, b64urlDecode(SECRET), 'hl-golden'),
    ).toBeNull()
  })

  it('reads the secret from the fragment only when it is whole', () => {
    expect(linkSecretFromHash(`#k=${SECRET}`)).toHaveLength(32)
    expect(linkSecretFromHash('')).toBeNull()
    expect(linkSecretFromHash(`#k=${SECRET.slice(0, 20)}`)).toBeNull()
    expect(linkSecretFromHash('#k=not a secret')).toBeNull()
  })
})
