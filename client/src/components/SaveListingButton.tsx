/**
 * SaveListingButton — heart icon to bookmark a bazaar listing (§23.23).
 *
 * Lazy-probes ``GET /api/bazaar/{id}/save`` on mount so the button
 * renders the correct filled/empty state immediately. Clicking toggles
 * via POST / DELETE and flips the local state optimistically; failures
 * revert + surface a toast.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from './Toast'

interface Props {
  postId: string
  /** Optional size override — default 22px keeps the glyph visible
   *  inside a PostCard meta row without crowding other affordances. */
  size?: number
}

export function SaveListingButton({ postId, size = 22 }: Props) {
  const [saved, setSaved] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let stopped = false
    api.get(`/api/bazaar/${postId}/save`).then(
      (body: { saved: boolean }) => {
        if (!stopped) setSaved(Boolean(body.saved))
      },
    ).catch(() => { if (!stopped) setSaved(false) })
    return () => { stopped = true }
  }, [postId])

  const toggle = async () => {
    if (busy || saved === null) return
    const next = !saved
    setBusy(true)
    setSaved(next)  // optimistic
    try {
      if (next) {
        await api.post(`/api/bazaar/${postId}/save`, {})
        showToast('Saved to your bookmarks.', 'success')
      } else {
        await api.delete(`/api/bazaar/${postId}/save`)
        showToast('Removed from bookmarks.', 'info')
      }
    } catch (err: unknown) {
      setSaved(!next)  // revert
      showToast(
        `Could not ${next ? 'save' : 'unsave'}: ${(err as Error)?.message ?? err}`,
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const label = saved
    ? 'Remove from saved listings'
    : 'Save listing to bookmarks'
  return (
    <button type="button"
            class={`sh-save-listing-btn ${saved ? 'sh-save-listing-btn--on' : ''}`}
            aria-pressed={Boolean(saved)}
            aria-label={label}
            title={label}
            disabled={busy || saved === null}
            style={{ fontSize: `${size}px` }}
            onClick={() => void toggle()}>
      <span aria-hidden="true">{saved ? '♥' : '♡'}</span>
    </button>
  )
}
