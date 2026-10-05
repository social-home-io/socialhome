/**
 * MomentumComposerDialog — Modal-wrapped composer for new moments + replies.
 *
 * Replaces the routed ``MomentumComposerPage`` for the inline-feed flow:
 * tapping "What's on your mind?" or the reply button now opens a
 * dialog over the current page instead of navigating away. The deep
 * links ``/momentum/new`` and ``/momentum/{id}/reply`` still work —
 * they redirect to ``/momentum`` and auto-open the dialog so any
 * external bookmark or push-notification deep link lands the user in
 * the same place.
 *
 * Mirrors the ``CalendarEventDialog`` pattern — module-level signals
 * drive open / close + parent-id state, host page mounts a single
 * ``<MomentumComposerDialog />`` instance.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { isRestricted } from './ProtectedNotice'
import { MediaDropzone } from './MediaDropzone'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import {
  loadRegistrations,
  registrations,
} from '@/store/momentPublic'
import type { Moment } from '@/types'
import { t, isOne } from '@/i18n/i18n'

const MAX_CONTENT = 1_000
const MAX_VIDEO_MS = 15_000

const open = signal<boolean>(false)
const parentId = signal<string | null>(null)
const submitting = signal<boolean>(false)
const content = signal<string>('')
const mediaUrl = signal<string | null>(null)
const mediaPreviewUrl = signal<string | null>(null)
const mediaType = signal<'image' | 'video' | null>(null)
const durationMs = signal<number | null>(null)
const isPublic = signal<boolean>(false)

/** Open the composer dialog. Pass a moment id to compose a reply; omit
 *  for a new top-level moment. */
export function openMomentumComposer(parentMomentId: string | null = null): void {
  parentId.value = parentMomentId
  content.value = ''
  mediaUrl.value = null
  mediaPreviewUrl.value = null
  mediaType.value = null
  durationMs.value = null
  submitting.value = false
  open.value = true
  void loadRegistrations().then(() => {
    isPublic.value = registrations.value.some((r) => r.default_share)
  })
}

function closeDialog(): void {
  open.value = false
}

interface Props {
  /** Called after a successful post. Receives the created moment so the
   *  caller can refresh its local state without a refetch. */
  onPosted?: (m: Moment) => void
}

