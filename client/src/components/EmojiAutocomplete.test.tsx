import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
  document.body.innerHTML = ''
  vi.doMock('@/api', () => ({ api: { get: vi.fn(async () => []), post: vi.fn() } }))
  vi.doMock('@/ws', () => ({ ws: { on: vi.fn(), off: vi.fn(), send: vi.fn() } }))
  vi.doMock('@/store/auth', () => ({
    currentUser: { get value() { return { user_id: 'u-me', username: 'me', display_name: 'Me' } } },
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
})

// jsdom has no layout: report a popover where its inline style puts it
// (zero height), so the "cancel the containing-block offset" pass is a
// no-op instead of reading every rect as 0,0.
const origRect = HTMLDivElement.prototype.getBoundingClientRect
beforeEach(() => {
  HTMLDivElement.prototype.getBoundingClientRect = function (this: HTMLDivElement) {
    const l = parseFloat(this.style.left) || 0
    const t = parseFloat(this.style.top) || 0
    return { left: l, top: t, right: l, bottom: t, width: 0, height: 0, x: l, y: t, toJSON: () => ({}) } as DOMRect
  }
})

afterEach(() => {
  HTMLDivElement.prototype.getBoundingClientRect = origRect
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 768 })
})

async function composer() {
  const { Composer } = await import('./Composer')
  const utils = render(<Composer onSubmit={vi.fn(async () => undefined)} />)
  const ta = utils.container.querySelector('textarea') as HTMLTextAreaElement
  return { ...utils, ta }
}

function type(ta: HTMLTextAreaElement, value: string) {
  ta.value = value
  ta.setSelectionRange(value.length, value.length)
  fireEvent.input(ta)
}

function at(ta: HTMLTextAreaElement, top: number, bottom: number) {
  ta.getBoundingClientRect = () => ({
    left: 16, right: 344, top, bottom, width: 328, height: bottom - top,
    x: 16, y: top, toJSON: () => ({}),
  }) as DOMRect
}

const popover = () => document.querySelector<HTMLElement>('.sh-emoji-autocomplete')

describe('EmojiAutocomplete placement', () => {
  it('opens below a field with room under it', async () => {
    const { ta } = await composer()
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 720 })
    at(ta, 100, 160)
    type(ta, 'hi :smi')
    await waitFor(() => expect(popover()).toBeTruthy())
    expect(popover()!.style.transform).toBe('none')
    expect(popover()!.style.top).toBe('164px')
    expect(popover()!.style.maxHeight).toBe('280px')
  })

  it('flips above a field the phone keyboard sits on (visual viewport only)', async () => {
    const { ta } = await composer()
    // iOS / Android Chrome: innerHeight stays 844, the visual viewport is 508.
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
    Object.defineProperty(window, 'visualViewport', {
      configurable: true, value: { offsetTop: 0, height: 508, width: 390 },
    })
    at(ta, 300, 400)
    type(ta, 'hi :smi')
    await waitFor(() => expect(popover()).toBeTruthy())
    // Below would leave 96 px before the keyboard; above has 288 px.
    expect(popover()!.style.transform).toBe('translateY(-100%)')
    expect(popover()!.style.top).toBe('296px')
    expect(popover()!.style.maxHeight).toBe('280px')
  })

  it('caps the list to the room above on a short screen', async () => {
    const { ta } = await composer()
    Object.defineProperty(window, 'visualViewport', {
      configurable: true, value: { offsetTop: 0, height: 304, width: 360 },
    })
    at(ta, 200, 260)
    type(ta, 'hi :smi')
    await waitFor(() => expect(popover()).toBeTruthy())
    expect(popover()!.style.maxHeight).toBe('188px')
  })

  it('closes when the page scrolls under it', async () => {
    const { ta } = await composer()
    at(ta, 100, 160)
    type(ta, 'hi :smi')
    await waitFor(() => expect(popover()).toBeTruthy())
    // The field scrolling its own text is not a move.
    act(() => { ta.dispatchEvent(new Event('scroll')) })
    expect(popover()).toBeTruthy()
    act(() => { document.dispatchEvent(new Event('scroll')) })
    expect(popover()).toBeNull()
  })
})
