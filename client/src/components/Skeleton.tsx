/**
 * Skeleton — primitive shimmer-block + page-shaped helpers (§UX).
 *
 * Replaces the generic ``<Spinner />`` first-paint flash with a
 * placeholder that matches the final layout. The layout-stable
 * approach lets the eye relax on the page chrome (avatar circle,
 * post body bars, ring grid) while the data lands; when the real
 * content swaps in there's no jarring re-mount.
 *
 * The primitives are intentionally bare — width / height / shape are
 * controlled inline by callers because each page has its own
 * proportions. A single ``.sh-skeleton`` rule in ``app.css`` provides
 * the shared shimmer animation.
 */
import type { JSX } from 'preact'
import { t } from '@/i18n/i18n'


interface SkeletonProps {
  /** ``rect`` (default): rounded rectangle. ``line``: short text bar.
   *  ``circle``: avatar / ring placeholder. ``card``: card-shaped
   *  block with the standard card chrome. */
  shape?: 'rect' | 'line' | 'circle' | 'card'
  width?: number | string
  height?: number | string
  /** Additional class names for layout / margins. */
  class?: string
  /** Override the ARIA label that screen readers announce while the
   *  skeleton is on screen. Default: "Loading". */
  ariaLabel?: string
  style?: JSX.CSSProperties
}


export function Skeleton({
  shape = 'rect',
  width,
  height,
  class: className,
  ariaLabel,
  style,
}: SkeletonProps) {
  const cls =
    `sh-skeleton sh-skeleton--${shape}` + (className ? ` ${className}` : '')
  const inline: JSX.CSSProperties = { ...style }
  if (width != null) inline.width = typeof width === 'number' ? `${width}px` : width
  if (height != null) inline.height = typeof height === 'number' ? `${height}px` : height
  return (
    <span
      class={cls}
      style={inline}
      role="status"
      aria-label={ariaLabel ?? 'Loading'}
      aria-busy="true"
    />
  )
}


/** Stand-in for a ``PostCard`` while the feed loads. Reproduces the
 *  card chrome (avatar, two-line author meta, body lines, optional
 *  media block) so the layout doesn't shift when the real card lands. */
export function PostCardSkeleton({ withMedia = false }: { withMedia?: boolean }) {
  return (
    <article class="sh-post sh-post--skeleton" aria-hidden="true">
      <div class="sh-post-header">
        <Skeleton shape="circle" width={40} height={40} />
        <div class="sh-post-meta">
          <Skeleton shape="line" width="35%" height={12} />
          <Skeleton shape="line" width="20%" height={10} class="sh-skeleton-spaced" />
        </div>
      </div>
      <div class="sh-post-body">
        <Skeleton shape="line" width="92%" height={12} />
        <Skeleton shape="line" width="78%" height={12} class="sh-skeleton-spaced" />
        <Skeleton shape="line" width="55%" height={12} class="sh-skeleton-spaced" />
        {withMedia && (
          <Skeleton
            shape="rect"
            width="100%"
            height={180}
            class="sh-skeleton-spaced"
          />
        )}
      </div>
    </article>
  )
}


/** Stand-in for the household feed: 3 post cards (one with media)
 *  plus a thin presence-strip header row. */
export function FeedSkeleton() {
  return (
    <div class="sh-feed sh-feed--skeleton" aria-busy="true">
      <div class="sh-skeleton-presence-strip">
        {[0, 1, 2, 3].map(i => (
          <Skeleton key={i} shape="circle" width={36} height={36} />
        ))}
      </div>
      <div class="sh-feed-item"><PostCardSkeleton /></div>
      <div class="sh-feed-item"><PostCardSkeleton withMedia /></div>
      <div class="sh-feed-item"><PostCardSkeleton /></div>
    </div>
  )
}


/** Stand-in for the DM inbox: 5 rows with avatar + name + preview. */
export function DmInboxSkeleton() {
  return (
    <ul class="sh-dm-inbox sh-dm-inbox--skeleton" aria-busy="true">
      {[0, 1, 2, 3, 4].map(i => (
        <li key={i} class="sh-dm-inbox-row">
          <Skeleton shape="circle" width={44} height={44} />
          <div class="sh-dm-inbox-row-meta">
            <Skeleton shape="line" width="40%" height={12} />
            <Skeleton shape="line" width="65%" height={10} class="sh-skeleton-spaced" />
          </div>
        </li>
      ))}
    </ul>
  )
}


