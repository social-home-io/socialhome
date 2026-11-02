/**
 * GalleryViewer — one photo or video of an album, full screen.
 *
 * Shown by ``GalleryPage`` while the address is
 * ``…/gallery/albums/:albumId/:itemId``: Back, refresh and a shared link
 * all land here. The page owns the address; this component asks it to go
 * to another item (``onGo``), to close (``onClose``) or to delete.
 *
 * - Phone: swipe left / right, pinch or double-tap to zoom, pan when
 *   zoomed. Desktop: ← / →, wheel or +/- to zoom, double-click, 0 to fit.
 *   The keys are handled on the viewer itself, never the whole window, so
 *   a focused video keeps its own arrow keys for the scrubber.
 * - Every control is a real button of at least 44 px; Tab stays inside.
 * - Info: date taken, who added it, the album, size — and "Edit" (caption
 *   and date) for whoever may change the photo (``canEdit``).
 * - Only allowed actions are offered: Delete for the uploader or a
 *   household admin (D6; a protected account: its own uploads only), a
 *   space moderator; Report for someone else's space item.
 * - Delete asks nothing: the page offers Undo for 8 s (household: from
 *   the trash; space: the delete waits for the Undo to run out).
 * - Save hands out the stored file under a human name (``onDownload``).
 * - ♡ marks the photo one of your favourites (``onFavourite``, F on a
 *   keyboard) — private: nobody else learns it (owner decision D12).
 * - Reduced motion: the photo doesn't follow the finger or fade in.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { t, formatLocale } from '@/i18n/i18n'
import { safeHref } from '@/utils/safeHref'
import { formatDayKey } from '@/utils/calendar'
import { restoreFocus } from '@/utils/focus'
import { focusableWithin } from '@/components/Modal'
import { Skeleton } from '@/components/Skeleton'
import { copyReferenceForItem } from '@/components/ImageLightbox'
import { useViewerGestures } from './useViewerGestures'
import { isZoomed, type Size } from './zoomPan'
import { HEART_PATH } from './HeartIcon'

export interface ViewerItem {
  id: string
  item_type: 'photo' | 'video'
  url: string
  thumbnail_url: string
  width: number
  height: number
  caption?: string | null
  taken_at?: string | null
  created_at?: string
  uploaded_by: string
  duration_s?: number | null
  /** A video still transcoding: nothing to play yet. */
  processing?: boolean
}

export interface GalleryViewerProps {
  /** The album's items loaded so far. */
  items: ViewerItem[]
  /** The item shown, or ``-1`` while a link is still being looked up. */
  index: number
  /** How many items the whole album holds (``items`` may be one page). */
  total: number
  albumName: string
  /** Household or space — the delete warning says who loses it. */
  inSpace: boolean
  uploaderName: (userId: string) => string
  canDelete: (item: ViewerItem) => boolean
  /** Report someone else's space item — ``null`` when not offered. */
  reportFor: (item: ViewerItem) => (() => void) | null
  onGo: (index: number) => void
  onClose: () => void
  /** Delete (with the page's Undo); rejects when the server says no. */
  onDelete: (item: ViewerItem) => Promise<void>
  /** May the caption / date of this item be changed here? */
  canEdit?: (item: ViewerItem) => boolean
  onEdit?: (item: ViewerItem) => void
  /** Save the stored file with a human name; without it, a plain link. */
  onDownload?: (item: ViewerItem) => Promise<void> | void
  /** Is this item one of the viewer's favourites? With ``onFavourite``,
   *  the ♡ button shows it and toggles it. */
  isFavourite?: (item: ViewerItem) => boolean
  /** Mark (``on``) or unmark a favourite; rejects when it didn't save. */
  onFavourite?: (item: ViewerItem, on: boolean) => Promise<void>
}

const HINT_KEY = 'sh_gallery_viewer_hint_seen'
/** Arrow keys move a zoomed photo by this share of the stage. */
const KEY_PAN = 0.15

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
  } catch {
    return false
  }
}

