import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/preact'
import { Wordmark } from './Wordmark'

describe('Wordmark', () => {
  it('renders the logo mark, "Social", and an em-wrapped "Home"', () => {
    const { container } = render(<Wordmark />)
    expect(container.querySelector('svg')).toBeTruthy()
    const name = container.querySelector('.sh-wordmark__name')
    expect(name?.textContent).toContain('Social')
    expect(name?.querySelector('em')?.textContent).toBe('Home')
  })

  it('omits the motto by default and renders it when provided', () => {
    const { container, rerender } = render(<Wordmark />)
    expect(container.querySelector('.sh-wordmark__motto')).toBeNull()

    rerender(<Wordmark tagline="The social home for your household." />)
    const motto = container.querySelector('.sh-wordmark__motto')
    expect(motto?.textContent).toBe('The social home for your household.')
  })

  it('renders an anchor variant with the brand aria-label', () => {
    const { container } = render(<Wordmark as="a" href="/" />)
    const link = container.querySelector('a.sh-wordmark--link')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/')
    expect(link?.getAttribute('aria-label')).toBe('Social Home — home')
  })

  it('passes the size prop through to the logo mark', () => {
    const { container } = render(<Wordmark size={48} />)
    const svg = container.querySelector('svg')
    expect(svg?.getAttribute('width')).toBe('48')
  })
})

describe('Wordmark link under the HA ingress prefix', () => {
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
  })

  it('links home inside the ingress prefix (/api/hassio_ingress/<token>/)', async () => {
    const mod = await import('./Wordmark')
    const { container } = render(<mod.Wordmark as="a" />)
    expect(container.querySelector('a')?.getAttribute('href')).toBe('/api/hassio_ingress/tok/')
  })
})
