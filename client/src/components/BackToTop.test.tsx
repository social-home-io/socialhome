import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act } from '@testing-library/preact'
import { BackToTop, touchesStickyBar } from './BackToTop'

// Run rAF callbacks synchronously so a scroll measures in the same act().
beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => {})
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('BackToTop', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'scrollY', {
      value: 0, writable: true, configurable: true,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not render when the page is at the top', () => {
    const { container } = render(<BackToTop />)
    expect(container.querySelector('.sh-back-to-top')).toBeNull()
  })

  it('renders past the threshold and scrolls to top on click', () => {
    const scrollSpy = vi.fn()
    Object.defineProperty(window, 'scrollTo', { value: scrollSpy, configurable: true })

    const { container } = render(<BackToTop />)
    // Simulate scrolling past the threshold (700 > 600).
    act(() => {
      Object.defineProperty(window, 'scrollY', {
        value: 700, writable: true, configurable: true,
      })
      window.dispatchEvent(new Event('scroll'))
    })
    const btn = container.querySelector('.sh-back-to-top') as HTMLButtonElement
    expect(btn).toBeTruthy()
    fireEvent.click(btn)
    expect(scrollSpy).toHaveBeenCalledOnce()
    const arg = scrollSpy.mock.calls[0][0] as ScrollToOptions
    expect(arg.top).toBe(0)
  })
})

describe('BackToTop over a sticky save bar', () => {
  const rect = (top: number, bottom: number, left: number, right: number) =>
    ({ top, bottom, left, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) }) as DOMRect

  it('liftOverStickyBars: clears an overlapping bar, ignores others', async () => {
    const { liftOverStickyBars } = await import('./BackToTop')
    const rest = { top: 728, bottom: 772, left: 330, right: 374 }
    // A 390px phone: the Save row spans the width at 712–772.
    expect(liftOverStickyBars(rest, [rect(712, 772, 0, 390)])).toBe(772 - 712 + 8)
    // A bar elsewhere on the page, a hidden one, one off to the left.
    expect(liftOverStickyBars(rest, [
      rect(100, 160, 0, 390), rect(740, 740, 0, 390), rect(712, 772, 0, 200),
    ])).toBe(0)
    // Several overlapping bars: the tallest wins.
    expect(liftOverStickyBars(rest, [rect(740, 780, 0, 390), rect(700, 772, 300, 390)]))
      .toBe(772 - 700 + 8)
  })

  it('lifts the button above a .sh-form-actions row that covers its spot', () => {
    Object.defineProperty(window, 'scrollY', { value: 0, writable: true, configurable: true })
    const bar = document.createElement('div')
    bar.className = 'sh-form-actions'
    document.body.appendChild(bar)
    vi.spyOn(bar, 'getBoundingClientRect').mockReturnValue(rect(712, 772, 0, 390))
    vi.spyOn(HTMLButtonElement.prototype, 'getBoundingClientRect')
      .mockReturnValue(rect(728, 772, 330, 374))

    const { container } = render(<BackToTop />)
    act(() => {
      Object.defineProperty(window, 'scrollY', { value: 900, writable: true, configurable: true })
      window.dispatchEvent(new Event('scroll'))
    })
    const btn = container.querySelector<HTMLButtonElement>('.sh-back-to-top')!
    expect(btn.style.getPropertyValue('--sh-back-to-top-lift')).toBe('68px')

    // The bar scrolls away (not sticky any more) → the button settles back.
    vi.spyOn(bar, 'getBoundingClientRect').mockReturnValue(rect(1500, 1560, 0, 390))
    act(() => { window.dispatchEvent(new Event('scroll')) })
    expect(btn.style.getPropertyValue('--sh-back-to-top-lift')).toBe('')
    bar.remove()
  })
})

describe('BackToTop re-measures when a sticky bar mounts', () => {
  it('touchesStickyBar: only add/remove of a bar (or a subtree with one) counts', () => {
    const bar = document.createElement('div'); bar.className = 'sh-form-actions'
    const wrap = document.createElement('section'); wrap.appendChild(bar.cloneNode())
    const other = document.createElement('p')
    const rec = (added: Node[], removed: Node[] = []) =>
      ({ addedNodes: added, removedNodes: removed } as unknown as MutationRecord)
    expect(touchesStickyBar([rec([bar])])).toBe(true)
    expect(touchesStickyBar([rec([wrap])])).toBe(true)
    expect(touchesStickyBar([rec([], [bar])])).toBe(true)
    expect(touchesStickyBar([rec([other, document.createTextNode('x')])])).toBe(false)
  })

  it('lifts when a save bar is mounted under the button without any scroll', async () => {
    const r = (top: number, bottom: number, left: number, right: number) =>
      ({ top, bottom, left, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) }) as DOMRect
    Object.defineProperty(window, 'scrollY', { value: 900, writable: true, configurable: true })
    vi.spyOn(HTMLButtonElement.prototype, 'getBoundingClientRect').mockReturnValue(r(728, 772, 330, 374))
    const { container } = render(<BackToTop />)
    act(() => { window.dispatchEvent(new Event('scroll')) })
    const btn = container.querySelector<HTMLButtonElement>('.sh-back-to-top')!
    expect(btn.style.getPropertyValue('--sh-back-to-top-lift')).toBe('')

    const bar = document.createElement('div')
    bar.className = 'sh-form-actions'
    vi.spyOn(bar, 'getBoundingClientRect').mockReturnValue(r(712, 772, 0, 390))
    await act(async () => {
      document.body.appendChild(bar)
      await new Promise(res => setTimeout(res, 0))   // MutationObserver is a microtask
    })
    expect(btn.style.getPropertyValue('--sh-back-to-top-lift')).toBe('68px')
    bar.remove()
  })
})
