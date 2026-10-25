/**
 * HighlightQuickShareDialog — single-frame highlight composer (80% case).
 *
 * The full ``HighlightComposerPage`` carries a multi-frame storyboard,
 * per-frame captions, and an audience picker with two pickers + an
 * advanced per-person sub-picker. That's the right surface for someone
 * actually building a multi-frame story — but it's overkill for the
 * day-to-day "I just want to share this one photo before midnight"
 * case, which dominates the inbox.
 *
 * This dialog is the streamlined entry: drop a photo or video, an
 * optional caption, hit Share. A small "Build a multi-frame story →"
 * link in the dialog footer escalates to the full composer for users who
 * want the storyboard or a narrower audience.
 *
 * Audience: once the author has posted today, the frame joins today's
 * highlight and its audience, and the dialog says who that is. The frame
 * always names the audience the dialog showed — so one sent after UTC
 * midnight starts the new day for the same people, and the server refuses
 * it (409) if today's highlight is for other people. A new day's highlight
 * goes to all paired households when that is the Settings default; with a
 * "pick per highlight" default the dialog sends the author to the full
 * composer instead of guessing. Share waits until today is known.
 *
 * Mirrors the ``MomentumComposerDialog`` + ``CalendarEventDialog``
 * pattern: module-level signals + ``openHighlightQuickShare()`` helper
 * + a single ``<HighlightQuickShareDialog />`` mount on the host page.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { MediaDropzone } from './MediaDropzone'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'
import {
  audienceBody,
  defaultAudienceKind,
  loadMyToday,
  todayFixedNote,
} from '@/features/highlights/todayAudience'
import type { OwnHighlight } from '@/types'

const CAPTION_MAX = 140

const open = signal<boolean>(false)
const submitting = signal<boolean>(false)
const caption = signal<string>('')
/** Canonical ``/api/media/{filename}`` URL — what we send to the
 *  server. Empty until a file is uploaded. */
const mediaUrl = signal<string>('')
/** Short-lived signed URL — used only for the local preview ``<img>``
 *  / ``<video>``. Never sent. */
const mediaPreviewUrl = signal<string>('')
const mediaType = signal<'image' | 'video' | null>(null)
/** ``undefined`` while loading; ``null`` when the author hasn't posted
 *  today; else today's highlight (its audience is fixed). */
const today = signal<OwnHighlight | null | undefined>(undefined)
/** Today's highlight has a public link — its note says so. */
const todayPublished = signal<boolean>(false)

/** Read today's highlight. A failed read counts as "none today": the
 *  frame then names the Settings default, and the server refuses it (409)
 *  if today's highlight is for other people — never wider. */
function loadToday(): void {
  loadMyToday().then(
    (mine) => {
      today.value = mine?.highlight ?? null
      todayPublished.value = mine?.published ?? false
    },
    () => {
      today.value = null
      todayPublished.value = false
    },
  )
}

/** Open the quick-share dialog. Resets all state — call this fresh
 *  every time, no carry-over from a previous abandoned share. */
export function openHighlightQuickShare(): void {
  caption.value = ''
  mediaUrl.value = ''
  mediaPreviewUrl.value = ''
  mediaType.value = null
  submitting.value = false
  today.value = undefined
  todayPublished.value = false
  open.value = true
  loadToday()
}

/** A new day's highlight whose Settings default is "pick per highlight" —
 *  quick share can't pick, the full composer can. */
function needsComposer(): boolean {
  return today.value === null && defaultAudienceKind() !== 'all_paired'
}

function closeDialog(): void {
  open.value = false
}

interface Props {
  /** Called after a successful share. Receives the highlight id so the
   *  caller can route to the viewer or refresh its inbox. */
  onShared?: (highlightId: string) => void
}