function coarsePointer(): boolean {
  try {
    return window.matchMedia?.('(pointer: coarse)').matches === true
  } catch {
    return false
  }
}

/** Line icons for the action bar — drawn, not font glyphs (ⓘ, ⧉ are
 *  missing from many phone fonts and showed as empty boxes). */
const ICONS = {
  download: 'M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-10v6m0-9.5v.5',
  delete: 'M4.5 6.5h15M9.5 6.5V4.5h5v2m-8 0 1 13h9l1-13M10 10v6m4-6v6',
  report: 'M5.5 21V4m0 0h11l-2 4 2 4h-11',
  copy: 'M8.5 8.5h11v11h-11zM15.5 8.5v-4h-11v11h4',
  edit: 'M4.5 19.5h4l10-10-4-4-10 10zM13 7l4 4',
  heart: HEART_PATH,
} as const

function Icon({ name }: { name: keyof typeof ICONS }) {
  return (
    <svg class="sh-gviewer-icon" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"
         fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d={ICONS[name]} />
    </svg>
  )
}

function typeLabel(kind: 'photo' | 'video'): string {
  return kind === 'video' ? t('gallery.item_video') : t('gallery.item_photo')
}

/** ``YYYY-MM-DD`` (the camera's day) in the reader's language. */
function dayLabel(day: string | null | undefined, long: boolean): string {
  if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return ''
  return formatDayKey(day, long
    ? { weekday: 'short', year: 'numeric', month: 'long', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' })
}

/** A stored UTC timestamp as a local date. */
function dateLabel(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso.includes('T') || iso.includes('+') ? iso : iso.replace(' ', 'T') + 'Z')
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString(formatLocale(), { year: 'numeric', month: 'short', day: 'numeric' })
}

function durationLabel(s: number | null | undefined): string {
  if (!s || s <= 0) return ''
  const total = Math.round(s)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

/** The photo's size when fitted into ``stage``. */
function fitted(item: ViewerItem, stage: Size): Size | null {
  if (!item.width || !item.height || !stage.w || !stage.h) return null
  const k = Math.min(stage.w / item.width, stage.h / item.height)
  return { w: Math.round(item.width * k), h: Math.round(item.height * k) }
}

export function GalleryViewer(props: GalleryViewerProps) {
  const { items, index, total, albumName } = props
  const item: ViewerItem | undefined = index >= 0 ? items[index] : undefined
  const count = Math.max(total, items.length)

  const rootRef = useRef<HTMLDivElement | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const mediaRef = useRef<HTMLImageElement | null>(null)
  const infoBtnRef = useRef<HTMLButtonElement | null>(null)

  const [reducedMotion] = useState(prefersReducedMotion)
  const [infoOpen, setInfoOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [faving, setFaving] = useState(false)
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null)
  const [loadedId, setLoadedId] = useState<string | null>(null)
  const [failedId, setFailedId] = useState<string | null>(null)
  const [stage, setStage] = useState<Size>({ w: 0, h: 0 })
  const [hint, setHint] = useState(false)

  const canPrev = index > 0
  const canNext = index >= 0 && index < items.length - 1
  const isPhoto = item?.item_type === 'photo' && !item.processing
  const go = (delta: number) => {
    if (index < 0) return
    const next = index + delta
    if (next >= 0 && next < items.length) props.onGo(next)
  }

  const gestures = useViewerGestures({
    stageRef, mediaRef,
    zoomable: isPhoto && failedId !== item?.id,
    reducedMotion,
    onSwipe: (dir) => go(dir),
    resetKey: item?.id ?? '',
  })
  const zoomed = isZoomed(gestures.view)

  // Focus: on open the close button; Tab stays inside (see onKeyDown);
  // on close back to whatever opened the viewer (the tile).
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const opener = document.activeElement as HTMLElement | null
    root.querySelector<HTMLElement>('.sh-gviewer-close')?.focus()
    return () => restoreFocus(opener, root)
  }, [])

  // The stage's size, for fitting the photo and the zoom limits.
  useLayoutEffect(() => {
    const el = stageRef.current
    if (!el) return
    const measure = () => setStage({ w: el.clientWidth, h: el.clientHeight })
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [infoOpen])

  // Neighbours load ahead, so a swipe shows the next photo at once.
  useEffect(() => {
    if (index < 0) return
    for (const n of [items[index + 1], items[index - 1]]) {
      if (n && n.item_type === 'photo' && !n.processing && typeof Image !== 'undefined') {
        const img = new Image()
        img.decoding = 'async'
        img.src = n.url
      }
    }
  }, [index, items])

  // "Swipe to browse · pinch to zoom", once, on touch screens.
  useEffect(() => {
    if (!coarsePointer() || items.length < 2) return
    try {
      if (localStorage.getItem(HINT_KEY)) return
      localStorage.setItem(HINT_KEY, '1')
    } catch { /* private mode: show it anyway */ }
    setHint(true)
    const timer = setTimeout(() => setHint(false), 3500)
    return () => clearTimeout(timer)
  }, [items.length >= 2]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 3500)
    return () => clearTimeout(timer)
  }, [notice])

  const say = (text: string, error = false) => setNotice({ text, error })

  const favourite = item && props.onFavourite && props.isFavourite ? props.isFavourite(item) : null
  const toggleFavourite = async () => {
    if (!item || favourite === null || faving) return
    setFaving(true)
    try {
      await props.onFavourite?.(item, !favourite)
    } catch {
      say(t('gallery.fav.failed'), true)
    } finally {
      setFaving(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent) => {
    const target = e.target as HTMLElement | null
    if (e.key === 'Tab') {
      const root = rootRef.current
      if (!root) return
      const scope = root
      const list = focusableWithin(scope)
      if (list.length === 0) return
      const first = list[0]
      const last = list[list.length - 1]
      const active = document.activeElement
      if (!scope.contains(active) || active === root) {
        e.preventDefault()
        ;(e.shiftKey ? last : first).focus()
      } else if (e.shiftKey && active === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      if (infoOpen) { setInfoOpen(false); infoBtnRef.current?.focus(); return }
      props.onClose()
      return
    }
    // A video (its scrubber, volume) and form fields keep their own keys.
    if (target?.closest('video, input, textarea, select, [contenteditable="true"]')) return
    const panStep = Math.max(40, stage.w * KEY_PAN)
    switch (e.key) {
      case 'ArrowLeft':
        e.preventDefault()
        if (zoomed) gestures.pan(panStep, 0)
        else go(-1)
        break
      case 'ArrowRight':
        e.preventDefault()
        if (zoomed) gestures.pan(-panStep, 0)
        else go(1)
        break
      case 'ArrowUp':
        if (zoomed) { e.preventDefault(); gestures.pan(0, panStep) }
        break
      case 'ArrowDown':
        if (zoomed) { e.preventDefault(); gestures.pan(0, -panStep) }
        break
      case '+':
      case '=':
        if (isPhoto) { e.preventDefault(); gestures.zoomIn() }
        break
      case '-':
        if (isPhoto) { e.preventDefault(); gestures.zoomOut() }
        break
      case '0':
        if (isPhoto) { e.preventDefault(); gestures.reset() }
        break
      case 'f':
      case 'F':
        if (favourite !== null && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault()
          void toggleFavourite()
        }
        break
      case 'Home':
        if (index > 0) { e.preventDefault(); props.onGo(0) }
        break
      case 'End':
        if (index >= 0 && index < items.length - 1) { e.preventDefault(); props.onGo(items.length - 1) }
        break
    }
  }

  const doDelete = async () => {
    if (!item || busy) return
    setBusy(true)
    try {
      await props.onDelete(item)
    } catch {
      say(t('gallery.delete_failed'), true)
    } finally {
      setBusy(false)
    }
  }

  const position = item
    ? t('gallery.viewer_position', { i: String(index + 1), n: String(count) })
    : ''
  const dialogLabel = item
    ? t('gallery.viewer_aria', { type: typeLabel(item.item_type), i: String(index + 1), n: String(count) })
    : t('lightbox.aria')
  const taken = item ? dayLabel(item.taken_at, true) : ''
  const added = item ? dateLabel(item.created_at) : ''
  const who = item ? props.uploaderName(item.uploaded_by) : ''
  // "by Anna · 14 Jul 2026" under Info's "Added"; "Added by Anna · …"
  // under the caption.
  const addedInfo = item
    ? (who ? t('gallery.info_added_by', { name: who, date: added }) : added)
    : ''
  const addedLine = item
    ? (who ? t('gallery.viewer_added_by', { name: who, date: added }) : added)
    : ''
  const report = item ? props.reportFor(item) : null
  const deletable = item ? props.canDelete(item) : false
  const fit = item && item.item_type === 'photo' ? fitted(item, stage) : null
  const ready = item ? loadedId === item.id : false
  const failed = item ? failedId === item.id : false
  const v = gestures.view
  const mediaStyle = fit
    ? {
      width: `${fit.w}px`, height: `${fit.h}px`,
      transform: `translate(${v.x}px, ${v.y}px) scale(${v.scale})`,
    }
    : undefined

  return (
    <div
      ref={rootRef}
      class={
        'sh-gviewer' +
        (infoOpen ? ' sh-gviewer--info' : '') +
        (zoomed ? ' sh-gviewer--zoomed' : '') +
        (reducedMotion ? ' sh-gviewer--still' : '')
      }
      role="dialog"
      aria-modal="true"
      aria-label={dialogLabel}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <header class="sh-gviewer-top">
        <button
          type="button"
          class="sh-gviewer-btn sh-gviewer-close"
          onClick={props.onClose}
          aria-label={t('lightbox.close_aria')}
          title={t('lightbox.close_title')}
        ><span aria-hidden="true">✕</span></button>
        <div class="sh-gviewer-title">
          {item ? (
            <>
              <strong>{taken || added || typeLabel(item.item_type)}</strong>
              <small>{position} · {albumName}</small>
            </>
          ) : (
            <Skeleton shape="line" width={160} ariaLabel={t('common.loading')} />
          )}
        </div>
        <div class="sh-gviewer-top-actions">
          {isPhoto && (
            <>
              <button
                type="button"
                class="sh-gviewer-btn sh-gviewer-zoom"
                onClick={gestures.zoomOut}
                disabled={!zoomed}
                aria-label={t('gallery.viewer_zoom_out')}
                title={t('gallery.viewer_zoom_out')}
              ><span aria-hidden="true">−</span></button>
              <button
                type="button"
                class="sh-gviewer-btn sh-gviewer-zoom"
                onClick={gestures.zoomIn}
                disabled={v.scale >= 3.99}
                aria-label={t('gallery.viewer_zoom_in')}
                title={t('gallery.viewer_zoom_in')}
              ><span aria-hidden="true">+</span></button>
            </>
          )}
        </div>
      </header>

      <div class="sh-gviewer-body">
        <div
          ref={stageRef}
          class="sh-gviewer-stage"
          {...gestures.handlers}
        >
          {item ? (
            <div
              class="sh-gviewer-track"
              style={gestures.dragX ? { transform: `translateX(${gestures.dragX}px)` } : undefined}
            >
              {item.processing ? (
                <div class="sh-gviewer-message">
                  <span class="sh-upload-progress__spinner" aria-hidden="true" />
                  <p>{t('gallery.viewer_processing')}</p>
                </div>
              ) : item.item_type === 'video' ? (
                <video
                  key={item.id}
                  class="sh-gviewer-video"
                  src={item.url}
                  // The poster is the first frame, so a video shows a
                  // picture (and the native play button) — never black.
                  poster={item.thumbnail_url}
                  controls
                  playsInline
                  preload="metadata"
                  aria-label={item.caption || typeLabel('video')}
                />
              ) : failed ? (
                <div class="sh-gviewer-message" role="alert">
                  <p>{t('gallery.viewer_load_error')}</p>
                </div>
              ) : (
                <>
                  {/* The grid's thumbnail is already here: it stands in,
                    * blurred, until the full photo has loaded. */}
                  {!ready && (
                    <img
                      key={`ph-${item.id}`}
                      class="sh-gviewer-placeholder"
                      src={item.thumbnail_url}
                      alt=""
                      aria-hidden="true"
                      style={fit ? { width: `${fit.w}px`, height: `${fit.h}px` } : undefined}
                    />
                  )}
                  <img
                    key={item.id}
                    ref={mediaRef}
                    class={'sh-gviewer-photo' + (ready ? ' sh-gviewer-photo--ready' : '')}
                    src={item.url}
                    alt={item.caption || t('gallery.tile_dated', {
                      type: typeLabel('photo'), date: dayLabel(item.taken_at, false) || added,
                    })}
                    draggable={false}
                    style={mediaStyle}
                    onLoad={() => setLoadedId(item.id)}
                    onError={() => setFailedId(item.id)}
                  />
                </>
              )}
            </div>
          ) : (
            <Skeleton shape="rect" class="sh-gviewer-skeleton" ariaLabel={t('common.loading')} />
          )}
        </div>

        {canPrev && (
          <button
            type="button"
            class="sh-gviewer-btn sh-gviewer-nav sh-gviewer-nav--prev"
            onClick={() => go(-1)}
            aria-label={t('lightbox.prev_aria')}
            title={t('lightbox.prev_title')}
          ><span aria-hidden="true">‹</span></button>
        )}
        {canNext && (
          <button
            type="button"
            class="sh-gviewer-btn sh-gviewer-nav sh-gviewer-nav--next"
            onClick={() => go(1)}
            aria-label={t('lightbox.next_aria')}
            title={t('lightbox.next_title')}
          ><span aria-hidden="true">›</span></button>
        )}

        {infoOpen && item && (
          <aside
            id="sh-gviewer-info"
            class="sh-gviewer-info"
            aria-labelledby="sh-gviewer-info-title"
          >
            <div class="sh-gviewer-info-head">
              <h2 id="sh-gviewer-info-title" class={item.caption ? '' : 'sh-gviewer-info-nocaption'}>
                {item.caption || t('gallery.info_no_caption')}
              </h2>
              <button
                type="button"
                class="sh-gviewer-btn sh-gviewer-info-close"
                onClick={() => { setInfoOpen(false); infoBtnRef.current?.focus() }}
                aria-label={t('gallery.info_close')}
              ><span aria-hidden="true">✕</span></button>
            </div>
            <dl>
              <dt>{t('gallery.info_taken')}</dt>
              <dd>{taken || t('gallery.info_taken_unknown')}</dd>
              <dt>{t('gallery.info_added')}</dt>
              <dd>{addedInfo || '—'}</dd>
              <dt>{t('gallery.info_album')}</dt>
              <dd>{albumName}</dd>
              {item.width > 0 && item.height > 0 && item.item_type === 'photo' && (
                <>
                  <dt>{t('gallery.info_size')}</dt>
                  <dd>{item.width} × {item.height}</dd>
                </>
              )}
              {item.item_type === 'video' && durationLabel(item.duration_s) && (
                <>
                  <dt>{t('gallery.info_length')}</dt>
                  <dd>{durationLabel(item.duration_s)}</dd>
                </>
              )}
            </dl>
            {item.item_type === 'photo' && (
              <p class="sh-gviewer-info-note">
                <span aria-hidden="true">🔒</span> {t('gallery.info_no_location')}
              </p>
            )}
            {props.canEdit?.(item) && props.onEdit && (
              <button
                type="button"
                class="sh-gviewer-info-action"
                onClick={() => props.onEdit?.(item)}
              >
                <Icon name="edit" /> {t('gallery.manage.edit_action')}
              </button>
            )}
            <button
              type="button"
              class="sh-gviewer-info-action"
              onClick={() => void copyReferenceForItem(item, (text, kind) => say(text, kind === 'error'))}
            >
              <Icon name="copy" /> {t('gallery.info_copy_for_pages')}
            </button>
          </aside>
        )}
      </div>

      {item && !infoOpen && !zoomed && (item.caption || addedLine) && (
        <div class="sh-gviewer-caption">
          {item.caption && <p class="sh-gviewer-caption-text">{item.caption}</p>}
          {addedLine && <p class="sh-gviewer-caption-meta">{addedLine}</p>}
        </div>
      )}

      <div class="sh-gviewer-bar" role="toolbar" aria-label={t('gallery.viewer_actions')}>
        {item && favourite !== null && (
          <button
            type="button"
            class={'sh-gviewer-action sh-gviewer-fav' + (favourite ? ' sh-gviewer-fav--on' : '')}
            aria-pressed={favourite}
            aria-busy={faving || undefined}
            title={t('gallery.fav.toggle_title')}
            onClick={() => void toggleFavourite()}
          >
            <Icon name="heart" />
            <span>{t('gallery.fav.toggle')}</span>
          </button>
        )}
        {item && !item.processing && (props.onDownload ? (
          <button
            type="button"
            class="sh-gviewer-action"
            disabled={saving}
            aria-busy={saving || undefined}
            onClick={() => {
              setSaving(true)
              void Promise.resolve(props.onDownload?.(item)).finally(() => setSaving(false))
            }}
          >
            <Icon name="download" />
            <span>{t('media.download')}</span>
          </button>
        ) : (
          <a
            class="sh-gviewer-action"
            href={safeHref(item.url, { blob: true })}
            download
          >
            <Icon name="download" />
            <span>{t('media.download')}</span>
          </a>
        ))}
        {item && (
          <button
            ref={infoBtnRef}
            type="button"
            class="sh-gviewer-action"
            aria-expanded={infoOpen}
            aria-controls="sh-gviewer-info"
            onClick={() => setInfoOpen(o => !o)}
          >
            <Icon name="info" />
            <span>{t('gallery.viewer_info')}</span>
          </button>
        )}
        {item && deletable && (
          <button
            type="button"
            class="sh-gviewer-action sh-gviewer-action--danger"
            disabled={busy}
            onClick={() => void doDelete()}
          >
            <Icon name="delete" />
            <span>{t('gallery.viewer_delete')}</span>
          </button>
        )}
        {report && (
          <button
            type="button"
            class="sh-gviewer-action"
            onClick={() => { props.onClose(); report() }}
          >
            <Icon name="report" />
            <span>{t('report.action')}</span>
          </button>
        )}
      </div>


      {hint && (
        <p class="sh-gviewer-hint" aria-hidden="true">{t('gallery.viewer_hint')}</p>
      )}
      {/* What an action did — read out too. Always in the tree, so a
        * screen reader hears the change. */}
      <div role="status">
        {notice && (
          <p class={'sh-gviewer-notice' + (notice.error ? ' sh-gviewer-notice--error' : '')}>
            {notice.text}
          </p>
        )}
      </div>
      {/* Screen readers hear where they are after each step. */}
      <p class="sr-only" aria-live="polite">
        {item ? `${dialogLabel}${item.caption ? `: ${item.caption}` : ''}` : ''}
      </p>
    </div>
  )
}
