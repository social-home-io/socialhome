import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({ api: { get: apiGet, post: vi.fn(), delete: vi.fn() } }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SaveListingButton } from './SaveListingButton'

describe('SaveListingButton', () => {
  beforeEach(() => apiGet.mockReset())

  it('probes GET /save when nobody told it the saved state', async () => {
    apiGet.mockResolvedValue({ saved: true })
    const { container } = render(<SaveListingButton postId="p1" />)
    await waitFor(() =>
      expect(container.querySelector('button')?.getAttribute('aria-pressed')).toBe('true'),
    )
    expect(apiGet).toHaveBeenCalledWith('/api/bazaar/p1/save')
  })

  it('a card that already read the saved flag makes no read of its own', () => {
    // One GET per card per listing: the flag rides on GET /api/bazaar/{id}.
    const { container } = render(<SaveListingButton postId="p1" initialSaved={true} />)
    const btn = container.querySelector('button')!
    expect(btn.getAttribute('aria-pressed')).toBe('true')
    expect(btn.hasAttribute('disabled')).toBe(false)
    expect(apiGet).not.toHaveBeenCalled()
  })
})
