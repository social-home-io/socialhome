import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/preact'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  CLOSE_GRACE_MS, KBD_INSET_VAR, KBD_OPEN_CLASS, KEYBOARD_SHRINK_PX, coveredBottom,
  keyboardInset, keyboardOpen, measuringWindow, opensKeyboard, startKeyboardWatch,
  useKeyboardOpen,
} from './useKeyboardOpen'

/** A controllable ``visualViewport`` stand-in (jsdom has none). */
class FakeViewport extends EventTarget {
  height: number
  width = 390
  scale = 1
  offsetTop = 0
  offsetLeft = 0
  pageTop = 0
  pageLeft = 0
  constructor(height: number) { super(); this.height = height }
  resizeTo(height: number, scale = 1) {
    this.height = height
    this.scale = scale
    this.dispatchEvent(new Event('resize'))
  }
  panTo(offsetTop: number) {
    this.offsetTop = offsetTop
    this.dispatchEvent(new Event('scroll'))
  }
}

let vv: FakeViewport
let coarse: boolean

function setInner(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

function field(html: string): HTMLElement {
  const wrap = document.createElement('div')
  wrap.innerHTML = html
  document.body.appendChild(wrap)
  return wrap.firstElementChild as HTMLElement
}

/** Let the deferred blur check run. */
async function flushBlur() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}

beforeEach(() => {
  coarse = true
  vv = new FakeViewport(844)
  setInner(390, 844)
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (q: string) => ({ matches: q.includes('coarse') ? coarse : false }),
  })
  window.requestAnimationFrame = (cb: FrameRequestCallback) => {
    cb(0)
    return 0
  }
})

afterEach(() => {
  document.body.innerHTML = ''
  document.documentElement.classList.remove(KBD_OPEN_CLASS)
  document.documentElement.style.removeProperty(KBD_INSET_VAR)
})

const insetVar = () => document.documentElement.style.getPropertyValue(KBD_INSET_VAR)

describe('opensKeyboard', () => {
  it.each([
    '<input>', '<input type="text">', '<input type="search">',
    '<input type="email">', '<input type="url">', '<input type="tel">',
    '<input type="password">', '<input type="number">', '<textarea></textarea>',
    '<div contenteditable="true"></div>',
  ])('true for %s', (html) => {
    const el = field(html)
    // jsdom doesn't implement isContentEditable.
    if (el.hasAttribute('contenteditable')) {
      Object.defineProperty(el, 'isContentEditable', { value: true })
    }
    expect(opensKeyboard(el)).toBe(true)
  })

  it.each([
    '<input type="checkbox">', '<input type="radio">', '<input type="range">',
    '<input type="date">', '<input type="time">', '<input type="color">',
    '<input type="file">', '<input type="submit">', '<button>x</button>',
    '<select><option>a</option></select>', '<input readonly>',
    '<input disabled>', '<textarea readonly></textarea>',
    '<input inputmode="none">', '<div></div>',
  ])('false for %s', (html) => {
    expect(opensKeyboard(field(html))).toBe(false)
  })

  it('false for null', () => {
    expect(opensKeyboard(null)).toBe(false)
  })
})

describe('coveredBottom', () => {
  const box = (top: number, bottom: number) => () => ({ top, bottom } as DOMRect)

  it('counts a Save row stuck to the visible bottom', () => {
    const bar = field('<div class="sh-form-actions"></div>')
    bar.getBoundingClientRect = box(440, 500)
    expect(coveredBottom(document, field('<input>'), 500)).toBe(60)
  })

  it("ignores a bar resting mid-page, a hidden bar, and the field's own bar", () => {
    const mid = field('<div class="sh-form-actions"></div>')
    mid.getBoundingClientRect = box(200, 260)
    const hidden = field('<div class="sh-form-actions"></div>')
    hidden.getBoundingClientRect = box(0, 0)
    const own = field('<div class="sh-form-actions"><input id="own"></div>')
    own.getBoundingClientRect = box(440, 500)
    expect(coveredBottom(document, own.querySelector('#own')!, 500)).toBe(0)
  })
})

