/**
 * FeedPage — household feed (§23.43/§23.44/§23.48).
 * Uses PostCard for display and Composer for creation.
 */
import { useEffect } from 'preact/hooks'
import { posts, feedLoading, feedHasMore, loadFeed, mergePostEdit } from '@/store/feed'
import { api } from '@/api'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { useTitle } from '@/store/pageTitle'
import { PostCard } from '@/components/PostCard'
import { Composer } from '@/components/Composer'
import { openCommentOverlay } from '@/components/CommentOverlay'
import { HouseholdPresenceStrip } from '@/components/HouseholdPresenceStrip'
import { FeedSkeleton, PostCardSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { PullToRefresh } from '@/components/PullToRefresh'
import { showToast } from '@/components/Toast'
import { instanceConfig } from '@/store/instance'
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import type { FeedPost } from '@/types'
import { confirmDialog } from '@/components/confirm'

export default function FeedPage() {
  // The household's federated display name from instanceConfig — the
  // single source of truth (set in admin Settings; what peers also
  // see). Falls back to "Home" while the cold-start config fetch is in
  // flight, matching the default the backend ships with on first boot.
  const householdName = instanceConfig.value?.instance_name ?? t('nav.home')
  useTitle(householdName)
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
      noLinkPreview?: boolean
    },
  ) => {
    const body: Record<string, unknown> = {
      type, content,
      media_url: mediaUrl ?? null,
      image_urls: extras?.imageUrls ?? [],
    }
    if (extras?.location) body.location = extras.location
    if (extras?.noLinkPreview) body.no_link_preview = true
    const post = await api.post('/api/feed/posts', body) as FeedPost
    showToast(t('feed.post_shared'), 'success')
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
    if (!await confirmDialog(t('feed.delete_confirm'), { destructive: true })) return
    await api.delete(`/api/feed/posts/${postId}`)
    showToast(t('feed.post_deleted'), 'info')
    // wireFeedWs() removes the row on `post.deleted`. No reload.
  }

  /** Inline edit of a post's text (author or household admin — the
   *  server's rule). ``true`` closes the editor. */
  const handleEdit = async (postId: string, content: string): Promise<boolean> => {
    try {
      const updated = await api.patch(`/api/feed/posts/${postId}`, { content }) as FeedPost
      posts.value = posts.value.map((p) => (p.id === postId ? mergePostEdit(p, updated) : p))
      showToast(t('post.edit.saved'), 'success')
      return true
    } catch (err: unknown) {
      showToast(t('post.edit.failed', { error: String((err as Error)?.message ?? err) }), 'error')
      return false
    }
  }
  const me = currentUser.value

  // Cold-start: no posts yet AND a fetch in flight → show the
  // layout-stable skeleton instead of an isolated spinner so the eye
  // can register the page chrome immediately. Subsequent fetches
  // (paging) keep the spinner since the layout is already mounted.
  const isInitialLoad = feedLoading.value && posts.value.length === 0

  if (isInitialLoad) {
    return <FeedSkeleton />
  }

  return (
    <PullToRefresh onRefresh={() => loadFeed()}>
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
            onEdit={me && (post.author === me.user_id || me.is_admin)
              ? (content) => handleEdit(post.id, content)
              : undefined}
          />
        </div>
      ))}
      {/* Pagination spinner — full reload uses the FeedSkeleton above. */}
      {feedLoading.value && posts.value.length > 0 && <PostCardSkeleton />}
      {!feedLoading.value && posts.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">📝</div>
          <h3>{t('feed.empty.title')}</h3>
          <p>{t('feed.empty.body')}</p>
          <p class="sh-muted">{t('feed.empty.hint')}</p>
        </div>
      )}
      {feedHasMore.value && !feedLoading.value && posts.value.length > 0 && (
        <Button variant="secondary" onClick={handleLoadMore}>{t('feed.load_more')}</Button>
      )}
    </div>
    </PullToRefresh>
  )
}
