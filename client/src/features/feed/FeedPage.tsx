/**
 * FeedPage — household feed (§23.43/§23.44/§23.48).
 * Uses PostCard for display and Composer for creation.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { posts, feedLoading, feedHasMore, loadFeed } from '@/store/feed'
import { api } from '@/api'
import { ws } from '@/ws'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { CommentThread } from '@/components/CommentThread'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import type { Comment, FeedPost } from '@/types'

const expandedComments = signal<Record<string, Comment[]>>({})

export default function FeedPage() {
  useEffect(() => {
    void loadHouseholdUsers()
    loadFeed()
    const refreshIfExpanded = (postId: string) => {
      if (!expandedComments.value[postId]) return
      void api.get(`/api/feed/posts/${postId}/comments`).then((rows) => {
        expandedComments.value = {
          ...expandedComments.value,
          [postId]: rows as Comment[],
        }
      })
    }
    const off1 = ws.on('comment.added', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (!d.space_id) refreshIfExpanded(d.post_id)
    })
    const off2 = ws.on('comment.updated', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (!d.space_id) refreshIfExpanded(d.post_id)
    })
    const off3 = ws.on('comment.deleted', (e) => {
      const d = e.data as { post_id: string; space_id?: string | null }
      if (!d.space_id) refreshIfExpanded(d.post_id)
    })
    return () => { off1(); off2(); off3() }
  }, [])

  const handleLoadMore = () => {
    const last = posts.value[posts.value.length - 1]
    if (last) loadFeed(last.created_at)
  }

  const handleSubmit = async (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: { location?: { lat: number; lon: number; label: string | null } },
  ) => {
    const body: Record<string, unknown> = {
      type, content,
      media_url: mediaUrl ?? null,
    }
    if (extras?.location) body.location = extras.location
    const post = await api.post('/api/feed/posts', body) as FeedPost
    showToast('Post shared', 'success')
    // No local prepend here — wireFeedWs() handles `post.created` and
    // dedupes by id, so the new post lands at the top exactly once.
    return post?.id
  }

  const handleReact = async (postId: string, emoji: string) => {
    const updated = await api.post(
      `/api/feed/posts/${postId}/reactions`, { emoji },
    ) as FeedPost
    posts.value = posts.value.map((p) => (p.id === postId ? updated : p))
  }

  const refreshComments = async (postId: string) => {
    const rows = await api.get(
      `/api/feed/posts/${postId}/comments`,
    ) as Comment[]
    expandedComments.value = {
      ...expandedComments.value, [postId]: rows,
    }
  }

  const handleToggleComments = async (postId: string) => {
    if (expandedComments.value[postId]) {
      const { [postId]: _dropped, ...rest } = expandedComments.value
      expandedComments.value = rest
    } else {
      await refreshComments(postId)
    }
  }

  const handleReply = async (
    postId: string, parentId: string | null, content: string,
  ) => {
    await api.post(
      `/api/feed/posts/${postId}/comments`,
      { content, parent_id: parentId },
    )
    await refreshComments(postId)
    // wireFeedWs() bumps comment_count on `comment.added`. No reload.
  }

  const handleCommentEdit = async (
    postId: string, commentId: string, content: string,
  ) => {
    try {
      await api.patch(
        `/api/feed/posts/${postId}/comments/${commentId}`, { content },
      )
      await refreshComments(postId)
      showToast('Comment updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Edit failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleCommentDelete = async (postId: string, commentId: string) => {
    try {
      await api.delete(
        `/api/feed/posts/${postId}/comments/${commentId}`,
      )
      await refreshComments(postId)
      // No `comment.deleted` count-decrement event today, so adjust
      // the local count optimistically; the next cold-load is
      // authoritative.
      posts.value = posts.value.map((p) =>
        p.id === postId
          ? { ...p, comment_count: Math.max(0, p.comment_count - 1) }
          : p,
      )
      showToast('Comment deleted', 'info')
    } catch (err: unknown) {
      showToast(
        `Delete failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleDelete = async (postId: string) => {
    if (!confirm('Delete this post?')) return
    await api.delete(`/api/feed/posts/${postId}`)
    showToast('Post deleted', 'info')
    // wireFeedWs() removes the row on `post.deleted`. No reload.
  }

  return (
    <div class="sh-feed">
      <h1>Household Feed</h1>
      <Composer onSubmit={handleSubmit} context="Household" />
      {posts.value.map(post => (
        <div key={post.id} class="sh-feed-item">
          <PostCard
            post={post}
            onReact={(emoji) => handleReact(post.id, emoji)}
            onComment={() => handleToggleComments(post.id)}
            onDelete={() => handleDelete(post.id)}
          />
          {expandedComments.value[post.id] && (
            <CommentThread
              comments={expandedComments.value[post.id]}
              onReply={(parentId, content) =>
                handleReply(post.id, parentId, content)}
              onEdit={(commentId, content) =>
                handleCommentEdit(post.id, commentId, content)}
              onDelete={(commentId) =>
                handleCommentDelete(post.id, commentId)}
            />
          )}
        </div>
      ))}
      {feedLoading.value && <Spinner />}
      {!feedLoading.value && posts.value.length === 0 && (
        <p class="sh-muted">No posts yet. Share something with your household!</p>
      )}
      {feedHasMore.value && !feedLoading.value && posts.value.length > 0 && (
        <Button variant="secondary" onClick={handleLoadMore}>Load more</Button>
      )}
    </div>
  )
}
