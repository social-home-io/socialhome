import { signal, computed } from '@preact/signals'
import type { User } from '@/types'
import { api } from '@/api'

export const token       = signal<string | null>(localStorage.getItem('sh_token'))
export const currentUser = signal<User | null>(null)
export const isAuthed    = computed(() => token.value !== null && currentUser.value !== null)

/**
 * Fetch the current user from `/api/me` and populate `currentUser`.
 *
 * `isAuthed` is computed from `token != null && currentUser != null`,
 * so any code path that hands us a fresh token (login form, /setup
 * wizard, cold start with a stashed token) MUST follow up with this
 * call — otherwise the SPA never advances past the login screen.
 *
 * Returns the loaded User (or null on failure). Failures are silent
 * here; api.ts already calls logout() on 401 so the token + user are
 * cleared in lock-step.
 */
export async function loadCurrentUser(): Promise<User | null> {
  if (token.value === null) {
    currentUser.value = null
    return null
  }
  try {
    const me = await api.get('/api/me') as User
    currentUser.value = me
    return me
  } catch {
    return null
  }
}

export function setToken(t: string) {
  token.value = t
  localStorage.setItem('sh_token', t)
}

export function logout() {
  token.value = null
  currentUser.value = null
  localStorage.removeItem('sh_token')
}
