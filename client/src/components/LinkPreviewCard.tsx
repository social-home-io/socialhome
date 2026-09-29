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
 * tab with ``noopener noreferrer`` so the site learns neither the referrer
 * nor gets a handle on this window.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { LinkPreview } from '@/types'
import { linkDomain, safeWebUrl } from '@/utils/linkPreview'

interface LinkPreviewCardProps {
  preview: LinkPreview
  /** Composer mode: shows a remove (×) button that opts the post out. */
  onRemove?: () => void
}

export function LinkPreviewCard({ preview, onRemove }: LinkPreviewCardProps) {
  const [imageFailed, setImageFailed] = useState(false)
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
  return (
    <div class={`sh-link-preview${showImage ? '' : ' sh-link-preview--no-image'}`}>
      {href ? (
        <a
          class="sh-link-preview-link"
          href={href}
          target="_blank"
          rel="noopener noreferrer nofollow ugc"
          aria-label={t('link_preview.open', { site: domain || href })}
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
