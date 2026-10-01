import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { TouchDragHint, DRAG_HINT_KEY } from './TouchDragHint'

let mm: typeof window.matchMedia
function pointer(coarse: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: coarse && q.includes('coarse'), media: q,
    addEventListener() {}, removeEventListener() {},
  })) as unknown as typeof window.matchMedia
}

beforeEach(() => { mm = window.matchMedia; localStorage.clear() })
afterEach(() => { window.matchMedia = mm })

describe('TouchDragHint', () => {
  it('shows once on a touch screen and can be dismissed for good', () => {
    pointer(true)
    const r = render(<TouchDragHint />)
    expect(r.getByRole('note').textContent).toContain('Tip: press and hold a card to move it, or use ⋯ → Move to')
    fireEvent.click(r.getByRole('button', { name: 'Dismiss tip' }))
    expect(r.queryByRole('note')).toBeNull()
    expect(localStorage.getItem(DRAG_HINT_KEY)).toBe('1')
    r.unmount()
    expect(render(<TouchDragHint />).queryByRole('note')).toBeNull()
  })

  it('never shows with a mouse', () => {
    pointer(false)
    expect(render(<TouchDragHint />).queryByRole('note')).toBeNull()
  })

  it('works when storage throws (shows; dismiss still hides it)', () => {
    pointer(true)
    const get = Storage.prototype.getItem
    const set = Storage.prototype.setItem
    Storage.prototype.getItem = () => { throw new Error('blocked') }
    Storage.prototype.setItem = () => { throw new Error('blocked') }
    try {
      const r = render(<TouchDragHint />)
      expect(r.getByRole('note')).toBeTruthy()
      fireEvent.click(r.getByRole('button', { name: 'Dismiss tip' }))
      expect(r.queryByRole('note')).toBeNull()
    } finally {
      Storage.prototype.getItem = get
      Storage.prototype.setItem = set
    }
  })
})
