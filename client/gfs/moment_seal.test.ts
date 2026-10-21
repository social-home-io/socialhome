/* Interop: blobs sealed by ``socialhome/moment_public_media.py`` (golden
 * vector ``tests/fixtures/moment_share_vector.json``, which a pytest also
 * opens) open here — and nothing else does. */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { openBlob, openShare, sha256Hex } from './moment_seal'
import { b64urlDecode } from './stream_seal'

const V = JSON.parse(
  readFileSync(join(__dirname, '..', '..', 'tests', 'fixtures', 'moment_share_vector.json'), 'utf8'),
)

function b64(text: string): Uint8Array<ArrayBuffer> {
  const bin = atob(text)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

const KEY = b64urlDecode(V.item_key_b64url)
const SHARE = b64(V.share_b64)
const MEDIA = b64(V.media_b64)
const base = { itemKey: KEY, author: 'u-noor', momentId: 'm-golden' }

describe('moment_seal (pinned viewer)', () => {
  it('opens the share copy and its picture sealed in Python', async () => {
    expect(await sha256Hex(SHARE)).toBe(V.share_id)
    const body = await openShare({ ...base, blob: SHARE, blobId: V.share_id })
    expect(body).toEqual(V.body)
    const pic = await openBlob({ ...base, blob: MEDIA, blobId: V.media_id, role: 'media' })
    expect(Array.from(pic)).toEqual(Array.from(b64(V.media_plain_b64)))
  })

  it('refuses a blob that is not at its address (a GFS swapping bytes)', async () => {
    await expect(openShare({ ...base, blob: SHARE, blobId: V.media_id })).rejects.toThrow(
      /address/,
    )
  })

  it.each([
    ['another moment', { momentId: 'm-other' }],
    ['another author', { author: 'u-mallory' }],
    ['another key', { itemKey: new Uint8Array(32) }],
  ])('a share copy does not open for %s', async (_label, over) => {
    await expect(
      openShare({ ...base, ...over, blob: SHARE, blobId: V.share_id }),
    ).rejects.toThrow()
  })

  it('a media blob does not open as a share copy or a poster', async () => {
    await expect(openShare({ ...base, blob: MEDIA, blobId: V.media_id })).rejects.toThrow()
    await expect(
      openBlob({ ...base, blob: MEDIA, blobId: V.media_id, role: 'poster' }),
    ).rejects.toThrow()
  })

  it('refuses a tampered or truncated blob', async () => {
    const bad = SHARE.slice()
    bad[100] ^= 1
    await expect(
      openShare({ ...base, blob: bad, blobId: await sha256Hex(bad) }),
    ).rejects.toThrow()
    const short = SHARE.slice(0, 10)
    await expect(
      openShare({ ...base, blob: short, blobId: await sha256Hex(short) }),
    ).rejects.toThrow(/size/)
  })
})
