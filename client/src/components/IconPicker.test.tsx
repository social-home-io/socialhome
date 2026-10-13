import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { IconPicker, type IconPickerGroup } from './IconPicker'

const GROUPS: readonly IconPickerGroup[] = [
  {
    label: 'Places',
    presets: [
      { emoji: '🏠', key: 'home', label: 'zones.icon.home' },
      { emoji: '🏫', key: 'school', label: 'zones.icon.school' },
    ],
  },
  {
    label: 'Fun',
    presets: [{ emoji: '🎵', key: 'music', label: 'zones.icon.music' }],
  },
]

const renderPicker = (value: string | null, onChange = vi.fn()) => ({
  onChange,
  ...render(
    <IconPicker value={value} onChange={onChange} id="t" groups={GROUPS}
                legend="Icon" noneLabel="No icon" customLabel="Other emoji" />,
  ),
})

describe('IconPicker', () => {
  it('renders one labelled group per preset list with localised tile names', () => {
    const { getByRole } = renderPicker(null)
    expect(getByRole('group', { name: 'Places' })).toBeTruthy()
    expect(getByRole('group', { name: 'Fun' })).toBeTruthy()
    expect(getByRole('button', { name: 'Home' })).toBeTruthy()
    expect(getByRole('button', { name: 'School' })).toBeTruthy()
    expect(getByRole('button', { name: 'Music' })).toBeTruthy()
  })

  it('marks the current preset pressed and reports a new pick', () => {
    const { getByRole, onChange } = renderPicker('🏠')
    expect(getByRole('button', { name: 'Home' }).getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: 'School' }).getAttribute('aria-pressed')).toBe('false')
    expect(getByRole('button', { name: 'No icon' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(getByRole('button', { name: 'School' }))
    expect(onChange).toHaveBeenCalledWith('🏫')
  })

  it('"No icon" is pressed for null and reports null', () => {
    const { getByRole, onChange } = renderPicker('🏠')
    fireEvent.click(getByRole('button', { name: 'No icon' }))
    expect(onChange).toHaveBeenCalledWith(null)
    const again = renderPicker(null)
    expect(again.getAllByRole('button', { name: 'No icon' })[1].getAttribute('aria-pressed')).toBe('true')
  })

  it('shows a non-preset emoji in the custom field and no preset pressed', () => {
    const { container, getByRole } = renderPicker('🦄')
    expect(getByRole('button', { name: 'Home' }).getAttribute('aria-pressed')).toBe('false')
    expect(container.querySelector('.sh-emoji-field')?.textContent).toContain('🦄')
  })

  it('renders one tile per preset', () => {
    const { container } = renderPicker(null)
    expect(container.querySelectorAll('.sh-icon-picker__grid button').length).toBe(3)
  })

  it('gives every preset tile the class the 44 px tap-target rule targets', () => {
    const { container } = renderPicker(null)
    const tiles = Array.from(container.querySelectorAll('.sh-icon-picker__grid button'))
    expect(tiles.length).toBe(3)
    for (const tile of tiles) {
      expect(tile.classList.contains('sh-icon-picker__btn')).toBe(true)
    }
  })

  it('reports a non-preset emoji chosen through the custom field', () => {
    const { container, onChange } = renderPicker(null)
    fireEvent.click(container.querySelector('.sh-emoji-field-tile')!)
    fireEvent.input(container.querySelector('.sh-reaction-search')!, { target: { value: 'fire' } })
    const fire = Array.from(container.querySelectorAll('.sh-reaction-grid .sh-emoji-btn'))
      .find(b => b.textContent === '🔥')!
    fireEvent.click(fire)
    expect(onChange).toHaveBeenCalledWith('🔥')
    // Clean up the module-level picker search for later tests.
    fireEvent.click(container.querySelector('.sh-emoji-field-tile')!)
    fireEvent.input(container.querySelector('.sh-reaction-search')!, { target: { value: '' } })
  })

  it('clears a custom emoji to null via the field\'s Remove', () => {
    const { container, onChange } = renderPicker('🦄')
    fireEvent.click(container.querySelector('.sh-emoji-field-clear')!)
    expect(onChange).toHaveBeenCalledWith(null)
  })
})
