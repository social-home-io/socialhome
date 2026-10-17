import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LocationLink } from './LocationLink'

describe('LocationLink', () => {
  it('links http(s) URLs directly', () => {
    const { container } = render(
      <LocationLink value="https://meet.example.com/abc" />,
    )
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://meet.example.com/abc')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('puts the class prop on the link', () => {
    const { container } = render(<LocationLink value="Library" class="sh-event-location" />)
    expect(container.querySelector('a')!.className).toBe('sh-event-location')
  })

  it('upgrades a bare www.example URL to https://', () => {
    const { container } = render(<LocationLink value="www.example.com/x" />)
    expect(container.querySelector('a')!.getAttribute('href')).toBe(
      'https://www.example.com/x',
    )
  })

  it('wraps free-form text in a Google Maps search URL', () => {
    const { container } = render(<LocationLink value="123 Main St" />)
    const href = container.querySelector('a')!.getAttribute('href')!
    expect(href).toBe(
      'https://www.google.com/maps/search/?api=1&query=123%20Main%20St',
    )
  })

  it('sets an accessible label that includes the location', () => {
    const { container } = render(<LocationLink value="Pier 39" />)
    expect(container.querySelector('a')!.getAttribute('aria-label')).toBe(
      'Location: Pier 39',
    )
  })

  it('renders nothing for blank input', () => {
    const { container } = render(<LocationLink value="   " />)
    expect(container.querySelector('a')).toBeNull()
  })

  it('stops click propagation so the wrapping row toggle does not fire', () => {
    let outerFired = false
    const { getByRole } = render(
      <div onClick={() => { outerFired = true }}>
        <LocationLink value="https://example.com" />
      </div>,
    )
    const link = getByRole('link') as HTMLAnchorElement
    link.addEventListener('click', (e) => e.preventDefault())
    link.click()
    expect(outerFired).toBe(false)
  })
})
