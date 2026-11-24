/**
 * LinkPreviewCard — the card a post shows for the first web link in its
 * text (and the live card in the composer).
 *
 * The preview is built on the AUTHOR's household and travels inside the
 * post, so this component never fetches the linked page: the image is local
 * media (a signed ``api/media/…`` URL, resolved against ``<base href>``) and
 * the text fields are plain strings rendered as text (Preact escapes them —
 * nothing here goes through ``dangerouslySetInnerHTML``).
 *
 * The link is rebuilt with ``safeWebUrl`` — only ``http(s)`` URLs become an
 * ``href``; anything else renders the card without a link. It opens in a new
 * tab with the same ``rel`` as inline links (``EXTERNAL_LINK_REL``) so the
 * site learns neither the referrer nor gets a handle on this window. Its
 * accessible name is "{title} – {site}, opens in a new tab".
 *
 * Layout: a landscape picture (YouTube, most og:images — 1.91:1) sits on
 * top at full width; a square or portrait one stays a side thumbnail. A
 * card with neither picture nor description is a compact one-line card.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { LinkPreview } from '@/types'
import { linkDomain, safeWebUrl } from '@/utils/linkPreview'
import { EXTERNAL_LINK_REL } from './markdown'

/** Wider than this (width / height) and the picture goes on top. */
const LANDSCAPE_RATIO = 1.3

interface LinkPreviewCardProps {
  preview: LinkPreview
  /** Composer mode: shows a remove (×) button that opts the post out. */
  onRemove?: () => void
}

export function LinkPreviewCard({ preview, onRemove }: LinkPreviewCardProps) {
  const [imageFailed, setImageFailed] = useState(false)
  // Most og:images are landscape (1.91:1): lay the card out that way
  // from the start so the feed does not jump when the picture arrives; a
  // square or portrait one switches to the side thumbnail on load.
  const [landscape, setLandscape] = useState(true)
  const href = safeWebUrl(preview.url)
  const domain = linkDomain(preview.url)
  const showImage = !!preview.thumbnail_url && !imageFailed
  const body = (
    <>
      {showImage && (
        <img
          class="sh-link-preview-image"
          src={preview.thumbnail_url!}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setImageFailed(true)}
          onLoad={(e) => {
            const img = e.currentTarget as HTMLImageElement
            setLandscape(img.naturalHeight > 0
              && img.naturalWidth / img.naturalHeight >= LANDSCAPE_RATIO)
          }}
        />
      )}
      <span class="sh-link-preview-text">
        {(preview.site_name || domain) && (
          <span class="sh-link-preview-site">{preview.site_name || domain}</span>
        )}
        {preview.title && <span class="sh-link-preview-title">{preview.title}</span>}
        {preview.description && (
          <span class="sh-link-preview-description">{preview.description}</span>
        )}
        {domain && preview.site_name && (
          <span class="sh-link-preview-domain">{domain}</span>
        )}
      </span>
    </>
  )
  const site = preview.site_name || domain || href || ''
  const cls = ['sh-link-preview']
  if (!showImage) cls.push('sh-link-preview--no-image')
  if (showImage && landscape) cls.push('sh-link-preview--landscape')
  if (!showImage && !preview.description) cls.push('sh-link-preview--compact')
  return (
    <div class={cls.join(' ')}>
      {href ? (
        <a
          class="sh-link-preview-link"
          href={href}
          target="_blank"
          rel={EXTERNAL_LINK_REL}
          aria-label={preview.title
            ? t('link_preview.open_titled', { title: preview.title, site })
            : t('link_preview.open', { site })}
        >
          {body}
        </a>
      ) : (
        <div class="sh-link-preview-link">{body}</div>
      )}
      {onRemove && (
        <button
          type="button"
          class="sh-link-preview-remove"
          onClick={onRemove}
          aria-label={t('link_preview.remove')}
          title={t('link_preview.remove')}
        >
          ×
        </button>
      )}
    </div>
  )
}
