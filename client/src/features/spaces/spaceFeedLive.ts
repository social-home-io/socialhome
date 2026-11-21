/**
 * The open space feed, live from ``space.post.created`` frames.
 *
 * The frame carries the whole post (``RealtimeService._on_space_post_created``,
 * media URLs signed) and goes to the same seats that may read
 * ``GET /api/spaces/{id}/feed`` — so the feed puts it on top instead of
 * refetching the whole page per post. The feed query's own rules apply
 * here too: a deleted post or an anchor the author kept out of the feed
 * (``hidden_from_feed``: a Bazaar listing, a calendar event) never shows.
 * A post held for review is no post yet (it sits in the moderation queue),
 * so no frame goes out for it until it is approved.
 *
 * Only the feed route spells a bot's persona (``bot``) and a video's
 * signed poster (``media_thumbnail_url``); for those the page refetches,
 * on a throttle.
 */
import type { FeedPost } from '@/types'

/** ``socialhome.domain.user.SYSTEM_AUTHOR`` (as in ``PostCard``). */
const SYSTEM_AUTHOR = 'system-integration'

interface SpacePostFrame {
  id?: unknown
  author?: unknown
  type?: unknown
  deleted?: unknown
  hidden_from_feed?: unknown
  bot_id?: unknown
}

/** The feed with the frame's post on top; ``null`` when nothing changes;
 *  ``'refetch'`` when only the feed route can render it. */
export function applySpacePostCreated(
  posts: readonly FeedPost[],
  raw: unknown,
): FeedPost[] | 'refetch' | null {
  const post = raw as SpacePostFrame | null | undefined
  if (!post || typeof post !== 'object' || typeof post.id !== 'string') return 'refetch'
  if (post.deleted === true || post.hidden_from_feed === true) return null
  if (posts.some(p => p.id === post.id)) return null
  if (post.bot_id || post.author === SYSTEM_AUTHOR || post.type === 'video') return 'refetch'
  return [post as unknown as FeedPost, ...posts]
}
