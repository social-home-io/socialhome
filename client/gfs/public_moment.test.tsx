import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/preact'
import type { MomentBoot } from './moment_link'
import { MomentVideo, PublicMomentViewer } from './public_moment'
import { b64urlDecode } from './stream_seal'
import { locale, setLocale } from './viewer_i18n'

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

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

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

  it('writes the date in the page’s language, not the browser’s', async () => {
    const before = locale()
    // The browser says en-US; the page was picked for a German visitor.
    setLocale('de')
    try {
      const { container } = render(<PublicMomentViewer boot={BOOT} />)
      await waitFor(() => expect(container.textContent).toContain('Bees swarmed the plum tree'))
      const german = new Intl.DateTimeFormat('de', { dateStyle: 'medium', timeStyle: 'short' })
        .format(new Date(V.body.created_at))
      expect(container.textContent).toContain(german)
    } finally {
      setLocale(before)
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

  it('says a moment the GFS answers 410 for is no longer available', async () => {
    const real = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) =>
      url.endsWith(V.share_id) ? new Response('', { status: 410 }) : real(url, init)))
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('no longer available'))
    expect(container.querySelector('button')).toBeNull()
  })

  it.each([
    ['a server error', () => new Response('', { status: 502 })],
    ['a rate limit', () => new Response('', { status: 429 })],
    ['no network', () => { throw new TypeError('Failed to fetch') }],
  ])('on %s says it can’t reach the GFS — not that the link is broken — and retries', async (_n, fail) => {
    const real = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>
    let down = true
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) =>
      down ? fail() : real(url, init)))
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('Can’t reach gfs.example.org'))
    expect(container.textContent).toContain('The link itself looks fine.')
    expect(container.textContent).not.toMatch(/incomplete|no longer available/)
    const button = screen.getByRole('button', { name: 'Try again' })
    await waitFor(() => expect(document.activeElement).toBe(button))
    down = false
    fireEvent.click(button)
    await waitFor(() => expect(container.textContent).toContain('Bees swarmed the plum tree'))
  })

  it('a link naming a blob the GFS refuses outright is incomplete, not unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 400 })))
    const { container } = render(<PublicMomentViewer boot={BOOT} />)
    await waitFor(() => expect(container.textContent).toContain('incomplete or damaged'))
    expect(container.querySelector('button')).toBeNull()
  })
})

describe('MomentVideo', () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  })

  it('starts muted so autoplay works, with Pause and Unmute buttons instead of native controls', () => {
    const { container } = render(<MomentVideo src="blob:v" poster={null} />)
    const video = container.querySelector('video')!
    expect(video.hasAttribute('autoplay')).toBe(true)
    expect(video.hasAttribute('playsinline')).toBe(true)
    expect(video.hasAttribute('muted')).toBe(true)
    expect(video.muted).toBe(true)
    expect(video.hasAttribute('controls')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Unmute' }))
    expect(video.muted).toBe(false)
    // One video: nothing to step through.
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull()
  })

  it('follows the video: Play once it pauses or ends, and the keyboard toggles it', () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause')
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play')
    const { container } = render(<MomentVideo src="blob:v" poster={null} />)
    const video = container.querySelector('video')!
    fireEvent(video, new Event('play'))
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(pause).toHaveBeenCalled()
    fireEvent(video, new Event('pause'))
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    play.mockClear()
    fireEvent.keyDown(document.body, { key: 'k' })
    expect(play).toHaveBeenCalled()
    fireEvent(video, new Event('play'))
    fireEvent(video, new Event('ended'))
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    fireEvent.keyDown(document.body, { key: 'm' })
    expect(video.muted).toBe(false)
  })
})
