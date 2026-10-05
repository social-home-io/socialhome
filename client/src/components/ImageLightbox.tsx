/**
 * ImageLightbox — full-screen media viewer (§23.30).
 *
 * Supports photo + video, prev/next navigation through a passed-in
 * item list, keyboard shortcuts (← → Esc), a metadata overlay with
 * caption + date taken, a download button, and a "Copy reference"
 * button that puts a markdown image snippet on the clipboard so the
 * user can paste a gallery image into a wiki-style page (§Pages).
 *
 * Opened via ``openLightbox({items, index})``; consumers that only
 * have a single URL can still use the legacy ``openLightbox(url)``
 * call.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'

import { showToast } from './Toast'
import { t, formatLocale } from '@/i18n/i18n'
import { safeHref } from '@/utils/safeHref'

export interface LightboxItem {
  id?:            string
  item_type?:     'photo' | 'video'
  url:            string
  thumbnail_url?: string
  caption?:       string | null
  taken_at?:      string | null
  width?:         number
  height?:        number
  /** Offer "Report" (a space gallery item by someone else). */
  onReport?:      () => void
}

interface LightboxState {
  items: LightboxItem[]
  index: number
}

const lightbox = signal<LightboxState | null>(null)

export function openLightbox(
  arg: string | { items: LightboxItem[]; index?: number },
): void {
  if (typeof arg === 'string') {
    lightbox.value = { items: [{ url: arg }], index: 0 }
  } else {
    lightbox.value = { items: arg.items, index: arg.index ?? 0 }
  }
}

export function closeLightbox(): void { lightbox.value = null }


/** Strip ``?exp=&sig=…`` (and any other query string) from a media URL.
 *  The lightbox always shows server-signed URLs (they have a 1h TTL);
 *  pasting that signed form into a saved page body would expire fast.
 *  We copy the canonical path instead — the page route re-signs on
 *  every read. */
function canonicalMediaUrl(url: string): string {
  const q = url.indexOf('?')
  return q >= 0 ? url.slice(0, q) : url
}


/** Build a friendly alt-text from the item's caption or its URL slug. */
function defaultAltText(item: LightboxItem): string {
  const cap = item.caption?.trim()
  if (cap) return cap
  const path = canonicalMediaUrl(item.url)
  const last = path.split('/').filter(Boolean).pop() ?? 'image'
  // Drop the file extension for a more readable alt-text.
  const dot = last.lastIndexOf('.')
  return dot > 0 ? last.slice(0, dot) : last
}


/** Copy a markdown-image snippet pointing at the canonical
 *  ``/api/media/{filename}`` URL so it pastes into a page editor and
 *  renders correctly on every reader's reload. */
export async function copyReferenceForItem(item: LightboxItem): Promise<void> {
  const canonical = canonicalMediaUrl(item.url)
  const snippet = `![${defaultAltText(item)}](${canonical})`
  try {
    await navigator.clipboard.writeText(snippet)
    showToast(t('lightbox.reference_copied'), 'success')
  } catch (err) {
    showToast(t('lightbox.copy_failed', { error: String((err as Error)?.message ?? err) }), 'error')
  }
}

export function ImageLightbox() {
  const state = lightbox.value

  useEffect(() => {
    if (!state) return
    const onKey = (e: KeyboardEvent) => {
      const s = lightbox.value
      if (!s) return
      if (e.key === 'Escape') {
        e.preventDefault()
        closeLightbox()
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        lightbox.value = { ...s, index: Math.min(s.items.length - 1, s.index + 1) }
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        lightbox.value = { ...s, index: Math.max(0, s.index - 1) }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [state])

  if (!state) return null

  const item = state.items[state.index]
  const canPrev = state.index > 0
  const canNext = state.index < state.items.length - 1

  const goto = (delta: number) => {
    const s = lightbox.value
    if (!s) return
    const next = Math.min(Math.max(0, s.index + delta), s.items.length - 1)
    lightbox.value = { ...s, index: next }
  }

  return (
    <div
      class="sh-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={t('lightbox.aria')}
    >
      {/* Dim backdrop — click closes. */}
      <div
        class="sh-lightbox-backdrop"
        onClick={closeLightbox}
        aria-hidden="true"
      />

      <button
        type="button" class="sh-lightbox-close"
        onClick={closeLightbox} aria-label={t('lightbox.close_aria')}
        title={t('lightbox.close_title')}
      >✕</button>

      {canPrev && (
        <button
          type="button" class="sh-lightbox-nav sh-lightbox-nav--prev"
          onClick={() => goto(-1)}
          aria-label={t('lightbox.prev_aria')}
          title={t('lightbox.prev_title')}
        >‹</button>
      )}
      {canNext && (
        <button
          type="button" class="sh-lightbox-nav sh-lightbox-nav--next"
          onClick={() => goto(+1)}
          aria-label={t('lightbox.next_aria')}
          title={t('lightbox.next_title')}
        >›</button>
      )}

      <div class="sh-lightbox-stage" onClick={(e) => e.stopPropagation()}>
        {item.item_type === 'video' ? (
          <video
            src={item.url}
            // Poster = the item's thumbnail so the viewer shows a clean
            // first frame (with the native play button) instead of a black
            // box — browsers block the unmuted autoPlay below, so without a
            // poster the user faces a black rectangle until they hit play.
            poster={item.thumbnail_url}
            class="sh-lightbox-media"
            controls
            autoPlay
            // Muted so an opened clip doesn't blast audio — and browsers
            // only allow autoplay when muted, so this also makes the
            // autoPlay actually fire instead of being blocked.
            muted
            playsInline
          />
        ) : (
          <img
            src={item.url}
            alt={item.caption || t('lightbox.media_alt')}
            class="sh-lightbox-media"
          />
        )}
      </div>

      <div class="sh-lightbox-meta">
        <div class="sh-lightbox-meta-line">
          {item.caption && <strong>{item.caption}</strong>}
          {item.taken_at && (
            <time class="sh-muted">
              {new Date(item.taken_at).toLocaleDateString(formatLocale(), {
                year:  'numeric',
                month: 'short',
                day:   'numeric',
              })}
            </time>
          )}
          {state.items.length > 1 && (
            <span class="sh-muted">
              {state.index + 1} / {state.items.length}
            </span>
          )}
        </div>
        <div class="sh-lightbox-actions" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            class="sh-lightbox-copyref"
            onClick={() => void copyReferenceForItem(item)}
            aria-label={t('lightbox.copy_reference_aria')}
            title={t('lightbox.copy_reference')}
          >📋 {t('lightbox.copy_reference')}</button>
          <a
            class="sh-lightbox-download"
            href={safeHref(item.url, { blob: true })}
            download
            aria-label={t('lightbox.download_aria')}
          >↓ {t('media.download')}</a>
          {item.onReport && (
            <button
              type="button"
              class="sh-lightbox-copyref sh-lightbox-report"
              onClick={() => { const go = item.onReport; closeLightbox(); go?.() }}
            >🚩 {t('report.action')}</button>
          )}
        </div>
      </div>
    </div>
  )
}
