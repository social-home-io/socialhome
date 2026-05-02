/**
 * CommentOverlay — Facebook-style comments overlay (§23.46).
 *
 * Replaces the inline-under-each-post comment thread with a focused
 * sheet that opens when a viewer taps "Comment" on a post:
 *
 *   • Mobile (≤640 px): slides up from the bottom, fills ~92 vh,
 *     drag-handle at the top, sticky composer at the bottom — same
 *     pattern Facebook / Instagram / WhatsApp use because it keeps
 *     the keyboard, the composer, and the most recent comments all in
 *     thumb-reach without forcing the user to scroll the whole feed.
 *   • Desktop (≥641 px): centred modal card, 640 px wide, max 82 vh
 *     tall. Same sticky-composer + scrollable-list shape.
 *
 * The post header at the top shows the author + a content snippet so
 * the reader keeps context after the feed scrolls past. Tap "View post"
 * to fall back to the inline rendering (deep-link to ``#post-{id}``).
 *
 * Lives behind a global signal so the open/close handle is just
 * ``openCommentOverlay(post, spaceId?)``. Mounted once at the App
 * level, alongside the other global dialogs.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from './Avatar'
import { Spinner } from './Spinner'
import { CommentThread } from './CommentThread'
import { OnlinePill } from './OnlinePill'
import { showToast } from './Toast'
import { posts as feedPosts } from '@/store/feed'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { Comment, FeedPost } from '@/types'

interface OverlayState {
  post: FeedPost
  spaceId: string | null
}

const overlay = signal<OverlayState | null>(null)

/** Open the overlay for a post. ``spaceId`` is ``null`` for household
 *  feed posts, the space id for space feed posts (drives the right
 *  endpoints + avatar override resolution). */
export function openCommentOverlay(
  post: FeedPost,
  spaceId: string | null = null,
): void {
  overlay.value = { post, spaceId }
}

export function closeCommentOverlay(): void {
  overlay.value = null
}

function commentsUrl(state: OverlayState): string {
  return state.spaceId
    ? `/api/spaces/${state.spaceId}/posts/${state.post.id}/comments`
    : `/api/feed/posts/${state.post.id}/comments`
}

/** Compose a one-line preview of the post's content for the sticky
 *  header — strips newlines and truncates to keep the bar slim. */
function previewLine(post: FeedPost): string {
  if (!post.content) {
    if (post.media_url || (post.image_urls && post.image_urls.length > 0))
      return '🖼️ Photo'
    return ''
  }
  const flat = post.content.replace(/\s+/g, ' ').trim()
  return flat.length > 140 ? `${flat.slice(0, 140)}…` : flat
}

