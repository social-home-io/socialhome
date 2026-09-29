/**
 * ``me.protection_changed`` — the server tells only this account that its
 * protection (or its guardians / blocks) changed. The SPA must reload
 * ``/api/me`` and ``/api/me/protection`` right away, not on the next page
 * load, so a newly protected account sees its limits at once and a lifted
 * one gets its features back.
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

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), patch: vi.fn() },
}))
vi.mock('@/api', () => ({ api: apiMock, _resetApiLoggedOut: vi.fn() }))

import { currentUser } from './auth'
import {
  loadMyProtection,
  myProtection,
  myProtectionFailed,
  wireProtectionWs,
} from './protection'

const KID = {
  user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  tz: 'Europe/Zurich',
}
const SUMMARY = {
  protected: true,
  restrictions: ['bazaar'],
  guardians: [{ user_id: 'u-mom', username: 'mom', display_name: 'Mom' }],
}

function routeGets(me: object, summary: object | Error) {
  apiMock.get.mockImplementation((url: string) => {
    if (url === '/api/me') return Promise.resolve(me)
    if (url === '/api/me/protection') {
      return summary instanceof Error ? Promise.reject(summary) : Promise.resolve(summary)
    }
    return Promise.reject(new Error(`unexpected ${url}`))
  })
}

describe('store/protection', () => {
  beforeEach(() => {
    Object.keys(handlers).forEach((k) => delete handlers[k])
    apiMock.get.mockReset()
    currentUser.value = { ...KID, protected: false, restrictions: [] }
    myProtection.value = null
    myProtectionFailed.value = false
    wireProtectionWs()
  })

  it('reloads /api/me and the summary when protection is turned on', async () => {
    routeGets({ ...KID, protected: true, restrictions: ['bazaar'] }, SUMMARY)
    handlers['me.protection_changed']({ data: { type: 'me.protection_changed' } })
    await vi.waitFor(() => expect(myProtection.value).toEqual(SUMMARY))
    expect(currentUser.value?.protected).toBe(true)
    expect(apiMock.get).toHaveBeenCalledWith('/api/me')
    expect(apiMock.get).toHaveBeenCalledWith('/api/me/protection')
  })

  it('drops the summary when protection is lifted', async () => {
    currentUser.value = { ...KID, protected: true, restrictions: ['bazaar'] }
    myProtection.value = SUMMARY
    routeGets({ ...KID, protected: false, restrictions: [] }, SUMMARY)
    handlers['me.protection_changed']({ data: { type: 'me.protection_changed' } })
    await vi.waitFor(() => expect(currentUser.value?.protected).toBe(false))
    expect(myProtection.value).toBeNull()
    expect(apiMock.get).not.toHaveBeenCalledWith('/api/me/protection')
  })

  it('keeps the last summary when a reload fails', async () => {
    myProtection.value = SUMMARY
    routeGets({ ...KID, protected: true, restrictions: ['bazaar'] }, new Error('offline'))
    await loadMyProtection()
    expect(myProtection.value).toEqual(SUMMARY)
    expect(myProtectionFailed.value).toBe(true)
  })
})
