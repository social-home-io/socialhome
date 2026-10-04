import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { EmojiField } from './EmojiField'

describe('EmojiField', () => {
  it('shows an empty dashed tile with a + when no icon is set', () => {
    const value = signal('')
    const { container } = render(<EmojiField value={value} openKey="t1" />)
    const tile = container.querySelector('.sh-emoji-field-tile')
    expect(tile).toBeTruthy()
    expect(tile!.classList.contains('is-empty')).toBe(true)
    expect(tile!.textContent).toContain('+')
    // No "Remove" affordance until an icon exists.
    expect(container.querySelector('.sh-emoji-field-clear')).toBeNull()
  })

  it('renders the current emoji and a Remove control when set', () => {
    const value = signal('🏠')
    const { container } = render(<EmojiField value={value} openKey="t2" />)
    expect(container.querySelector('.sh-emoji-field-glyph')!.textContent).toBe('🏠')
    expect(container.querySelector('.sh-emoji-field-clear')).toBeTruthy()
  })

  it('opens the inline picker on tap and sets the value on select', () => {
    const value = signal('')
    const { container } = render(<EmojiField value={value} openKey="t3" />)
    // Picker closed initially.
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
    fireEvent.click(container.querySelector('.sh-emoji-field-tile')!)
    const picker = container.querySelector('.sh-reaction-picker')
    expect(picker).toBeTruthy()
    // Inline variant (not an absolute popover).
    expect(picker!.classList.contains('sh-reaction-picker--inline')).toBe(true)
    // Choosing the first emoji sets the value and closes the picker.
    fireEvent.click(container.querySelector('.sh-emoji-btn')!)
    expect(value.value).not.toBe('')
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
  })

  it('clears the value via Remove', () => {
    const value = signal('🎉')
    const { container } = render(<EmojiField value={value} openKey="t4" />)
    fireEvent.click(container.querySelector('.sh-emoji-field-clear')!)
    expect(value.value).toBe('')
  })

  it('toggles closed when the tile is tapped again', () => {
    const value = signal('')
    const { container } = render(<EmojiField value={value} openKey="t5" />)
    const tile = container.querySelector('.sh-emoji-field-tile')!
    fireEvent.click(tile)
    expect(container.querySelector('.sh-reaction-picker')).toBeTruthy()
    fireEvent.click(tile)
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
  })
})

describe('EmojiField in German', () => {
  it('labels, hints and the Remove control follow the UI language', async () => {
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('de')
    try {
      const value = signal('🏠')
      const { container, getByText } = render(<EmojiField value={value} openKey="de1" />)
      expect(container.querySelector('.sh-emoji-field-label')!.textContent).toBe('Symbol')
      expect(getByText('Tippe auf das Symbol, um es zu ändern')).toBeTruthy()
      expect(container.querySelector('.sh-emoji-field-clear')!.textContent?.trim()).toBe('Entfernen')
    } finally {
      await setLocale('en')
    }
  })
})
