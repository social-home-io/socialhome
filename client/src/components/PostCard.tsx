/**
 * PostCard — canonical post display component (§23.43).
 * Renders in household feed, space feeds, search results.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { Avatar } from './Avatar'
import { OnlinePill } from './OnlinePill'
import { openLightbox } from './ImageLightbox'
import { BazaarPostBody } from './BazaarPostBody'
import { BotAvatar } from './BotAvatar'
import { EventPostCard } from './EventPostCard'
import { HighlightShareCard } from './HighlightShareCard'
import { FileRenderer, VideoRenderer, ImageRenderer } from './FileRenderer'
import { LinkPreviewCard } from './LinkPreviewCard'
import { LocationPostCard } from './LocationPostCard'
import { renderMarkdown } from './markdown'
import { openReport } from './ReportDialog'
import { PollUI } from './PollUI'
import { ReactionPicker } from './ReactionPicker'
import { ScheduleUI } from './ScheduleUI'
import { currentUser } from '@/store/auth'
import { spaceMentionRender } from '@/store/spaceMembers'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import { isOne, t } from '@/i18n/i18n'
import { Button } from './Button'
import type { FeedPost } from '@/types'
import { addBase } from '@/baseUrl'

// Module-level signal so only one reaction picker is open across the
// feed at a time. Holds the post id of the currently open picker, or
// ``null``.
const reactionPickerFor = signal<string | null>(null)

// DB-level marker for posts created by BotBridgeService. Matches
// socialhome.domain.user.SYSTEM_AUTHOR on the backend.
const SYSTEM_AUTHOR = 'system-integration'

function isBotPost(post: FeedPost): boolean {
  return post.author === SYSTEM_AUTHOR
}

/** The server's post-text cap (``MAX_POST_LENGTH``). */
const MAX_POST_LENGTH = 10_000

/** Can this post's text be edited inline? Not a deleted or bot post, and
 *  not one whose text is generated from a linked object (an event card's
 *  title, a bazaar listing). */
export function isTextEditable(post: FeedPost): boolean {
  if (post.content === null || isBotPost(post)) return false
  return post.type !== 'event' && post.type !== 'bazaar'
}

interface PostCardProps {
  post: FeedPost
  onReact?: (emoji: string) => void
  onComment?: () => void
  onDelete?: () => void
  /** Offers "Edit" in the post menu: the text becomes an inline editor,
   *  and Save calls this with the new text. Resolve ``true`` when the
   *  edit is done (saved, or held for review) to close the editor;
   *  ``false`` keeps it open (the caller toasted the error). Only for
   *  posts whose text is editable (see ``isTextEditable``). */
  onEdit?: (content: string) => Promise<boolean>
  /** Space the post belongs to. Threaded into sub-renderers
   *  (avatar / display-name resolvers, PollUI, ScheduleUI). On
   *  ``surface='space'`` callers should pass this so the inline
   *  reactions/zones use the correct scope. */
  spaceId?: string
  /** Display label for a cross-space context (e.g. Corner aggregator).
   *  When provided AND ``surface !== 'space'`` a small badge linking
   *  to the space surfaces under the post chrome. Inside a space's
   *  own feed the badge is redundant — callers there leave this
   *  unset. */
  spaceName?: string
  /** Render context. ``'household'`` (default) shows the author's
   *  zone alongside their online pill — HA zones are household-private.
   *  ``'space'`` suppresses zone names so they never leak across the
   *  household boundary. */
  surface?: 'household' | 'space'
}

export function PostCard({ post, onReact, onComment, onDelete, onEdit, spaceId, spaceName, surface }: PostCardProps) {
  const timeAgo = formatRelative(post.created_at)

  if (post.pinned) {
    return (
      <article class="sh-post sh-post--pinned">
        <div class="sh-post-pin-badge">📌 {t('post.pinned')}</div>
        <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
          onComment={onComment} onDelete={onDelete} onEdit={onEdit}
          spaceId={spaceId} spaceName={spaceName} surface={surface} />
      </article>
    )
  }

  return (
    <article class={`sh-post ${post.content === null ? 'sh-post--deleted' : ''}`}>
      <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
        onComment={onComment} onDelete={onDelete} onEdit={onEdit}
        spaceId={spaceId} spaceName={spaceName} surface={surface} />
    </article>
  )
}

