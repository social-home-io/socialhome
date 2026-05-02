/**
 * FeedPage — household feed (§23.43/§23.44/§23.48).
 * Uses PostCard for display and Composer for creation.
 */
import { useEffect } from 'preact/hooks'
import { posts, feedLoading, feedHasMore, loadFeed } from '@/store/feed'
import { api } from '@/api'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { useTitle } from '@/store/pageTitle'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { HouseholdPresenceStrip } from '@/components/HouseholdPresenceStrip'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import type { FeedPost } from '@/types'

export default function FeedPage() {
  useTitle('Household')
  useEffect(() => {
    void loadHouseholdUsers()
    loadFeed()
  }, [])

  const handleLoadMore = () => {
    const last = posts.value[posts.value.length - 1]
    if (last) loadFeed(last.created_at)
  }

  const handleSubmit = async (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: {
      location?: { lat: number; lon: number; label: string | null }
      imageUrls?: string[]
    },
  ) => {
    const body: Record<string, unknown> = {
      type, content,
      media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
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

  const handleDelete = async (postId: string) => {
    if (!confirm('Delete this post?')) return
    await api.delete(`/api/feed/posts/${postId}`)
    showToast('Post deleted', 'info')
    // wireFeedWs() removes the row on `post.deleted`. No reload.
  }

  return (
    <div class="sh-feed">
      <HouseholdPresenceStrip />
      <Composer onSubmit={handleSubmit} context="Household" />
      {posts.value.map(post => (
        <div key={post.id} class="sh-feed-item">
          <PostCard
            post={post}
            onReact={(emoji) => handleReact(post.id, emoji)}
            onComment={() => openCommentOverlay(post, null)}
            onDelete={() => handleDelete(post.id)}
          />
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
