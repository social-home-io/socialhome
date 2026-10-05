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
import { t, isOne, formatLocale } from '@/i18n/i18n'

const inbox = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(true)


function humaniseDate(iso: string): string {
  // Highlights list groups by ``highlight_date`` (YYYY-MM-DD UTC). For a
  // friendlier inbox we surface "Today" / "Yesterday" / weekday for
  // the recent past, and a fully spelled-out date otherwise.
  const today = new Date()
  const todayStr = today.toISOString().slice(0, 10)
  if (iso === todayStr) return t('highlight.inbox.today')
  const yest = new Date(today)
  yest.setUTCDate(yest.getUTCDate() - 1)
  if (iso === yest.toISOString().slice(0, 10)) return t('highlight.inbox.yesterday')
  const dt = new Date(iso + 'T00:00:00Z')
  if (Number.isNaN(dt.getTime())) return iso
  return dt.toLocaleDateString(formatLocale(), {
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
          showToast(t('highlight.inbox.load_failed', { error: String((err as Error)?.message ?? err) }),
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
          aria-label={t('highlight.inbox.open_aria', { name })}
        >
          <span class="sh-highlight-ring-avatar">
            <Avatar name={name} src={picture} size={56} />
          </span>
          <span class="sh-highlight-ring-label">
            {isMine ? t('highlight.inbox.yours') : name}
          </span>
        </button>
        {!isMine && (
          <button
            type="button"
            class="sh-highlight-ring-overflow"
            aria-label={t('highlight.more_actions', { name })}
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
      <section class="sh-highlight-rings" aria-label={t('highlight.inbox.recent_aria')}>
        <button
          type="button"
          class="sh-highlight-ring sh-highlight-ring--new"
          onClick={() => openHighlightQuickShare()}
          aria-label={t('highlight.inbox.new_aria')}
        >
          <span class="sh-highlight-ring-avatar">
            <span class="sh-highlight-ring-plus" aria-hidden="true">+</span>
          </span>
          <span class="sh-highlight-ring-label">{t('highlight.inbox.new')}</span>
        </button>
        {myItems.map(ring)}
        {peerItems.map(ring)}
      </section>

      {items.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🌅</div>
          <h3>{t('highlight.inbox.empty_title')}</h3>
          <p>
            {t('highlight.inbox.empty_body')}
          </p>
          <Button onClick={() => openHighlightQuickShare()}>
            {t('highlight.inbox.share_first')}
          </Button>
        </div>
      )}

      {items.length > 0 && (
        <section class="sh-highlight-list" aria-label={t('highlight.inbox.all_aria')}>
          {items.map(item => {
            const first = item.frames[0]
            const isMine = item.highlight.author_user_id === me
            const name = isMine
              ? t('highlight.inbox.you')
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
                    {t(isOne(item.frames.length) ? 'highlight.frames_one' : 'highlight.frames', { n: String(item.frames.length) })}
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