function PostContent({ post, timeAgo, onReact, onComment, onDelete, onEdit, spaceId, spaceName, surface }: PostCardProps & { timeAgo: string }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const closeMenu = () => setMenuOpen(false)
  // Inline edit: the draft text while the editor is open, else ``null``.
  const [draft, setDraft] = useState<string | null>(null)
  const canEdit = !!onEdit && isTextEditable(post)
  const menuButton = useRef<HTMLButtonElement | null>(null)
  // Closing the inline editor hands focus back to the ··· button that
  // opened it (keyboard and screen-reader users land where they were).
  const closeEditor = () => {
    setDraft(null)
    window.setTimeout(() => menuButton.current?.focus(), 0)
  }

  // Menu always exists for non-deleted posts so users can report. Edit /
  // Delete are owner-only and driven by the parent passing callbacks.
  const hasMenu = post.content !== null

  const scopedSpaceId = spaceId ?? null
  const bot = isBotPost(post) ? post.bot ?? null : null
  const avatarUrl = bot ? null : resolveAvatar(scopedSpaceId, post.author, null)
  const authorName = bot
    ? bot.name
    : resolveDisplayName(scopedSpaceId, post.author, post.author)
  // Show the cross-space badge only when the caller explicitly opted
  // in by providing a name AND we're not already inside that space.
  const showCrossSpaceBadge = Boolean(
    spaceName && spaceId && surface !== 'space',
  )
  // Attribution subtext under the bot name:
  //   scope=space  → "via Home Assistant" (shared household voice)
  //   scope=member → "via {member.display_name}" (personal automation)
  //   bot missing  → "via Home Assistant" fallback for posts whose bot was deleted
  const botAttribution = !isBotPost(post)
    ? null
    : bot === null
      ? t('post.via', { name: 'Home Assistant' })
      : bot.scope === 'space'
        ? t('post.via', { name: 'Home Assistant' })
        : t('post.via', { name: bot.created_by_display_name })

  return (
    <>
      {/* Header */}
      <div class={`sh-post-header ${isBotPost(post) ? 'sh-post-header--bot' : ''}`}>
        {isBotPost(post) ? (
          <BotAvatar bot={bot} size={40} />
        ) : (
          <Avatar name={authorName} src={avatarUrl} size={40} />
        )}
        <div class="sh-post-meta">
          <span class="sh-post-author">{authorName}</span>
          {/* Online-status pill — bots and system posts skip it. Zone
              name appears only on the household feed (showZone=true);
              space feeds suppress HA zones via showZone=false on the
              caller surface. */}
          {!isBotPost(post) && (
            <OnlinePill
              user_id={post.author}
              showZone={surface !== 'space' && !spaceId}
            />
          )}
          {botAttribution && (
            <span class="sh-post-bot-attribution">{botAttribution}</span>
          )}
          <span class="sh-post-time">{timeAgo}</span>
          {post.edited_at && <span class="sh-post-edited">(edited)</span>}
        </div>
        {showCrossSpaceBadge && (
          <a class="sh-post-space-badge" href={addBase(`/spaces/${spaceId}`)}>
            {spaceName}
          </a>
        )}
        {hasMenu && (
          <div class="sh-post-overflow-wrap">
            <button
              ref={menuButton}
              class="sh-post-overflow"
              type="button"
              aria-label={t('post.actions')}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
              onBlur={() => setTimeout(closeMenu, 100)}
            >
              ···
            </button>
            {menuOpen && (
              <div class="sh-post-menu" role="menu">
                {canEdit && (
                  <button
                    role="menuitem"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { closeMenu(); setDraft(post.content ?? '') }}
                  >
                    {t('post.edit')}
                  </button>
                )}
                {onDelete && (
                  <button
                    role="menuitem"
                    class="sh-post-menu-danger"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { closeMenu(); onDelete() }}
                  >
                    {t('common.delete')}
                  </button>
                )}
                <button
                  role="menuitem"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    closeMenu()
                    openReport('post', post.id, spaceId)
                  }}
                >
                  {t('report.action')}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Content */}
      <div class="sh-post-content">
        {post.content === null ? (
          <em class="sh-muted">{t('post.deleted')}</em>
        ) : (
          <>
            {/* Event posts own their title rendering inside
                ``EventPostCard`` so the summary doesn't appear twice
                (once as a markdown paragraph here, once as the card's
                headline). Same for highlight shares which carry their
                own headline. Every other post type keeps the
                markdown body. */}
            {draft !== null && onEdit ? (
              <PostEditForm
                draft={draft}
                setDraft={setDraft}
                original={post.content ?? ''}
                requireText={post.type === 'text'}
                onSave={onEdit}
                onDone={closeEditor}
              />
            ) : post.content && post.type !== 'event' && (
              <PostBody content={post.content} spaceId={scopedSpaceId}
                authorId={post.author} />
            )}
            {post.link_preview && <LinkPreviewCard preview={post.link_preview} />}
            {post.type === 'file' && post.file_meta && <FileRenderer file={post.file_meta} />}
            {post.type === 'video' && post.media_url && (
              <VideoRenderer src={post.media_url} poster={post.media_thumbnail_url} mediaStatus={post.media_status} />
            )}
            {post.type === 'image' && post.image_urls?.length > 0 && (
              <PostImageGrid urls={post.image_urls} alt={post.content ?? undefined} />
            )}
            {post.type === 'schedule' && currentUser.value && (
              <ScheduleUI
                postId={post.id}
                authorUserId={post.author}
                currentUserId={currentUser.value.user_id}
                spaceId={scopedSpaceId}
              />
            )}
            {post.type === 'poll' && currentUser.value && (
              <PollUI
                postId={post.id}
                authorUserId={post.author}
                currentUserId={currentUser.value.user_id}
                spaceId={scopedSpaceId}
              />
            )}
            {post.type === 'bazaar' && (
              <BazaarPostBody postId={post.id} />
            )}
            {post.type === 'event' && (
              <EventPostCard eventId={post.linked_event_id ?? null} />
            )}
            {post.type === 'highlight_share' && (
              <HighlightShareCard
                highlightId={post.linked_highlight_id ?? null}
                note={post.content}
              />
            )}
            {post.type === 'location' && post.location && (
              <LocationPostCard location={post.location} />
            )}
            {post.type !== 'file' && post.type !== 'video' &&
              post.type !== 'image' && post.type !== 'schedule' &&
              post.type !== 'poll' && post.type !== 'bazaar' &&
              post.type !== 'event' && post.type !== 'location' &&
              post.type !== 'highlight_share' &&
              post.media_url && (
                <ImageRenderer src={post.media_url} alt={post.content ?? undefined} />
              )}
          </>
        )}
      </div>

      {/* Actions — bot posts intentionally hide the reaction bar and
          comment button. They're system notifications, not conversation
          starters; reactions/comments would muddy the signal and create
          follow-up threads that can't be routed back to an HA entity. */}
      {post.content !== null && !isBotPost(post) && (
        <div class="sh-post-actions">
          <div class="sh-reactions">
            {Object.entries(post.reactions || {}).map(([emoji, users]) => (
              <button key={emoji} class="sh-reaction-chip"
                onClick={() => onReact?.(emoji)}>
                {emoji} {(users as string[]).length}
              </button>
            ))}
            <div class="sh-reaction-add-wrap">
              <button
                class="sh-reaction-add"
                aria-label={t('post.add_reaction')}
                aria-haspopup="dialog"
                aria-expanded={reactionPickerFor.value === post.id}
                onClick={() => {
                  reactionPickerFor.value =
                    reactionPickerFor.value === post.id ? null : post.id
                }}>
                <span class="sh-reaction-add__face" aria-hidden="true">🙂</span>
                <span class="sh-reaction-add__plus" aria-hidden="true">+</span>
              </button>
              {reactionPickerFor.value === post.id && (
                <ReactionPicker
                  onSelect={(emoji) => onReact?.(emoji)}
                  onClose={() => { reactionPickerFor.value = null }}
                />
              )}
            </div>
          </div>
          {/* Comment count chip — invites a click on cold posts
           *  ("💬 Comment") and reads as a normal sentence on warm
           *  ones ("💬 1 comment" / "💬 5 comments") rather than the
           *  bare-number format that read like a count badge. */}
          <button
            class="sh-comment-btn"
            onClick={onComment}
            aria-label={
              post.comment_count === 0
                ? t('post.comment_aria')
                : t(isOne(post.comment_count) ? 'post.comments_open_one' : 'post.comments_open', { n: String(post.comment_count) })
            }
          >
            💬 {post.comment_count === 0
              ? t('post.comment')
              : t(isOne(post.comment_count) ? 'post.comments_one' : 'post.comments', { n: String(post.comment_count) })}
          </button>
        </div>
      )}
      {/* Latest-comment preview — a one-liner that surfaces engagement
       *  on the card without forcing the reader into the comment
       *  overlay just to see if there is anything to read. Hidden when
       *  the server didn't carry the field (space feeds today) or when
       *  the post has no comments yet. Tapping the line opens the
       *  thread, same as the "💬 N comments" button. */}
      {post.latest_comment && (
        <button
          type="button"
          class="sh-post-latest-comment"
          onClick={onComment}
          aria-label={t('post.open_thread')}
        >
          <LatestCommentPreview
            scopedSpaceId={scopedSpaceId}
            comment={post.latest_comment}
          />
        </button>
      )}
    </>
  )
}

