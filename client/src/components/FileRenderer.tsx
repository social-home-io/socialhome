/**
 * FileRenderer — file & video post rendering (§23.51).
 * Renders file attachments, videos, and images within PostCard.
 * Images are click-to-zoom via :mod:`Lightbox`.
 *
 * URLs flowing in here are already short-lived signed (server-side
 * ``MediaUrlSigner`` appends ``?exp=&sig=`` at serialization), so the
 * browser can load them via raw ``<img src>`` / ``<video src>`` /
 * ``<a download>`` without needing an ``Authorization`` header. See
 * ``socialhome.media_signer`` and ``socialhome.auth.SignedMediaStrategy``.
 */
import { useState } from 'preact/hooks'
import { Lightbox } from './Lightbox'

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
      <span class="sh-file-download" aria-label="Download">⬇</span>
    </a>
  )
}

export function VideoRenderer({ src, poster }: { src: string; poster?: string }) {
  return (
    <div class="sh-video-wrapper">
      <video
        class="sh-video"
        src={src}
        poster={poster}
        controls
        preload="metadata"
        playsinline
      />
    </div>
  )
}

export function ImageRenderer({ src, alt }: { src: string; alt?: string }) {
  const [zoomed, setZoomed] = useState(false)
  return (
    <>
      <button type="button" class="sh-image-wrapper"
              aria-label="Open image full-size"
              onClick={() => setZoomed(true)}>
        <img class="sh-image" src={src} alt={alt || 'Post image'}
             loading="lazy" />
      </button>
      {zoomed && (
        <Lightbox src={src} alt={alt} onClose={() => setZoomed(false)} />
      )}
    </>
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
