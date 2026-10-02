/**
 * The viewer's own moderation-queue items, per space
 * (``GET /api/spaces/{id}/moderation/mine``, any member).
 *
 * A write into a space whose feature is MODERATED ("Reviewed") answers
 * ``202 {queued: true, ...}`` — nothing was saved yet. The author still
 * wants to see what they sent: the "Pending review (n)" strip on each
 * space tab and the task board's "Pending review" chip read from here.
 *
 * Refetched on mount of the space page (``useModerationMine``), right
 * after a queued write (``refreshModerationMine``) and on every
 * ``space.moderation.mine`` frame — the server sends that to the
 * submitter on queue and on every decision (approved / rejected /
 * expired). A failed fetch (offline, an older host without the route)
 * keeps what's on screen.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { ModerationItem } from '@/features/spaces/moderationItems'

/** ``space_id`` → the caller's items there, newest first. */
export const moderationMine = signal<Readonly<Record<string, readonly ModerationItem[]>>>({})

const inflight = new Map<string, Promise<void>>()

/** Fetch the caller's items for ``spaceId``. Calls while one is in
 *  flight share it. */
export function refreshModerationMine(spaceId: string): Promise<void> {
  const running = inflight.get(spaceId)
  if (running) return running
  // ``Promise.resolve().then`` so even a synchronous throw lands in the
  // ``catch`` below — a write's success path must never fail on this.
  const run = Promise.resolve()
    .then(() => api.get(`/api/spaces/${encodeURIComponent(spaceId)}/moderation/mine`))
    .then((rows: unknown) => {
      moderationMine.value = {
        ...moderationMine.value,
        [spaceId]: Array.isArray(rows) ? rows as ModerationItem[] : [],
      }
    })
    .catch(() => { /* keep what's on screen */ })
    .finally(() => { inflight.delete(spaceId) })
  inflight.set(spaceId, run)
  return run
}

/** The caller's still-pending items in ``spaceId`` (optionally of one
 *  feature). */
export function pendingMine(spaceId: string, feature?: string): ModerationItem[] {
  return (moderationMine.value[spaceId] ?? []).filter(
    i => i.status === 'pending' && (feature === undefined || i.feature === feature),
  )
}

/** Ids of the existing items the caller has a pending edit / delete of
 *  (``feature`` only) — the board's "Pending review" chip. */
export function pendingTargetIds(spaceId: string, feature: string): ReadonlySet<string> {
  const out = new Set<string>()
  for (const i of pendingMine(spaceId, feature)) {
    if (i.action !== 'create' && i.target_id) out.add(i.target_id)
  }
  return out
}

/** Load the caller's items for the open space and keep them live. */
export function useModerationMine(spaceId: string): void {
  useEffect(() => {
    void refreshModerationMine(spaceId)
    return ws.on('space.moderation.mine', (e) => {
      if ((e.data as { space_id?: string }).space_id === spaceId) {
        void refreshModerationMine(spaceId)
      }
    })
  }, [spaceId])
}

/** Logout: forget everything. */
export function resetModerationMine(): void {
  moderationMine.value = {}
  inflight.clear()
}
