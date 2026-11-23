import { describe, it, expect } from 'vitest'
import { growTextarea } from './autoGrow'

/** A textarea whose content needs ``content()`` px, with a 1 px border. */
function field(content: () => number): HTMLTextAreaElement {
  const el = document.createElement('textarea')
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: content })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: content })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, get: () => content() + 2 })
  return el
}

describe('growTextarea', () => {
  it('fits one line exactly, with no scrollbar', () => {
    const el = field(() => 36)
    growTextarea(el, 300)
    // The border is added on top of scrollHeight, so no scrollbar shows.
    expect(el.style.height).toBe('38px')
    expect(el.style.overflowY).toBe('hidden')
  })

  it('grows with long text', () => {
    const el = field(() => 180)
    growTextarea(el, 300)
    expect(el.style.height).toBe('182px')
    expect(el.style.overflowY).toBe('hidden')
  })

  it('stops at the cap and scrolls inside', () => {
    const el = field(() => 900)
    growTextarea(el, 300)
    expect(el.style.height).toBe('300px')
    expect(el.style.overflowY).toBe('auto')
  })

  it('shrinks back when the text goes (after sending)', () => {
    let content = 400
    const el = field(() => content)
    growTextarea(el, 300)
    expect(el.style.height).toBe('300px')
    content = 36
    growTextarea(el, 300)
    expect(el.style.height).toBe('38px')
    expect(el.style.overflowY).toBe('hidden')
  })
})
