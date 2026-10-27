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
        // Every offer has had its one poll by now. A viewer left over from
        // an earlier test (the page mounts on import and the next test only
        // replaces the DOM, which never unmounts it) can post its offer
        // here a moment after this test's viewer reached the relay: its
        // poll comes one interval later. Waiting for it — rather than
        // asserting at the first relay call — is what made this flaky.
        const offers = urls.filter((u) => u.endsWith('/gfs/highlight_rtc/offer'))
        const polls = urls.filter((u) => u.includes('/gfs/highlight_rtc/session/'))
        expect(polls.length).toBe(offers.length)
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


describe('a viewer that goes away while it starts', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('posts no offer and opens no peer connection after it is unmounted', async () => {
    // "Try again" remounts the viewer (and a page can be left): the old
    // one's start-up was still in flight — it went on to open an
    // RTCPeerConnection nobody closed, post an offer to the GFS and poll
    // that session every second, for nobody.
    let releaseIce: (r: Response) => void = () => {}
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Promise<Response>((resolve) => { releaseIce = resolve })
      }
      return new Response(JSON.stringify({ session_id: 's-1' }), { status: 201 })
    })
    const built = vi.fn()
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      constructor() { built() }
      addEventListener() {}
      createDataChannel() { return {} }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() {}
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    const { render } = await import('@testing-library/preact')
    const { PublicHighlightViewer } = await import('./public_highlight')
    const { linkSecretFromHash } = await import('./stream_seal')
    const retry = { manual: () => {}, auto: () => {}, nextAutoDelayMs: () => null }
    const view = render(
      <PublicHighlightViewer
        boot={{
          instanceId: 'inst-1', highlightId: 'hl-1', token: 'tok-1',
          gfs: 'https://gfs.example.org',
        }}
        secret={linkSecretFromHash(pinnedHash())!}
        retry={retry}
      />,
    )
    await vi.waitFor(() => {
      const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.endsWith('/gfs/highlights/ice-servers'))).toBe(true)
    }, { timeout: 4000 })
    view.unmount()
    // Preact runs an unmounted component's effect cleanup after the frame;
    // the GFS answering is slower than that.
    await new Promise((r) => setTimeout(r, 50))
    releaseIce(new Response(JSON.stringify({ servers: [] }), { status: 200 }))
    await new Promise((r) => setTimeout(r, 50))
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.endsWith('/gfs/highlight_rtc/offer'))).toBe(false)
    expect(built).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('stops polling and closes its connection when unmounted after the offer', async () => {
    let releaseOffer: (r: Response) => void = () => {}
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      if (url.endsWith('/gfs/highlight_rtc/offer')) {
        return new Promise<Response>((resolve) => { releaseOffer = resolve })
      }
      return new Response('{}', { status: 200 })
    })
    const closed = vi.fn()
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      addEventListener() {}
      createDataChannel() { return {} }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() { closed() }
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    const { render } = await import('@testing-library/preact')
    const { PublicHighlightViewer } = await import('./public_highlight')
    const { linkSecretFromHash } = await import('./stream_seal')
    const retry = { manual: () => {}, auto: () => {}, nextAutoDelayMs: () => null }
    const view = render(
      <PublicHighlightViewer
        boot={{
          instanceId: 'inst-1', highlightId: 'hl-1', token: 'tok-1',
          gfs: 'https://gfs.example.org',
        }}
        secret={linkSecretFromHash(pinnedHash())!}
        retry={retry}
      />,
    )
    await vi.waitFor(() => {
      const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
      expect(urls.some((u) => u.endsWith('/gfs/highlight_rtc/offer'))).toBe(true)
    }, { timeout: 4000 })
    view.unmount()
    await new Promise((r) => setTimeout(r, 50))
    releaseOffer(new Response(JSON.stringify({ session_id: 's-1' }), { status: 201 }))
    // Past the 1 s poll interval: nothing polls the session.
    await new Promise((r) => setTimeout(r, 1300))
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/gfs/highlight_rtc/session/'))).toBe(false)
    expect(closed).toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})