/** One-line preview of the newest comment on a post.  Renders on the
 *  card itself, below the reactions row, so engaged posts feel "alive"
 *  without a click into the overlay. */
function LatestCommentPreview({
  scopedSpaceId,
  comment,
}: {
  scopedSpaceId: string | null
  comment: NonNullable<FeedPost['latest_comment']>
}) {
  const authorName = resolveDisplayName(scopedSpaceId, comment.author, comment.author)
  const avatarUrl = resolveAvatar(scopedSpaceId, comment.author, null)
  const body = comment.deleted
    ? t('post.comment_removed')
    : comment.content
      ? comment.content.replace(/\s+/g, ' ').trim()
      : comment.media_url
        ? `🖼️ ${t('post.image')}`
        : ''
  const truncated = body.length > 120 ? `${body.slice(0, 120)}…` : body
  return (
    <span class="sh-post-latest-comment-row">
      <Avatar name={authorName} src={avatarUrl} size={20} />
      <span class="sh-post-latest-comment-name">{authorName}</span>
      <span class="sh-post-latest-comment-body">{truncated}</span>
    </span>
  )
}

/** WhatsApp-style image grid for an image post. Layouts:
 *  - 1 image  → full-width, ``aspect-ratio: auto`` (lets tall photos stay tall)
 *  - 2 images → 2 columns equal
 *  - 3 images → 1 large left + 2 stacked right
 *  - 4 images → 2x2 grid
 *  - 5 images → 2x2 grid; the 4th tile is a "+1" overlay tappable to
 *    the lightbox at index 4
 *
 *  Click any tile → ``openLightbox`` with the full URL list and the
 *  clicked index, so the user can swipe / arrow through every image.
 */
