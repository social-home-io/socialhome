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
