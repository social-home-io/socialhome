/**
 * Tests for the userPreferences store's WS handler wiring and API load.
 *
 * ``wireUserPreferencesWs()`` handles ``user.preferences_changed`` frames
 * and applies the changed fields only when the frame's user_id matches the
 * currently loaded preferences owner. We mock ``ws`` and ``api`` so we can
 * drive synthetic frames and assert signal mutations without a real backend.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

const mockGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
  },
}))

import { userPreferences, loadUserPreferences, wireUserPreferencesWs } from './userPreferences'

describe('wireUserPreferencesWs', () => {
  beforeEach(() => {
    userPreferences.value = {
      user_id: 'u1',
      hide_highlights: false,
      hide_momentum: false,
      hide_bazaar: false,
      hide_profile_from_followers: false,
    }
    Object.keys(handlers).forEach(k => delete handlers[k])
    wireUserPreferencesWs()
  })

  it('user.preferences_changed for matching user_id updates the signal', () => {
    handlers['user.preferences_changed']({
      data: { user_id: 'u1', changed: { hide_highlights: true } },
    })
    expect(userPreferences.value.hide_highlights).toBe(true)
    // Other fields must remain unchanged
    expect(userPreferences.value.hide_momentum).toBe(false)
    expect(userPreferences.value.hide_bazaar).toBe(false)
  })

  it('user.preferences_changed applies multiple changed fields at once', () => {
    handlers['user.preferences_changed']({
      data: { user_id: 'u1', changed: { hide_momentum: true, hide_bazaar: true } },
    })
    expect(userPreferences.value.hide_momentum).toBe(true)
    expect(userPreferences.value.hide_bazaar).toBe(true)
    expect(userPreferences.value.hide_highlights).toBe(false)
  })

  it('user.preferences_changed for a different user_id is ignored', () => {
    handlers['user.preferences_changed']({
      data: { user_id: 'u2', changed: { hide_highlights: true } },
    })
    expect(userPreferences.value.hide_highlights).toBe(false)
  })

  it('user.preferences_changed is a no-op when user_id is missing', () => {
    handlers['user.preferences_changed']({
      data: { changed: { hide_highlights: true } },
    })
    expect(userPreferences.value.hide_highlights).toBe(false)
  })
})

describe('loadUserPreferences', () => {
  beforeEach(() => {
    userPreferences.value = {
      user_id: '',
      hide_highlights: false,
      hide_momentum: false,
      hide_bazaar: false,
      hide_profile_from_followers: false,
    }
    mockGet.mockReset()
  })

  it('populates the signal from the API response', async () => {
    const prefs = { user_id: 'u1', hide_highlights: true, hide_momentum: false, hide_bazaar: true, hide_profile_from_followers: true }
    mockGet.mockResolvedValueOnce(prefs)
    await loadUserPreferences()
    expect(userPreferences.value).toEqual(prefs)
  })

  it('leaves defaults in place when the API call fails', async () => {
    mockGet.mockRejectedValueOnce(new Error('network error'))
    await loadUserPreferences()
    expect(userPreferences.value.user_id).toBe('')
    expect(userPreferences.value.hide_highlights).toBe(false)
  })
})
