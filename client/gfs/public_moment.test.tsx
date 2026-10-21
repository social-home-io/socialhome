import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import type { MomentBoot } from './moment_link'
import { PublicMomentViewer } from './public_moment'
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

const BOOT: MomentBoot = {
  gfs: 'https://gfs.example.org',
  author: 'u-noor',
  momentId: 'm-golden',
  shareId: V.share_id,
  itemKey: b64urlDecode(V.item_key_b64url),
}

let blobs: Record<string, Uint8Array<ArrayBuffer>>
let calls: { url: string; init: RequestInit | undefined }[]

beforeEach(() => {
  blobs = { [V.share_id]: b64(V.share_b64), [V.media_id]: b64(V.media_b64) }
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    const id = url.split('/').pop() ?? ''
    const blob = blobs[id]
    return blob
      ? new Response(blob, { status: 200 })
      : new Response('', { status: 404 })
  }))
  URL.createObjectURL = vi.fn(() => 'blob:moment')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => vi.unstubAllGlobals())

describe('PublicMomentViewer', () => {
  it('opens the moment and its picture from ciphertext only', async () => {
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('Bees swarmed the plum tree'))
    expect(container.textContent).toContain('Noor Haddad')
    expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:moment')
    // Only the GFS the link names, cross-origin, without credentials or a
    // referrer — and never the key.
    expect(calls.map((c) => c.url)).toEqual([
      `https://gfs.example.org/gfs/moments/media/${V.share_id}`,
      `https://gfs.example.org/gfs/moments/media/${V.media_id}`,
    ])
    for (const c of calls) {
      expect(c.init?.credentials).toBe('omit')
      expect(c.init?.referrerPolicy).toBe('no-referrer')
      expect(c.url).not.toContain(V.item_key_b64url)
    }
  })

  it('says a moment that is gone is no longer available', async () => {
    delete blobs[V.share_id]
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('no longer available'))
    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('shows nothing of a copy the GFS swapped', async () => {
    blobs[V.share_id] = b64(V.media_b64)
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('incomplete or damaged'))
    expect(container.textContent).not.toContain('Bees')
  })

  it('a link with another key opens nothing', async () => {
    const { container } = render(
      <PublicMomentViewer boot={{ ...BOOT, itemKey: new Uint8Array(32) }} />,
    )
    await waitFor(() => expect(container.textContent).toContain('incomplete or damaged'))
  })
})
