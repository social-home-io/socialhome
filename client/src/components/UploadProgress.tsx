/**
 * UploadProgress — file upload indicator (§23.73).
 *
 * Three observable phases:
 *
 *  1. ``uploading`` — XHR is sending bytes. ``percent`` reflects the
 *     network transmission (0..99).
 *  2. ``processing`` — the request body has reached the server and is
 *     being transcoded (Pillow re-encode for images, PyAV transcode
 *     for video, OGG/Opus validate for audio). The XHR's
 *     ``upload.onload`` fires when the body has finished uploading;
 *     ``xhr.onload`` fires only after the server returns. In the
 *     window between, the user used to see "100%" hang silently —
 *     up to several minutes for a 1080p video transcode. We surface
 *     this as a discrete ``processing`` state so the composer can
 *     show "Processing…" instead of a stuck 100% bar.
 *  3. ``done`` / ``failed`` — terminal.
 *
 * Per-call subscribers receive events via the optional ``onEvent``
 * callback. The legacy global ``uploadProgress`` signal is still
 * updated for the surfaces that mount ``<UploadProgressBar />`` —
 * composers that want chip-local feedback can ignore it and read the
 * callback events directly.
 */
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'

export type UploadPhase = 'uploading' | 'processing' | 'done' | 'failed'

export interface UploadEvent {
  phase: UploadPhase
  /** Bytes-sent percent for ``uploading``; 100 once the body has
   *  reached the server. Holds at 100 throughout ``processing``. */
  percent: number
  filename: string
}

export const uploadProgress = signal<{ filename: string; percent: number; phase: UploadPhase } | null>(null)

export interface UploadResult {
  /** Canonical (unsigned) URL — store this on the post / message
   *  ``media_url`` field. The server signs fresh on every read. */
  url: string
  /** Signed URL the SPA can drop straight into ``<img src>`` /
   *  ``<video src>`` for the immediate post-upload preview. */
  signed_url: string
  filename: string
}

export async function uploadWithProgress(
  file: File,
  onEvent?: (e: UploadEvent) => void,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const formData = new FormData()
    formData.append('file', file)

    // Helper: fan an event out to both the per-call callback AND the
    // legacy global signal. Terminal phases (``done`` / ``failed``)
    // clear the global signal so the global bar disappears.
    const emit = (phase: UploadPhase, percent: number) => {
      if (onEvent) onEvent({ phase, percent, filename: file.name })
      if (phase === 'done' || phase === 'failed') {
        uploadProgress.value = null
      } else {
        uploadProgress.value = { filename: file.name, percent, phase }
      }
    }

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        emit('uploading', Math.round((e.loaded / e.total) * 100))
      }
    }
    // ``xhr.upload.onload`` fires when the request body has finished
    // streaming to the server. ``xhr.onload`` fires only once the
    // server returns a response. The window in between IS the
    // server-side processing time — surface it as a distinct
    // ``processing`` event so the composer chip can swap copy from
    // "Uploading…" to "Processing…".
    xhr.upload.onload = () => emit('processing', 100)
    xhr.onload = () => {
      if (xhr.status < 300) {
        const data = JSON.parse(xhr.responseText)
        emit('done', 100)
        // Server returns ``{url, signed_url, filename}``. Pre-signed
        // URL backend rollouts may omit ``signed_url`` — fall back to
        // the canonical URL so the preview at least attempts to load.
        resolve({
          url: data.url || data.filename,
          signed_url: data.signed_url || data.url || data.filename,
          filename: data.filename,
        })
      } else {
        emit('failed', 100)
        reject(new Error(`Upload failed: ${xhr.status}`))
      }
    }
    xhr.onerror = () => { emit('failed', 0); reject(new Error('Upload failed')) }
    // Relative URL (no leading slash) so the browser resolves it
    // against ``<base href>`` — under HA Supervisor ingress that's
    // ``/api/hassio_ingress/<token>/``, so the upload lands on the
    // add-on instead of HA Core. An absolute ``/api/...`` bypassed
    // ``<base href>`` and 404'd (#303).
    xhr.open('POST', 'api/media/upload')
    const token = localStorage.getItem('sh_token')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.send(formData)
  })
}

export function UploadProgressBar() {
  const p = uploadProgress.value
  if (!p) return null
  const processing = p.phase === 'processing'
  // Title sits above the bar so the filename has room to breathe and
  // we can show a status word ("Uploading…" / "Processing…") without
  // fighting the percentage for space. Centred on mobile.
  const status = processing ? t('media.processing') : t('media.uploading')
  return (
    <div
      class={
        'sh-upload-progress'
        + (processing ? ' sh-upload-progress--processing' : '')
      }
      role="progressbar"
      aria-valuenow={p.percent}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`${status} ${p.filename}`}
    >
      <div class="sh-upload-progress__header">
        <span class="sh-upload-progress__status" aria-hidden="true">
          {processing ? (
            <span class="sh-upload-progress__spinner" />
          ) : null}
          {status}
        </span>
        <span class="sh-upload-progress__filename">{p.filename}</span>
        {!processing && (
          <span class="sh-upload-progress__pct">{p.percent}%</span>
        )}
      </div>
      <div class="sh-upload-progress__track">
        <div
          class="sh-upload-progress__fill"
          style={{ width: `${p.percent}%` }}
        />
      </div>
    </div>
  )
}