export function CommentOverlay() {
  const state = overlay.value
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const [comments, setComments] = useState<Comment[]>([])
  const [loading, setLoading] = useState(true)
  const post = state?.post
  const spaceId = state?.spaceId ?? null

  // Refresh helper — pulls the canonical list from the server. Used
  // on open, after every action, and on any inbound WS frame for this
  // post.
  const refresh = async () => {
    if (!state) return
    try {
      const rows = await api.get(commentsUrl(state)) as Comment[]
      setComments(rows)
    } catch {
      // Keep the previous list visible; transient failures are handled
      // by the showToast in the action callbacks.
    }
  }

  // Open / close lifecycle. Loads comments, traps focus, listens to WS
  // for live updates, locks body scroll on mobile so the page behind
  // doesn't drift while the user reads.
  useEffect(() => {
    if (!state) return
    setLoading(true)
    setComments([])
    let cancelled = false
    void api.get(commentsUrl(state)).then((rows) => {
      if (cancelled) return
      setComments(rows as Comment[])
      setLoading(false)
    }).catch(() => { if (!cancelled) setLoading(false) })

    // Body-scroll lock — releases on close.
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    // Focus the dialog so the close-button + composer are reachable
    // by keyboard immediately.
    const previouslyFocused = document.activeElement as HTMLElement | null
    requestAnimationFrame(() => dialogRef.current?.focus())

    // Live-merge — same scope filter the inline path used.
    const matches = (d: { post_id?: string; space_id?: string | null }) => {
      if (d.post_id !== state.post.id) return false
      const spaceMatches = (d.space_id ?? null) === state.spaceId
      return spaceMatches
    }
    const offAdded = ws.on('comment.added', (e) => {
      if (matches(e.data as { post_id?: string; space_id?: string | null })) void refresh()
    })
    const offUpdated = ws.on('comment.updated', (e) => {
      if (matches(e.data as { post_id?: string; space_id?: string | null })) void refresh()
    })
    const offDeleted = ws.on('comment.deleted', (e) => {
      if (matches(e.data as { post_id?: string; space_id?: string | null })) void refresh()
    })

    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') {
        ev.preventDefault()
        closeCommentOverlay()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      cancelled = true
      offAdded(); offUpdated(); offDeleted()
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
      previouslyFocused?.focus?.()
    }
  // ``state`` identity changes when openCommentOverlay reopens for a
  // different post — re-running the effect is exactly what we want.
  }, [state?.post.id, state?.spaceId])

  if (!state || !post) return null

  // The author rendering matches PostCard's logic — display name
  // resolution + signed avatar URL — so the overlay header reads as
  // a continuation of the feed item the user just tapped.
  const authorName = resolveDisplayName(spaceId, post.author, post.author)
  const avatarUrl = resolveAvatar(spaceId, post.author, null)

  const handleReply = async (parentId: string | null, content: string) => {
    try {
      await api.post(commentsUrl(state), { content, parent_id: parentId })
      await refresh()
      // Optimistic count bump on the household feed list so the post
      // card outside the overlay shows the right comment count even
      // before the WS frame lands.
      feedPosts.value = feedPosts.value.map(p =>
        p.id === post.id ? { ...p, comment_count: p.comment_count + 1 } : p,
      )
    } catch (err: unknown) {
      showToast(
        `Comment failed: ${(err as Error)?.message ?? err}`, 'error',
      )
    }
  }

  const handleEdit = async (commentId: string, content: string) => {
    try {
      await api.patch(
        `${commentsUrl(state)}/${commentId}`, { content },
      )
      await refresh()
      showToast('Comment updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Edit failed: ${(err as Error)?.message ?? err}`, 'error',
      )
    }
  }

  const handleDelete = async (commentId: string) => {
    try {
      await api.delete(`${commentsUrl(state)}/${commentId}`)
      await refresh()
      feedPosts.value = feedPosts.value.map(p =>
        p.id === post.id
          ? { ...p, comment_count: Math.max(0, p.comment_count - 1) }
          : p,
      )
      showToast('Comment deleted', 'info')
    } catch (err: unknown) {
      showToast(
        `Delete failed: ${(err as Error)?.message ?? err}`, 'error',
      )
    }
  }

  const heading = post.comment_count === 1
    ? '1 comment'
    : `${comments.length || post.comment_count} comments`

  return (
    <div
      class="sh-comment-overlay-backdrop"
      onClick={closeCommentOverlay}
      role="presentation"
    >
      <div
        ref={dialogRef}
        class="sh-comment-overlay"
        role="dialog"
        aria-modal="true"
        aria-label={`Comments on ${authorName}'s post`}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drag handle — visual cue on mobile that the sheet can be
            dismissed; tap is also wired to close. */}
        <button
          type="button"
          class="sh-comment-overlay-handle"
          aria-label="Close comments"
          onClick={closeCommentOverlay}
        />
        <header class="sh-comment-overlay-header">
          <Avatar name={authorName} src={avatarUrl} size={36} />
          <div class="sh-comment-overlay-header-meta">
            <div class="sh-comment-overlay-header-name">
              <strong>{authorName}</strong>
              <OnlinePill user_id={post.author} compact />
            </div>
            {previewLine(post) && (
              <p class="sh-comment-overlay-header-preview">
                {previewLine(post)}
              </p>
            )}
          </div>
          <button
            type="button"
            class="sh-comment-overlay-close"
            aria-label="Close"
            onClick={closeCommentOverlay}
          >×</button>
        </header>

        <div class="sh-comment-overlay-count" aria-live="polite">{heading}</div>

        <div class="sh-comment-overlay-body">
          {loading && <Spinner />}
          {!loading && comments.length === 0 && (
            <div class="sh-comment-overlay-empty">
              <p class="sh-muted">Be the first to comment.</p>
            </div>
          )}
          {!loading && comments.length > 0 && (
            <CommentThread
              comments={comments}
              spaceId={spaceId}
              onReply={handleReply}
              onEdit={handleEdit}
              onDelete={handleDelete}
            />
          )}
        </div>
      </div>
    </div>
  )
}
