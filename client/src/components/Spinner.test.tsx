import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { Spinner } from './Spinner'

describe('Spinner', () => {
  it('renders the brand LogoMark in the loading state', () => {
    const { container } = render(<Spinner />)
    const svg = container.querySelector('svg.sh-logo')
    expect(svg).toBeTruthy()
    expect(svg?.classList.contains('sh-logo--loading')).toBe(true)
  })

  it('upscales legacy dot-sized values to a logo-readable size', () => {
    const { container } = render(<Spinner size={8} />)
    const svg = container.querySelector('svg') as SVGElement
    // size=8 (legacy dot diameter) → ≥24px logo so it stays readable.
    const w = parseInt(svg.getAttribute('width') || '0', 10)
    expect(w).toBeGreaterThanOrEqual(24)
  })

  it('honours an explicit logo size when callers pass ≥24', () => {
    const { container } = render(<Spinner size={48} />)
    const svg = container.querySelector('svg') as SVGElement
    expect(svg.getAttribute('width')).toBe('48')
  })

  it('exposes role=status with the loading label for a11y', () => {
    const { container } = render(<Spinner label="Working" />)
    const wrap = container.querySelector('[role="status"]') as HTMLElement
    expect(wrap).toBeTruthy()
    expect(wrap.getAttribute('aria-label')).toBe('Working')
  })
})
