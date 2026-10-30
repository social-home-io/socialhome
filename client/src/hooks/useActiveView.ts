/**
 * useActiveView — tell the backend "this person is looking at *key* right
 * now" (the ``dm.active`` frame), so what arrives for it makes no bell or
 * push: a DM thread (its conversation id) or an app on screen
 * (``app:<id>``, never a conversation id).
 *
 * The backend keeps ONE active key per WebSocket connection, so every
 * view on the page shares one claim. The view that claimed last owns it
 * (module-level ``owner``), and a view only lets go while it still owns
 * it: an older view unmounting — or going to the background — never
 * un-suppresses the one on screen, whichever kinds the two views are.
 *
 * A tab in the background is not looking: it lets go when hidden and
 * claims again when it comes back (``onClaim`` runs on every claim, e.g.
 * to read rows that came meanwhile). ``key`` null claims nothing.
 */
import { useEffect, useRef } from 'preact/hooks'
import { ws } from '@/ws'

/** The mounted view holding the connection's claim, or ``null``. */
let owner: symbol | null = null

/** Who holds the claim now — for tests. */
export function activeViewOwner(): symbol | null {
  return owner
}

export function useActiveView(key: string | null, onClaim?: () => void): void {
  const onClaimRef = useRef(onClaim)
  onClaimRef.current = onClaim
  useEffect(() => {
    if (!key) return
    const me = Symbol(key)
    const claim = () => {
      owner = me
      ws.send('dm.active', { conversation_id: key })
      onClaimRef.current?.()
    }
    const release = () => {
      if (owner !== me) return
      owner = null
      ws.send('dm.active', { conversation_id: null })
    }
    const onVis = () => {
      if (document.visibilityState === 'visible') claim()
      else release()
    }
    if (document.visibilityState !== 'hidden') claim()
    document.addEventListener('visibilitychange', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      release()
    }
  }, [key])
}
