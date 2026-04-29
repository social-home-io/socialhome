import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { Spinner } from './Spinner'

describe('Spinner', () => {
  it('renders three pulsing dots', () => {
    const { container } = render(<Spinner />)
    const dots = container.querySelectorAll('.sh-spinner-dot')
    expect(dots.length).toBe(3)
  })

  it('drives the dot diameter from the size prop via a custom property', () => {
    const { container } = render(<Spinner size={12} />)
    const wrap = container.firstElementChild as HTMLElement
    // Custom properties land on the inline style attribute as `--name`.
    expect(wrap.getAttribute('style')).toContain('--sh-spinner-dot: 12px')
  })

  it('exposes role=status with the loading label for a11y', () => {
    const { container } = render(<Spinner label="Working" />)
    const wrap = container.querySelector('[role="status"]') as HTMLElement
    expect(wrap).toBeTruthy()
    expect(wrap.getAttribute('aria-label')).toBe('Working')
  })
})
