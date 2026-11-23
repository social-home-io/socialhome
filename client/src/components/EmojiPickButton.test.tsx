import { describe, it, expect, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { EmojiPickButton } from './EmojiPickButton'

describe('EmojiPickButton', () => {
  afterEach(() => { cleanup() })

  it('opens the picker anchored to the button, so no scrolling sheet can clip it', () => {
    const { container } = render(<EmojiPickButton openKey="t" target={signal('')} />)
    fireEvent.click(container.querySelector('.sh-emoji-pick-btn')!)
    const picker = container.querySelector<HTMLElement>('.sh-reaction-picker')!
    expect(picker.classList.contains('sh-reaction-picker--anchored')).toBe(true)
    expect(picker.style.top).not.toBe('')
  })

  it('appends the chosen emoji to a signal target', () => {
    const target = signal('hi ')
    const { container } = render(<EmojiPickButton openKey="t2" target={target} />)
    fireEvent.click(container.querySelector('.sh-emoji-pick-btn')!)
    fireEvent.click(container.querySelector('.sh-reaction-frequent .sh-emoji-btn')!)
    expect(target.value).toBe('hi 👍')
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
  })
})
