/**
 * MomentumArchiveTab — full retention-window list of moments
 * (§Momentum). Same API shape as the inbox but renders with date
 * headers so the calendar-style scroll works. Optional ``?tag``
 * URL filter narrows to a single hashtag; the chip row at the top
 * shows the trending tags inside the viewer's visibility window.
 *
 * Mounted by :class:`MomentumPage` when ``?tab=archive`` is set.
 */
import { useEffect } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { openLightbox } from '@/components/ImageLightbox'
import { MomentumArchiveSkeleton } from '@/components/Skeleton'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import {
  namedPersonLabel,
  namedPersonPicture,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { renderHashtagged } from './hashtags'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { formatDayKey } from '@/utils/calendar'
import { localClockTime, localDayKey } from '@/utils/relativeTime'

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)
const trending = signal<Array<{ tag: string; count: number }>>([])
const activeTag = signal<string | null>(null)
/** A failed load is never the empty archive (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)

/** Load (or refresh, on a ``moment.*`` frame) the archive for the tag. */
function fetchAll(initial: boolean): Promise<void> {
  const tagParam = activeTag.value ? { tag: activeTag.value } : undefined
  return api.get('/api/moments/archive', tagParam)
    .then((rows: Moment[]) => {
      moments.value = rows ?? []
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { status.value = loadFailed(status.value) })
    .finally(() => { if (initial) loading.value = false })
}

const grouped = computed<Map<string, Moment[]>>(() => {
  const m = new Map<string, Moment[]>()
  for (const x of moments.value) {
    // The viewer's own calendar day — never the UTC date of the stamp.
    const day = localDayKey(x.created_at) ?? x.created_at
    if (!m.has(day)) m.set(day, [])
    m.get(day)!.push(x)
  }
  return m
})


export default function MomentumArchiveTab() {
  const loc = useLocation()

  useEffect(() => {
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    const params = new URLSearchParams(window.location.search)
    activeTag.value = params.get('tag') || null
    loading.value = true
    status.value = INITIAL_LOAD
    const fetchTrending = () =>
      api.get<{ hashtags: Array<{ tag: string; count: number }> }>(
        '/api/moments/hashtags',
      )
        .then((res) => { trending.value = res?.hashtags ?? [] })
        .catch(() => { /* trending is decorative; swallow */ })
    void fetchAll(true)
    void fetchTrending()
    const dispose = [
      ws.on('moment.created', () => { void fetchAll(false); void fetchTrending() }),
      ws.on('moment.deleted', () => { void fetchAll(false); void fetchTrending() }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [loc.path, loc.url])

  if (loading.value) return <MomentumArchiveSkeleton />

  const days = [...grouped.value.keys()]  // already newest-first
  const tag = activeTag.value
  const renderTrendingRow = trending.value.length > 0
    ? (
      <nav class="sh-momentum-trending" aria-label={t('moment.archive.trending_aria')}>
        {trending.value.map(t => (
          <a
            key={t.tag}
            href={addBase(`/momentum?tab=archive&tag=${encodeURIComponent(t.tag)}`)}
            class={`sh-momentum-chip${tag === t.tag ? ' sh-momentum-chip--active' : ''}`}
            onClick={(ev) => {
              ev.preventDefault()
              loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t.tag)}`)
            }}
          >
            #{t.tag}
            <span class="sh-momentum-chip-count">{t.count}</span>
          </a>
        ))}
      </nav>
    )
    // No tags yet — quietly invite users to start one. Stays visible
    // when the archive itself isn't empty (i.e. only the *trending*
    // list is empty), so the hint feels like a discoverability nudge
    // rather than an empty state.
    : days.length > 0 && (
      <p class="sh-muted sh-momentum-trending-hint">
        💡 {t('moment.archive.hashtag_hint_before')}<code>#{t('moment.archive.hashtag_example')}</code>{t('moment.archive.hashtag_hint_after')}
      </p>
    )
  const renderActiveBanner = tag && (
    <div class="sh-momentum-filter-banner" role="status">
      <span>{t('moment.archive.filtering_by')} <strong>#{tag}</strong></span>
      <a
        href={addBase('/momentum?tab=archive')}
        onClick={(ev) => { ev.preventDefault(); loc.route('/momentum?tab=archive') }}
      >{t('moment.archive.clear')}</a>
    </div>
  )

  const retry = () => void fetchAll(false)
  if (status.value.failed) {
    return (
      <div class="sh-momentum-archive">
        <h2>{t('page_title.moments_archive')}</h2>
        <LoadErrorState
          message={t('moment.inbox.load_failed_title')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          onRetry={retry}
        />
      </div>
    )
  }
  const renderStale = status.value.staleSince !== null && (
    <StaleNotice
      message={t('moment.inbox.stale', { time: clockTime(status.value.staleSince) })}
      onRetry={retry}
    />
  )

  if (days.length === 0) {
    return (
      <div class="sh-momentum-archive">
        <h2>{t('page_title.moments_archive')}</h2>
        {renderStale}
        {renderActiveBanner}
        {renderTrendingRow}
        <div class="sh-empty-state">
          <p>{tag
            ? t('moment.archive.empty_tag', { tag })
            : t('moment.archive.empty')}</p>
        </div>
      </div>
    )
  }

  return (
    <div class="sh-momentum-archive">
      <h2>{t('page_title.moments_archive')}</h2>
      {!tag && (
        <p class="sh-muted">
          {t('moment.archive.lifetime')}
        </p>
      )}
      {renderStale}
      {renderActiveBanner}
      {renderTrendingRow}
      {days.map(day => (
        <section key={day} class="sh-momentum-archive-day">
          <h3>{formatDayKey(day, {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
          })}</h3>
          <ul class="sh-momentum-list">
            {grouped.value.get(day)!.map(m => (
              <li key={m.id} class="sh-momentum-row">
                <a href={addBase(`/momentum/${m.id}`)}
                  class="sh-momentum-row-link"
                  onClick={(ev) => {
                    ev.preventDefault()
                    loc.route(`/momentum/${m.id}`)
                  }}>
                  <Avatar
                    name={namedPersonLabel(m.author_user_id, m.author)}
                    src={namedPersonPicture(m.author_user_id, m.author)}
                    size={32}
                  />
                  <div class="sh-momentum-row-body">
                    <div class="sh-momentum-row-head">
                      <strong class="sh-momentum-row-author">
                        {namedPersonLabel(m.author_user_id, m.author)}
                      </strong>
                      <time class="sh-muted" dateTime={m.created_at}>
                        {localClockTime(m.created_at)}
                      </time>
                    </div>
                    {m.content && (
                      <p class="sh-momentum-row-content">
                        {renderHashtagged(m.content, (t, ev) => {
                          ev.preventDefault()
                          ev.stopPropagation()
                          loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)
                        })}
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
                          alt={m.content ? '' : t('moment.photo_from', { name: namedPersonLabel(m.author_user_id, m.author) })}
                          loading="lazy"
                          class="sh-momentum-row-media"
                        />
                      </button>
                    )}
                    {m.media_type === 'video' && m.media_url && (
                      <span class="sh-momentum-row-media sh-momentum-row-media--video">
                        🎬 {t('moment.video')}
                      </span>
                    )}
                  </div>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
