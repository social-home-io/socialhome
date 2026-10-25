/**
 * HighlightShareCard — feed-card body for ``post.type === 'highlight_share'``.
 *
 * Lazy-fetches the linked highlight via ``GET /api/highlights/{id}?via_post=``.
 * The server gates the read: a reader outside the highlight's audience
 * sees it only through a share post they can read, so the card passes
 * its post id (and the viewer link carries it on). A 404 here means
 * "expired" or "not visible to me". Renders a thumbnail of the first frame plus the
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
import { Button } from '@/components/Button'
import type { Highlight, HighlightFrame } from '@/types'
import { t, isOne } from '@/i18n/i18n'
import { resolveDisplayName } from '@/utils/avatar'
import { personLabel } from '@/utils/names'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { loadSpaceMembers } from '@/store/spaceMembers'
import { highlightDateLabel } from '@/utils/highlightDate'

interface HighlightDetailResponse {
  highlight: Highlight
  frames: HighlightFrame[]
}

export interface HighlightShareCardProps {
  /** ``post.linked_highlight_id`` — null when retention purged the source. */
  highlightId: string | null
  /** ``post.content`` — the sharer's optional caption. */
  note?: string | null
  /** The space the post lives in, so a remote author resolves through
   *  that space's roster. ``null`` / absent on the household feed. */
  spaceId?: string | null
  /** The ``highlight_share`` post this card renders — the server opens
   *  the highlight to whoever can read that post. */
  postId?: string | null
}

/** ``?via_post=`` suffix for the share path, or ``''`` without a post. */
export function viaPostQuery(postId: string | null | undefined): string {
  return postId ? `?via_post=${encodeURIComponent(postId)}` : ''
}

const cache = signal<Record<string, HighlightDetailResponse | null>>({})
const loading = signal<Record<string, boolean>>({})
/** Reads that failed for another reason than 404 — shown with Try again,
 *  never remembered as "ended". */
const failed = signal<Record<string, boolean>>({})


export function HighlightShareCard({ highlightId, note, spaceId = null, postId = null }: HighlightShareCardProps) {
  const loc = useLocation()
  const via = viaPostQuery(postId)

  // The highlight only carries the author's id; resolve it like a post
  // author (space roster → household users), never print the id.
  useEffect(() => {
    void loadHouseholdUsers()
    if (spaceId) void loadSpaceMembers(spaceId)
  }, [spaceId])

  useEffect(() => {
    if (!highlightId) return
    if (highlightId in cache.value) return
    if (loading.value[highlightId] || failed.value[highlightId]) return
    loading.value = { ...loading.value, [highlightId]: true }
    api.get(`/api/highlights/${highlightId}${via}`)
      .then((d: HighlightDetailResponse) => {
        cache.value = { ...cache.value, [highlightId]: d }
      })
      .catch((err: unknown) => {
        // 404 (ended, or not for this reader) → cache null so we render
        // the "ended" placeholder and don't retry on every re-render.
        // Anything else is a failed read: never cached as "ended" — the
        // card offers Try again.
        if ((err as { status?: number })?.status === 404) {
          cache.value = { ...cache.value, [highlightId]: null }
        } else {
          failed.value = { ...failed.value, [highlightId]: true }
        }
      })
      .finally(() => {
        const next = { ...loading.value }
        delete next[highlightId]
        loading.value = next
      })
  }, [highlightId, via, failed.value[highlightId ?? '']])

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
  if (failed.value[highlightId] && !loading.value[highlightId]) {
    const retry = () => {
      const next = { ...failed.value }
      delete next[highlightId]
      failed.value = next  // the effect above reads again
    }
    return (
      <div class="sh-highlight-share-card sh-highlight-share-card--failed">
        <span class="sh-highlight-share-card-thumb sh-highlight-share-card-thumb--ended" aria-hidden="true">
          ⚠️
        </span>
        <div class="sh-highlight-share-card-body">
          <strong>{t('highlight.share_card.load_failed')}</strong>
          {note && <p class="sh-muted">{note}</p>}
          <Button variant="secondary" onClick={retry}>{t('load.try_again')}</Button>
        </div>
      </div>
    )
  }
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
  // The space's own name for them first; the server's name covers a
  // friend from another household the local caches don't know.
  const authorName = resolveDisplayName(
    spaceId,
    detail.highlight.author_user_id,
    personLabel(detail.highlight.author?.display_name),
  )
  return (
    <button
      type="button"
      class="sh-highlight-share-card"
      onClick={() => loc.route(`/highlights/${detail.highlight.id}${via}`)}
      aria-label={t('highlight.share_card.watch_aria', { name: authorName })}
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
        <strong>{authorName}</strong>
        <span class="sh-muted">
          {highlightDateLabel(detail.highlight.highlight_date)} · {t(isOne(detail.frames.length) ? 'highlight.frames_one' : 'highlight.frames', { n: String(detail.frames.length) })}
        </span>
        {note && <p>{note}</p>}
        <span class="sh-highlight-share-card-cta">{t('highlight.share_card.cta')}</span>
      </div>
    </button>
  )
}