describe('measuringWindow (ingress iframe)', () => {
  it('is the window itself at top level', () => {
    expect(measuringWindow(window)).toBe(window)
  })

  it('is the parent in a same-origin frame (haos ingress)', () => {
    const top = { innerHeight: 900 } as unknown as Window
    const frame = { top } as unknown as Window
    expect(measuringWindow(frame)).toBe(top)
  })

  it('is null in a cross-origin frame', () => {
    const top = {}
    Object.defineProperty(top, 'innerHeight', {
      get() { throw new DOMException('blocked', 'SecurityError') },
    })
    const frame = { top } as unknown as Window
    expect(measuringWindow(frame)).toBeNull()
  })
})

describe('keyboardInset', () => {
  it('is the keyboard height when only the visual viewport shrinks', () => {
    expect(keyboardInset({ innerHeight: 844, vvTop: 0, vvHeight: 508 })).toBe(336)
  })

  it('is 0 when the keyboard resized the layout viewport too', () => {
    expect(keyboardInset({ innerHeight: 508, vvTop: 0, vvHeight: 508 })).toBe(0)
  })

  it('is 0 when the browser panned the visual viewport to the bottom', () => {
    expect(keyboardInset({ innerHeight: 844, vvTop: 336, vvHeight: 508 })).toBe(0)
  })

  it('measures the part of an ingress frame below the top window keyboard', () => {
    // HA toolbar 56 px above the frame; the frame runs to the screen bottom.
    expect(keyboardInset({
      innerHeight: 788, vvTop: 0, vvHeight: 508, frame: { top: 56, bottom: 844 },
    })).toBe(336)
  })

  it('never exceeds our own height', () => {
    expect(keyboardInset({
      innerHeight: 200, vvTop: 0, vvHeight: 100, frame: { top: 600, bottom: 800 },
    })).toBe(200)
  })
})

