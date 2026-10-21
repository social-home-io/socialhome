/**
 * The public highlight page needs the link secret from the URL fragment.
 * Without it (a link from before highlights were end-to-end encrypted, or
 * one cut short when pasted) the page says so and contacts nobody — not
 * even the GFS — since nothing could open the stream anyway.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => {
  document.body.innerHTML =
    '<div id="root"></div><script id="boot" type="application/json">' +
    '{"instanceId":"inst-1","highlightId":"hl-1","token":"tok-1"}</script>'
  vi.resetModules()
})

afterEach(() => {
  window.location.hash = ''
  vi.restoreAllMocks()
})

describe('public highlight bootstrap', () => {
  it('a link without its secret shows how to get a new one and fetches nothing', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    window.location.hash = ''
    await import('./public_highlight')
    const root = document.getElementById('root')!
    expect(root.querySelector('[role="alert"]')?.textContent).toMatch(
      /Ask whoever shared it for a new link/,
    )
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('a link with its secret starts connecting', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'))
    window.location.hash = '#k=0htX0WWeIJnofu_HTifPGX_9pz5oTvHMWAG9Ys-9JyQ'
    await import('./public_highlight')
    const root = document.getElementById('root')!
    expect(root.textContent).not.toMatch(/new link/)
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    // The secret is never part of any request.
    for (const call of fetchSpy.mock.calls) {
      expect(String(call[0])).not.toContain('0htX0WWe')
      expect(JSON.stringify(call[1] ?? {})).not.toContain('0htX0WWe')
    }
  })
})

const SECRET = '0htX0WWeIJnofu_HTifPGX_9pz5oTvHMWAG9Ys-9JyQ'

function pinnedHash(over: Record<string, string> = {}): string {
  return '#' + new URLSearchParams({
    g: 'https://gfs.example.org',
    i: 'inst-1',
    h: 'hl-1',
    t: 'tok-1',
    k: SECRET,
    ...over,
  }).toString()
}

describe('the GFS-served page (boot block) is unchanged', () => {
  it('fetches relative to the page, as before, without the pinned options', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'))
    window.location.hash = pinnedHash({ g: 'https://elsewhere.example' })
    await import('./public_highlight')
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect(fetchSpy.mock.calls[0][0]).toBe('gfs/highlights/ice-servers')
    expect(fetchSpy.mock.calls[0][1]).toBeUndefined()
  })
})

describe('the pinned page (no boot block, everything in the fragment)', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>'
  })

  it('talks to the GFS named in the fragment, absolute, without the secret', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'))
    window.location.hash = pinnedHash()
    await import('./public_highlight')
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const [url, init] = fetchSpy.mock.calls[0]
    expect(url).toBe('https://gfs.example.org/gfs/highlights/ice-servers')
    expect(init).toMatchObject({
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      mode: 'cors',
    })
    for (const call of fetchSpy.mock.calls) {
      expect(String(call[0]).startsWith('https://gfs.example.org/')).toBe(true)
      expect(String(call[0])).not.toContain('0htX0WWe')
      expect(JSON.stringify(call[1] ?? {})).not.toContain('0htX0WWe')
    }
  })

  it('posts the offer to the GFS with the ids from the fragment, never the secret', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (String(input).endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      return new Response('{}', { status: 503 })
    })
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      addEventListener() {}
      createDataChannel() { return {} }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() {}
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    window.location.hash = pinnedHash()
    await import('./public_highlight')
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
    const [url, init] = fetchSpy.mock.calls[1]
    expect(url).toBe('https://gfs.example.org/gfs/highlight_rtc/offer')
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer' })
    const body = JSON.parse(String((init as RequestInit).body))
    expect(body).toMatchObject({ instance_id: 'inst-1', highlight_id: 'hl-1', token: 'tok-1' })
    expect(JSON.stringify(body)).not.toContain('0htX0WWe')
    vi.unstubAllGlobals()
  })

  it('without the secret asks for a new link and fetches nothing', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const params = new URLSearchParams(pinnedHash().slice(1))
    params.delete('k')
    window.location.hash = '#' + params.toString()
    await import('./public_highlight')
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(
      /Ask whoever shared it for a new link/,
    )
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it.each([
    ['an http GFS that is not localhost', { g: 'http://gfs.example.org' }],
    ['a javascript: GFS', { g: 'javascript:alert(1)' }],
    ['an overlong token', { t: 'a'.repeat(300) }],
  ])('refuses %s and fetches nothing', async (_name, over) => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    window.location.hash = pinnedHash(over)
    await import('./public_highlight')
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(
      /link is incomplete or damaged/,
    )
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('keeps the fragment in the address bar so a reload still opens it', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'))
    const h = pinnedHash()
    window.location.hash = h
    await import('./public_highlight')
    expect(window.location.hash).toBe(h)
  })
})

describe('a stream that closes early', () => {
  it('says part of the highlight did not arrive when there is no stream_end', async () => {
    const { closedStreamVerdict } = await import('./public_highlight')
    expect(closedStreamVerdict({ sawSeal: true, sawEnd: false, failed: false, frames: 3 }))
      .toBe('incomplete')
  })

  it('is complete once stream_end came, or an error is already shown', async () => {
    const { closedStreamVerdict } = await import('./public_highlight')
    expect(closedStreamVerdict({ sawSeal: true, sawEnd: true, failed: false, frames: 3 }))
      .toBe('complete')
    expect(closedStreamVerdict({ sawSeal: true, sawEnd: false, failed: true, frames: 0 }))
      .toBe('complete')
  })

  it('cannot open a stream that never sent its seal, and says when nothing came', async () => {
    const { closedStreamVerdict } = await import('./public_highlight')
    expect(closedStreamVerdict({ sawSeal: false, sawEnd: false, failed: false, frames: 0 }))
      .toBe('cant_open')
    expect(closedStreamVerdict({ sawSeal: true, sawEnd: false, failed: false, frames: 0 }))
      .toBe('nothing_arrived')
  })
})

describe('a signalling session the GFS no longer holds', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>'
  })

  it('is lost on a 404 or a server error, not on a transient client error', async () => {
    const { sessionLost } = await import('./public_highlight')
    expect(sessionLost(404)).toBe(true)
    expect(sessionLost(502)).toBe(true)
    expect(sessionLost(503)).toBe(true)
    expect(sessionLost(200)).toBe(false)
    expect(sessionLost(429)).toBe(false)
  })

  it('goes to the relay at the first lost poll instead of polling out the budget', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      if (url.endsWith('/gfs/highlight_rtc/offer')) {
        return new Response(JSON.stringify({ session_id: 'node-b.abc' }), { status: 201 })
      }
      // The poll reached a server that does not hold the session; the relay
      // is unavailable too — the page shows that instead of hanging.
      return new Response('{"error":"not_found"}', { status: url.includes('/relay/') ? 503 : 404 })
    })
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      addEventListener() {}
      createDataChannel() { return {} }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() {}
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    window.location.hash = pinnedHash()
    await import('./public_highlight')
    await vi.waitFor(
      () => {
        const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
        expect(urls.some((u) => u.includes('/gfs/highlight_rtc/relay/inst-1/hl-1'))).toBe(true)
      },
      { timeout: 4000 },
    )
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    // One poll per offer — a viewer left over from an earlier test that
    // finishes its start-up here makes two of each, never more polls.
    const offers = urls.filter((u) => u.endsWith('/gfs/highlight_rtc/offer'))
    const polls = urls.filter((u) => u.includes('/gfs/highlight_rtc/session/'))
    expect(offers.length).toBeGreaterThan(0)
    expect(polls).toEqual(
      offers.map(() => 'https://gfs.example.org/gfs/highlight_rtc/session/node-b.abc'),
    )
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')).not.toBeNull(),
    )
    vi.unstubAllGlobals()
  })
})
