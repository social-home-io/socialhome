/**
 * MomentumInboxTab — inbox for the Momentum pillar (§Momentum).
 *
 * Twitter-style row layout: tight rows with an inline name + relative
 * time header, content, and a chip row of reply / reaction counts.
 * One-tap reaction (default ❤️) lands without opening the detail page.
 *
 * The top of the tab is a sticky inline composer entry that routes
 * to the standalone composer for full text + media. The tab also
 * subscribes to the ``moment.*`` WS frames and refetches the inbox on
 * any update. The host :class:`MomentumPage` switches between this
 * and :class:`MomentumArchiveTab`.
 */
import { useEffect } from 'preact/hooks'
import { signal, useSignal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { openLightbox } from '@/components/ImageLightbox'
import { openMomentumComposer } from '@/components/MomentumComposerDialog'
import { VideoMedia } from '@/components/VideoMedia'
import { MomentumInboxSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { openUserActions } from '@/components/UserActionsMenu'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { renderHashtagged } from './hashtags'
import { t, isOne, formatLocale } from '@/i18n/i18n'

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)

const CONTENT_TRUNCATE_AT = 280
const DEFAULT_REACTION = '❤️'


function relativeTime(iso: string): string {
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const ms = Date.now() - dt.getTime()
  const m = Math.floor(ms / 60_000)
  if (m < 1)   return t('moment.time.now')
  if (m < 60)  return narrowUnit(m, 'minute')
  const h = Math.floor(m / 60)
  if (h < 24)  return narrowUnit(h, 'hour')
  const d = Math.floor(h / 24)
  if (d < 7)   return narrowUnit(d, 'day')
  return dt.toLocaleDateString(formatLocale(), { month: 'short', day: 'numeric' })
}

/** ``5m`` / ``5 Min.`` / ``5j`` — the UI language's narrow unit form. */
function narrowUnit(n: number, unit: 'minute' | 'hour' | 'day'): string {
  try {
    return new Intl.NumberFormat(formatLocale(), {
      style: 'unit', unit, unitDisplay: 'narrow',
    }).format(n)
  } catch {
    return `${n}${unit[0]}`
  }
}


export default function MomentumInboxTab() {
  const loc = useLocation()
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    void loadBlocks()
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    const fetchInbox = (initial: boolean) =>
      api.get('/api/moments')
        .then((rows: Moment[]) => {
          moments.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) loading.value = false
          showToast(t('moment.inbox.load_failed', { error: String((err as Error)?.message ?? err) }),
            'error')
        })
    void fetchInbox(true)
    const dispose = [
      ws.on('moment.created',          () => { void fetchInbox(false) }),
      ws.on('moment.deleted',          () => { void fetchInbox(false) }),
      ws.on('moment.reaction_changed', () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <MomentumInboxSkeleton />

  const blocked = blockedUserIds.value
  const visible = moments.value.filter(m => !blocked.has(m.author_user_id))
  const topLevel = visible.filter(m => !m.parent_moment_id)

  const myDisplayName = me ? householdDisplayName(me) : '?'
  const myPicture = householdPictureUrl(me)

  const quickReact = async (m: Moment, ev: Event) => {
    ev.preventDefault()
    ev.stopPropagation()
    // Optimistic bump + server PUT. The next refetch (post WS frame)
    // confirms the count.
    const idx = moments.value.findIndex(x => x.id === m.id)
    if (idx >= 0) {
      moments.value = moments.value.map((x, i) =>
        i === idx ? { ...x, reaction_count: x.reaction_count + 1 } : x,
      )
    }
    try {
      await api.put(`/api/moments/${m.id}/reaction`, { emoji: DEFAULT_REACTION })
    } catch (err: unknown) {
      // Roll back on failure.
      if (idx >= 0) {
        moments.value = moments.value.map((x, i) =>
          i === idx ? { ...x, reaction_count: Math.max(0, x.reaction_count - 1) } : x,
        )
      }
      showToast(t('highlight.viewer.reaction_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  return (
    <div class="sh-momentum">
      {/* Inline composer entry — Twitter's "What's happening?" pattern.
          Tap routes to the full composer page; the inline box is
          intentionally read-only so we don't duplicate the rich-media
          flow inline. */}
      <button
        type="button"
        class="sh-momentum-compose-entry"
        onClick={() => openMomentumComposer()}
      >
        <Avatar name={myDisplayName} src={myPicture} size={32} />
        <span class="sh-momentum-compose-prompt">{t('moment.inbox.prompt')}</span>
      </button>

      {topLevel.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🌅</div>
          <h3>{t('moment.inbox.empty_title')}</h3>
          <p>
            {t('moment.inbox.empty_body')}
          </p>
        </div>
      )}

      <ul class="sh-momentum-list" aria-label={t('moment.inbox.list_aria')}>
        {topLevel.map(m => (
          <MomentRow
            key={m.id}
            m={m}
            mine={m.author_user_id === me}
            authorName={householdDisplayName(m.author_user_id)}
            authorPicture={householdPictureUrl(m.author_user_id)}
            onOpen={() => loc.route(`/momentum/${m.id}`)}
            onReact={(ev) => void quickReact(m, ev)}
            onTagClick={(t) => loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)}
          />
        ))}
      </ul>
    </div>
  )
}


function MomentRow({
  m,
  mine,
  authorName,
  authorPicture,
  onOpen,
  onReact,
  onTagClick,
}: {
  m: Moment
  mine: boolean
  authorName: string
  authorPicture: string | null
  onOpen: () => void
  onReact: (ev: Event) => void
  onTagClick: (tag: string) => void
}) {
  const expanded = useSignal(false)
  const longContent = m.content.length > CONTENT_TRUNCATE_AT
  const visibleText = longContent && !expanded.value
    ? m.content.slice(0, CONTENT_TRUNCATE_AT) + '…'
    : m.content

  return (
    <li class="sh-momentum-row" onClick={onOpen}>
      <Avatar name={authorName} src={authorPicture} size={36} />
      <div class="sh-momentum-row-body">
        <div class="sh-momentum-row-head">
          <strong class="sh-momentum-row-author">
            {mine ? t('highlight.inbox.you') : authorName}
          </strong>
          <span class="sh-muted">· {relativeTime(m.created_at)}</span>
          {m.received_via === 'gfs' && (
            <span
              class="sh-momentum-row-via-gfs"
              title={t('moment.inbox.via_gfs')}
            >
              · via GFS
            </span>
          )}
          {!mine && (
            <button
              type="button"
              class="sh-momentum-row-overflow"
              aria-label={t('highlight.more_actions', { name: authorName })}
              onClick={(ev) => {
                ev.preventDefault()
                ev.stopPropagation()
                openUserActions(m.author_user_id)
              }}
            >
              ⋯
            </button>
          )}
        </div>

        {m.content && (
          <p class="sh-momentum-row-content">
            {renderHashtagged(visibleText, (t, ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              onTagClick(t)
            })}
            {longContent && !expanded.value && (
              <button
                type="button"
                class="sh-momentum-row-more"
                onClick={(ev) => {
                  ev.preventDefault()
                  ev.stopPropagation()
                  expanded.value = true
                }}
              >
                {t('moment.inbox.show_more')}
              </button>
            )}
          </p>
        )}

        {m.media_type === 'image' && m.media_url && (
          <button
            type="button"
            class="sh-momentum-row-media-button"
            aria-label={t('moment.open_photo')}
            onClick={(ev) => {
              ev.preventDefault()
              ev.stopPropagation()
              openLightbox({
                items: [{
                  url:       m.media_url!,
                  item_type: 'photo',
                  caption:   m.content || null,
                }],
              })
            }}
          >
            <img
              src={m.media_url}
              alt={m.content ? '' : t('moment.photo_from', { name: authorName })}
              loading="lazy"
              class="sh-momentum-row-media"
            />
          </button>
        )}
        {m.media_type === 'video' && m.media_url && (
          <span onClick={(ev) => ev.stopPropagation()}>
            <VideoMedia
              src={m.media_url}
              poster={m.media_thumbnail_url}
              mediaStatus={m.media_status}
              class="sh-momentum-row-media"
            />
          </span>
        )}

        {/* Engagement chip row — Twitter-style icons + counts. */}
        <div class="sh-momentum-row-chips">
          <button
            type="button"
            class="sh-momentum-chip"
            aria-label={t(isOne(m.reply_count) ? 'moment.detail.replies_one' : 'moment.detail.replies', { n: String(m.reply_count) })}
            onClick={onOpen}
          >
            💬 {m.reply_count > 0 ? m.reply_count : ''}
          </button>
          <button
            type="button"
            class="sh-momentum-chip"
            aria-label={t('moment.detail.react_aria', { emoji: DEFAULT_REACTION })}
            disabled={mine}
            onClick={mine ? undefined : onReact}
          >
            {DEFAULT_REACTION} {m.reaction_count > 0 ? m.reaction_count : ''}
          </button>
        </div>
      </div>
    </li>
  )
}
