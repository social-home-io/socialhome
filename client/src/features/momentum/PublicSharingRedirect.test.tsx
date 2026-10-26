import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const route = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({ useLocation: () => ({ route, url: '/momentum/public/sharing' }) }))

import PublicSharingRedirect from './PublicSharingRedirect'

describe('PublicSharingRedirect', () => {
  it('sends the old sharing page to Settings › Privacy › Public sharing', async () => {
    render(<PublicSharingRedirect />)
    await waitFor(() => expect(route).toHaveBeenCalledWith('/settings#public-sharing', true))
  })
})
