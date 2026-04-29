import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { FeedPost } from '@/types'

export const posts        = signal<FeedPost[]>([])
export const feedLoading  = signal(false)
export const feedHasMore  = signal(true)

export async function loadFeed(before?: string) {
  feedLoading.value = true
  const data = await api.get(`/api/feed${before ? `?before=${before}` : ''}`)
  posts.value = before ? [...posts.value, ...data] : data
  feedHasMore.value = data.length === 50
  feedLoading.value = false
}

/** Wire post/comment WS events into the feed store. Idempotent.
 *
 *  Payload shapes come from ``RealtimeService._on_post_*`` /
 *  ``_on_comment_*`` — the server sends ``{type, post: {...}}`` for
 *  post-shaped events and ``{type, post_id, ...}`` for delete /
 *  comment events. ``e.data`` is the whole frame (see ``ws.ts``), so
 *  we always extract the named field rather than treating ``e.data``
 *  itself as the payload. */
export function wireFeedWs(): void {
  ws.on('post.created', (e) => {
    const post = (e.data as { post?: FeedPost }).post
    if (!post) return
    if (posts.value.some((p) => p.id === post.id)) return
    posts.value = [post, ...posts.value]
  })
  ws.on('post.edited', (e) => {
    const post = (e.data as { post?: FeedPost }).post
    if (!post) return
    posts.value = posts.value.map((p) => (p.id === post.id ? post : p))
  })
  ws.on('post.reaction_changed', (e) => {
    const post = (e.data as { post?: FeedPost }).post
    if (!post) return
    posts.value = posts.value.map((p) => (p.id === post.id ? post : p))
  })
  ws.on('post.deleted', (e) => {
    const postId = (e.data as { post_id?: string }).post_id
    if (!postId) return
    posts.value = posts.value.filter((p) => p.id !== postId)
  })
  ws.on('comment.added', (e) => {
    const { post_id, space_id } = e.data as {
      post_id?: string
      space_id?: string | null
    }
    // Household feed only — space comments are owned by SpaceFeedPage.
    if (space_id) return
    if (!post_id) return
    posts.value = posts.value.map((p) =>
      p.id === post_id ? { ...p, comment_count: p.comment_count + 1 } : p,
    )
  })
}
