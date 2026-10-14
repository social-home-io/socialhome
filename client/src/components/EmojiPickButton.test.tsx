import { describe, it, expect, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { EmojiPickButton, fitPicker } from './EmojiPickButton'

const phone = { top: 0, bottom: 844, width: 390 }

describe('fitPicker', () => {
  it('leaves a picker that fits alone', () => {
    expect(fitPicker({ left: 60, right: 360, top: 200, height: 327 }, { top: 540, bottom: 576 }, phone))
      .toEqual({ shiftX: 0, below: false })
  })

  it('pulls a picker that runs off the left edge back on screen', () => {
    // The post composer's 😀 sits mid-row: end-aligned, 300 px wide.
    expect(fitPicker({ left: -59, right: 241, top: 49, height: 327 }, { top: 382, bottom: 418 }, phone).shiftX)
      .toBe(67)
  })

  it('pulls a picker that runs off the right edge back', () => {
    expect(fitPicker({ left: 120, right: 420, top: 200, height: 327 }, { top: 540, bottom: 576 }, phone).shiftX)
      .toBe(-38)
  })

  it('opens below when the room above runs out and there is more below', () => {
    expect(fitPicker({ left: 60, right: 360, top: -200, height: 327 }, { top: 130, bottom: 166 }, phone).below)
      .toBe(true)
  })

  it('stays above when below has even less room', () => {
    const short = { top: 0, bottom: 304, width: 360 }
    expect(fitPicker({ left: 20, right: 320, top: -100, height: 327 }, { top: 230, bottom: 266 }, short).below)
      .toBe(false)
  })
})

describe('EmojiPickButton', () => {
  afterEach(() => { cleanup() })

  it('nudges the open picker inside the screen', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
    const orig = HTMLElement.prototype.getBoundingClientRect
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      if (this.classList.contains('sh-reaction-picker')) {
        return { left: -59, right: 241, top: 49, bottom: 376, height: 327, width: 300 } as DOMRect
      }
      return { left: 205, right: 241, top: 382, bottom: 418, height: 36, width: 36 } as DOMRect
    }
    try {
      const { container } = render(<EmojiPickButton openKey="t" target={signal('')} />)
      fireEvent.click(container.querySelector('.sh-emoji-pick-btn')!)
      const picker = container.querySelector<HTMLElement>('.sh-reaction-picker')!
      expect(picker.style.right).toBe('-67px')
      expect(picker.classList.contains('sh-reaction-picker--below')).toBe(false)
    } finally {
      HTMLElement.prototype.getBoundingClientRect = orig
    }
  })
})
