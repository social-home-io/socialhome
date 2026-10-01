import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { ChipRadioGroup } from './ChipRadioGroup'

const OPTIONS = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta', title: 'The second' },
  { value: 'c', label: 'Gamma' },
]

function Harness({ onChange }: { onChange?: (v: string) => void }) {
  const [value, setValue] = useState('b')
  return (
    <ChipRadioGroup
      options={OPTIONS}
      value={value}
      ariaLabel="Letters"
      onChange={v => { setValue(v); onChange?.(v) }}
    />
  )
}

function radios(container: Element): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[role="radio"]'))
}

describe('ChipRadioGroup', () => {
  it('renders a labelled radiogroup with the checked option as the only Tab stop', () => {
    const { getByRole, container } = render(<Harness />)
    expect(getByRole('radiogroup', { name: 'Letters' })).toBeTruthy()
    const r = radios(container)
    expect(r.map(x => x.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(r.map(x => x.tabIndex)).toEqual([-1, 0, -1])
    expect(r[1].getAttribute('title')).toBe('The second')
    expect(r[1].className).toContain('sh-locale-option--active')
  })

  it('arrow keys move focus and selection, wrapping at both ends', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness onChange={onChange} />)
    let r = radios(container)
    fireEvent.keyDown(r[1], { key: 'ArrowRight' })
    expect(onChange).toHaveBeenLastCalledWith('c')
    r = radios(container)
    expect(document.activeElement).toBe(r[2])
    fireEvent.keyDown(r[2], { key: 'ArrowDown' })
    expect(onChange).toHaveBeenLastCalledWith('a')
    fireEvent.keyDown(radios(container)[0], { key: 'ArrowLeft' })
    expect(onChange).toHaveBeenLastCalledWith('c')
    fireEvent.keyDown(radios(container)[2], { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith('b')
  })

  it('Home and End jump to the first and last option', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness onChange={onChange} />)
    fireEvent.keyDown(radios(container)[1], { key: 'End' })
    expect(onChange).toHaveBeenLastCalledWith('c')
    expect(document.activeElement).toBe(radios(container)[2])
    fireEvent.keyDown(radios(container)[2], { key: 'Home' })
    expect(onChange).toHaveBeenLastCalledWith('a')
    expect(document.activeElement).toBe(radios(container)[0])
  })

  it('other keys do nothing', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness onChange={onChange} />)
    fireEvent.keyDown(radios(container)[1], { key: 'x' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('click selects', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness onChange={onChange} />)
    fireEvent.click(radios(container)[0])
    expect(onChange).toHaveBeenLastCalledWith('a')
  })

  it('keeps focus on the checked option when the value is changed from outside while focused', () => {
    // A reverted save flips ``value`` back; focus must follow so the
    // group's single Tab stop stays the focused element.
    const { container, rerender } = render(
      <ChipRadioGroup options={OPTIONS} value="c" ariaLabel="Letters" onChange={() => {}} />,
    )
    radios(container)[2].focus()
    rerender(
      <ChipRadioGroup options={OPTIONS} value="a" ariaLabel="Letters" onChange={() => {}} />,
    )
    expect(document.activeElement).toBe(radios(container)[0])
  })

  it('does not steal focus when the value changes while focus is elsewhere', () => {
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    const { rerender } = render(
      <ChipRadioGroup options={OPTIONS} value="c" ariaLabel="Letters" onChange={() => {}} />,
    )
    rerender(
      <ChipRadioGroup options={OPTIONS} value="a" ariaLabel="Letters" onChange={() => {}} />,
    )
    expect(document.activeElement).toBe(outside)
    outside.remove()
  })

  it('supports aria-labelledby instead of aria-label', () => {
    const { getByRole } = render(
      <div>
        <h3 id="hdr">Pick one</h3>
        <ChipRadioGroup options={OPTIONS} value="a" labelledBy="hdr" onChange={() => {}} />
      </div>,
    )
    expect(getByRole('radiogroup', { name: 'Pick one' })).toBeTruthy()
  })
})
