import { describe, it, expect, beforeEach, vi } from 'vitest'
import { token, currentUser, isAuthed, setToken, logout, loadCurrentUser } from './auth'

describe('auth store', () => {
  beforeEach(() => {
    token.value = null
    currentUser.value = null
    localStorage.clear()
  })

  it('isAuthed is false when no token or user', () => {
    expect(isAuthed.value).toBe(false)
  })

  it('setToken persists to localStorage', () => {
    setToken('abc')
    expect(token.value).toBe('abc')
    expect(localStorage.getItem('sh_token')).toBe('abc')
  })

  it('isAuthed is true when both token and user are set', () => {
    setToken('tok')
    currentUser.value = { user_id: 'u1', username: 'a', display_name: 'A', is_admin: false, picture_url: null, picture_hash: null, bio: null, is_new_member: false }
    expect(isAuthed.value).toBe(true)
  })

  it('logout clears everything', () => {
    setToken('tok')
    currentUser.value = { user_id: 'u1', username: 'a', display_name: 'A', is_admin: false, picture_url: null, picture_hash: null, bio: null, is_new_member: false }
    logout()
    expect(token.value).toBe(null)
    expect(currentUser.value).toBe(null)
    expect(localStorage.getItem('sh_token')).toBe(null)
  })

  it('loadCurrentUser is a no-op when no token is stashed', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const me = await loadCurrentUser()
    expect(me).toBeNull()
    expect(currentUser.value).toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it('loadCurrentUser fetches /api/me with the token and populates currentUser', async () => {
    setToken('tok')
    const u = {
      user_id: 'u1', username: 'pascal', display_name: 'Pascal',
      is_admin: true, picture_url: null, picture_hash: null,
      bio: null, is_new_member: false,
    }
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200,
      json: async () => u,
    } as any)
    const me = await loadCurrentUser()
    expect(me).toEqual(u)
    expect(currentUser.value).toEqual(u)
    expect(isAuthed.value).toBe(true)
    const called = fetchSpy.mock.calls[0]!
    expect(called[0]).toBe('/api/me')
    const headers = (called[1] as any)?.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer tok')
    fetchSpy.mockRestore()
  })

  it('loadCurrentUser leaves currentUser null on a server error', async () => {
    setToken('tok')
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false, status: 500, json: async () => ({}),
    } as any)
    const me = await loadCurrentUser()
    expect(me).toBeNull()
    expect(currentUser.value).toBeNull()
    fetchSpy.mockRestore()
  })
})
