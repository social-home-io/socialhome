/**
 * The caller's own status, kept live app-wide.
 *
 * ``user.status_changed`` fans out to the whole household; the frame for
 * *our* user id (a status set on another device, or cleared by the
 * server's expiry sweep) lands on ``currentUser``, so every surface that
 * reads it (the top bar's "You" badge, your Friends row) follows along.
 * Other people's frames are left to the pages that list them.
 */
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import type { UserStatus } from '@/types'

const EMPTY: UserStatus = { emoji: null, text: null, expires_at: null }

/** Subscribe once at boot (``main.tsx``); returns the unsubscribe. */
export function wireMyStatusWs(): () => void {
  return ws.on('user.status_changed', (e) => {
    const me = currentUser.value
    if (!me || e.data.user_id !== me.user_id) return
    currentUser.value = { ...me, status: (e.data.status as UserStatus | null) ?? EMPTY }
  })
}