function PostImageGrid({ urls, alt }: { urls: string[]; alt?: string }) {
  const count = Math.min(urls.length, 5)
  const layoutClass = `sh-post-image-grid sh-post-image-grid--${count}`
  const open = (index: number) => {
    openLightbox({
      items: urls.map((url) => ({ url, item_type: 'photo' as const })),
      index,
    })
  }
  // For 5+ images we render the first 4 tiles plus a "+N" overlay on
  // the 4th (which counts the remaining ``urls.length - 3`` images).
  const overflow = urls.length > 4 ? urls.length - 3 : 0
  const visibleCount = overflow > 0 ? 3 : count
  return (
    <div class={layoutClass}>
      {urls.slice(0, visibleCount).map((url, i) => (
        <button
          type="button"
          key={url}
          class="sh-post-image-tile"
          aria-label={alt || `Image ${i + 1} of ${urls.length}`}
          onClick={() => open(i)}
        >
          <img src={url} alt="" loading="lazy" />
        </button>
      ))}
      {overflow > 0 && (
        <button
          type="button"
          class="sh-post-image-tile sh-post-image-tile--more"
          aria-label={t('post.open_images', { n: String(urls.length) })}
          onClick={() => open(visibleCount)}
        >
          <img src={urls[visibleCount]} alt="" loading="lazy" />
          <span class="sh-post-image-more">+{overflow}</span>
        </button>
      )}
    </div>
  )
}

