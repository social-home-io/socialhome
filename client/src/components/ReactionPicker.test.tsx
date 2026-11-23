import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { createRef } from 'preact'
import { ReactionPicker } from './ReactionPicker'
import { POPOVER_GAP } from '@/utils/popoverPlacement'

describe('ReactionPicker', () => {

  it('renders frequent emoji', () => {
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={() => {}} />)
    expect(container.textContent).toContain('👍')
    expect(container.textContent).toContain('❤️')
  })

  it('calls onSelect when emoji clicked', () => {
    const fn = vi.fn()
    const { container } = render(<ReactionPicker onSelect={fn} onClose={() => {}} />)
    const buttons = container.querySelectorAll('.sh-emoji-btn')
    if (buttons.length > 0) fireEvent.click(buttons[0])
    expect(fn).toHaveBeenCalled()
  })

  it('has search input', () => {
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={() => {}} />)
    expect(container.querySelector('.sh-reaction-search')).toBeTruthy()
  })

  it('calls onClose when close clicked', () => {
    const fn = vi.fn()
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={fn} />)
    const close = container.querySelector('.sh-reaction-close')
    if (close) fireEvent.click(close)
    expect(fn).toHaveBeenCalledOnce()
  })
})

/** A 300 × 330 px picker hung off a 36 px button; the picker's rect
 *  follows its inline ``left`` / ``top`` (``translateY(-100%)`` when it
 *  opens above), like a real ``position: fixed`` box. */
function fakeLayout(button: { top: number, left: number }) {
  const orig = HTMLElement.prototype.getBoundingClientRect
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    if (this.classList.contains('sh-reaction-picker')) {
      const left = parseFloat(this.style.left) || 0
      const y = parseFloat(this.style.top) || 0
      const above = this.classList.contains('sh-reaction-picker--above')
      const top = above ? y - 330 : y
      return { left, right: left + 300, top, bottom: top + 330, width: 300, height: 330 } as DOMRect
    }
    if (this.dataset.anchor) {
      const { top, left } = button
      return { left, right: left + 36, top, bottom: top + 36, width: 36, height: 36 } as DOMRect
    }
    return orig.call(this)
  }
  return () => { HTMLElement.prototype.getBoundingClientRect = orig }
}

describe('ReactionPicker anchored to a button', () => {
  let restore: () => void = () => {}
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 720 })
  })
  afterEach(() => { restore(); cleanup() })

  function mount(button: { top: number, left: number }, onClose = vi.fn()) {
    restore = fakeLayout(button)
    const anchor = createRef<HTMLButtonElement>()
    const r = render(
      <div>
        <button ref={anchor} data-anchor="1">😀</button>
        <ReactionPicker anchor={anchor} onSelect={() => {}} onClose={onClose} />
      </div>,
    )
    return { ...r, anchor, picker: r.container.querySelector<HTMLElement>('.sh-reaction-picker')! }
  }

  it('is fixed to the viewport, so a scrolling sheet cannot clip it', () => {
    const { picker } = mount({ top: 610, left: 834 })
    expect(picker.classList.contains('sh-reaction-picker--anchored')).toBe(true)
  })

  it('opens above a button at the bottom of the screen, end-aligned', () => {
    const { picker } = mount({ top: 610, left: 834 })
    expect(picker.classList.contains('sh-reaction-picker--above')).toBe(true)
    expect(picker.style.top).toBe(`${610 - POPOVER_GAP}px`)
    expect(picker.style.left).toBe(`${870 - 300}px`)
  })

  it('opens below a button near the top of the screen', () => {
    const { picker } = mount({ top: 90, left: 834 })
    expect(picker.classList.contains('sh-reaction-picker--above')).toBe(false)
    expect(picker.style.top).toBe(`${126 + POPOVER_GAP}px`)
  })

  it('caps its height to the room there is', () => {
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 400 })
    const { picker } = mount({ top: 250, left: 834 })
    expect(picker.style.maxHeight).toBe('238px')
  })

  it('Escape closes only the picker and hands focus back to its button', () => {
    const onClose = vi.fn()
    const sheetEscape = vi.fn()
    document.addEventListener('keydown', sheetEscape)
    try {
      const { anchor } = mount({ top: 610, left: 834 }, onClose)
      fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledOnce()
      expect(sheetEscape).not.toHaveBeenCalled()
      expect(document.activeElement).toBe(anchor.current)
    } finally {
      document.removeEventListener('keydown', sheetEscape)
    }
  })
})
