/**
 * The signed-in account's own protection (§CP.R): what is limited and who
 * the guardians are, from ``GET /api/me/protection``.
 *
 * ``/api/me`` already says *whether* the account is protected and lists the
 * restrictions; this store adds the guardians and keeps both fresh. When a
 * household admin turns protection on or off, or a guardian changes, the
 * server sends ``me.protection_changed`` to this account alone (no data) —
 * the SPA then reloads ``/api/me`` and this summary instead of waiting for
 * the next page load.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { currentUser, loadCurrentUser } from './auth'

export interface ProtectionGuardian {
  user_id: string
  username: string
  display_name: string
}

export interface MyProtection {
  protected: boolean
  restrictions: string[]
  guardians: ProtectionGuardian[]
}

/** ``null`` until loaded (or when the account isn't protected). */
export const myProtection = signal<MyProtection | null>(null)
/** The last ``/api/me/protection`` load failed. */
export const myProtectionFailed = signal(false)

/** Load the summary. Failures keep the last known value and set the flag. */
export async function loadMyProtection(): Promise<void> {
  try {
    myProtection.value = await api.get<MyProtection>('/api/me/protection')
    myProtectionFailed.value = false
  } catch {
    myProtectionFailed.value = true
  }
}

/** Reload ``/api/me`` and — for a protected account — the summary. */
export async function refreshMyProtection(): Promise<void> {
  const me = await loadCurrentUser()
  if (me === null) return
  if (currentUser.value?.protected) {
    await loadMyProtection()
  } else {
    myProtection.value = null
    myProtectionFailed.value = false
  }
}

export function wireProtectionWs(): void {
  ws.on('me.protection_changed', () => { void refreshMyProtection() })
}
