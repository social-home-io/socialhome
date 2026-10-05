/**
 * FileRenderer — file & video post rendering (§23.51).
 * Renders file attachments, videos, and images within PostCard.
 * Images are click-to-zoom via the global :mod:`ImageLightbox`
 * overlay (mounted once in :mod:`App`), so the prev/next + keyboard
 * + download + copy-reference UX matches the gallery and multi-image
 * posts.
 *
 * URLs flowing in here are already short-lived signed (server-side
 * ``MediaUrlSigner`` appends ``?exp=&sig=`` at serialization), so the
 * browser can load them via raw ``<img src>`` / ``<video src>`` /
 * ``<a download>`` without needing an ``Authorization`` header. See
 * ``socialhome.media_signer`` and ``socialhome.auth.SignedMediaStrategy``.
 */
import { openLightbox } from './ImageLightbox'
import { VideoMedia } from './VideoMedia'
import { t } from '@/i18n/i18n'

interface FileAttachment {
  url: string
  mime_type: string
  original_name: string
  size_bytes: number
}

export function FileRenderer({ file }: { file: FileAttachment }) {
  const sizeLabel = formatSize(file.size_bytes)
  const icon = iconFor(file.mime_type, file.original_name)

  return (
    <a href={file.url} download={file.original_name}
       class="sh-file-attachment" target="_blank" rel="noopener">
      <span class="sh-file-icon" aria-hidden="true">{icon}</span>
      <div class="sh-file-info">
        <span class="sh-file-name">{file.original_name}</span>
        <span class="sh-file-size">{sizeLabel}</span>
      </div>
      <span class="sh-file-download" aria-label={t('media.download')}>⬇</span>
    </a>
  )
}

export function VideoRenderer({
  src, poster, mediaStatus,
}: {
  src: string
  poster?: string
  /** Background-transcode state from the list payload — ``'processing'``
   *  shows a placeholder until the ``media.ready`` WS frame swaps it for
   *  the player. Absent on older payloads → treated as ready. */
  mediaStatus?: 'processing' | 'failed' | 'ready'
}) {
  return (
    <div class="sh-video-wrapper">
      <VideoMedia src={src} poster={poster} mediaStatus={mediaStatus} />
    </div>
  )
}

export function ImageRenderer({ src, alt }: { src: string; alt?: string }) {
  return (
    <button type="button" class="sh-image-wrapper"
            aria-label={t('media.open_full_size')}
            onClick={() => openLightbox({
              items: [{ url: src, item_type: 'photo', caption: alt }],
            })}>
      <img class="sh-image" src={src} alt={alt || t('media.post_image')}
           loading="lazy" />
    </button>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function iconFor(mime: string, name: string): string {
  if (mime.startsWith('image/')) return '🖼️'
  if (mime.startsWith('video/')) return '🎬'
  if (mime.startsWith('audio/')) return '🎵'
  if (mime === 'application/pdf' || name.toLowerCase().endsWith('.pdf')) return '📕'
  if (/\.(zip|tar|gz|7z|rar)$/i.test(name)) return '🗜'
  if (/\.(md|txt|rtf)$/i.test(name)) return '📝'
  if (/\.(csv|xlsx?|ods)$/i.test(name)) return '📊'
  if (/\.(docx?|odt)$/i.test(name)) return '📃'
  return '📎'
}
