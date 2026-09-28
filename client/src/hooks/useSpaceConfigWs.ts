/**
 * useSpaceConfigWs — keep an open space screen in step with the
 * ``space.config.changed`` frame (``RealtimeService``; members only).
 *
 * * ``event_type === 'dissolved'`` — the space is gone (the host
 *   dissolved it, locally or on a remote household). Leave the screen
 *   for the spaces list with a toast instead of stranding the user on
 *   a space whose every request now 404s. Skipped when this tab is the
 *   one dissolving it (see ``markLocalDissolve``): that flow owns its
 *   own toast + redirect.
 * * anything else (rename, emoji, cover / icon, features, join mode,
 *   admin granted / revoked, archive…) — ``onChanged`` refetches what
 *   the screen shows. The frame is a thin signal (no config body), so
 *   the canonical GET stays the single source of the rendered shape.
 */
import { useEffect, useRef } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { ws } from '@/ws'
import { showToast } from '@/components/Toast'
import { isLocalDissolve } from '@/store/spaces'

export const SPACE_DISSOLVED_TOAST =
  'This space was dissolved and is no longer available.'

export function useSpaceConfigWs(spaceId: string, onChanged: () => void): void {
  const { route } = useLocation()
  // Latest callback without re-subscribing on every render.
  const changed = useRef(onChanged)
  changed.current = onChanged
  useEffect(() => ws.on('space.config.changed', (e) => {
    const d = e.data as { space_id?: string; event_type?: string }
    if (d.space_id !== spaceId) return
    if (d.event_type === 'dissolved') {
      if (isLocalDissolve(spaceId)) return
      showToast(SPACE_DISSOLVED_TOAST, 'info')
      route('/spaces', true)
      return
    }
    changed.current()
  }), [spaceId, route])
}
