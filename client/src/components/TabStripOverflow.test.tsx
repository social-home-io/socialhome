import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/preact'
import { useRef } from 'preact/hooks'
import {
  TAB_STRIP_FADE_PX,
  activeTabScrollDelta,
  useScrollActiveTabIntoView,
  useTabStripOverflow,
} from './TabStripOverflow'

// Geometry taken from a real 390 px phone render of the space header:
// the strip spans x=47..232 once the bell, settings and ⋯ buttons have
// mounted, and the active "Pages" tab sits at x=205..276.
const PHONE_STRIP = { left: 47, right: 232 }

describe('activeTabScrollDelta', () => {
  it('scrolls a half-hidden active tab into view on a 390 px phone', () => {
    const pages = { left: 205, right: 276 }
    const delta = activeTabScrollDelta(PHONE_STRIP, pages)
    // Centred: (205 + 276) / 2 - (47 + 232) / 2
    expect(delta).toBeCloseTo(101)
    const after = { left: pages.left - delta, right: pages.right - delta }
    expect(after.left).toBeGreaterThanOrEqual(PHONE_STRIP.left + TAB_STRIP_FADE_PX)
    expect(after.right).toBeLessThanOrEqual(PHONE_STRIP.right - TAB_STRIP_FADE_PX)
  })

  it('scrolls back for an active tab hidden off the left edge on mobile', () => {
    expect(activeTabScrollDelta(PHONE_STRIP, { left: 10, right: 60 })).toBeLessThan(0)
  })

  it('treats a tab under the edge fade as not visible', () => {
    // Fully inside the strip box but its last 8 px sit in the fade.
    expect(activeTabScrollDelta(PHONE_STRIP, { left: 160, right: 224 })).not.toBe(0)
  })

  it('leaves the strip alone when the active tab is already readable', () => {
    expect(activeTabScrollDelta(PHONE_STRIP, { left: 80, right: 140 })).toBe(0)
    // Desktop: wide strip, every tab fits.
    expect(activeTabScrollDelta({ left: 0, right: 1000 }, { left: 300, right: 380 })).toBe(0)
  })
})

// --- Hook wiring ----------------------------------------------------------

type ROCallback = () => void
let roCallbacks: ROCallback[] = []

class FakeResizeObserver {
  private cb: ROCallback
  constructor(cb: ROCallback) { this.cb = cb; roCallbacks.push(cb) }
  observe() {}
  unobserve() {}
  disconnect() { roCallbacks = roCallbacks.filter((c) => c !== this.cb) }
}

function Strip({ active, onStrip }: { active: string; onStrip?: (el: HTMLElement) => void }) {
  const ref = useRef<HTMLElement | null>(null)
  useTabStripOverflow(ref, [])
  useScrollActiveTabIntoView(ref, active, [])
  return (
    <nav ref={(el) => { ref.current = el; if (el) onStrip?.(el) }} class="sh-space-tabs">
      {['feed', 'members', 'pages'].map((t) => (
        <button key={t} role="tab" aria-selected={t === active} data-tab={t}>{t}</button>
      ))}
    </nav>
  )
}

const ORIGINAL_RO = (window as unknown as Record<string, unknown>).ResizeObserver

describe('useScrollActiveTabIntoView', () => {
  let stripRect = { left: 0, right: 1000 }
  let pagesRect = { left: 205, right: 276 }
  const scrollBy = vi.fn()

  beforeEach(() => {
    roCallbacks = []
    ;(window as unknown as Record<string, unknown>).ResizeObserver = FakeResizeObserver
    stripRect = { left: 0, right: 1000 }
    pagesRect = { left: 205, right: 276 }
    scrollBy.mockReset()
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      function (this: HTMLElement) {
        const r = this.classList.contains('sh-space-tabs')
          ? stripRect
          : this.dataset.tab === 'pages' ? pagesRect : { left: 0, right: 50 }
        return { ...r, top: 0, bottom: 44, width: r.right - r.left, height: 44, x: r.left, y: 0 } as DOMRect
      },
    )
    ;(HTMLElement.prototype as unknown as Record<string, unknown>).scrollBy = scrollBy
  })

  afterEach(() => {
    vi.restoreAllMocks()
    ;(window as unknown as Record<string, unknown>).ResizeObserver = ORIGINAL_RO
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollBy
  })

  it('re-reveals the active tab when the strip shrinks after mount on a 390 px phone', () => {
    render(<Strip active="pages" />)
    // First paint: strip still roomy (actions + ⋯ not mounted yet).
    expect(scrollBy).not.toHaveBeenCalled()

    // Bell, settings and ⋯ mount — the strip shrinks to 185 px and
    // "Pages" now hangs off the right edge.
    stripRect = PHONE_STRIP
    roCallbacks.forEach((cb) => cb())

    expect(scrollBy).toHaveBeenCalledTimes(1)
    expect(scrollBy.mock.calls[0][0].left).toBeCloseTo(101)
  })

  it('reveals a newly selected tab that sits off-screen on mobile', () => {
    stripRect = PHONE_STRIP
    pagesRect = { left: 300, right: 360 }
    const { rerender } = render(<Strip active="feed" />)
    scrollBy.mockReset()
    rerender(<Strip active="pages" />)
    expect(scrollBy).toHaveBeenCalled()
    expect(scrollBy.mock.calls.at(-1)?.[0].left).toBeGreaterThan(0)
  })
})

describe('useTabStripOverflow edge fades', () => {
  afterEach(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollWidth
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
  })

  it('fades only the edge that still hides tabs on a mobile strip', () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
      configurable: true, get() { return 738 },
    })
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true, get() { return 185 },
    })
    let strip: HTMLElement | null = null
    render(<Strip active="feed" onStrip={(el) => { strip = el }} />)
    expect(strip!.hasAttribute('data-fade-left')).toBe(false)
    expect(strip!.hasAttribute('data-fade-right')).toBe(true)

    strip!.scrollLeft = 738 - 185
    strip!.dispatchEvent(new Event('scroll'))
    expect(strip!.hasAttribute('data-fade-left')).toBe(true)
    expect(strip!.hasAttribute('data-fade-right')).toBe(false)
  })

  it('shows no fade on desktop when every tab fits', () => {
    let strip: HTMLElement | null = null
    render(<Strip active="feed" onStrip={(el) => { strip = el }} />)
    expect(strip!.hasAttribute('data-fade-left')).toBe(false)
    expect(strip!.hasAttribute('data-fade-right')).toBe(false)
  })
})
