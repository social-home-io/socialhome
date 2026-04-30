/**
 * ImageLightbox — full-screen media viewer (§23.30).
 *
 * Supports photo + video, prev/next navigation through a passed-in
 * item list, keyboard shortcuts (← → Esc), a metadata overlay with
 * caption + date taken, and a download button. Opened via
 * ``openLightbox({items, index})``; consumers that only have a single
 * URL can still use the legacy ``openLightbox(url)`` call.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'

export interface LightboxItem {
  id?:            string
  item_type?:     'photo' | 'video'
  url:            string
  thumbnail_url?: string
  caption?:       string | null
  taken_at?:      string | null
  width?:         number
  height?:        number
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
      aria-label="Media viewer"
    >
      {/* Dim backdrop — click closes. */}
      <div
        class="sh-lightbox-backdrop"
        onClick={closeLightbox}
        aria-hidden="true"
      />

      <button
        type="button" class="sh-lightbox-close"
        onClick={closeLightbox} aria-label="Close viewer (Esc)"
        title="Close (Esc)"
      >✕</button>

      {canPrev && (
        <button
          type="button" class="sh-lightbox-nav sh-lightbox-nav--prev"
          onClick={() => goto(-1)}
          aria-label="Previous item (←)"
          title="Previous (←)"
        >‹</button>
      )}
      {canNext && (
        <button
          type="button" class="sh-lightbox-nav sh-lightbox-nav--next"
          onClick={() => goto(+1)}
          aria-label="Next item (→)"
          title="Next (→)"
        >›</button>
      )}

      <div class="sh-lightbox-stage" onClick={(e) => e.stopPropagation()}>
        {item.item_type === 'video' ? (
          <video
            src={item.url}
            class="sh-lightbox-media"
            controls
            autoPlay
            playsInline
          />
        ) : (
          <img
            src={item.url}
            alt={item.caption || 'Media'}
            class="sh-lightbox-media"
          />
        )}
      </div>

      <div class="sh-lightbox-meta">
        <div class="sh-lightbox-meta-line">
          {item.caption && <strong>{item.caption}</strong>}
          {item.taken_at && (
            <time class="sh-muted">
              {new Date(item.taken_at).toLocaleDateString(undefined, {
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
        <a
          class="sh-lightbox-download"
          href={item.url}
          download
          onClick={(e) => e.stopPropagation()}
          aria-label="Download this item"
        >↓ Download</a>
      </div>
    </div>
  )
}
