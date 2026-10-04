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
import { showToast } from '@/components/Toast'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { renderHashtagged } from './hashtags'
import { addBase } from '@/baseUrl'

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)
const trending = signal<Array<{ tag: string; count: number }>>([])
const activeTag = signal<string | null>(null)

const grouped = computed<Map<string, Moment[]>>(() => {
  const m = new Map<string, Moment[]>()
  for (const x of moments.value) {
    const day = x.created_at.slice(0, 10)
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
    const fetchAll = (initial: boolean) => {
      const tagParam = activeTag.value ? { tag: activeTag.value } : undefined
      return api.get('/api/moments/archive', tagParam)
        .then((rows: Moment[]) => {
          moments.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) loading.value = false
          showToast(`Failed to load archive: ${(err as Error)?.message ?? err}`,
            'error')
        })
    }
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
      <nav class="sh-momentum-trending" aria-label="Trending hashtags">
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
        💡 Start a hashtag with <code>#yourtag</code> to make it easy to
        find later.
      </p>
    )
  const renderActiveBanner = tag && (
    <div class="sh-momentum-filter-banner" role="status">
      <span>Filtering by <strong>#{tag}</strong></span>
      <a
        href={addBase('/momentum?tab=archive')}
        onClick={(ev) => { ev.preventDefault(); loc.route('/momentum?tab=archive') }}
      >Clear</a>
    </div>
  )

  if (days.length === 0) {
    return (
      <div class="sh-momentum-archive">
        <h2>Moments archive</h2>
        {renderActiveBanner}
        {renderTrendingRow}
        <div class="sh-empty-state">
          <p>{tag
            ? `No moments tagged #${tag} in the retention window.`
            : 'No moments in the retention window yet.'}</p>
        </div>
      </div>
    )
  }

  return (
    <div class="sh-momentum-archive">
      <h2>Moments archive</h2>
      {!tag && (
        <p class="sh-muted">
          Moments live 24 h by default; 7 d for people you follow.
        </p>
      )}
      {renderActiveBanner}
      {renderTrendingRow}
      {days.map(day => (
        <section key={day} class="sh-momentum-archive-day">
          <h3>{new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, {
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
                    name={householdDisplayName(m.author_user_id)}
                    src={householdPictureUrl(m.author_user_id)}
                    size={32}
                  />
                  <div class="sh-momentum-row-body">
                    <div class="sh-momentum-row-meta">
                      <strong>{householdDisplayName(m.author_user_id)}</strong>
                      <span class="sh-muted">{m.created_at.slice(11, 16)}</span>
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
                        aria-label="Open photo full-size"
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
                          alt={m.content ? '' : `Photo from ${householdDisplayName(m.author_user_id)}`}
                          loading="lazy"
                          class="sh-momentum-row-media"
                        />
                      </button>
                    )}
                    {m.media_type === 'video' && m.media_url && (
                      <span class="sh-momentum-row-media sh-momentum-row-media--video">
                        🎬 video
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