describe('how the highlight reaches the viewer', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  async function renderViewer() {
    const { render } = await import('@testing-library/preact')
    const { PublicHighlightViewer } = await import('./public_highlight')
    const { linkSecretFromHash } = await import('./stream_seal')
    const retry = { manual: () => {}, auto: () => {}, nextAutoDelayMs: () => null }
    return render(
      <PublicHighlightViewer
        boot={{
          instanceId: 'inst-1', highlightId: 'hl-1', token: 'tok-1',
          gfs: 'https://gfs.example.org',
        }}
        secret={linkSecretFromHash(pinnedHash())!}
        retry={retry}
      />,
    )
  }

  it('says the stream comes straight from their home once the channel opens', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      if (url.endsWith('/gfs/highlight_rtc/offer')) {
        return new Response(JSON.stringify({ session_id: 's-1' }), { status: 201 })
      }
      return new Response(JSON.stringify({ answer_sdp: null, ice_candidates: [] }), { status: 200 })
    })
    const channel: { onopen?: () => void } = {}
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      addEventListener() {}
      createDataChannel() { return channel }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() {}
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    const view = await renderViewer()
    expect(view.container.querySelector('.highlight-transport')).toBeNull()
    await vi.waitFor(() => expect(channel.onopen).toBeTypeOf('function'), { timeout: 4000 })
    const { act } = await import('@testing-library/preact')
    act(() => channel.onopen!())
    await vi.waitFor(() => {
      const badge = view.container.querySelector('.highlight-transport')
      expect(badge?.textContent).toBe('Direct connection')
      expect(badge?.getAttribute('data-transport')).toBe('direct')
    })
    view.unmount()
    vi.unstubAllGlobals()
  })

  it('says it comes through the GFS, still encrypted, on the relay fallback', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      if (url.endsWith('/gfs/highlight_rtc/offer')) {
        return new Response(JSON.stringify({ session_id: 's-1' }), { status: 201 })
      }
      if (url.includes('/gfs/highlight_rtc/relay/')) {
        // The author streams through the GFS; nothing has arrived yet.
        return new Response(new ReadableStream({ start() {} }), { status: 200 })
      }
      // The session is gone → the viewer goes to the relay at once.
      return new Response('{"error":"not_found"}', { status: 404 })
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
    const view = await renderViewer()
    await vi.waitFor(() => {
      const badge = view.container.querySelector('.highlight-transport')
      expect(badge?.textContent).toBe('Through the GFS · still encrypted')
      expect(badge?.getAttribute('data-transport')).toBe('relay')
    }, { timeout: 4000 })
    view.unmount()
    vi.unstubAllGlobals()
  })
})


describe('reading the relayed byte stream', () => {
  function encoded(header: object, payload: Uint8Array): Uint8Array {
    const h = new TextEncoder().encode(JSON.stringify(header))
    const buf = new Uint8Array(4 + h.length + 4 + payload.length)
    const view = new DataView(buf.buffer)
    view.setUint32(0, h.length)
    buf.set(h, 4)
    view.setUint32(4 + h.length, payload.length)
    buf.set(payload, 4 + h.length + 4)
    return buf
  }

  it('waits for the whole payload when a frame is split across network reads', async () => {
    document.body.innerHTML = ''
    const { takeFrame } = await import('./public_highlight')
    const payload = new Uint8Array(1000).map((_, i) => i % 251)
    const whole = encoded({ kind: 'sealed' }, payload)
    // The header and part of the payload have arrived; the rest hasn't.
    expect(takeFrame(whole.subarray(0, whole.length - 10))).toBeNull()
    const taken = takeFrame(whole)
    expect(taken?.payload).toEqual(payload)
    expect(taken?.rest.length).toBe(0)
  })
})


