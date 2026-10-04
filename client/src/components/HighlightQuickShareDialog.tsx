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
 * optional caption, hit Share. Audience defaults to "all paired
 * households" — the existing default in the full composer. A small
 * "Build a multi-frame story →" link in the dialog footer escalates to
 * the full composer for users who want the storyboard.
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

/** Open the quick-share dialog. Resets all state — call this fresh
 *  every time, no carry-over from a previous abandoned share. */
export function openHighlightQuickShare(): void {
  caption.value = ''
  mediaUrl.value = ''
  mediaPreviewUrl.value = ''
  mediaType.value = null
  submitting.value = false
  open.value = true
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
      showToast('Pick an image or video.', 'error')
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
    if (!mediaUrl.value || submitting.value) return
    submitting.value = true
    try {
      const r = await api.post('/api/highlights/frames', {
        media_url:    mediaUrl.value,
        frame_type:   mediaType.value,
        caption_text: caption.value.trim() || null,
        // Audience kind matches the full composer's default — everyone
        // the household is paired with sees it. Users who want a
        // narrower audience step up to the multi-frame builder.
        audience_kind: 'all_paired',
        audience:      [],
      }) as { highlight: { id: string }; frame: { id: string } }
      showToast('Highlight shared', 'success')
      open.value = false
      onShared?.(r.highlight.id)
    } catch (err: unknown) {
      showToast(`Couldn't share: ${(err as Error)?.message ?? err}`, 'error')
      submitting.value = false
    }
  }

  const remaining = CAPTION_MAX - caption.value.length

  return (
    <Modal open={open.value} onClose={closeDialog} title="Share a highlight">
      <form class="sh-highlight-quick-share" onSubmit={submit}>
        {!mediaUrl.value && (
          <MediaDropzone
            accept="image/*,video/*"
            hint="Drag a photo or video here, or"
            pickLabel="choose media…"
            draggingHint="Drop to attach"
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
              aria-label="Remove media"
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
              aria-label="Remove media"
              onClick={clearMedia}
            >✕</button>
          </div>
        )}
        <UploadProgressBar />

        <label class="sh-highlight-quick-caption">
          Caption
          <textarea
            ref={captionRef}
            rows={2}
            maxLength={CAPTION_MAX}
            placeholder="A line for this moment…"
            value={caption.value}
            onInput={(e) => {
              caption.value = (e.target as HTMLTextAreaElement).value
            }}
          />
          <span class={remaining < 0 ? 'sh-error' : 'sh-muted'}>
            {remaining} characters left
          </span>
        </label>

        <p class="sh-muted sh-highlight-quick-audience-note">
          Visible to all your connected households for the rest of today.
        </p>

        <div class="sh-form-actions">
          <a
            class="sh-link sh-highlight-quick-escalate"
            href={addBase('/highlights/new')}
            onClick={closeDialog}
          >
            Build a multi-frame story →
          </a>
          <Button variant="secondary" onClick={closeDialog}>Cancel</Button>
          <Button
            type="submit"
            loading={submitting.value}
            disabled={!mediaUrl.value}
          >
            Share
          </Button>
        </div>
      </form>
    </Modal>
  )
}
