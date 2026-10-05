/**
 * HighlightShareCard — feed-card body for ``post.type === 'highlight_share'``.
 *
 * Lazy-fetches the linked highlight via ``GET /api/highlights/{id}`` (the
 * server already gates by audience, so a 404 here means "expired" or
 * "not visible to me"). Renders a thumbnail of the first frame plus the
 * sharer's optional note + a "Tap to watch" affordance that routes to
 * :class:`HighlightViewerPage`.
 *
 * Three render states: active (thumb + watch button), expired
 * ("Highlight has ended"), no link (``highlight_id === null`` because retention
 * already nulled the FK on the household table).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import type { Highlight, HighlightFrame } from '@/types'
import { t, isOne } from '@/i18n/i18n'

interface HighlightDetailResponse {
  highlight: Highlight
  frames: HighlightFrame[]
}

export interface HighlightShareCardProps {
  /** ``post.linked_highlight_id`` — null when retention purged the source. */
  highlightId: string | null
  /** ``post.content`` — the sharer's optional caption. */
  note?: string | null
}

const cache = signal<Record<string, HighlightDetailResponse | null>>({})
const loading = signal<Record<string, boolean>>({})


export function HighlightShareCard({ highlightId, note }: HighlightShareCardProps) {
  const loc = useLocation()

  useEffect(() => {
    if (!highlightId) return
    if (highlightId in cache.value) return
    if (loading.value[highlightId]) return
    loading.value = { ...loading.value, [highlightId]: true }
    api.get(`/api/highlights/${highlightId}`)
      .then((d: HighlightDetailResponse) => {
        cache.value = { ...cache.value, [highlightId]: d }
      })
      .catch(() => {
        // 404 / 403 → cache null so we render the "ended" placeholder
        // and don't retry on every re-render.
        cache.value = { ...cache.value, [highlightId]: null }
      })
      .finally(() => {
        const next = { ...loading.value }
        delete next[highlightId]
        loading.value = next
      })
  }, [highlightId])

  if (!highlightId) {
    return (
      <div class="sh-highlight-share-card sh-highlight-share-card--ended">
        <span class="sh-highlight-share-card-thumb sh-highlight-share-card-thumb--ended">
          ⏳
        </span>
        <div class="sh-highlight-share-card-body">
          <strong>{t('highlight.share_card.ended')}</strong>
          {note && <p class="sh-muted">{note}</p>}
        </div>
      </div>
    )
  }

  const detail = cache.value[highlightId]
  if (loading.value[highlightId] || detail === undefined) {
    return (
      <div class="sh-highlight-share-card sh-highlight-share-card--loading">
        <span class="sh-highlight-share-card-thumb sh-highlight-share-card-thumb--loading" />
        <div class="sh-highlight-share-card-body">
          <strong>{t('highlight.share_card.loading')}</strong>
        </div>
      </div>
    )
  }
  if (detail === null) {
    return (
      <div class="sh-highlight-share-card sh-highlight-share-card--ended">
        <span class="sh-highlight-share-card-thumb sh-highlight-share-card-thumb--ended">
          ⏳
        </span>
        <div class="sh-highlight-share-card-body">
          <strong>{t('highlight.share_card.ended')}</strong>
          {note && <p class="sh-muted">{note}</p>}
        </div>
      </div>
    )
  }

  const first = detail.frames[0]
  return (
    <button
      type="button"
      class="sh-highlight-share-card"
      onClick={() => loc.route(`/highlights/${detail.highlight.id}`)}
      aria-label={t('highlight.share_card.watch_aria', { name: detail.highlight.author_user_id })}
    >
      {first && first.frame_type === 'image' ? (
        <img
          src={first.media_url}
          alt=""
          loading="lazy"
          class="sh-highlight-share-card-thumb"
        />
      ) : (
        <span class="sh-highlight-share-card-thumb sh-highlight-share-card-thumb--video">
          🎬
        </span>
      )}
      <div class="sh-highlight-share-card-body">
        <strong>{detail.highlight.author_user_id}</strong>
        <span class="sh-muted">
          {detail.highlight.highlight_date} · {t(isOne(detail.frames.length) ? 'highlight.frames_one' : 'highlight.frames', { n: String(detail.frames.length) })}
        </span>
        {note && <p>{note}</p>}
        <span class="sh-highlight-share-card-cta">{t('highlight.share_card.cta')}</span>
      </div>
    </button>
  )
}
