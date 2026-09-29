import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }))
vi.mock('@/api', () => ({ api: apiMock }))

import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { ProtectedAccountSection } from './ProtectedAccountSection'

const ALL = [
  'bazaar', 'public_spaces', 'public_moments', 'public_links',
  'api_tokens', 'calendar_feeds',
]

function me(extra: Partial<User> = {}): User {
  return {
    user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
    picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    ...extra,
  }
}

beforeEach(() => { apiMock.get.mockReset() })
afterEach(() => { currentUser.value = null })

describe('ProtectedAccountSection', () => {
  it('renders nothing (and fetches nothing) for an unprotected account', () => {
    currentUser.value = me({ protected: false, restrictions: [] })
    const { container } = render(<ProtectedAccountSection />)
    expect(container.innerHTML).toBe('')
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('lists every restriction at once and the guardians once loaded', async () => {
    currentUser.value = me({ protected: true, restrictions: ALL })
    apiMock.get.mockResolvedValueOnce({
      protected: true,
      restrictions: ALL,
      guardians: [{ user_id: 'u-mom', username: 'mom', display_name: 'Mom' }],
    })
    const { container, findByText, getByRole } = render(<ProtectedAccountSection />)
    expect(getByRole('heading', { level: 2 }).textContent).toContain('Your account is protected')
    // Restrictions come from /api/me — no wait for the second request.
    expect(container.querySelectorAll('.sh-protected-account__list li')).toHaveLength(ALL.length)
    await findByText('Mom')
    expect(apiMock.get).toHaveBeenCalledWith('/api/me/protection')
    expect(container.querySelector('#protection')).toBeTruthy()
    expect(container.textContent).toContain('@mom')
  })

  it('says who to ask when no guardian is assigned', async () => {
    currentUser.value = me({ protected: true, restrictions: ALL })
    apiMock.get.mockResolvedValueOnce({ protected: true, restrictions: ALL, guardians: [] })
    const { findByText } = render(<ProtectedAccountSection />)
    await findByText(/Ask a household admin/)
  })

  it('keeps the restriction list when the guardian lookup fails', async () => {
    currentUser.value = me({ protected: true, restrictions: ['bazaar'] })
    apiMock.get.mockRejectedValueOnce(new Error('boom'))
    const { container } = render(<ProtectedAccountSection />)
    await waitFor(() => {
      expect(container.textContent).toContain("Couldn't load your guardians")
    })
    expect(container.querySelectorAll('.sh-protected-account__list li')).toHaveLength(1)
  })
})
