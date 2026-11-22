/**
 * NotSentMark — "○ Not sent yet" in the meta line of one of the viewer's
 * OWN space posts that no other household has yet (``sync.unsent_posts``:
 * every member household still waits for it and no GFS took it). A post
 * that reached anyone is not marked. Tapping it opens the reason: "Not
 * sent yet — goes out when {Host} is back". Same glyph as the DM receipt
 * "Sending" (``ReadReceipts.tsx``), so one idea has one look.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'

export function NotSentMark({ hostName, hostedHere }: {
  /** The host household's name, ``null`` when not known ("the host"). */
  hostName: string | null
  /** This household hosts the space: it waits for the other households. */
  hostedHere: boolean
}) {
  const [open, setOpen] = useState(false)
  const detail = hostedHere
    ? t('space.post.not_sent_detail_hosted')
    : t('space.post.not_sent_detail', { host: hostName || t('space.sync.host_fallback') })
  return (
    <button type="button" class="sh-post-not-sent" aria-expanded={open}
            data-testid="post-not-sent" onClick={() => setOpen(v => !v)}>
      <span class="sh-receipt sh-receipt--pending" aria-hidden="true">○</span>
      <span>{open ? detail : t('space.post.not_sent')}</span>
    </button>
  )
}
