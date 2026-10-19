import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, render } from '@testing-library/preact'
import { useMediaQuery } from './useMediaQuery'

function Probe({ q }: { q: string }) {
  return <span>{useMediaQuery(q) ? 'yes' : 'no'}</span>
}

const original = window.matchMedia
afterEach(() => { cleanup(); window.matchMedia = original })

describe('useMediaQuery', () => {
  it('false without matchMedia', () => {
    window.matchMedia = undefined as unknown as typeof window.matchMedia
    expect(render(<Probe q="(min-width: 1px)" />).container.textContent).toBe('no')
  })

  it('follows the query as it changes', () => {
    let matches = true
    let listener: (() => void) | null = null
    let removed = 0
    window.matchMedia = ((q: string) => ({
      get matches() { return matches }, media: q,
      addEventListener: (_: string, l: () => void) => { listener = l },
      removeEventListener: () => { removed++ },
    })) as unknown as typeof window.matchMedia
    const r = render(<Probe q="(min-width: 769px)" />)
    expect(r.container.textContent).toBe('yes')
    matches = false
    act(() => listener?.())
    expect(r.container.textContent).toBe('no')
    act(() => { r.unmount() })
    expect(removed).toBeGreaterThan(0)
  })
})
