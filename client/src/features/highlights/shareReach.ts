/**
 * shareReach — would sharing a highlight show it to more people than its
 * audience (S6 / P6)?
 *
 * A ``highlight_share`` post opens the highlight to everyone who can read
 * the post — all of today's frames, later ones included. When that is
 * more people than the author picked, the viewer asks first ("Everyone in
 * <space> will see today's frames, also people who can't see it now").
 *
 * Pure: the caller loads the members (household users, or the space's
 * members with ``instance_id`` set for other households) and the paired
 * household ids. Unknown members count as wider — never share silently.
 */
import type { Highlight, Space } from '@/types'

export interface ReachMember {
  user_id: string
  /** Set for a member from another household. */
  instance_id?: string | null
}

export type ShareTarget =
  | { kind: 'household'; members: readonly ReachMember[] }
  | { kind: 'space'; spaceType: Space['space_type']; members: readonly ReachMember[] | null }

export function widensAudience(
  highlight: Highlight,
  me: string,
  target: ShareTarget,
  pairedInstanceIds: ReadonlySet<string>,
): boolean {
  if (target.kind === 'space') {
    // Followers of a public / global space are not on its member list.
    if (target.spaceType === 'public' || target.spaceType === 'global') return true
    if (target.members === null) return true
  }
  const members = target.members ?? []
  const picked = new Set(highlight.audience ?? [])
  switch (highlight.audience_kind) {
    case 'users':
      return members.some(m => m.user_id !== me && !picked.has(m.user_id))
    case 'households':
      // The author's own household always sees it; other households only
      // when chosen.
      return members.some(m => !!m.instance_id && !picked.has(m.instance_id))
    default:
      return members.some(m => !!m.instance_id && !pairedInstanceIds.has(m.instance_id))
  }
}