describe('startKeyboardWatch', () => {
  let release: () => void
  afterEach(() => { release?.() })

  it('opens when a text field is focused and the viewport shrinks', () => {
    release = startKeyboardWatch()
    const input = field('<input type="text">') as HTMLInputElement
    act(() => { input.focus() })
    // Focus alone: the keyboard hasn't animated in yet.
    expect(keyboardOpen.value).toBe(false)
    act(() => { vv.resizeTo(844 - 300) })
    expect(keyboardOpen.value).toBe(true)
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(true)
  })

  it('ignores a shrink at or below the threshold (URL bar, toolbars)', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(844 - KEYBOARD_SHRINK_PX) })
    expect(keyboardOpen.value).toBe(false)
  })

  it('stays closed for a focused checkbox even if the viewport shrinks', () => {
    release = startKeyboardWatch()
    act(() => { field('<input type="checkbox">').focus() })
    act(() => { vv.resizeTo(500) })
    expect(keyboardOpen.value).toBe(false)
  })

  it('never opens without a coarse pointer (desktop, hardware keyboard)', () => {
    coarse = false
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(400) })
    expect(keyboardOpen.value).toBe(false)
  })

  it('closes on blur once the keyboard has gone and restores the html class', async () => {
    release = startKeyboardWatch()
    const input = field('<input>') as HTMLInputElement
    act(() => { input.focus() })
    act(() => { vv.resizeTo(500) })
    expect(keyboardOpen.value).toBe(true)
    act(() => { input.blur() })
    await flushBlur()
    // The keyboard is still animating away: hold the open layout so the
    // tab bar doesn't spring back over the composer mid-tap.
    expect(keyboardOpen.value).toBe(true)
    act(() => { vv.resizeTo(844) })
    expect(keyboardOpen.value).toBe(false)
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(false)
    expect(insetVar()).toBe('')
  })

  it('closes after the grace period if the viewport never grows back', async () => {
    vi.useFakeTimers()
    try {
      release = startKeyboardWatch()
      const input = field('<input>') as HTMLInputElement
      act(() => { input.focus() })
      act(() => { vv.resizeTo(500) })
      act(() => { input.blur() })
      act(() => { vi.advanceTimersByTime(0) })
      expect(keyboardOpen.value).toBe(true)
      act(() => { vi.advanceTimersByTime(CLOSE_GRACE_MS) })
      expect(keyboardOpen.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels the grace period when focus comes back to a field', async () => {
    vi.useFakeTimers()
    try {
      release = startKeyboardWatch()
      const input = field('<input>') as HTMLInputElement
      act(() => { input.focus() })
      act(() => { vv.resizeTo(500) })
      act(() => { input.blur() })
      act(() => { vi.advanceTimersByTime(0) })
      act(() => { input.focus() })
      act(() => { vi.advanceTimersByTime(CLOSE_GRACE_MS * 2) })
      expect(keyboardOpen.value).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('publishes the covered height as --sh-kbd-inset (visual-viewport keyboards)', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(844 - 336) })
    expect(insetVar()).toBe('336px')
    // The keyboard grows (emoji row, predictions): the inset follows.
    act(() => { vv.resizeTo(844 - 380) })
    expect(insetVar()).toBe('380px')
    // The browser panned the visual viewport down: nothing below is hidden.
    act(() => { vv.panTo(380) })
    expect(insetVar()).toBe('0px')
  })

  it('publishes 0 when the keyboard resizes the page (WebView)', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    setInner(390, 508)
    act(() => { vv.resizeTo(508) })
    expect(keyboardOpen.value).toBe(true)
    expect(insetVar()).toBe('0px')
  })

  it('holds a chat thread at the top of the page while its composer has focus', () => {
    release = startKeyboardWatch()
    const thread = field('<div class="sh-thread"><textarea></textarea></div>')
    const scrollTo = vi.fn()
    window.scrollTo = scrollTo as unknown as typeof window.scrollTo
    // iOS panned the page 336 px; the shrunk thread's composer ends at
    // 508 in page terms — right above the keyboard once at the top.
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 336 })
    const ta = thread.querySelector('textarea')!
    ta.scrollIntoView = vi.fn()
    ta.getBoundingClientRect = () => ({ top: 132, bottom: 172 } as DOMRect)
    act(() => { ta.focus() })
    act(() => { vv.resizeTo(508) })
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 })
  })

  it("doesn't pin a thread whose composer wouldn't fit at the top (falls back to reveal)", () => {
    release = startKeyboardWatch()
    const thread = field('<div class="sh-thread"><textarea></textarea></div>')
    const scrollTo = vi.fn()
    window.scrollTo = scrollTo as unknown as typeof window.scrollTo
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 100 })
    const ta = thread.querySelector('textarea')!
    ta.scrollIntoView = vi.fn()
    ta.getBoundingClientRect = () => ({ top: 560, bottom: 600 } as DOMRect)
    act(() => { ta.focus() })
    act(() => { vv.resizeTo(508) })
    expect(scrollTo).not.toHaveBeenCalled()
    expect(ta.scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 })
  })

  it('leaves the page scroll alone for a field outside a chat thread', () => {
    release = startKeyboardWatch()
    const scrollTo = vi.fn()
    window.scrollTo = scrollTo as unknown as typeof window.scrollTo
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 336 })
    act(() => { field('<textarea></textarea>').focus() })
    act(() => { vv.resizeTo(508) })
    expect(scrollTo).not.toHaveBeenCalled()
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 })
  })

  it('stays open while focus moves field to field', async () => {
    release = startKeyboardWatch()
    const a = field('<input>') as HTMLInputElement
    const b = field('<textarea></textarea>') as HTMLTextAreaElement
    act(() => { a.focus() })
    act(() => { vv.resizeTo(500) })
    const seen: boolean[] = []
    const stop = keyboardOpen.subscribe((v) => seen.push(v))
    act(() => { b.focus() })
    await flushBlur()
    stop()
    expect(seen.every(Boolean)).toBe(true)
    expect(keyboardOpen.value).toBe(true)
  })

  it('closes when the keyboard is dismissed but the field keeps focus (Android back)', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(500) })
    expect(keyboardOpen.value).toBe(true)
    act(() => { vv.resizeTo(844) })
    expect(keyboardOpen.value).toBe(false)
  })

  it('detects a keyboard that shrinks the layout viewport too (resizes-content / WebView)', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    setInner(390, 500)
    act(() => { vv.resizeTo(500) })
    expect(keyboardOpen.value).toBe(true)
  })

  it('does not read a pinch-zoom as a keyboard', () => {
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    // Zoomed 2x: the visual viewport is half as tall in CSS px.
    act(() => { vv.resizeTo(422, 2) })
    expect(keyboardOpen.value).toBe(false)
  })

  it('re-baselines on rotation (new width)', () => {
    release = startKeyboardWatch()
    const input = field('<input>') as HTMLInputElement
    // Landscape: shorter viewport, no keyboard.
    setInner(844, 390)
    act(() => { vv.resizeTo(390) })
    act(() => { input.focus() })
    expect(keyboardOpen.value).toBe(false)
    // Landscape keyboard eats ~200 px of 390.
    act(() => { vv.resizeTo(190) })
    expect(keyboardOpen.value).toBe(true)
  })

  it('falls back to focus alone on touch devices without visualViewport', () => {
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined })
    release = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    expect(keyboardOpen.value).toBe(true)
  })

  it('scrolls the focused field into view if it ends below the visible area', () => {
    release = startKeyboardWatch()
    const input = field('<input>') as HTMLInputElement
    const scroll = vi.fn()
    input.scrollIntoView = scroll
    input.getBoundingClientRect = () => ({ top: 600, bottom: 640 } as DOMRect)
    act(() => { input.focus() })
    act(() => { vv.resizeTo(500) })
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
  })

  it('reveals a field hidden behind a sticky Save row', () => {
    release = startKeyboardWatch()
    const bar = field('<div class="sh-form-actions"><button>Save</button></div>')
    bar.getBoundingClientRect = () => ({ top: 440, bottom: 500 } as DOMRect)
    const input = field('<input>') as HTMLInputElement
    let margin = ''
    input.scrollIntoView = vi.fn(() => { margin = input.style.scrollMarginBottom })
    input.getBoundingClientRect = () => ({ top: 430, bottom: 470 } as DOMRect)
    act(() => { input.focus() })
    act(() => { vv.resizeTo(500) })
    expect(input.scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
    // Bar covers 60 px + an 8 px gap; the inline style is restored after.
    expect(margin).toBe('68px')
    expect(input.style.scrollMarginBottom).toBe('')
  })

  it('leaves a visible field where it is', () => {
    release = startKeyboardWatch()
    const input = field('<input>') as HTMLInputElement
    const scroll = vi.fn()
    input.scrollIntoView = scroll
    input.getBoundingClientRect = () => ({ top: 100, bottom: 140 } as DOMRect)
    act(() => { input.focus() })
    act(() => { vv.resizeTo(500) })
    expect(scroll).not.toHaveBeenCalled()
  })

  it('is ref-counted: listeners stay until the last release', () => {
    const a = startKeyboardWatch()
    const b = startKeyboardWatch()
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(500) })
    a()
    a() // double release is a no-op
    expect(keyboardOpen.value).toBe(true)
    b()
    expect(keyboardOpen.value).toBe(false)
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(false)
  })
})

