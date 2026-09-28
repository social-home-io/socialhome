import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Space } from '@/types'

export const spaces       = signal<Space[]>([])
export const activeSpace  = signal<Space | null>(null)

/** Refresh the cached spaces list from the server. Used by the spaces
 *  list page on mount and by SpaceCreateDialog after a successful
 *  create so the new row shows up without a manual reload. */
export async function loadSpaces(): Promise<void> {
  try {
    const rows = await api.get('/api/spaces') as Space[] | undefined | null
    // Defend against test mocks of ``@/api`` that don't configure a
    // resolved value — ``api.get(...)`` returns undefined, ``await
    // undefined`` resolves to undefined, and assigning that to the
    // signal would break consumers that call ``spaces.value.length``.
    spaces.value = Array.isArray(rows) ? rows : []
  } catch {
    /* leave the cached list — the page renders an empty / stale state. */
  }
}

/** Spaces this tab is dissolving itself. The server's ``dissolved``
 *  frame lands while the tab's own request is still in flight; the
 *  initiating flow owns the toast + redirect, so the live handlers
 *  skip it (no double toast, no race with its redirect). */
const localDissolves = new Set<string>()

export function markLocalDissolve(spaceId: string): void {
  localDissolves.add(spaceId)
}

export function clearLocalDissolve(spaceId: string): void {
  localDissolves.delete(spaceId)
}

export function isLocalDissolve(spaceId: string): boolean {
  return localDissolves.has(spaceId)
}

/** Keep the cached spaces list (side nav, spaces page) in step with
 *  ``space.config.changed``. A dissolved space — dissolved here, or by
 *  its remote host (``SPACE_DISSOLVED``) — drops out at once; any other
 *  config change (rename, emoji, cover, features, roles…) of a listed
 *  space refetches the list. The frame only reaches that space's local
 *  members. Idempotent per call site: wire once at startup. */
export function wireSpacesWs(): void {
  ws.on('space.config.changed', (e) => {
    const d = e.data as { space_id?: string; event_type?: string }
    if (!d.space_id) return
    if (d.event_type === 'dissolved') {
      spaces.value = spaces.value.filter(s => s.id !== d.space_id)
      return
    }
    if (spaces.value.some(s => s.id === d.space_id)) void loadSpaces()
  })
}
