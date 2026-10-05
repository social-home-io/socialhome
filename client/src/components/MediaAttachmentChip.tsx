/**
 * MediaAttachmentChip — the composer-local "you attached this file"
 * chip, with first-class feedback for the upload + server-processing
 * window.
 *
 * The chip renders in three phases that the composer drives:
 *
 *   * ``uploading`` — the user just picked a file. Show a local
 *     blob-URL preview (image / video) or a file icon, with a
 *     percent ring overlay. The percent comes from
 *     ``XMLHttpRequest.upload.onprogress`` — bytes sent over the
 *     wire.
 *   * ``processing`` — the bytes are on the server but the response
 *     hasn't arrived yet. For an image this is Pillow re-encoding
 *     to WebP; for a video this is PyAV transcoding to VP9/Opus,
 *     which can take a minute on a phone-shot 1080p clip. Pulse a
 *     spinner with copy that matches the media kind.
 *   * ``ready`` — the response landed. Chip becomes a normal
 *     "attached" affordance with a clear / cancel button.
 *
 * The chip is the single render surface — no global progress bar
 * needed alongside (the composer that mounts this chip can drop
 * ``<UploadProgressBar />`` entirely, or keep it as a redundant
 * page-bottom fixture).
 */
import type preact from 'preact'
import { t } from '@/i18n/i18n'

export type AttachmentChipPhase = 'uploading' | 'processing' | 'ready' | 'failed'
export type AttachmentChipKind = 'image' | 'video' | 'file' | 'audio'

interface Props {
  phase: AttachmentChipPhase
  kind: AttachmentChipKind
  filename: string
  /** Local ``URL.createObjectURL`` (image / video) or signed-URL
   *  preview (post-upload). Optional — the chip falls back to the
   *  kind glyph when absent. */
  previewUrl?: string | null
  /** 0..100 — only used when ``phase === 'uploading'``. */
  percent?: number
  /** Optional byte size, surfaced as the "MB" caption when known.
   *  ``null`` for in-flight uploads where we only know the local
   *  ``File.size`` — that's already correct. */
  sizeBytes?: number | null
  /** Click handler for the ``×`` clear button. ``undefined`` hides
   *  the button entirely — useful for surfaces that don't allow
   *  cancellation mid-upload. */
  onClear?: () => void
  /** Optional retry button surfaced on ``phase === 'failed'``. */
  onRetry?: () => void
  /** Error message rendered under the chip on ``phase === 'failed'``. */
  errorMessage?: string | null
}

export function MediaAttachmentChip({
  phase,
  kind,
  filename,
  previewUrl,
  percent = 0,
  sizeBytes,
  onClear,
  onRetry,
  errorMessage,
}: Props): preact.JSX.Element {
  const labelForPhase =
    phase === 'uploading'
      ? t('media.uploading_percent', { n: String(Math.min(99, percent)) })
      : phase === 'processing'
        ? _processingCopy(kind)
        : phase === 'failed'
          ? t('media.upload_failed')
          : null
  const inFlight = phase === 'uploading' || phase === 'processing'

  return (
    <div
      class={
        'sh-attach-chip'
        + ` sh-attach-chip--${kind}`
        + ` sh-attach-chip--${phase}`
      }
      role="status"
      aria-live="polite"
      aria-busy={inFlight}
    >
      {/* Preview tile. Image / video use the local blob URL while in
       *  flight so the user sees what they're sending; ``file`` /
       *  ``audio`` fall back to a glyph. */}
      <div class="sh-attach-chip__preview">
        {previewUrl && kind === 'image' && (
          <img
            class="sh-attach-chip__media"
            src={previewUrl}
            alt={filename}
          />
        )}
        {previewUrl && kind === 'video' && (
          <video
            class="sh-attach-chip__media"
            src={previewUrl}
            preload="metadata"
            muted
            playsInline
          />
        )}
        {(!previewUrl || kind === 'file' || kind === 'audio') && (
          <span class="sh-attach-chip__glyph" aria-hidden="true">
            {kind === 'audio' ? '🎙' : '📎'}
          </span>
        )}
        {inFlight && (
          <span class="sh-attach-chip__overlay" aria-hidden="true">
            {phase === 'uploading' && (
              <ProgressRing percent={percent} />
            )}
            {phase === 'processing' && (
              <span class="sh-attach-chip__spinner" />
            )}
          </span>
        )}
      </div>
      <div class="sh-attach-chip__meta">
        <span class="sh-attach-chip__name">{filename}</span>
        {labelForPhase
          ? <span class="sh-attach-chip__phase">{labelForPhase}</span>
          : sizeBytes != null
            ? <span class="sh-attach-chip__size">{_formatBytes(sizeBytes)}</span>
            : null}
        {phase === 'failed' && errorMessage && (
          <span class="sh-attach-chip__error">{errorMessage}</span>
        )}
      </div>
      {phase === 'failed' && onRetry && (
        <button
          type="button"
          class="sh-attach-chip__retry"
          onClick={onRetry}
        >
          {t('common.retry')}
        </button>
      )}
      {onClear && (
        <button
          type="button"
          class="sh-attach-chip__clear"
          aria-label={inFlight ? t('media.cancel_upload') : t('composer.remove_attachment')}
          title={inFlight ? t('media.cancel_upload') : t('composer.remove_attachment')}
          onClick={onClear}
        >×</button>
      )}
    </div>
  )
}

/** Per-kind processing copy. Image re-encode is fast (~0.5 s for a
 *  big phone JPEG); video transcode is slow (minutes for 1080p);
 *  audio is fast but the validation is what we're surfacing. The
 *  user sees the right expectation in each case. */
function _processingCopy(kind: AttachmentChipKind): string {
  switch (kind) {
    case 'image': return t('media.processing_image')
    case 'video': return t('media.processing_video')
    case 'audio': return t('media.checking_audio')
    default: return t('media.processing')
  }
}

function _formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  const mb = bytes / (1024 * 1024)
  if (mb < 10) return `${mb.toFixed(1)} MB`
  return `${Math.round(mb)} MB`
}

/** Lightweight SVG progress ring — 32×32 stroke that draws as
 *  ``percent`` increases. Centred inside the preview tile's overlay
 *  so the user sees both the preview and the live %. */
function ProgressRing({ percent }: { percent: number }): preact.JSX.Element {
  const RADIUS = 12
  const CIRCUMFERENCE = 2 * Math.PI * RADIUS
  const offset = CIRCUMFERENCE * (1 - Math.min(100, Math.max(0, percent)) / 100)
  return (
    <svg
      class="sh-attach-chip__ring"
      width="32"
      height="32"
      viewBox="0 0 32 32"
      aria-hidden="true"
    >
      <circle
        cx="16"
        cy="16"
        r={RADIUS}
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-opacity="0.25"
      />
      <circle
        cx="16"
        cy="16"
        r={RADIUS}
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
        stroke-dasharray={CIRCUMFERENCE}
        stroke-dashoffset={offset}
        transform="rotate(-90 16 16)"
      />
    </svg>
  )
}