/** Stand-in for the Highlights inbox: + tile + 5 rings. */
export function HighlightsRingSkeleton() {
  return (
    <div class="sh-highlights-page sh-highlights-page--skeleton" aria-busy="true">
      <header class="sh-highlights-header">
        <Skeleton shape="line" width={120} height={20} />
      </header>
      <div class="sh-highlight-rings">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} class="sh-highlight-ring">
            <Skeleton shape="circle" width={64} height={64} />
            <Skeleton shape="line" width={56} height={10} class="sh-skeleton-spaced" />
          </div>
        ))}
      </div>
    </div>
  )
}


/** Stand-in for ``CalendarPage``: header row + 5×7 day-grid placeholder
 *  shaped to match the rendered calendar so the swap is in-place. */
export function CalendarSkeleton() {
  return (
    <div class="sh-calendar sh-calendar--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={140} height={22} />
        <Skeleton shape="rect" width={120} height={32} />
      </div>
      <div class="sh-calendar-controls">
        <Skeleton shape="rect" width={80} height={28} />
        <Skeleton shape="line" width={160} height={18} />
        <Skeleton shape="rect" width={80} height={28} />
      </div>
      <div class="sh-skeleton-calendar-grid" aria-hidden="true">
        {Array.from({ length: 35 }).map((_, i) => (
          <Skeleton
            key={i}
            shape="rect"
            width="100%"
            height={64}
            class="sh-skeleton-calendar-day"
          />
        ))}
      </div>
    </div>
  )
}


/** Stand-in for ``SpaceListPage`` and ``SpaceBrowserPage``: N space-card
 *  shapes with emoji + name + description bars. */
