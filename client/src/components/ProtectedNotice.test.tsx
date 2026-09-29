import { describe, it, expect, afterEach } from 'vitest'
import { render } from '@testing-library/preact'
import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { ProtectedNotice, isRestricted } from './ProtectedNotice'

function me(extra: Partial<User> = {}): User {
  return {
    user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
    picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    ...extra,
  }
}

afterEach(() => { currentUser.value = null })

describe('isRestricted', () => {
  it('is false when signed out', () => {
    expect(isRestricted('bazaar')).toBe(false)
  })

  it('is false for an adult (no restrictions on /api/me)', () => {
    currentUser.value = me({ protected: false, restrictions: [] })
    expect(isRestricted('bazaar')).toBe(false)
  })

  it('is false for an older server that sends no restrictions field', () => {
    currentUser.value = me()
    expect(isRestricted('api_tokens')).toBe(false)
  })

  it('reads the capability list the server reports', () => {
    currentUser.value = me({ protected: true, restrictions: ['bazaar', 'api_tokens'] })
    expect(isRestricted('bazaar')).toBe(true)
    expect(isRestricted('api_tokens')).toBe(true)
    expect(isRestricted('calendar_feeds')).toBe(false)
  })
})

describe('ProtectedNotice', () => {
  it('explains the surface, points at a guardian, and links to the settings section', () => {
    const { container, getByRole } = render(<ProtectedNotice capability="bazaar" />)
    const note = getByRole('note')
    expect(note.textContent).toContain('Your account is protected by your household')
    expect(note.textContent).toContain('Bazaar')
    expect(note.textContent).toContain('Ask a guardian')
    const link = container.querySelector('a')
    expect(link?.getAttribute('href')).toBe('/settings#protection')
    // Never an age or a minor flag.
    expect(note.textContent?.toLowerCase()).not.toContain('minor')
    expect(note.textContent?.toLowerCase()).not.toContain('age')
  })

  it('can drop the link', () => {
    const { container } = render(<ProtectedNotice capability="api_tokens" hideLink />)
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('API tokens')
  })
})
