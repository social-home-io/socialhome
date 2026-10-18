/**
 * "Needs you" counts for one space — what waits for the viewer's
 * decision: items held for review and open reports (content authority:
 * owner / admin / moderator), join requests (settings authority: owner /
 * admin). The space's More button carries the total as a filled badge;
 * the Moderation row in More breaks it down.
 *
 * Read from the endpoints the Moderation section already uses (no new
 * backend): ``GET /api/spaces/{id}/moderation``, ``…/reports`` and
 * ``…/join-requests``. Refreshed live on the moderation queue's WS
 * frames and on a join request, and whenever the page asks (a decision
 * made in the Moderation section). A failed read counts as zero — the
 * badge is a hint, the section itself is the authority.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { ws, type WsEvent } from '@/ws'

export interface SpaceAttention {
  spaceId: string
  review: number
  reports: number
  joinRequests: number
}

export const spaceAttention = signal<SpaceAttention | null>(null)

/** The total a badge shows. */
export function attentionTotal(a: SpaceAttention | null, spaceId: string): number {
  if (!a || a.spaceId !== spaceId) return 0
  return a.review + a.reports + a.joinRequests
}

const count = (p: Promise<unknown>): Promise<number> =>
  p.then((rows) => (Array.isArray(rows) ? rows.length : 0)).catch(() => 0)

export async function loadSpaceAttention(
  spaceId: string, moderate: boolean, admin: boolean,
): Promise<void> {
  if (!moderate) {
    spaceAttention.value = { spaceId, review: 0, reports: 0, joinRequests: 0 }
    return
  }
  const [review, reports, joinRequests] = await Promise.all([
    count(api.get(`/api/spaces/${spaceId}/moderation`)),
    count(api.get(`/api/spaces/${spaceId}/reports`)),
    admin ? count(api.get(`/api/spaces/${spaceId}/join-requests`)) : Promise.resolve(0),
  ])
  spaceAttention.value = { spaceId, review, reports, joinRequests }
}

/** WS frames after which the counts may have changed. */
const FRAMES = [
  'space.moderation.queued',
  'space.moderation.approved',
  'space.moderation.rejected',
  'space.moderation.expired',
  'space.join.requested',
  'space.join.approved',
  'space.join.denied',
] as const

/** Keep ``spaceAttention`` current for ``spaceId`` while mounted. */
export function useSpaceAttention(spaceId: string, moderate: boolean, admin: boolean): void {
  useEffect(() => {
    void loadSpaceAttention(spaceId, moderate, admin)
    if (!moderate) return
    const onFrame = (e: WsEvent) => {
      const sid = (e.data as { space_id?: string } | undefined)?.space_id
      if (sid === spaceId) void loadSpaceAttention(spaceId, moderate, admin)
    }
    const offs = FRAMES.map((type) => ws.on(type, onFrame))
    return () => offs.forEach((off) => off())
  }, [spaceId, moderate, admin])
}