export function SpaceListSkeleton({ count = 4 }: { count?: number } = {}) {
  return (
    <div class="sh-spaces sh-spaces--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={120} height={22} />
      </div>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} class="sh-space-card sh-space-card--skeleton">
          <Skeleton shape="circle" width={44} height={44} />
          <div class="sh-space-card__body">
            <Skeleton shape="line" width="50%" height={14} />
            <Skeleton
              shape="line"
              width="80%"
              height={11}
              class="sh-skeleton-spaced"
            />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for ``BazaarPage``: 3 listing cards in a column. */
export function BazaarSkeleton() {
  return (
    <div class="sh-bazaar sh-bazaar--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="line" width={120} height={22} />
        <Skeleton shape="rect" width={140} height={32} />
      </div>
      <div class="sh-bazaar-filters">
        <Skeleton shape="rect" width={100} height={28} />
        <Skeleton shape="rect" width={100} height={28} />
        <Skeleton shape="rect" width={140} height={28} />
      </div>
      {[0, 1, 2].map(i => (
        <div key={i} class="sh-bazaar-card sh-bazaar-card--skeleton">
          <Skeleton shape="rect" width={120} height={120} />
          <div class="sh-bazaar-card__body">
            <Skeleton shape="line" width="60%" height={14} />
            <Skeleton shape="line" width="35%" height={12} class="sh-skeleton-spaced" />
            <Skeleton shape="line" width="85%" height={11} class="sh-skeleton-spaced" />
            <Skeleton shape="line" width="70%" height={11} class="sh-skeleton-spaced" />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for the comment thread inside ``CommentOverlay``: a few
 *  short rows with avatar + author + line. The overlay swaps this in
 *  while the comment fetch is in flight, replacing the previous
 *  full-width spinner so the user sees the chat-shaped layout
 *  immediately. */
export function CommentThreadSkeleton({ count = 3 }: { count?: number } = {}) {
  return (
    <div class="sh-comment-thread sh-comment-thread--skeleton" aria-busy="true">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} class="sh-comment-item sh-comment-item--skeleton">
          <Skeleton shape="circle" width={28} height={28} />
          <div class="sh-comment-body">
            <Skeleton shape="line" width="30%" height={11} />
            <Skeleton
              shape="line"
              width={i % 2 === 0 ? '85%' : '60%'}
              height={12}
              class="sh-skeleton-spaced"
            />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for the DM thread bubble layout: alternating-side
 *  message blocks, brief flash on cold-load before the WS hydrate
 *  fills the list.
 *
 *  ``sh-thread--skeleton`` pins the placeholder bubbles to the BOTTOM
 *  of the container (via ``justify-content: flex-end`` in app.css) so
 *  that the swap from skeleton → real-messages is positionally
 *  invisible: the last real message lands right where the last
 *  skeleton bubble was, no top-to-bottom content jump the user
 *  perceives as "the chat scrolled". */
export function DmThreadSkeleton({ count = 4 }: { count?: number } = {}) {
  return (
    <div class="sh-thread sh-thread--skeleton" aria-busy="true">
      <div class="sh-thread-header">
        <Skeleton shape="line" width={140} height={14} />
      </div>
      <div class="sh-messages">
        {Array.from({ length: count }).map((_, i) => {
          const mine = i % 2 === 1
          return (
            <div
              key={i}
              class={`sh-message sh-message--skeleton${mine ? ' sh-message--mine' : ''}`}
            >
              <Skeleton
                shape="line"
                width={mine ? 220 : 160}
                height={12}
              />
              <Skeleton
                shape="line"
                width={mine ? 140 : 90}
                height={11}
                class="sh-skeleton-spaced"
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}


/** Stand-in for ``NotificationsPage``: a header + a stack of rows
 *  with leading status dot + title + timestamp line. */
export function NotificationListSkeleton({ count = 5 }: { count?: number } = {}) {
  return (
    <div class="sh-notifications-page sh-notifications-page--skeleton" aria-busy="true">
      <div class="sh-page-header">
        <Skeleton shape="rect" width={120} height={32} />
      </div>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} class="sh-notif-row sh-notif-row--skeleton">
          <Skeleton shape="circle" width={12} height={12} />
          <div class="sh-notif-content">
            <Skeleton
              shape="line"
              width={i % 2 === 0 ? '70%' : '55%'}
              height={13}
            />
            <Skeleton
              shape="line"
              width="35%"
              height={10}
              class="sh-skeleton-spaced"
            />
          </div>
        </div>
      ))}
    </div>
  )
}


/** Stand-in for ``MomentumPage``: composer entry + 3 rows. Each row
 *  has the avatar + author/time meta + 1-2 content bars + the
 *  reply/reaction chip strip so the layout doesn't shift when the real
 *  inbox lands. */
export function MomentumInboxSkeleton({ count = 3 }: { count?: number } = {}) {
  return (
    <div class="sh-momentum sh-momentum--skeleton" aria-busy="true">
      <header class="sh-momentum-header">
        <Skeleton shape="line" width={120} height={22} />
      </header>
      <Skeleton
        shape="rect"
        width="100%"
        height={48}
        class="sh-skeleton-momentum-compose"
      />
      <ul class="sh-momentum-list">
        {Array.from({ length: count }).map((_, i) => (
          <li key={i} class="sh-momentum-row sh-momentum-row--skeleton">
            <Skeleton shape="circle" width={36} height={36} />
            <div class="sh-momentum-row-body">
              <div class="sh-momentum-row-head">
                <Skeleton shape="line" width="30%" height={12} />
                <Skeleton
                  shape="line"
                  width={40}
                  height={10}
                  class="sh-skeleton-spaced"
                />
              </div>
              <Skeleton shape="line" width="85%" height={12} />
              <Skeleton
                shape="line"
                width="65%"
                height={12}
                class="sh-skeleton-spaced"
              />
              <div class="sh-momentum-row-chips">
                <Skeleton shape="rect" width={48} height={20} />
                <Skeleton shape="rect" width={48} height={20} />
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}


/** Stand-in for ``MomentumDetailPage``: detail header + content +
 *  reaction strip + replies list. Same row shape as the inbox skeleton
 *  so transition between the two routes feels continuous. */
export function MomentumDetailSkeleton() {
  return (
    <div class="sh-momentum-detail sh-momentum-detail--skeleton" aria-busy="true">
      <header class="sh-momentum-detail-header">
        <Skeleton shape="circle" width={48} height={48} />
        <div class="sh-momentum-detail-meta">
          <Skeleton shape="line" width={140} height={14} />
          <Skeleton
            shape="line"
            width={100}
            height={11}
            class="sh-skeleton-spaced"
          />
        </div>
      </header>
      <Skeleton shape="line" width="92%" height={12} />
      <Skeleton shape="line" width="78%" height={12} class="sh-skeleton-spaced" />
      <Skeleton shape="line" width="60%" height={12} class="sh-skeleton-spaced" />
      <div class="sh-momentum-reactions">
        <Skeleton shape="rect" width={220} height={36} />
      </div>
      <ul class="sh-momentum-list">
        {[0, 1].map(i => (
          <li key={i} class="sh-momentum-row sh-momentum-row--skeleton">
            <Skeleton shape="circle" width={32} height={32} />
            <div class="sh-momentum-row-body">
              <Skeleton shape="line" width="25%" height={11} />
              <Skeleton
                shape="line"
                width="70%"
                height={12}
                class="sh-skeleton-spaced"
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}


/** Stand-in for ``MomentumArchivePage``: title row + a day section
 *  with 2 rows. The chip row appears later (only after trending lands)
 *  so the skeleton omits it. */
export function MomentumArchiveSkeleton() {
  return (
    <div class="sh-momentum-archive sh-momentum-archive--skeleton" aria-busy="true">
      <Skeleton shape="line" width={180} height={22} />
      <Skeleton
        shape="line"
        width="60%"
        height={12}
        class="sh-skeleton-spaced"
      />
      <section class="sh-momentum-archive-day">
        <Skeleton shape="line" width={220} height={14} />
        <ul class="sh-momentum-list">
          {[0, 1].map(i => (
            <li key={i} class="sh-momentum-row sh-momentum-row--skeleton">
              <Skeleton shape="circle" width={32} height={32} />
              <div class="sh-momentum-row-body">
                <Skeleton shape="line" width="30%" height={11} />
                <Skeleton
                  shape="line"
                  width="80%"
                  height={12}
                  class="sh-skeleton-spaced"
                />
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}


/** Stand-in for ``TaskPage``: sidebar list + main column with task rows. */
export function TasksSkeleton() {
  return (
    <div class="sh-tasks sh-tasks--skeleton" aria-busy="true">
      <aside class="sh-tasks-sidebar">
        <Skeleton shape="line" width="60%" height={14} />
        {[0, 1, 2].map(i => (
          <Skeleton
            key={i}
            shape="line"
            width="80%"
            height={12}
            class="sh-skeleton-spaced"
          />
        ))}
      </aside>
      <main class="sh-tasks-main">
        <Skeleton shape="line" width="40%" height={20} />
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} class="sh-skeleton-task-row">
            <Skeleton shape="rect" width={18} height={18} />
            <Skeleton shape="line" width="65%" height={13} />
          </div>
        ))}
      </main>
    </div>
  )
}


/** A decorative shimmer bar — no role, no label. Only for use inside a
 *  container that already announces itself (``ListSkeleton``). */
function Bone({ class: cls = '', width, height }: {
  class?: string
  width?: string | number
  height?: number
}) {
  const style: JSX.CSSProperties = {}
  if (width != null) style.width = typeof width === 'number' ? `${width}px` : width
  if (height != null) style.height = `${height}px`
  return <span class={`sh-skeleton sh-skeleton--line ${cls}`.trim()} style={style} />
}

const ROW_WIDTHS = ['62%', '48%', '71%', '40%', '56%', '66%', '44%']

/** Stand-in for the Organize lists while they load.
 *
 *  - ``list``: rows with a round tick and a text bar (Shopping, Tasks).
 *  - ``board``: three columns of cards (the task board).
 *  - ``sticky``: a grid of note tiles (Stickies).
 *
 *  One visually-hidden status line, announced once, next to one
 *  ``aria-busy`` / ``aria-hidden`` block of bones — so a screen reader
 *  hears "Loading" once, not once per bar. */
export function ListSkeleton({
  variant = 'list',
  rows = 5,
  label,
}: {
  variant?: 'list' | 'board' | 'sticky'
  rows?: number
  label?: string
} = {}) {
  return (
    <div class={`sh-list-skeleton sh-list-skeleton--${variant}`}>
      {/* A sibling of the busy art, so it IS announced (content under
        * aria-busy is skipped until it clears). */}
      <span class="sr-only" role="status">{label ?? t('common.loading')}</span>
      <div class="sh-list-skeleton__art" aria-hidden="true" aria-busy="true">
        {variant === 'list' && Array.from({ length: rows }).map((_, i) => (
          <div key={i} class="sh-list-skeleton__row">
            <span class="sh-skeleton sh-skeleton--circle sh-list-skeleton__tick" />
            <Bone width={ROW_WIDTHS[i % ROW_WIDTHS.length]} height={13} />
          </div>
        ))}
        {variant === 'board' && [3, 2, 1].map((cards, col) => (
          <div key={col} class="sh-list-skeleton__column">
            <Bone width="40%" height={14} />
            {Array.from({ length: cards }).map((_, i) => (
              <div key={i} class="sh-list-skeleton__card">
                <Bone width={ROW_WIDTHS[(col + i) % ROW_WIDTHS.length]} height={12} />
                <Bone width="30%" height={10} />
              </div>
            ))}
          </div>
        ))}
        {variant === 'sticky' && Array.from({ length: rows }).map((_, i) => (
          <div key={i} class="sh-list-skeleton__note">
            <Bone width="80%" height={12} />
            <Bone width="55%" height={12} />
          </div>
        ))}
      </div>
    </div>
  )
}