export function MomentumComposerDialog({ onPosted }: Props = {}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  // Auto-focus the textarea when the dialog opens. The Modal focus-trap
  // lands on the dialog itself by default; for a composer the operator
  // expects to start typing immediately.
  useEffect(() => {
    if (open.value) {
      // Defer one frame so the Modal's own focus management runs first.
      const id = requestAnimationFrame(() => {
        textareaRef.current?.focus()
      })
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
    if (isVideo) {
      const url = URL.createObjectURL(f)
      const probe: HTMLVideoElement = document.createElement('video')
      probe.preload = 'metadata'
      probe.src = url
      const dur = await new Promise<number>((resolve, reject) => {
        probe.onloadedmetadata = () => resolve(probe.duration * 1_000)
        probe.onerror = () => reject(new Error('Could not read video metadata'))
      }).finally(() => URL.revokeObjectURL(url))
      if (dur > MAX_VIDEO_MS + 50) {
        showToast(
          t('moment.composer.video_too_long', { max: String(MAX_VIDEO_MS / 1000), len: (dur / 1000).toFixed(1) }),
          'error',
        )
        return
      }
      durationMs.value = Math.round(dur)
    }
    try {
      const result = await uploadWithProgress(f)
      mediaUrl.value = result.url
      mediaPreviewUrl.value = result.signed_url
      mediaType.value = isVideo ? 'video' : 'image'
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file: f }), 'error')
      mediaUrl.value = null
      mediaPreviewUrl.value = null
      mediaType.value = null
      durationMs.value = null
    }
  }

  const clearMedia = () => {
    mediaUrl.value = null
    mediaPreviewUrl.value = null
    mediaType.value = null
    durationMs.value = null
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    const text = content.value.trim()
    if (!text && !mediaUrl.value) {
      showToast(t('moment.composer.empty'), 'error')
      return
    }
    if (text.length > MAX_CONTENT) {
      showToast(t('moment.composer.too_long', { n: String(MAX_CONTENT) }), 'error')
      return
    }
    submitting.value = true
    try {
      const body: Record<string, unknown> = { content: text }
      if (mediaUrl.value) {
        body.media_url = mediaUrl.value
        body.media_type = mediaType.value
        if (mediaType.value === 'video') body.duration_ms = durationMs.value
      }
      if (parentId.value) body.parent_moment_id = parentId.value
      if (registrations.value.length > 0) {
        body.is_public = isPublic.value && !isRestricted('public_moments')
      }
      const m = await api.post('/api/moments', body) as Moment
      open.value = false
      onPosted?.(m)
    } catch (err: unknown) {
      const msg = (err as Error)?.message ?? String(err)
      showToast(t('moment.composer.failed', { error: msg }), 'error')
      submitting.value = false
    }
  }

  const remaining = MAX_CONTENT - content.value.length
  const isReply = parentId.value !== null

  return (
    <Modal
      open={open.value}
      onClose={closeDialog}
      title={isReply ? t('moment.composer.reply') : t('moment.composer.title')}
    >
      <form class="sh-momentum-composer-form" onSubmit={submit}>
        {isReply && (
          <p class="sh-muted sh-momentum-composer-reply-hint">
            ↪ {t('moment.composer.replying')}
          </p>
        )}

        <textarea
          ref={textareaRef}
          class="sh-momentum-composer-text"
          rows={5}
          maxLength={MAX_CONTENT}
          placeholder={isReply ? t('moment.composer.reply_placeholder') : t('moment.composer.placeholder')}
          aria-label={isReply ? t('moment.composer.reply_aria') : t('moment.composer.aria')}
          value={content.value}
          onInput={(e) => { content.value = (e.target as HTMLTextAreaElement).value }}
        />
        <div class="sh-momentum-composer-meta">
          <span class={remaining < 0 ? 'sh-error' : 'sh-muted'}>
            {t(isOne(remaining) ? 'highlight.quick.chars_left_one' : 'highlight.quick.chars_left', { n: String(remaining) })}
          </span>
        </div>

        <div class="sh-momentum-composer-media">
          {mediaUrl.value && mediaType.value === 'image' && (
            <div class="sh-composer-attachment">
              <img
                src={mediaPreviewUrl.value ?? mediaUrl.value}
                alt=""
                class="sh-momentum-composer-preview"
              />
              <button
                type="button"
                class="sh-composer-remove-attach"
                aria-label={t('composer.remove_attachment')}
                onClick={clearMedia}
              >✕</button>
            </div>
          )}
          {mediaUrl.value && mediaType.value === 'video' && (
            <div class="sh-composer-attachment">
              <video
                src={mediaPreviewUrl.value ?? mediaUrl.value}
                class="sh-momentum-composer-preview"
                controls
                muted
                preload="metadata"
              />
              {durationMs.value !== null && (
                <span class="sh-momentum-composer-duration"
                      aria-label={t('moment.composer.duration_aria', { len: (durationMs.value / 1000).toFixed(1), max: String(MAX_VIDEO_MS / 1000) })}>
                  ⏱ {(durationMs.value / 1000).toFixed(1)}s
                </span>
              )}
              <button
                type="button"
                class="sh-composer-remove-attach"
                aria-label={t('composer.remove_attachment')}
                onClick={clearMedia}
              >✕</button>
            </div>
          )}
          {!mediaUrl.value && (
            <MediaDropzone
              accept="image/*,video/*"
              hint={t('highlight.quick.drop_hint')}
              pickLabel={t('highlight.quick.pick_label')}
              draggingHint={t('composer.drop.dragging')}
              onFiles={acceptFiles}
            />
          )}
          <UploadProgressBar />
        </div>

        {registrations.value.length > 0 && !isRestricted('public_moments') && (
          <label class="sh-momentum-composer-public">
            <input
              type="checkbox"
              checked={isPublic.value}
              onChange={(ev) => {
                isPublic.value = (ev.currentTarget as HTMLInputElement).checked
              }}
            />
            {t(registrations.value.length === 1 ? 'moment.composer.share_public_one' : 'moment.composer.share_public', { n: String(registrations.value.length) })}
          </label>
        )}

        <div class="sh-form-actions">
          <Button variant="secondary" onClick={closeDialog}>{t('common.cancel')}</Button>
          <Button type="submit" loading={submitting.value}>
            {isReply ? t('moment.composer.reply') : t('moment.composer.post')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
