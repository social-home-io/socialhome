import { describe, it, expect, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { EmojiPickButton, spliceAtCaret } from './EmojiPickButton'

describe('EmojiPickButton', () => {
  afterEach(() => { cleanup() })

  it('opens the picker anchored to the button, so no scrolling sheet can clip it', () => {
    const { container } = render(<EmojiPickButton openKey="t" target={signal('')} field={{ current: null }} />)
    fireEvent.click(container.querySelector('.sh-emoji-pick-btn')!)
    const picker = container.querySelector<HTMLElement>('.sh-reaction-picker')!
    expect(picker.classList.contains('sh-reaction-picker--anchored')).toBe(true)
    expect(picker.style.top).not.toBe('')
  })

  it('with no field to take the caret from, adds the emoji at the end', () => {
    const target = signal('hi ')
    const { container } = render(<EmojiPickButton openKey="t2" target={target} field={{ current: null }} />)
    fireEvent.click(container.querySelector('.sh-emoji-pick-btn')!)
    fireEvent.click(container.querySelector('.sh-reaction-frequent .sh-emoji-btn')!)
    expect(target.value).toBe('hi 👍')
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
  })
})

describe('spliceAtCaret', () => {
  const field = (value: string, start: number, end = start) => {
    const el = document.createElement('textarea')
    el.value = value
    el.setSelectionRange(start, end)
    return el
  }

  it('inserts at the caret, and the caret goes right after the insert', () => {
    expect(spliceAtCaret('ab', '😀', field('ab', 1))).toEqual({ text: 'a😀b', caret: 3 })
  })

  it('replaces the selection', () => {
    expect(spliceAtCaret('a big b', '😀', field('a big b', 2, 5))).toEqual({ text: 'a 😀 b', caret: 4 })
  })

  it('appends without a field', () => {
    expect(spliceAtCaret('ab', '😀', null)).toEqual({ text: 'ab😀', caret: 4 })
  })
})
