/**
 * PostCard — canonical post display component (§23.43).
 * Renders in household feed, space feeds, search results.
 */
import { useState } from 'preact/hooks'
import { Avatar } from './Avatar'
import { BazaarPostBody } from './BazaarPostBody'
import { BotAvatar } from './BotAvatar'
import { EventPostCard } from './EventPostCard'
import { FileRenderer, VideoRenderer, ImageRenderer } from './FileRenderer'
import { renderMarkdown } from './markdown'
import { openReport } from './ReportDialog'
import { PollUI } from './PollUI'
import { ScheduleUI } from './ScheduleUI'
import { currentUser } from '@/store/auth'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { FeedPost } from '@/types'

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
}

export function PostCard({ post, onReact, onComment, onDelete, onEdit, showSpaceBadge }: PostCardProps) {
  const timeAgo = formatRelative(post.created_at)

  if (post.pinned) {
    return (
      <article class="sh-post sh-post--pinned">
        <div class="sh-post-pin-badge">📌 Pinned</div>
        <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
          onComment={onComment} onDelete={onDelete} onEdit={onEdit}
          showSpaceBadge={showSpaceBadge} />
      </article>
    )
  }

  return (
    <article class={`sh-post ${post.content === null ? 'sh-post--deleted' : ''}`}>
      <PostContent post={post} timeAgo={timeAgo} onReact={onReact}
        onComment={onComment} onDelete={onDelete} onEdit={onEdit}
        showSpaceBadge={showSpaceBadge} />
    </article>
  )
}

function PostContent({ post, timeAgo, onReact, onComment, onDelete, onEdit, showSpaceBadge }: PostCardProps & { timeAgo: string }) {
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
            {post.type === 'image' && post.media_url && (
              <ImageRenderer src={post.media_url} alt={post.content ?? undefined} />
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
            {post.type !== 'file' && post.type !== 'video' &&
              post.type !== 'image' && post.type !== 'schedule' &&
              post.type !== 'poll' && post.type !== 'bazaar' &&
              post.type !== 'event' &&
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
            <button class="sh-reaction-add" onClick={() => onReact?.('👍')}>+</button>
          </div>
          <button class="sh-comment-btn" onClick={onComment}>
            💬 {post.comment_count}
          </button>
        </div>
      )}
    </>
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
