import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

import { SpaceLinksStrip } from './SpaceLinksStrip'

describe('SpaceLinksStrip', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceLinksStrip')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceLinksStrip).toBe('function')
  })

  it('renders http(s) links and skips a link whose url is not one', async () => {
    apiGet.mockResolvedValue({
      links: [
        { id: 'a', label: 'Wiki', url: 'https://wiki.example/', position: 0 },
        { id: 'b', label: 'Evil', url: 'javascript:alert(1)', position: 1 },
        { id: 'c', label: 'Data', url: 'data:text/html,x', position: 2 },
      ],
    })
    const { container } = render(<SpaceLinksStrip spaceId="sp-1" />)
    await waitFor(() => {
      expect(container.querySelectorAll('a').length).toBe(1)
    })
    const a = container.querySelector('a') as HTMLAnchorElement
    expect(a.textContent).toBe('Wiki')
    expect(a.getAttribute('href')).toBe('https://wiki.example/')
    expect(container.textContent).not.toContain('Evil')
  })
})