describe('the player: controls, muted video, pause, keyboard', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  })

  afterEach(async () => {
    const { cleanup } = await import('@testing-library/preact')
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    document.documentElement.removeAttribute('dir')
  })

  const IMAGES = [
    { frame_id: 'f1', sequence: 0, content_type: 'image/webp', byte_length: 1, duration_ms: 1000 },
    { frame_id: 'f2', sequence: 1, content_type: 'image/webp', byte_length: 1, duration_ms: 1000 },
    { frame_id: 'f3', sequence: 2, content_type: 'image/webp', byte_length: 1, duration_ms: 1000 },
  ]
  const URLS = { f1: 'blob:f1', f2: 'blob:f2', f3: 'blob:f3', v1: 'blob:v1' }
  const VIDEO = [
    { frame_id: 'v1', sequence: 0, content_type: 'video/mp4', byte_length: 1, duration_ms: 1000 },
    ...IMAGES.slice(1),
  ]

  async function renderPlayer(manifest = IMAGES, onEnded = vi.fn()) {
    const tl = await import('@testing-library/preact')
    const { HighlightPlayer } = await import('./public_highlight')
    const view = tl.render(
      <HighlightPlayer manifest={manifest} frameUrls={URLS} placeholder="Buffering…" onEnded={onEnded} />,
    )
    const shown = () => view.container.querySelector('.stage img, .stage video')?.getAttribute('src')
    const click = (name: string) => tl.fireEvent.click(tl.screen.getByRole('button', { name }))
    return { ...tl, view, shown, click, onEnded }
  }

  it('starts a video muted so the browser lets it play, with an Unmute button', async () => {
    const { view, click, screen } = await renderPlayer(VIDEO)
    const video = view.container.querySelector('video')!
    expect(video.hasAttribute('autoplay')).toBe(true)
    expect(video.hasAttribute('playsinline')).toBe(true)
    expect(video.muted).toBe(true)
    expect(video.hasAttribute('muted')).toBe(true)
    expect(video.hasAttribute('controls')).toBe(false)
    click('Unmute')
    expect(video.muted).toBe(false)
    expect(screen.getByRole('button', { name: 'Mute' })).toBeTruthy()
  })

  it('has no Mute button for a highlight of pictures only', async () => {
    const { screen } = await renderPlayer()
    expect(screen.queryByRole('button', { name: /mute/i })).toBeNull()
    for (const name of ['Previous', 'Pause', 'Next']) {
      expect(screen.getByRole('button', { name })).toBeTruthy()
    }
  })

  it('holds the frame while paused and goes on from where it stopped', async () => {
    vi.useFakeTimers()
    const { shown, click, act } = await renderPlayer()
    expect(shown()).toBe('blob:f1')
    await act(() => { vi.advanceTimersByTime(600) })
    click('Pause')
    await act(() => { vi.advanceTimersByTime(10_000) })
    expect(shown()).toBe('blob:f1')
    click('Play')
    await act(() => { vi.advanceTimersByTime(300) })
    expect(shown()).toBe('blob:f1')
    await act(() => { vi.advanceTimersByTime(150) })
    expect(shown()).toBe('blob:f2')
  })

  it('runs a video frame’s timer only while the video plays', async () => {
    vi.useFakeTimers()
    const { view, shown, act, fireEvent } = await renderPlayer(VIDEO)
    // Not playing yet (autoplay still deciding, or blocked): the frame waits.
    await act(() => { vi.advanceTimersByTime(5000) })
    expect(shown()).toBe('blob:v1')
    const video = view.container.querySelector('video')!
    fireEvent(video, new Event('playing'))
    await act(() => { vi.advanceTimersByTime(500) })
    fireEvent(video, new Event('waiting')) // buffering: the timer waits too
    await act(() => { vi.advanceTimersByTime(5000) })
    expect(shown()).toBe('blob:v1')
    fireEvent(video, new Event('playing'))
    await act(() => { vi.advanceTimersByTime(520) })
    expect(shown()).toBe('blob:f2')
  })

  it('pauses the video with the Pause button', async () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause')
    const { click } = await renderPlayer(VIDEO)
    click('Pause')
    expect(pause).toHaveBeenCalled()
  })

  it('shows Play when the browser refuses to start the video', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('NotAllowedError'))
    const { screen } = await renderPlayer(VIDEO)
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy())
  })

  it('steps with Next / Previous and ends after the last frame', async () => {
    const { shown, click, onEnded } = await renderPlayer()
    click('Next')
    expect(shown()).toBe('blob:f2')
    click('Previous')
    expect(shown()).toBe('blob:f1')
    click('Previous') // restarts the first
    expect(shown()).toBe('blob:f1')
    click('Next')
    click('Next')
    expect(onEnded).not.toHaveBeenCalled()
    click('Next')
    expect(onEnded).toHaveBeenCalledOnce()
  })

  it('answers the keyboard: Space / K pause, arrows step (mirrored in RTL), M mutes', async () => {
    const { view, shown, screen, fireEvent } = await renderPlayer(VIDEO)
    fireEvent.keyDown(document.body, { key: ' ' })
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    fireEvent.keyDown(document.body, { key: 'k' })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy()
    fireEvent.keyDown(document.body, { key: 'm' })
    expect(view.container.querySelector('video')!.muted).toBe(false)
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    expect(shown()).toBe('blob:f2')
    document.documentElement.setAttribute('dir', 'rtl')
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    expect(shown()).toBe('blob:f3')
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    expect(shown()).toBe('blob:f2')
  })

  it('fills the current segment smoothly, paused with the player', async () => {
    const { view, click } = await renderPlayer()
    const fill = view.container.querySelector('.progress .seg.active .seg-fill') as HTMLElement
    expect(fill).not.toBeNull()
    expect(fill.style.animationDuration).toBe('1000ms')
    expect(fill.style.animationPlayState).toBe('running')
    click('Pause')
    expect(fill.style.animationPlayState).toBe('paused')
  })

  it('steps the progress instead under prefers-reduced-motion', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce') }))
    const { view } = await renderPlayer()
    expect(view.container.querySelector('.seg-fill')).toBeNull()
    expect(view.container.querySelector('.progress .seg.active')).not.toBeNull()
  })
})