/** The inline editor that replaces a post's text while editing. Esc
 *  cancels, Ctrl/⌘+Enter saves; focus lands in the field. */
function PostEditForm({ draft, setDraft, original, requireText, onSave, onDone }: {
  draft: string
  setDraft: (s: string) => void
  original: string
  /** A text post can't be emptied (it would be a blank card). */
  requireText: boolean
  onSave: (content: string) => Promise<boolean>
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [])
  const unchanged = draft === original
  const empty = requireText && !draft.trim()
  const save = async () => {
    if (busy || unchanged || empty) return
    setBusy(true)
    try {
      if (await onSave(draft)) onDone()
    } finally {
      setBusy(false)
    }
  }
  return (
    <form
      class="sh-post-edit"
      onSubmit={(e) => { e.preventDefault(); void save() }}
    >
      <textarea
        ref={ref}
        class="sh-post-edit-input"
        aria-label={t('post.edit.label')}
        value={draft}
        maxLength={MAX_POST_LENGTH}
        rows={Math.min(12, Math.max(3, draft.split('\n').length + 1))}
        onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
        onKeyDown={(e) => {
          // No cancel mid-save: the answer would land on a closed editor.
          if (e.key === 'Escape') { e.preventDefault(); if (!busy) onDone() }
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void save() }
        }}
      />
      <div class="sh-post-edit-actions">
        <Button variant="secondary" type="button" onClick={onDone} disabled={busy}>
          {t('common.cancel')}
        </Button>
        <Button type="submit" loading={busy} disabled={unchanged || empty}>
          {t('common.save')}
        </Button>
      </div>
    </form>
  )
}

/** Threshold above which the post body is collapsed behind a "Show
 *  more" toggle. Roughly the height of a 12-line post — long enough
 *  to be informative, short enough that a wall of text doesn't
 *  dominate the feed. */
const SHOW_MORE_THRESHOLD = 600

function PostBody({ content, spaceId, authorId }: {
  content: string
  spaceId?: string | null
  authorId?: string | null
}) {
  const [expanded, setExpanded] = useState(false)
  const isLong = content.length > SHOW_MORE_THRESHOLD
  // Space posts highlight @-mentions of the space's members.
  const html = renderMarkdown(content, spaceMentionRender(spaceId, authorId))
  if (!isLong) {
    return (
      <div
        class="sh-post-body"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    )
  }
  return (
    <div class={`sh-post-body sh-post-body--clamp${expanded ? ' sh-post-body--expanded' : ''}`}>
      <div
        class="sh-post-body-inner"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      {!expanded && (
        <button
          type="button"
          class="sh-post-body-more sh-link"
          onClick={() => setExpanded(true)}
        >
          {t('post.show_more')}
        </button>
      )}
    </div>
  )
}


function formatRelative(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return t('time.just_now')
  if (mins < 60) return t('time.minutes_ago_short', { n: String(mins) })
  const hours = Math.floor(mins / 60)
  if (hours < 24) return t('time.hours_ago_short', { n: String(hours) })
  const days = Math.floor(hours / 24)
  return t('time.days_ago_short', { n: String(days) })
}
