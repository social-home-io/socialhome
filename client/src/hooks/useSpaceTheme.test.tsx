import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const get = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => get(...a) } }))

import { useSpaceTheme } from './useSpaceTheme'

function Probe({ id }: { id: string }) {
  useSpaceTheme(id)
  return null
}

const root = document.documentElement

beforeEach(() => {
  get.mockReset()
  root.removeAttribute('style')
  root.removeAttribute('data-space-theme')
})

describe('useSpaceTheme', () => {
  it('leaves the brand default colours to the active light / dark palette', async () => {
    // Every space row carries the column defaults. Pinning them inline on
    // <html> would force the LIGHT hearth over the dark theme's lifted one
    // (4.3:1 text on dark surfaces instead of 5.6:1).
    get.mockResolvedValue({ primary_color: '#D2542A', accent_color: '#c8902f' })
    render(<Probe id="sp-1" />)
    await waitFor(() => expect(root.getAttribute('data-space-theme')).toBe('sp-1'))
    expect(root.style.getPropertyValue('--sh-primary')).toBe('')
    expect(root.style.getPropertyValue('--sh-accent')).toBe('')
  })

  it('applies a colour the space admin actually picked, and rolls it back', async () => {
    get.mockResolvedValue({ primary_color: '#3366ff', accent_color: '#C8902F' })
    const { unmount } = render(<Probe id="sp-2" />)
    await waitFor(() => expect(root.style.getPropertyValue('--sh-primary')).toBe('#3366ff'))
    expect(root.style.getPropertyValue('--sh-accent')).toBe('')
    unmount()
    expect(root.style.getPropertyValue('--sh-primary')).toBe('')
    expect(root.hasAttribute('data-space-theme')).toBe(false)
  })

  it('pairs a custom primary with readable on-fill ink per theme, and rolls it back', async () => {
    // #3A7D6E under the dark theme's dark ink is ~3.9:1 — the hook picks
    // white there; light mode keeps the cream --sh-bg ink.
    get.mockResolvedValue({ primary_color: '#3A7D6E' })
    const { unmount } = render(<Probe id="sp-3" />)
    await waitFor(() => expect(root.style.getPropertyValue('--sh-primary')).toBe('#3A7D6E'))
    expect(root.style.getPropertyValue('--sh-on-primary-fill-light')).toBe('var(--sh-bg)')
    expect(root.style.getPropertyValue('--sh-on-primary-fill-dark')).toBe('#fff')
    expect(root.style.getPropertyValue('--sh-primary-fill-hover-dark'))
      .toBe('color-mix(in srgb, var(--sh-primary-fill) 85%, #000)')
    unmount()
    for (const p of ['--sh-on-primary-fill-light', '--sh-on-primary-fill-dark',
      '--sh-primary-fill-hover-light', '--sh-primary-fill-hover-dark']) {
      expect(root.style.getPropertyValue(p)).toBe('')
    }
  })

  it('sets no on-fill ink for the brand default primary (CSS defaults apply)', async () => {
    get.mockResolvedValue({ primary_color: '#d2542a' })
    render(<Probe id="sp-4" />)
    await waitFor(() => expect(root.getAttribute('data-space-theme')).toBe('sp-4'))
    expect(root.style.getPropertyValue('--sh-on-primary-fill-dark')).toBe('')
  })

  it('maps a stored font id to a CSS stack, and leaves "system" to the app font', async () => {
    // space_themes.font_family is an id (CHECK IN system/serif/rounded/mono);
    // painting it raw produced ``--sh-font-family: system`` — not a font.
    get.mockResolvedValue({ primary_color: '#3366ff', font_family: 'system' })
    const first = render(<Probe id="sp-5" />)
    await waitFor(() => expect(root.getAttribute('data-space-theme')).toBe('sp-5'))
    expect(root.style.getPropertyValue('--sh-font-family')).toBe('')
    first.unmount()

    get.mockResolvedValue({ font_family: 'serif' })
    const { unmount } = render(<Probe id="sp-6" />)
    await waitFor(() => expect(root.style.getPropertyValue('--sh-font-family')).toContain('Georgia'))
    unmount()
    expect(root.style.getPropertyValue('--sh-font-family')).toBe('')

    get.mockResolvedValue({ font_family: 'Comic Sans' })
    render(<Probe id="sp-7" />)
    await waitFor(() => expect(root.getAttribute('data-space-theme')).toBe('sp-7'))
    expect(root.style.getPropertyValue('--sh-font-family')).toBe('')
  })
})