describe('the transport label explains itself', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('opens a "What’s this?" note that names whose home it comes from', async () => {
    const { render, screen, fireEvent } = await import('@testing-library/preact')
    const { TransportNote } = await import('./public_highlight')
    const view = render(<TransportNote transport="direct" authorName="Anna Berger" />)
    expect(view.container.querySelector('.highlight-transport')?.textContent).toBe('Direct connection')
    const button = screen.getByRole('button', { name: 'What’s this?' })
    expect(button.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    const note = document.getElementById(button.getAttribute('aria-controls')!)
    expect(note?.textContent).toContain('Direct from Anna Berger’s home')
    view.unmount()
  })

  it('says the GFS carries a relayed stream and still can’t read it', async () => {
    const { render, screen, fireEvent } = await import('@testing-library/preact')
    const { TransportNote } = await import('./public_highlight')
    const view = render(<TransportNote transport="relay" authorName={null} />)
    expect(view.container.querySelector('.highlight-transport')?.textContent)
      .toBe('Through the GFS · still encrypted')
    fireEvent.click(screen.getByRole('button', { name: 'What’s this?' }))
    expect(view.container.textContent).toMatch(/GFS passes it on/)
    view.unmount()
  })
})


describe('what the highlight page says when it can’t play', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  function frame(header: object): ArrayBuffer {
    const h = new TextEncoder().encode(JSON.stringify(header))
    const buf = new Uint8Array(4 + h.length + 4)
    const view = new DataView(buf.buffer)
    view.setUint32(0, h.length)
    buf.set(h, 4)
    view.setUint32(4 + h.length, 0)
    return buf.buffer
  }

  async function renderViewer(nextAutoDelayMs: () => number | null = () => null) {
    const { render } = await import('@testing-library/preact')
    const { PublicHighlightViewer } = await import('./public_highlight')
    const { linkSecretFromHash } = await import('./stream_seal')
    const retry = { manual: vi.fn(), auto: vi.fn(), nextAutoDelayMs }
    const view = render(
      <PublicHighlightViewer
        boot={{
          instanceId: 'inst-1', highlightId: 'hl-1', token: 'tok-1',
          gfs: 'https://gfs.example.org',
        }}
        secret={linkSecretFromHash(pinnedHash())!}
        retry={retry}
      />,
    )
    return { view, retry }
  }

  it('unknown, revoked and offline look the same: neutral, retrying on its own', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      if (String(input).endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      return new Response('{"error":"unavailable"}', { status: 503 })
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
    const { view } = await renderViewer(() => 30_000)
    await vi.waitFor(
      () => expect(view.container.querySelector('[role="alert"]')).not.toBeNull(),
      { timeout: 4000 },
    )
    const text = view.container.textContent ?? ''
    expect(text).toContain('This isn’t available right now.')
    expect(text).toContain('Try again later, or ask whoever shared it.')
    expect(text).toContain('Trying again in 30 s.')
    expect(text.toLowerCase()).not.toMatch(/reach|offline|online|ended|household/)
    expect(view.container.querySelector('button.viewer-retry')).not.toBeNull()
    view.unmount()
    vi.unstubAllGlobals()
  })

  it('says it has ended only when the household’s own stream says so, and stops retrying', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/gfs/highlights/ice-servers')) {
        return new Response(JSON.stringify({ servers: [] }), { status: 200 })
      }
      if (url.endsWith('/gfs/highlight_rtc/offer')) {
        return new Response(JSON.stringify({ session_id: 's-1' }), { status: 201 })
      }
      return new Response(JSON.stringify({ answer_sdp: null, ice_candidates: [] }), { status: 200 })
    })
    const channel: { onopen?: () => void; onmessage?: (ev: { data: ArrayBuffer }) => void } = {}
    class FakePc {
      iceConnectionState = 'new'
      remoteDescription = null
      addEventListener() {}
      createDataChannel() { return channel }
      async createOffer() { return { type: 'offer', sdp: 'v=0' } }
      async setLocalDescription() {}
      close() {}
    }
    vi.stubGlobal('RTCPeerConnection', FakePc)
    const { view, retry } = await renderViewer(() => 10_000)
    await vi.waitFor(() => expect(channel.onmessage).toBeTypeOf('function'), { timeout: 4000 })
    const { act } = await import('@testing-library/preact')
    act(() => channel.onopen!())
    act(() => channel.onmessage!({ data: frame({ kind: 'error', error: 'expired' }) }))
    await vi.waitFor(() => expect(view.container.textContent).toContain('This highlight has ended.'))
    expect(view.container.querySelector('button')).toBeNull()
    expect(view.container.textContent).not.toContain('Trying again')
    expect(retry.auto).not.toHaveBeenCalled()
    view.unmount()
    vi.unstubAllGlobals()
  })
})
