import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }))
vi.mock('@/api', () => ({ api: apiMock }))

import { currentUser } from '@/store/auth'
import { myProtection, myProtectionFailed } from '@/store/protection'
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

beforeEach(() => {
  apiMock.get.mockReset()
  myProtection.value = null
  myProtectionFailed.value = false
})
afterEach(() => {
  currentUser.value = null
  window.location.hash = ''
})

describe('ProtectedAccountSection — compact banner (decision 10)', () => {
  it('renders nothing (and fetches nothing) for an unprotected account', () => {
    currentUser.value = me({ protected: false, restrictions: [] })
    const { container } = render(<ProtectedAccountSection />)
    expect(container.innerHTML).toBe('')
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('names the guardians in one line and keeps the list behind "See what\'s limited"', async () => {
    currentUser.value = me({ protected: true, restrictions: ALL })
    apiMock.get.mockResolvedValueOnce({
      protected: true,
      restrictions: ALL,
      guardians: [
        { user_id: 'u-mom', username: 'melanie', display_name: 'Melanie' },
        { user_id: 'u-dad', username: 'pascal', display_name: '' },
      ],
    })
    const { container, getByRole, findByText } = render(<ProtectedAccountSection />)
    expect(getByRole('heading', { level: 2 }).textContent).toBe('Your account is protected')
    // A display name, else the username; joined for the language.
    await findByText('Melanie and pascal look after it.')
    expect(apiMock.get).toHaveBeenCalledWith('/api/me/protection')
    expect(container.querySelector('#protection')).toBeTruthy()

    const toggle = getByRole('button', { name: "See what's limited" })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    const details = container.querySelector('#sh-protected-details') as HTMLElement
    expect(details.hidden).toBe(true)
    expect(toggle.getAttribute('aria-controls')).toBe('sh-protected-details')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.textContent).toBe("Hide what's limited")
    expect(details.hidden).toBe(false)
    // Restrictions come from /api/me — every one listed.
    expect(details.querySelectorAll('li')).toHaveLength(ALL.length)
  })

  it('says "looks after" for a single guardian', async () => {
    currentUser.value = me({ protected: true, restrictions: ['bazaar'] })
    apiMock.get.mockResolvedValueOnce({
      protected: true,
      restrictions: ['bazaar'],
      guardians: [{ user_id: 'u-mom', username: 'mom', display_name: 'Mom' }],
    })
    const { findByText } = render(<ProtectedAccountSection />)
    await findByText('Mom looks after it.')
  })

  it('opens the disclosure when reached through /settings#protection', () => {
    window.location.hash = '#protection'
    currentUser.value = me({ protected: true, restrictions: ['bazaar'] })
    apiMock.get.mockReturnValue(new Promise(() => {}))
    const { getByRole, container } = render(<ProtectedAccountSection />)
    expect(getByRole('button', { name: "Hide what's limited" }).getAttribute('aria-expanded')).toBe('true')
    expect((container.querySelector('#sh-protected-details') as HTMLElement).hidden).toBe(false)
  })

  it('says who to ask when no guardian is assigned', async () => {
    currentUser.value = me({ protected: true, restrictions: ALL })
    apiMock.get.mockResolvedValueOnce({ protected: true, restrictions: ALL, guardians: [] })
    const { findByText } = render(<ProtectedAccountSection />)
    await findByText(/Ask a household admin/)
  })

  it('keeps the banner and the restriction list when the guardian lookup fails', async () => {
    currentUser.value = me({ protected: true, restrictions: ['bazaar'] })
    apiMock.get.mockRejectedValueOnce(new Error('boom'))
    const { container, getByTestId } = render(<ProtectedAccountSection />)
    await waitFor(() => {
      expect(container.textContent).toContain("Couldn't load your guardians")
    })
    expect(getByTestId('protected-guardians').textContent).toBe('Your household looks after it.')
    expect(container.querySelectorAll('.sh-protected-account__list li')).toHaveLength(1)
  })
})
