/**
 * PostPermalink — one post with its comments open, the page a post or
 * comment notification opens.
 *
 * Two homes: the household post page ``/post/{id}`` and a space's Feed
 * section with ``?post={id}`` (``/spaces/{id}?post={id}``). A link to a
 * comment (``?comment={cid}``) picks that comment out and scrolls it into
 * view — the moment-reply pattern (``MomentumDetailPage``). A post that is
 * deleted, or that the viewer can't read any more, says so with a way
 * back; a failed load offers a retry. Never a blank page.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { CommentThread } from '@/components/CommentThread'
import { LoadErrorState } from '@/components/LoadErrorState'
import { PostCard } from '@/components/PostCard'
import { CommentThreadSkeleton, PostCardSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { mergePostEdit } from '@/store/feed'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { markSeen, postLink } from '@/store/notifications'
import { t } from '@/i18n/i18n'
import type { Comment, FeedPost } from '@/types'
import { ws } from '@/ws'

interface PostPermalinkProps {
  postId: string
  /** The space the post lives in; ``null`` for the household feed. */
  spaceId: string | null
  /** The comment to pick out (``?comment=``), if any. */
  commentId?: string | null
  /** In-app path of the feed this post belongs to (the way back). */
  backPath: string
}

type Failure = 'gone' | 'failed' | null

/** The API path of the post (household or space). */
export function postApiPath(postId: string, spaceId: string | null): string {
  return spaceId
    ? `/api/spaces/${spaceId}/posts/${postId}`
    : `/api/feed/posts/${postId}`
}

/** 404 (deleted, blocked author, another space) or 403 (no seat any
 *  more): the post is gone for this viewer. */
function isGone(err: unknown): boolean {
  const status = (err as { status?: number })?.status
  return status === 404 || status === 403
}

