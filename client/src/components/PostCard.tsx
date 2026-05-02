/**
 * PostCard — canonical post display component (§23.43).
 * Renders in household feed, space feeds, search results.
 */
import { useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { Avatar } from './Avatar'
import { OnlinePill } from './OnlinePill'
import { openLightbox } from './ImageLightbox'
import { BazaarPostBody } from './BazaarPostBody'
import { BotAvatar } from './BotAvatar'
import { EventPostCard } from './EventPostCard'
import { FileRenderer, VideoRenderer, ImageRenderer } from './FileRenderer'
import { LocationPostCard } from './LocationPostCard'
import { renderMarkdown } from './markdown'
import { openReport } from './ReportDialog'
import { PollUI } from './PollUI'
import { ReactionPicker } from './ReactionPicker'
import { ScheduleUI } from './ScheduleUI'
import { currentUser } from '@/store/auth'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { FeedPost } from '@/types'

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

interface PostCardProps {
  post: FeedPost
  onReact?: (emoji: string) => void
  onComment?: () => void
  onDelete?: () => void
  onEdit?: () => void
  showSpaceBadge?: string
  /** Render context. ``'household'`` (default) shows the author's
   *  zone alongside their online pill — HA zones are household-private.
   *  ``'space'`` suppresses zone names so they never leak across the
   *  household boundary. */
  surface?: 'household' | 'space'
}

export function PostCard({ post, onReact, onComment, onDelete, onEdit, showSpaceBadge, surface }: PostCardProps) {
  const timeAgo = formatRelative(post.created_at)

  if (post.pinned) {
    return (
      <article class="sh-post sh-post--pinned">
        <div class="sh-post-pin-badge">📌 Pinned</div>
        <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
          onComment={onComment} onDelete={onDelete} onEdit={onEdit}
          showSpaceBadge={showSpaceBadge} surface={surface} />
      </article>
    )
  }

  return (
    <article class={`sh-post ${post.content === null ? 'sh-post--deleted' : ''}`}>
      <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
        onComment={onComment} onDelete={onDelete} onEdit={onEdit}
        showSpaceBadge={showSpaceBadge} surface={surface} />
    </article>
  )
}

function PostContent({ post, timeAgo, onReact, onComment, onDelete, onEdit, showSpaceBadge, surface }: PostCardProps & { timeAgo: string }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const closeMenu = () => setMenuOpen(false)

  // Menu always exists for non-deleted posts so users can report. Edit /
  // Delete are owner-only and driven by the parent passing callbacks.
  const hasMenu = post.content !== null

  const spaceId = showSpaceBadge ?? null
  const bot = isBotPost(post) ? post.bot ?? null : null
  const avatarUrl = bot ? null : resolveAvatar(spaceId, post.author, null)
  const authorName = bot
    ? bot.name
    : resolveDisplayName(spaceId, post.author, post.author)
  // Attribution subtext under the bot name:
  //   scope=space  → "via Home Assistant" (shared household voice)
  //   scope=member → "via {member.display_name}" (personal automation)
  //   bot missing  → "via Home Assistant" fallback for posts whose bot was deleted
  const botAttribution = !isBotPost(post)
    ? null
    : bot === null
      ? 'via Home Assistant'
      : bot.scope === 'space'
        ? 'via Home Assistant'
        : `via ${bot.created_by_display_name}`

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
              showZone={surface !== 'space' && !showSpaceBadge}
            />
          )}
          {botAttribution && (
            <span class="sh-post-bot-attribution">{botAttribution}</span>
          )}
          <span class="sh-post-time">{timeAgo}</span>
          {post.edited_at && <span class="sh-post-edited">(edited)</span>}
        </div>
        {showSpaceBadge && <a class="sh-post-space-badge" href={`/spaces/${showSpaceBadge}`}>{showSpaceBadge}</a>}
        {hasMenu && (
          <div class="sh-post-overflow-wrap">
            <button
              class="sh-post-overflow"
              type="button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
              onBlur={() => setTimeout(closeMenu, 100)}
            >
              ···
            </button>
            {menuOpen && (
              <div class="sh-post-menu" role="menu">
                {onEdit && (
                  <button
                    role="menuitem"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { closeMenu(); onEdit() }}
                  >
                    Edit
                  </button>
                )}
                {onDelete && (
                  <button
                    role="menuitem"
                    class="sh-post-menu-danger"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { closeMenu(); onDelete() }}
                  >
                    Delete
                  </button>
                )}
                <button
                  role="menuitem"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    closeMenu()
                    openReport('post', post.id)
                  }}
                >
                  Report
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Content */}
      <div class="sh-post-content">
        {post.content === null ? (
          <em class="sh-muted">This post was deleted</em>
        ) : (
          <>
            {post.content && (
              <div
                class="sh-post-body"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(post.content) }}
              />
            )}
            {post.type === 'file' && post.file_meta && <FileRenderer file={post.file_meta} />}
            {post.type === 'video' && post.media_url && (
              <VideoRenderer src={post.media_url} />
            )}
            {post.type === 'image' && post.image_urls?.length > 0 && (
              <PostImageGrid urls={post.image_urls} alt={post.content ?? undefined} />
            )}
            {post.type === 'schedule' && currentUser.value && (
              <ScheduleUI
                postId={post.id}
                authorUserId={post.author}
                currentUserId={currentUser.value.user_id}
                spaceId={showSpaceBadge ?? null}
              />
            )}
            {post.type === 'poll' && currentUser.value && (
              <PollUI
                postId={post.id}
                authorUserId={post.author}
                currentUserId={currentUser.value.user_id}
                spaceId={showSpaceBadge ?? null}
              />
            )}
            {post.type === 'bazaar' && (
              <BazaarPostBody postId={post.id} />
            )}
            {post.type === 'event' && (
              <EventPostCard eventId={post.linked_event_id ?? null} />
            )}
            {post.type === 'location' && post.location && (
              <LocationPostCard location={post.location} />
            )}
            {post.type !== 'file' && post.type !== 'video' &&
              post.type !== 'image' && post.type !== 'schedule' &&
              post.type !== 'poll' && post.type !== 'bazaar' &&
              post.type !== 'event' && post.type !== 'location' &&
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
                aria-label="Add reaction"
                aria-haspopup="dialog"
                aria-expanded={reactionPickerFor.value === post.id}
                onClick={() => {
                  reactionPickerFor.value =
                    reactionPickerFor.value === post.id ? null : post.id
                }}>
                +
              </button>
              {reactionPickerFor.value === post.id && (
                <ReactionPicker
                  onSelect={(emoji) => onReact?.(emoji)}
                  onClose={() => { reactionPickerFor.value = null }}
                />
              )}
            </div>
          </div>
          <button class="sh-comment-btn" onClick={onComment}>
            💬 {post.comment_count}
          </button>
        </div>
      )}
    </>
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
          aria-label={`Open ${urls.length} images`}
          onClick={() => open(visibleCount)}
        >
          <img src={urls[visibleCount]} alt="" loading="lazy" />
          <span class="sh-post-image-more">+{overflow}</span>
        </button>
      )}
    </div>
  )
}

function formatRelative(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
