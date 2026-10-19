/**
 * The empty Feed's line under "No posts in this space yet." — honest
 * about who will read the first post.
 *
 * Only a space with a member from another household mentions other
 * households; a private or household space with just you says nothing
 * about an audience at all.
 */

export type EmptyFeedBodyKey =
  | 'space.feed.empty_body'
  | 'space.feed.empty_body_solo'
  | 'space.feed.empty_body_members'
  | 'space.feed.empty_body_open'

export function emptyFeedBodyKey(
  tier: string | null | undefined,
  memberCount: number | null | undefined,
  hasRemoteHouseholds: boolean | null | undefined,
): EmptyFeedBodyKey {
  if (hasRemoteHouseholds) return 'space.feed.empty_body'
  if (tier === 'public' || tier === 'global') return 'space.feed.empty_body_open'
  if (memberCount == null || memberCount <= 1) return 'space.feed.empty_body_solo'
  return 'space.feed.empty_body_members'
}
