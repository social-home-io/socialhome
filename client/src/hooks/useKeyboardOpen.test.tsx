import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/preact'
import {
  KBD_OPEN_CLASS, KEYBOARD_SHRINK_PX, coveredBottom, keyboardOpen, measuringWindow,
  opensKeyboard, startKeyboardWatch, useKeyboardOpen,
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
})

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

  it('closes on blur and restores the html class', async () => {
    release = startKeyboardWatch()
    const input = field('<input>') as HTMLInputElement
    act(() => { input.focus() })
    act(() => { vv.resizeTo(500) })
    expect(keyboardOpen.value).toBe(true)
    act(() => { input.blur() })
    await flushBlur()
    expect(keyboardOpen.value).toBe(false)
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(false)
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
