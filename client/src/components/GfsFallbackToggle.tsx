/**
 * GfsFallbackToggle — admin switch that lets a paired household be reached
 * through a GFS (Global Federation Server) when the direct connection fails.
 *
 * Both households have to turn it on, and they need a GFS in common; the
 * status line says which of those is still missing. Which GFS the two share
 * is never shown — the backend only counts the routes it found.
 *
 * Optimistic flip + PATCH /api/pairing/connections/{id} {gfs_relay}, revert
 * + toast on error. After switching on, the status is re-read once a few
 * seconds later: route discovery answers in the background.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from './Toast'
import { t, isOne } from '@/i18n/i18n'

/** The four connection fields the switch reads (``GET /api/connections``). */
export interface GfsFallbackState {
  /** Our own opt-in for this household. */
  gfs_relay: boolean
  /** GFSes both households were proven to use (a count, never a list). */
  gfs_routes: number
  /** We hold the other household's key — it has turned the switch on. */
  peer_keywrap_known: boolean
  /** The switch can work with this household at all (new enough). */
  gfs_relay_available: boolean
}

export interface GfsFallbackToggleProps {
  instanceId: string
  peerName: string
  initial: GfsFallbackState
}

/** How long after switching on the status is re-read once. */
export const GFS_FALLBACK_RECHECK_MS = 4000

function statusText(s: GfsFallbackState, peer: string): string {
  if (!s.gfs_relay_available) return t('gfs_fallback.unavailable', { peer })
  if (!s.gfs_relay) return t('gfs_fallback.off', { peer })
  if (s.gfs_routes > 0) {
    return t(isOne(s.gfs_routes) ? 'gfs_fallback.on_routes_one' : 'gfs_fallback.on_routes', {
      peer, n: String(s.gfs_routes),
    })
  }
  if (!s.peer_keywrap_known) return t('gfs_fallback.waiting', { peer })
  return t('gfs_fallback.searching', { peer })
}

function pick(body: unknown, fallback: GfsFallbackState): GfsFallbackState {
  const b = (body ?? {}) as Partial<GfsFallbackState>
  return {
    gfs_relay: typeof b.gfs_relay === 'boolean' ? b.gfs_relay : fallback.gfs_relay,
    gfs_routes: typeof b.gfs_routes === 'number' ? b.gfs_routes : fallback.gfs_routes,
    peer_keywrap_known: typeof b.peer_keywrap_known === 'boolean'
      ? b.peer_keywrap_known : fallback.peer_keywrap_known,
    gfs_relay_available: typeof b.gfs_relay_available === 'boolean'
      ? b.gfs_relay_available : fallback.gfs_relay_available,
  }
}

export function GfsFallbackToggle({ instanceId, peerName, initial }: GfsFallbackToggleProps) {
  const [state, setState] = useState<GfsFallbackState>(initial)
  const [busy, setBusy] = useState(false)
  /** Bumped by every successful "on" — schedules one status re-read. */
  const [recheck, setRecheck] = useState(0)

  useEffect(() => {
    if (recheck === 0) return
    let cancelled = false
    const timer = setTimeout(() => {
      api.get('/api/connections')
        .then((rows: unknown) => {
          if (cancelled || !Array.isArray(rows)) return
          const row = rows.find((r: { instance_id?: string }) => r?.instance_id === instanceId)
          if (row) setState(prev => pick(row, prev))
        })
        .catch(() => { /* the line keeps its last known state */ })
    }, GFS_FALLBACK_RECHECK_MS)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [recheck, instanceId])

  const toggle = async () => {
    if (busy) return
    const before = state
    const next = !state.gfs_relay
    setState({ ...state, gfs_relay: next, gfs_routes: next ? state.gfs_routes : 0 })
    setBusy(true)
    try {
      const body = await api.patch(`/api/pairing/connections/${instanceId}`, { gfs_relay: next })
      setState(prev => pick(body, prev))
      if (next) setRecheck(n => n + 1)
    } catch (e: any) {
      setState(before)
      showToast(e?.message || t('gfs_fallback.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  // Off and nothing the other household could ever answer with: the switch
  // stays visible (so the admin learns why) but cannot be turned on. Once
  // on, it can always be turned off.
  const locked = !state.gfs_relay && !state.gfs_relay_available
  const helpId = `sh-gfs-fallback-status-${instanceId}`
  return (
    <div class="sh-gfs-fallback">
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={state.gfs_relay}
          disabled={locked || busy}
          aria-describedby={helpId}
          onChange={() => void toggle()}
        />
        {t('gfs_fallback.label')}
      </label>
      <p
        id={helpId}
        class={`sh-gfs-fallback__status${state.gfs_relay_available ? ' sh-muted' : ' sh-text-warning'}`}
        role="status"
        style={{ margin: 'var(--sh-space-xs) 0 0', fontSize: 'var(--sh-font-size-sm)' }}
      >
        {statusText(state, peerName)}
      </p>
      {!locked && (
        <p
          class="sh-muted sh-gfs-fallback__privacy"
          role="note"
          style={{ margin: 'var(--sh-space-xs) 0 0', fontSize: 'var(--sh-font-size-sm)' }}
        >
          {t('gfs_fallback.privacy', { peer: peerName })}
        </p>
      )}
    </div>
  )
}