describe('useKeyboardOpen', () => {
  function Probe() {
    return <span data-testid="kbd">{useKeyboardOpen() ? 'open' : 'closed'}</span>
  }

  it('re-renders with the live state and stops watching on unmount', () => {
    const { getByTestId, unmount } = render(<Probe />)
    expect(getByTestId('kbd').textContent).toBe('closed')
    act(() => { field('<input>').focus() })
    act(() => { vv.resizeTo(500) })
    expect(getByTestId('kbd').textContent).toBe('open')
    unmount()
    expect(keyboardOpen.value).toBe(false)
  })
})

describe('app.css keyboard layout', () => {
  // jsdom doesn't apply media-queried rules; guard the rules themselves.
  const css = readFileSync(join(__dirname, '..', 'styles', 'app.css'), 'utf8')

  it('ends chat columns above a visual-viewport keyboard', () => {
    const host = css.match(/html\.sh-kbd-open \.sh-space-feed--chat \{([^}]*)\}/)
    expect(host![1]).toContain('var(--sh-kbd-inset, 0px)')
    expect(host![1]).toContain('min-height: 0')
    expect(css).toMatch(/html\.sh-kbd-open body\.sh-dm-thread-open \.sh-thread \{[^}]*var\(--sh-kbd-inset, 0px\)/)
  })

  it('folds the rows above a chat while its composer has focus', () => {
    expect(css).toContain('html.sh-kbd-open .sh-thread:focus-within > .sh-thread-header')
    expect(css).toContain('html.sh-kbd-open .sh-space-feed--chat:has(> .sh-thread:focus-within) > :not(.sh-thread)')
    expect(css).toContain('html.sh-kbd-open .sh-feed-host--chat:has(> .sh-thread:focus-within) > :not(.sh-thread)')
  })

  it('docks the comment sheet above the keyboard', () => {
    expect(css).toMatch(/html\.sh-kbd-open \.sh-comment-overlay-backdrop \{ bottom: var\(--sh-kbd-inset, 0px\); \}/)
  })
})
