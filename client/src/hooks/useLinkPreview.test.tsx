import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/preact'

const post = vi.fn()

beforeEach(() => {
  vi.resetModules()
  post.mockReset()
  vi.doMock('@/api', () => ({ api: { post } }))
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

const CARD = {
  url: 'https://example.com/',
  title: 'Card',
  description: null,
  site_name: null,
  thumbnail_url: null,
}

async function mount(initial: string) {
  const mod = await import('./useLinkPreview')
  const state: { current: ReturnType<typeof mod.useLinkPreview> | null } = { current: null }
  function Probe({ text }: { text: string }) {
    state.current = mod.useLinkPreview(text)
    return null
  }
  const view = render(<Probe text={initial} />)
  return {
    mod,
    state,
    rerender: (text: string) => view.rerender(<Probe text={text} />),
  }
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(700)
  })
}

describe('useLinkPreview', () => {
  it('asks once for the first link after the debounce', async () => {
    post.mockResolvedValue({ preview: CARD })
    const { state } = await mount('look https://example.com/ ok')
    expect(state.current!.loading).toBe(true)
    expect(post).not.toHaveBeenCalled()
    await settle()
    expect(post).toHaveBeenCalledWith('/api/link-preview', { url: 'https://example.com/' })
    expect(state.current!.preview).toEqual(CARD)
    expect(state.current!.loading).toBe(false)
  })

  it('does nothing without a link', async () => {
    const { state } = await mount('no link here')
    await settle()
    expect(post).not.toHaveBeenCalled()
    expect(state.current!.url).toBeNull()
    expect(state.current!.preview).toBeNull()
  })

  it('debounces while typing and ignores a stale answer', async () => {
    post.mockResolvedValue({ preview: CARD })
    const { state, rerender } = await mount('https://a.example')
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    rerender('https://example.com/')
    await settle()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/api/link-preview', { url: 'https://example.com/' })
    expect(state.current!.preview).toEqual(CARD)
  })

  it('remove is per link and can be undone', async () => {
    post.mockResolvedValue({ preview: CARD })
    const { state, rerender } = await mount('https://example.com/')
    await settle()
    act(() => state.current!.dismiss())
    expect(state.current!.dismissed).toBe(true)
    act(() => state.current!.restore())
    expect(state.current!.dismissed).toBe(false)
    act(() => state.current!.dismiss())
    rerender('https://other.example/')
    expect(state.current!.dismissed).toBe(false)
  })

  it('a failed request means no card', async () => {
    post.mockRejectedValue(new Error('boom'))
    const { state } = await mount('https://example.com/')
    await settle()
    expect(state.current!.preview).toBeNull()
    expect(state.current!.loading).toBe(false)
  })

  it('a 403 (previews off) silences it for the session', async () => {
    post.mockRejectedValue(Object.assign(new Error('off'), { status: 403 }))
    const { state, rerender, mod } = await mount('https://example.com/')
    await settle()
    rerender('https://other.example/')
    await settle()
    expect(post).toHaveBeenCalledTimes(1)
    expect(state.current!.url).toBeNull()
    mod.resetLinkPreviewSession()
  })
})
