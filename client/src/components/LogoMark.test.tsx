import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LogoMark } from './LogoMark'

describe('LogoMark', () => {
  it('renders an svg with the brand viewBox and default aria label', () => {
    const { container } = render(<LogoMark />)
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('viewBox')).toBe('0 0 64 64')
    expect(svg?.getAttribute('role')).toBe('img')
    expect(svg?.getAttribute('aria-label')).toBe('Social Home')
  })

  it('honours the size prop', () => {
    const { container } = render(<LogoMark size={48} />)
    const svg = container.querySelector('svg')
    expect(svg?.getAttribute('width')).toBe('48')
    expect(svg?.getAttribute('height')).toBe('48')
  })

  it('accepts a custom aria label and class', () => {
    const { container } = render(
      <LogoMark ariaLabel="Home" class="brand-mark" />,
    )
    const svg = container.querySelector('svg')
    expect(svg?.getAttribute('aria-label')).toBe('Home')
    expect(svg?.classList.contains('sh-logo')).toBe(true)
    expect(svg?.classList.contains('brand-mark')).toBe(true)
  })

  it('toggles the sh-logo--loading class when loading=true', () => {
    const { container, rerender } = render(<LogoMark />)
    const svg = container.querySelector('svg')!
    expect(svg.classList.contains('sh-logo--loading')).toBe(false)
    rerender(<LogoMark loading />)
    expect(container.querySelector('svg')!.classList.contains('sh-logo--loading')).toBe(true)
  })
})
