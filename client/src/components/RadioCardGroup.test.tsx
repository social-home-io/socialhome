import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

import { RadioCardGroup, type RadioCardOption } from './RadioCardGroup'
import { joinOptionsForVisibility } from './spaceModeOptions'

const OPTS: RadioCardOption[] = [
  { value: 'a', icon: '🔒', title: 'Alpha', subtitle: 'first option' },
  { value: 'b', icon: '🏠', title: 'Beta', subtitle: 'second option' },
]

describe('RadioCardGroup', () => {
  it('renders the legend, every option, and groups the radios by name', () => {
    const { container, getByText } = render(
      <RadioCardGroup
        legend="Pick one" name="grp" value="a" options={OPTS} onChange={() => {}}
      />,
    )
    expect(getByText('Pick one')).toBeTruthy()
    expect(getByText('Alpha')).toBeTruthy()
    expect(getByText('first option')).toBeTruthy()
    expect(getByText('Beta')).toBeTruthy()
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    expect(radios).toHaveLength(2)
    expect([...radios].every(r => r.name === 'grp')).toBe(true)
  })

  it('marks the selected option (checked radio + selected class)', () => {
    const { container } = render(
      <RadioCardGroup
        legend="L" name="g" value="b" options={OPTS} onChange={() => {}}
      />,
    )
    const cards = container.querySelectorAll('.sh-radio-card')
    expect(cards[0].classList.contains('sh-radio-card--selected')).toBe(false)
    expect(cards[1].classList.contains('sh-radio-card--selected')).toBe(true)
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    expect(radios[0].checked).toBe(false)
    expect(radios[1].checked).toBe(true)
  })

  it('calls onChange with the option value when a card is selected', () => {
    const onChange = vi.fn()
    const { container } = render(
      <RadioCardGroup
        legend="L" name="g" value="a" options={OPTS} onChange={onChange}
      />,
    )
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    fireEvent.click(radios[1])
    expect(onChange).toHaveBeenCalledWith('b')
  })

  it('disables every radio when the group is disabled', () => {
    const { container } = render(
      <RadioCardGroup
        legend="L" name="g" value="a" options={OPTS} onChange={() => {}} disabled
      />,
    )
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    expect([...radios].every(r => r.disabled)).toBe(true)
  })

  it('disables individual options flagged disabled', () => {
    const opts: RadioCardOption[] = [
      { value: 'a', icon: '', title: 'A', subtitle: '' },
      { value: 'b', icon: '', title: 'B', subtitle: '', disabled: true },
    ]
    const { container } = render(
      <RadioCardGroup legend="L" name="g" value="a" options={opts} onChange={() => {}} />,
    )
    const radios = container.querySelectorAll<HTMLInputElement>('input[type="radio"]')
    expect(radios[0].disabled).toBe(false)
    expect(radios[1].disabled).toBe(true)
    expect(container.querySelectorAll('.sh-radio-card--disabled')).toHaveLength(1)
  })
})

describe('joinOptionsForVisibility', () => {
  it('private → only invite_only is enabled (others shown but disabled)', () => {
    const opts = joinOptionsForVisibility('private')
    const byVal = Object.fromEntries(opts.map(o => [o.value, o]))
    expect(byVal['invite_only'].disabled).toBeFalsy()
    expect(byVal['request'].disabled).toBe(true)
    expect(byVal['open'].disabled).toBe(true)
  })

  it('household → all join modes enabled', () => {
    const opts = joinOptionsForVisibility('household')
    expect(opts.every(o => !o.disabled)).toBe(true)
  })
})

describe('RadioCardGroup fontFamily', () => {
  it('renders an option title + icon in its own font stack, others unstyled', () => {
    const { getByText } = render(
      <RadioCardGroup
        legend="Font" name="f" value="a"
        options={[
          { value: 'a', icon: 'Aa', title: 'Serif', subtitle: 's', fontFamily: 'Georgia, serif' },
          { value: 'b', icon: 'Bb', title: 'Plain', subtitle: 'p' },
        ]}
        onChange={() => {}}
      />,
    )
    expect(getByText('Serif').style.fontFamily).toContain('Georgia')
    expect(getByText('Aa').style.fontFamily).toContain('Georgia')
    expect(getByText('Plain').getAttribute('style')).toBeNull()
  })
})
