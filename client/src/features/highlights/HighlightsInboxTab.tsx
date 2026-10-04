/**
 * HighlightsInboxTab — recent rings + per-author list (§Highlights).
 *
 * Renders a horizontal "rings" row at the top (avatars circled with a
 * terracotta glow when the viewer has unseen frames) plus a per-author
 * grouped list below. Tapping a ring or row entry routes to
 * :class:`HighlightViewerPage` with that highlight id.
 *
 * Authors get a "+ New" tile leading to :class:`HighlightComposerPage`.
 *
 * Audience filtering happens server-side; this tab just renders the
 * inbox the API returns. The host :class:`HighlightsPage` switches
 * between this and :class:`HighlightArchiveTab`.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { HighlightsRingSkeleton } from '@/components/Skeleton'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { openHighlightQuickShare } from '@/components/HighlightQuickShareDialog'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { openUserActions } from '@/components/UserActionsMenu'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { ws } from '@/ws'
import type { HighlightInboxItem } from '@/types'
import { addBase } from '@/baseUrl'

const inbox = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(true)


function humaniseDate(iso: string): string {
  // Highlights list groups by ``highlight_date`` (YYYY-MM-DD UTC). For a
  // friendlier inbox we surface "Today" / "Yesterday" / weekday for
  // the recent past, and a fully spelled-out date otherwise.
  const today = new Date()
  const todayStr = today.toISOString().slice(0, 10)
  if (iso === todayStr) return 'Today'
  const yest = new Date(today)
  yest.setUTCDate(yest.getUTCDate() - 1)
  if (iso === yest.toISOString().slice(0, 10)) return 'Yesterday'
  const dt = new Date(iso + 'T00:00:00Z')
  if (Number.isNaN(dt.getTime())) return iso
  return dt.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  })
}


export default function HighlightsInboxTab() {
  const loc = useLocation()
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    void loadBlocks()  // populate the optimistic block-id set
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    const fetchInbox = (initial: boolean) =>
      api.get('/api/highlights')
        .then((rows: HighlightInboxItem[]) => {
          inbox.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) loading.value = false
          showToast(`Failed to load highlights: ${(err as Error)?.message ?? err}`,
            'error')
        })
    void fetchInbox(true)
    // Live updates — both local writes and federated arrivals fan a
    // narrow ``highlight.*`` frame; we just refetch so the audience filter
    // and unseen counts stay server-authoritative.
    const dispose = [
      ws.on('highlight.frame_added',   () => { void fetchInbox(false) }),
      ws.on('highlight.frame_removed', () => { void fetchInbox(false) }),
      ws.on('highlight.removed',       () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <HighlightsRingSkeleton />

  // Belt + braces: the server already strips blocked authors. The
  // local hide makes a same-tab "Block @bob" land in the rings list
  // without a refresh — the next /api/highlights fetch confirms it.
  const blocked = blockedUserIds.value
  const items = inbox.value.filter(i => !blocked.has(i.highlight.author_user_id))

  // Build a "rings" row: my own + everyone else's, grouped by author.
  const myItems = items.filter(i => i.highlight.author_user_id === me)
  const peerItems = items.filter(i => i.highlight.author_user_id !== me)

  const ring = (item: HighlightInboxItem) => {
    const cls = item.unseen_count > 0
      ? 'sh-highlight-ring sh-highlight-ring--unseen'
      : 'sh-highlight-ring'
    const onClick = () => loc.route(`/highlights/${item.highlight.id}`)
    const isMine = item.highlight.author_user_id === me
    const name = householdDisplayName(item.highlight.author_user_id)
    const picture = householdPictureUrl(item.highlight.author_user_id)
    return (
      <div key={item.highlight.id} class="sh-highlight-ring-wrap">
        <button
          type="button"
          class={cls}
          onClick={onClick}
          aria-label={`Open ${name}'s highlight`}
        >
          <span class="sh-highlight-ring-avatar">
            <Avatar name={name} src={picture} size={56} />
          </span>
          <span class="sh-highlight-ring-label">
            {isMine ? 'Your highlight' : name}
          </span>
        </button>
        {!isMine && (
          <button
            type="button"
            class="sh-highlight-ring-overflow"
            aria-label={`More actions for ${name}`}
            onClick={(ev) => {
              ev.stopPropagation()
              openUserActions(item.highlight.author_user_id)
            }}
          >
            ⋯
          </button>
        )}
      </div>
    )
  }

  return (
    <div class="sh-highlights-page">
      <section class="sh-highlight-rings" aria-label="Recent highlights">
        <button
          type="button"
          class="sh-highlight-ring sh-highlight-ring--new"
          onClick={() => openHighlightQuickShare()}
          aria-label="Post a new highlight"
        >
          <span class="sh-highlight-ring-avatar">
            <span class="sh-highlight-ring-plus" aria-hidden="true">+</span>
          </span>
          <span class="sh-highlight-ring-label">New</span>
        </button>
        {myItems.map(ring)}
        {peerItems.map(ring)}
      </section>

      {items.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🌅</div>
          <h3>No highlights yet</h3>
          <p>
            Highlights are short photo or video moments that disappear
            after the day is over. Share one with your household and
            connected peers.
          </p>
          <Button onClick={() => openHighlightQuickShare()}>
            + Share your first highlight
          </Button>
        </div>
      )}

      {items.length > 0 && (
        <section class="sh-highlight-list" aria-label="All highlights">
          {items.map(item => {
            const first = item.frames[0]
            const isMine = item.highlight.author_user_id === me
            const name = isMine
              ? 'You'
              : householdDisplayName(item.highlight.author_user_id)
            return (
              <a
                key={item.highlight.id}
                href={addBase(`/highlights/${item.highlight.id}`)}
                class="sh-highlight-row"
              >
                {first && first.frame_type === 'image' && (
                  <img
                    src={first.media_url}
                    alt=""
                    loading="lazy"
                    class="sh-highlight-row-thumb"
                  />
                )}
                {first && first.frame_type === 'video' && (
                  <span class="sh-highlight-row-thumb sh-highlight-row-thumb--video">
                    🎬
                  </span>
                )}
                {!first && (
                  <span class="sh-highlight-row-thumb sh-highlight-row-thumb--empty" />
                )}
                <span class="sh-highlight-row-meta">
                  <strong>{name}</strong>
                  <span class="sh-muted">
                    {humaniseDate(item.highlight.highlight_date)} ·{' '}
                    {item.frames.length} frame{item.frames.length === 1 ? '' : 's'}
                  </span>
                </span>
                {item.unseen_count > 0 && (
                  <span class="sh-highlight-row-unseen">{item.unseen_count}</span>
                )}
              </a>
            )
          })}
        </section>
      )}
    </div>
  )
}
