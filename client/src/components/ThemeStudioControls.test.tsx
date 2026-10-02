import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

import { FONT_STACKS } from '@/utils/themeFonts'
import {
  THEME_PRESETS, ThemeFontPicker, ThemePresetRow, fontOptions,
} from './ThemeStudioControls'

const SYSTEM = { title: 'Same as household', hint: 'h', stack: 'Georgia, serif' }

describe('ThemeFontPicker', () => {
  it('offers the four schema ids, each titled in its own stack', () => {
    const opts = fontOptions(SYSTEM)
    expect(opts.map(o => o.value)).toEqual(['system', 'serif', 'rounded', 'mono'])
    for (const o of opts.slice(1)) {
      expect(o.fontFamily).toBe(FONT_STACKS[o.value as keyof typeof FONT_STACKS])
    }
  })

  it('labels + previews "system" the way the scope says (no override)', () => {
    const [sys] = fontOptions(SYSTEM)
    expect(sys).toMatchObject({ value: 'system', title: 'Same as household', fontFamily: 'Georgia, serif' })
  })

  it('reports the id (never a CSS stack) on change', () => {
    const onChange = vi.fn()
    const { container } = render(
      <ThemeFontPicker name="f" value="system" onChange={onChange} system={SYSTEM} />,
    )
    const mono = container.querySelector<HTMLInputElement>('input[value="mono"]')!
    fireEvent.click(mono)
    expect(onChange).toHaveBeenCalledWith('mono')
  })
})

describe('ThemePresetRow', () => {
  it('renders every preset and applies the clicked one', () => {
    const onApply = vi.fn()
    const { getByText } = render(<ThemePresetRow onApply={onApply} />)
    fireEvent.click(getByText('High contrast'))
    expect(onApply).toHaveBeenCalledWith(THEME_PRESETS.find(p => p.id === 'high_contrast'))
    expect(getByText('Calm')).toBeTruthy()
  })
})
