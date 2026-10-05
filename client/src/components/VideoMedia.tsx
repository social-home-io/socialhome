/**
 * VideoMedia — a status-aware video renderer (§ async video transcode).
 *
 * Video uploads transcode in the background. A list payload tags each
 * video item with ``media_status``:
 *
 *  - ``'processing'`` — the worker is still encoding; show a placeholder.
 *  - ``'failed'``     — transcode failed; show a small failed state.
 *  - ``'ready'`` / absent — play it (older payloads omit the field).
 *
 * When transcoding finishes the backend pushes a ``media.ready`` WS frame
 * to the uploader; :mod:`store/mediaReady` records the output filename and
 * this component (which reads :data:`readyMedia` in its body) re-renders
 * to swap placeholder → player, overriding a now-stale ``'processing'``
 * status from the initial fetch.
 *
 * The player shape mirrors :func:`FileRenderer.VideoRenderer` exactly so
 * a ready video looks identical wherever it's rendered. URLs arrive
 * already short-lived signed from the API, so we use them as-is in
 * ``src`` / ``poster`` (no base / auth-header surgery) — same as the
 * other media renderers.
 */
import { Spinner } from './Spinner'
import { readyMedia, failedMedia, mediaFilename } from '@/store/mediaReady'
import { t } from '@/i18n/i18n'

interface Props {
  src: string                 // api/media/<uuid>.webm (signed ok)
  poster?: string             // thumbnail url (signed ok)
  mediaStatus?: 'processing' | 'failed' | 'ready'
  class?: string              // forwarded to the <video> / placeholder
}

export function VideoMedia(props: Props) {
  const { src, poster, mediaStatus } = props
  const fn = mediaFilename(src)
  // Read both signals in the render body so either WS frame re-renders us.
  const readyFlag = readyMedia.value.has(fn)
  const failedFlag = failedMedia.value.has(fn)
  const ready =
    (mediaStatus !== 'processing' && mediaStatus !== 'failed') || readyFlag

  // A WS-driven failure wins over a stale 'processing'/'ready' status. A
  // later 'media.ready' still wins via readyFlag — but a failed transcode
  // never emits ready, so the order can't strand a playable clip.
  if ((mediaStatus === 'failed' && !readyFlag) || failedFlag) {
    return (
      <div class={'sh-video-wrapper sh-video-failed ' + (props.class || '')}>
        <span class="sh-video-failed-msg">
          ⚠️ {t('media.video_failed')}
        </span>
      </div>
    )
  }

  if (!ready) {
    return (
      <div class={'sh-video-wrapper sh-video-processing ' + (props.class || '')}>
        {poster && (
          <img class="sh-video-processing-poster" src={poster} alt=""
               aria-hidden="true" />
        )}
        <div class="sh-video-processing-overlay">
          <Spinner label={t('media.video_processing')} />
          <span class="sh-video-processing-msg">
            {t('media.video_processing_hint')}
          </span>
        </div>
      </div>
    )
  }

  return (
    <video
      class={'sh-video ' + (props.class || '')}
      src={src}
      poster={poster}
      controls
      preload="metadata"
      playsInline
    />
  )
}
