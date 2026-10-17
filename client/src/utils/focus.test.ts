import { afterEach, describe, expect, it } from 'vitest'
import { restoreFocus } from './focus'

function button(label: string, parent: Element = document.body): HTMLButtonElement {
  const b = document.createElement('button')
  b.textContent = label
  parent.appendChild(b)
  return b
}

afterEach(() => { document.body.innerHTML = '' })

describe('restoreFocus', () => {
  it('returns focus to the opener when focus fell back to the body', () => {
    const opener = button('open')
    const inside = button('inside')
    inside.focus()
    inside.remove()
    restoreFocus(opener)
    expect(document.activeElement).toBe(opener)
  })

  it('returns focus while it is still inside the closing surface', () => {
    const opener = button('open')
    const dialog = document.createElement('div')
    document.body.appendChild(dialog)
    button('inside', dialog).focus()
    restoreFocus(opener, dialog)
    expect(document.activeElement).toBe(opener)
  })

  it('leaves focus alone once a newer surface took it', () => {
    // A late cleanup (Preact 11 defers unmount cleanups past paint) must
    // not pull focus out of the dialog that replaced this one.
    const opener = button('open')
    const closing = document.createElement('div')
    document.body.appendChild(closing)
    const newer = button('newer')
    newer.focus()
    restoreFocus(opener, closing)
    expect(document.activeElement).toBe(newer)
  })

  it('ignores a missing opener', () => {
    const other = button('other')
    other.focus()
    restoreFocus(null)
    expect(document.activeElement).toBe(other)
  })
})
