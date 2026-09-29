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
})
