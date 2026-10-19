/**
 * "Needs you" for one space — what waits for the viewer's decision. The
 * server says it: ``GET /api/spaces/{id}`` carries ``attention`` (held
 * items and reports for owners / admins / moderators; join requests and
 * open proposals for owners / admins; ``null`` for everyone else), read
 * at request time from the same queues the Moderation section lists.
 *
 * Two places show it: the Needs-you card under the space hero (one row
 * per kind) and the filled badge on More (held items + reports + join
 * requests — what the Moderation row in More leads to).
 *
 * The page re-reads the space detail on the frames after which the
 * counts may have changed (``useSpaceAttentionFrames``). Nothing here
 * fetches on its own: one read of the detail, one source of truth.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { ws, type WsEvent } from '@/ws'
import type { SpaceProposal } from '@/components/SpaceProposalDialog'

/** An open proposal as ``attention`` lists it: the proposal view plus
 *  ``required`` (approvals it needs) and the per-reader ``voted_by_me``. */
export interface AttentionProposal extends SpaceProposal {
  required: number
  voted_by_me: boolean
}

/** ``attention`` as ``GET /api/spaces/{id}`` sends it. */
export interface WireAttention {
  pending_review: number
  reports: number
  join_requests: number
  proposals: AttentionProposal[]
}

export interface SpaceAttention {
  spaceId: string
  review: number
  reports: number
  joinRequests: number
  proposals: AttentionProposal[]
}

export const spaceAttention = signal<SpaceAttention | null>(null)

/** Store the detail's ``attention`` for ``spaceId``; ``null`` (a member,
 *  a follower) means nothing waits. */
export function setSpaceAttention(spaceId: string, wire: WireAttention | null | undefined): void {
  spaceAttention.value = {
    spaceId,
    review: wire?.pending_review ?? 0,
    reports: wire?.reports ?? 0,
    joinRequests: wire?.join_requests ?? 0,
    proposals: Array.isArray(wire?.proposals) ? wire.proposals : [],
  }
}

/** The filled badge on More: what the Moderation row leads to. */
export function attentionTotal(a: SpaceAttention | null, spaceId: string): number {
  if (!a || a.spaceId !== spaceId) return 0
  return a.review + a.reports + a.joinRequests
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
  'space.proposal.updated',
] as const

/** Call ``reload`` (re-read the space detail) on a frame for ``spaceId``
 *  while ``enabled`` (the viewer has content authority here). */
export function useSpaceAttentionFrames(spaceId: string, enabled: boolean, reload: () => void): void {
  useEffect(() => {
    if (!enabled) return
    const onFrame = (e: WsEvent) => {
      const sid = (e.data as { space_id?: string } | undefined)?.space_id
      if (sid === spaceId) reload()
    }
    const offs = FRAMES.map((type) => ws.on(type, onFrame))
    return () => offs.forEach((off) => off())
  }, [spaceId, enabled]) // eslint-disable-line react-hooks/exhaustive-deps
}
