/**
 * useLinkPreview — the composer's live link card.
 *
 * Watches the draft text, and once the first web link in it has been
 * stable for a moment asks the backend for its preview
 * (``POST /api/link-preview``). The backend builds the card on this
 * household behind its SSRF guard and caches it, so submitting the post
 * reuses the same result. The client never sends preview fields with the
 * post — only whether the author removed the card (``no_link_preview``).
 *
 * * Removing the card is per link: typing a different link shows its card
 *   again; ``restore`` brings a removed card back in one click.
 * * A 403 (the household admin turned previews off) silences the hook for
 *   the rest of the session; any other failure just means "no card".
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import type { LinkPreview } from '@/types'
import { firstUrl } from '@/utils/linkPreview'

/** Pause after the last keystroke before asking for a card. */
export const LINK_PREVIEW_DEBOUNCE_MS = 600

let disabledForSession = false

/** Test hook: forget the session-wide "previews are off" latch. */
export function resetLinkPreviewSession(): void {
  disabledForSession = false
}

export interface LinkPreviewState {
  /** The first link in the text, or ``null``. */
  url: string | null
  preview: LinkPreview | null
  loading: boolean
  /** The author removed the card for ``url``. */
  dismissed: boolean
  dismiss: () => void
  restore: () => void
}

export function useLinkPreview(text: string): LinkPreviewState {
  const url = disabledForSession ? null : firstUrl(text)
  const [preview, setPreview] = useState<LinkPreview | null>(null)
  const [previewFor, setPreviewFor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [dismissedUrl, setDismissedUrl] = useState<string | null>(null)
  const seq = useRef(0)

  useEffect(() => {
    if (!url || url === previewFor) {
      // Nothing to ask (or already answered): drop any pending answer
      // for a link that is no longer in the text.
      seq.current++
      setLoading(false)
      return
    }
    const mine = ++seq.current
    setLoading(true)
    const timer = setTimeout(async () => {
      try {
        const res = await api.post('/api/link-preview', { url }) as {
          preview: LinkPreview | null
        }
        if (mine !== seq.current) return
        setPreview(res?.preview ?? null)
        setPreviewFor(url)
      } catch (err: unknown) {
        if (mine !== seq.current) return
        if ((err as { status?: number } | null)?.status === 403) disabledForSession = true
        setPreview(null)
        setPreviewFor(url)
      } finally {
        if (mine === seq.current) setLoading(false)
      }
    }, LINK_PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [url, previewFor])

  const current = url && url === previewFor ? preview : null
  return {
    url,
    preview: current,
    loading: !!url && loading,
    dismissed: !!url && dismissedUrl === url,
    dismiss: () => setDismissedUrl(url),
    restore: () => setDismissedUrl(null),
  }
}