export function HighlightQuickShareDialog({ onShared }: Props = {}) {
  const captionRef = useRef<HTMLTextAreaElement | null>(null)

  // Auto-focus the caption when the dialog opens — drop-or-pick a file
  // first is the natural flow, but if the user already has a clipboard
  // photo to paste they may want to type first; either way starting
  // focused on a writable surface beats focusing the close button.
  useEffect(() => {
    if (open.value) {
      const id = requestAnimationFrame(() => captionRef.current?.focus())
      return () => cancelAnimationFrame(id)
    }
  }, [open.value])

  const acceptFiles = async (files: File[]) => {
    const f = files[0]
    if (!f) return
    const isVideo = f.type.startsWith('video/')
    const isImage = f.type.startsWith('image/')
    if (!isVideo && !isImage) {
      showToast(t('highlight.quick.pick_media'), 'error')
      return
    }
    try {
      const result = await uploadWithProgress(f)
      mediaUrl.value = result.url
      mediaPreviewUrl.value = result.signed_url
      mediaType.value = isVideo ? 'video' : 'image'
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file: f }), 'error')
      mediaUrl.value = ''
      mediaPreviewUrl.value = ''
      mediaType.value = null
    }
  }

  const clearMedia = () => {
    mediaUrl.value = ''
    mediaPreviewUrl.value = ''
    mediaType.value = null
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    const shown = today.value
    if (!mediaUrl.value || submitting.value || shown === undefined || needsComposer()) return
    submitting.value = true
    try {
      const body: Record<string, unknown> = {
        media_url:    mediaUrl.value,
        frame_type:   mediaType.value,
        caption_text: caption.value.trim() || null,
      }
      // Exactly the audience the dialog showed: today's, or a new day
      // for all paired households (the Settings default).
      Object.assign(body, shown !== null
        ? audienceBody(shown.audience_kind, shown.audience)
        : audienceBody('all_paired', []))
      const r = await api.post('/api/highlights/frames', body) as
        { highlight: { id: string }; frame: { id: string } }
      showToast(t('highlight.shared'), 'success')
      open.value = false
      onShared?.(r.highlight.id)
    } catch (err: unknown) {
      showToast(t('highlight.share_failed', { error: String((err as Error)?.message ?? err) }), 'error')
      submitting.value = false
      // Today may have changed (another tab, a new day): show who the
      // next try goes to before it is sent.
      loadToday()
    }
  }

  const remaining = CAPTION_MAX - caption.value.length

  return (
    <Modal open={open.value} onClose={closeDialog} title={t('composer.share_highlight')}>
      <form class="sh-highlight-quick-share" onSubmit={submit}>
        {!mediaUrl.value && (
          <MediaDropzone
            accept="image/*,video/*"
            hint={t('highlight.quick.drop_hint')}
            pickLabel={t('highlight.quick.pick_label')}
            draggingHint={t('composer.drop.dragging')}
            onFiles={acceptFiles}
          />
        )}
        {mediaUrl.value && mediaType.value === 'image' && (
          <div class="sh-composer-attachment">
            <img
              src={mediaPreviewUrl.value || mediaUrl.value}
              alt=""
              class="sh-highlight-quick-preview"
            />
            <button
              type="button"
              class="sh-composer-remove-attach"
              aria-label={t('highlight.quick.remove_media')}
              onClick={clearMedia}
            >✕</button>
          </div>
        )}
        {mediaUrl.value && mediaType.value === 'video' && (
          <div class="sh-composer-attachment">
            <video
              src={mediaPreviewUrl.value || mediaUrl.value}
              class="sh-highlight-quick-preview"
              controls
              muted
              preload="metadata"
            />
            <button
              type="button"
              class="sh-composer-remove-attach"
              aria-label={t('highlight.quick.remove_media')}
              onClick={clearMedia}
            >✕</button>
          </div>
        )}
        <UploadProgressBar />

        <label class="sh-highlight-quick-caption">
          {t('highlight.quick.caption')}
          <textarea
            ref={captionRef}
            rows={2}
            maxLength={CAPTION_MAX}
            placeholder={t('highlight.quick.caption_placeholder')}
            value={caption.value}
            onInput={(e) => {
              caption.value = (e.target as HTMLTextAreaElement).value
            }}
          />
          <span class={remaining < 0 ? 'sh-error' : 'sh-muted'}>
            {t(isOne(remaining) ? 'highlight.quick.chars_left_one' : 'highlight.quick.chars_left', { n: String(remaining) })}
          </span>
        </label>

        {today.value !== undefined && (
          <p class="sh-muted sh-highlight-quick-audience-note">
            {today.value !== null
              ? todayFixedNote(today.value, todayPublished.value)
              : needsComposer()
                ? t('highlight.quick.pick_in_composer')
                : t('highlight.audience.new_all')}
          </p>
        )}

        <div class="sh-form-actions">
          <a
            class="sh-link sh-highlight-quick-escalate"
            href={addBase('/highlights/new')}
            onClick={closeDialog}
          >
            {t('highlight.quick.escalate')}
          </a>
          <Button variant="secondary" onClick={closeDialog}>{t('common.cancel')}</Button>
          <Button
            type="submit"
            loading={submitting.value}
            disabled={!mediaUrl.value || today.value === undefined || needsComposer()}
          >
            {t('highlight.quick.share')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
