import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { CheckToggle } from './CheckToggle'

describe('CheckToggle', () => {
  it('is a labelled checkbox that reports its state', () => {
    const { getByRole } = render(
      <CheckToggle checked={false} label="Bought Milk" onChange={() => {}} />,
    )
    const box = getByRole('checkbox', { name: 'Bought Milk' })
    expect(box.tagName).toBe('BUTTON')
    expect(box.getAttribute('type')).toBe('button')
    expect(box.getAttribute('aria-checked')).toBe('false')
  })

  it('asks for the opposite state on click', () => {
    const onChange = vi.fn()
    const { getByRole, rerender } = render(
      <CheckToggle checked={false} label="Milk" onChange={onChange} />,
    )
    fireEvent.click(getByRole('checkbox'))
    expect(onChange).toHaveBeenLastCalledWith(true)
    rerender(<CheckToggle checked label="Milk" onChange={onChange} />)
    const box = getByRole('checkbox')
    expect(box.getAttribute('aria-checked')).toBe('true')
    expect(box.className).toContain('sh-check-toggle--checked')
    fireEvent.click(box)
    expect(onChange).toHaveBeenLastCalledWith(false)
  })

  it('shows the tick only when checked, hidden from assistive tech', () => {
    const { container, rerender } = render(
      <CheckToggle checked={false} label="Milk" onChange={() => {}} />,
    )
    const dot = container.querySelector('.sh-check-toggle__dot')!
    expect(dot.getAttribute('aria-hidden')).toBe('true')
    expect(dot.textContent).toBe('')
    rerender(<CheckToggle checked label="Milk" onChange={() => {}} />)
    expect(container.querySelector('.sh-check-toggle__dot')!.textContent).toBe('✓')
  })

  it('keeps a caller class and honours disabled', () => {
    const onChange = vi.fn()
    const { getByRole } = render(
      <CheckToggle checked={false} label="Milk" class="extra" disabled onChange={onChange} />,
    )
    const box = getByRole('checkbox') as HTMLButtonElement
    expect(box.className).toContain('sh-check-toggle')
    expect(box.className).toContain('extra')
    expect(box.disabled).toBe(true)
  })

  it('a disabledReason keeps it focusable but inert, and says why', () => {
    const onChange = vi.fn()
    const { getByRole } = render(
      <CheckToggle checked={false} label="Done: Tap" onChange={onChange} disabledReason="Read only" />,
    )
    const box = getByRole('checkbox', { name: 'Done: Tap' })
    expect(box.getAttribute('aria-disabled')).toBe('true')
    expect(box.hasAttribute('disabled')).toBe(false)
    expect(box.getAttribute('title')).toBe('Read only')
    fireEvent.click(box)
    expect(onChange).not.toHaveBeenCalled()
  })
})
