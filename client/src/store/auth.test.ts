import { describe, it, expect, beforeEach, vi } from 'vitest'
import { token, currentUser, isAuthed, setToken, logout, loadCurrentUser } from './auth'

describe('auth store', () => {
  beforeEach(() => {
    token.value = null
    currentUser.value = null
    localStorage.clear()
  })

  it('isAuthed is false when no user is loaded', () => {
    expect(isAuthed.value).toBe(false)
  })

  it('setToken persists to localStorage', () => {
    setToken('abc')
    expect(token.value).toBe('abc')
    expect(localStorage.getItem('sh_token')).toBe('abc')
  })

  it('isAuthed is true when currentUser is set (token-independent)', () => {
    // In haos mode the SPA never carries a token — ingress headers
    // stand in. ``isAuthed`` reflects "we have a user record" rather
    // than "we have a token AND a user record".
    currentUser.value = { user_id: 'u1', username: 'a', display_name: 'A', is_admin: false, picture_url: null, picture_hash: null, bio: null, is_new_member: false }
    expect(token.value).toBe(null)
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

  it('loadCurrentUser still fetches /api/me without a token (ingress mode)', async () => {
    // No token stashed — under haos, ingress headers added by HA
    // Supervisor authenticate the request. The api client must still
    // fire the fetch (without ``Authorization``); the backend's
    // ``HaIngressStrategy`` accepts the ingress headers as the
    // handshake. Verify by mocking a 200 and asserting fetch happens.
    const u = {
      user_id: 'u1', username: 'haos', display_name: 'HAOS',
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
    const called = fetchSpy.mock.calls[0]!
    const headers = (called[1] as any)?.headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
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
    // ``ApiClient`` strips the leading slash so ``fetch`` resolves the
    // path against ``<base href>`` (the ingress prefix when behind HA
    // Supervisor, ``/`` otherwise) — matching the production fetch.
    expect(called[0]).toBe('api/me')
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

  it('loadCurrentUser leaves currentUser null on 401 without a token (no toast)', async () => {
    // The "Session expired" toast is keyed on having had a token; an
    // ingress probe that 401s should stay quiet so the App shell can
    // render the dedicated IngressAuthFailed page instead.
    const showToastModule = await import('@/components/Toast')
    const toastSpy = vi.spyOn(showToastModule, 'showToast')
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false, status: 401, json: async () => ({}),
    } as any)
    const me = await loadCurrentUser()
    expect(me).toBeNull()
    expect(currentUser.value).toBeNull()
    expect(toastSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
    toastSpy.mockRestore()
  })
})

describe('onLogout hooks', () => {
  it('runs every registered hook on logout, isolated from failures', async () => {
    const { onLogout, logout: doLogout } = await import('./auth')
    const a = vi.fn()
    const boom = vi.fn(() => { throw new Error('x') })
    const b = vi.fn()
    const offA = onLogout(a)
    onLogout(boom)
    onLogout(b)
    doLogout()
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
    offA()
    doLogout()
    expect(a).toHaveBeenCalledTimes(1)
  })
})