export function PostPermalink({ postId, spaceId, commentId = null, backPath }: PostPermalinkProps) {
  const loc = useLocation()
  const [post, setPost] = useState<FeedPost | null>(null)
  const [comments, setComments] = useState<Comment[] | null>(null)
  const [failure, setFailure] = useState<Failure>(null)
  const [attempt, setAttempt] = useState(0)
  /** The scroll to the picked comment happened (once per visit). */
  const settledRef = useRef<string | null>(null)
  const base = postApiPath(postId, spaceId)

  const loadComments = async () => {
    try {
      setComments(await api.get(`${base}/comments`) as Comment[])
    } catch {
      // Keep what is on screen; the post itself still shows.
      setComments((prev) => prev ?? [])
    }
  }

  useEffect(() => {
    let cancelled = false
    setPost(null)
    setComments(null)
    setFailure(null)
    settledRef.current = null
    // Names and pictures for the author and the commenters.
    void loadHouseholdUsers()
    api.get(base)
      .then((p) => {
        if (cancelled) return
        setPost(p as FeedPost)
        void loadComments()
        // The post is on screen: its notifications (and its comments')
        // read as read on every device.
        markSeen(postLink(postId, spaceId))
      })
      .catch((err: unknown) => {
        if (!cancelled) setFailure(isGone(err) ? 'gone' : 'failed')
      })

    const mine = (d: { post_id?: string; space_id?: string | null }) =>
      d.post_id === postId && (d.space_id ?? null) === spaceId
    const refetchPost = () => {
      api.get(base)
        .then((p) => { if (!cancelled) setPost(p as FeedPost) })
        .catch((err: unknown) => { if (!cancelled && isGone(err)) setFailure('gone') })
    }
    const offs = [
      ws.on('comment.added', (e) => { if (mine(e.data as never)) { void loadComments(); refetchPost() } }),
      ws.on('comment.updated', (e) => { if (mine(e.data as never)) void loadComments() }),
      ws.on('comment.deleted', (e) => { if (mine(e.data as never)) { void loadComments(); refetchPost() } }),
      // The household feed's own frames; a space post's change comes
      // with the next visit (space post frames carry no per-post data).
      ws.on('post.deleted', (e) => {
        if (!spaceId && (e.data as { post_id?: string }).post_id === postId) setFailure('gone')
      }),
      ws.on('post.edited', (e) => {
        const p = (e.data as { post?: FeedPost }).post
        if (!spaceId && p?.id === postId) setPost((prev) => (prev ? mergePostEdit(prev, p) : prev))
      }),
      ws.on('post.reaction_changed', (e) => {
        const p = (e.data as { post?: FeedPost }).post
        if (!spaceId && p?.id === postId) setPost(p)
      }),
    ]
    return () => {
      cancelled = true
      offs.forEach((off) => off())
    }
  }, [postId, spaceId, attempt]) // eslint-disable-line react-hooks/exhaustive-deps

  // Once the thread shows: pick out the linked comment (once per visit).
  const shown = post !== null && comments !== null
  const pickedFound = !!commentId && !!comments?.some((c) => c.id === commentId)
  useEffect(() => {
    if (!shown || !commentId || settledRef.current === commentId) return
    settledRef.current = commentId
    const el = document.getElementById(`comment-${commentId}`)
    if (!el) return
    const scroll = () => el.scrollIntoView({ block: 'center' })
    scroll()
    el.focus({ preventScroll: true })
    // A picture above it that is still loading pushes the comment down:
    // scroll again once it has its size.
    el.closest('.sh-post-permalink')?.querySelectorAll('img').forEach((img) => {
      if (!img.complete) img.addEventListener('load', scroll, { once: true })
    })
  }, [shown, commentId])

  const goBack = (ev: MouseEvent) => {
    ev.preventDefault()
    loc.route(backPath)
  }
  const backLink = (
    <a class="sh-post-permalink__back" href={addBase(backPath)} onClick={goBack}>
      <span aria-hidden="true" class="sh-flip-rtl">←</span>
      <span>{t('post.permalink.back')}</span>
    </a>
  )

  if (failure === 'gone') {
    return (
      <div class="sh-post-permalink">
        {backLink}
        <div class="sh-empty-state" role="status">
          <div aria-hidden="true">🗒️</div>
          <h2>{t('post.permalink.gone_title')}</h2>
          <p>{t('post.permalink.gone_body')}</p>
          <div class="sh-empty-state__cta-row">
            <a class="sh-btn sh-btn--primary" href={addBase(backPath)} onClick={goBack}>
              {t('post.permalink.back')}
            </a>
          </div>
        </div>
      </div>
    )
  }
  if (failure === 'failed') {
    return (
      <div class="sh-post-permalink">
        {backLink}
        <LoadErrorState
          message={t('post.permalink.failed_title')}
          detail={t('post.permalink.failed_body')}
          retryLabel={t('load.try_again')}
          onRetry={() => setAttempt((n) => n + 1)}
          headingLevel={2}
        />
      </div>
    )
  }
  if (!post) {
    return (
      <div class="sh-post-permalink" aria-busy="true">
        {backLink}
        <PostCardSkeleton />
      </div>
    )
  }

  const react = async (emoji: string) => {
    try {
      const updated = await api.post(`${base}/reactions`, { emoji }) as FeedPost | null
      if (updated && typeof updated === 'object' && 'id' in updated) setPost(updated)
      else setPost(await api.get(base) as FeedPost)
    } catch (err: unknown) {
      showToast(t('post.permalink.react_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  const reply = async (parentId: string | null, content: string) => {
    try {
      await api.post(`${base}/comments`, { content, parent_id: parentId })
      await loadComments()
      setPost({ ...post, comment_count: post.comment_count + 1 })
    } catch (err: unknown) {
      showToast(t('comments.error.post', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  const edit = async (cid: string, content: string) => {
    try {
      await api.patch(`${base}/comments/${cid}`, { content })
      await loadComments()
      showToast(t('comments.updated'), 'success')
    } catch (err: unknown) {
      showToast(t('comments.error.edit', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  const remove = async (cid: string) => {
    try {
      await api.delete(`${base}/comments/${cid}`)
      await loadComments()
      setPost({ ...post, comment_count: Math.max(0, post.comment_count - 1) })
      showToast(t('comments.deleted'), 'info')
    } catch (err: unknown) {
      showToast(t('comments.error.delete', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  return (
    <div class="sh-post-permalink">
      {backLink}
      <PostCard
        post={post}
        onReact={(emoji) => { void react(emoji) }}
        onComment={() => document.getElementById('sh-comment-new-input')?.focus()}
        spaceId={spaceId ?? undefined}
        surface={spaceId ? 'space' : 'household'}
      />
      <section class="sh-post-permalink__comments" aria-label={t('post.permalink.comments_aria')}>
        {commentId && comments !== null && !pickedFound && (
          <p class="sh-post-permalink__note sh-muted" role="status">
            {t('post.permalink.comment_gone')}
          </p>
        )}
        {comments === null ? <CommentThreadSkeleton /> : (
          <CommentThread
            comments={comments}
            spaceId={spaceId}
            postId={post.id}
            pickedId={commentId}
            onReply={reply}
            onEdit={edit}
            onDelete={remove}
          />
        )}
      </section>
    </div>
  )
}
