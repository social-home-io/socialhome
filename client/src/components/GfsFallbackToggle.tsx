/**
 * GfsFallbackToggle — admin switch that lets a paired household be reached
 * through a GFS (Global Federation Server) when the direct connection fails.
 *
 * Both households have to turn it on, and they need a GFS in common; the
 * status line says which of those is still missing. Which GFS the two share
 * is never shown — the backend only counts the routes it found.
 *
 * Optimistic flip + PATCH /api/pairing/connections/{id} {gfs_relay}, revert
 * + toast on error. After switching on, the status is re-read a few times
 * with back-off (route discovery answers in the background), until a shared
 * GFS turns up or the steps run out; closing the panel cancels them.
 */
import { useEffect, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
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

/** Delays between the status re-reads after switching on (~22 s in all):
 *  a probe and its ack cross the GFS twice, and the other household's
 *  probe back may trail by a few seconds. */
export const GFS_FALLBACK_RECHECK_DELAYS_MS = [3000, 7000, 12000] as const

function statusText(s: GfsFallbackState, peer: string): string {
  if (!s.gfs_relay_available) {
    // Still on (e.g. the other household rolled back): say so — "not
    // available" next to a ticked box reads as a contradiction.
    return t(s.gfs_relay ? 'gfs_fallback.unavailable_on' : 'gfs_fallback.unavailable', { peer })
  }
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
    let timer: ReturnType<typeof setTimeout> | undefined
    const step = (i: number) => {
      if (i >= GFS_FALLBACK_RECHECK_DELAYS_MS.length) return
      timer = setTimeout(() => {
        api.get('/api/connections')
          .then((rows: unknown) => {
            if (cancelled || !Array.isArray(rows)) return
            const row = rows.find((r: { instance_id?: string }) => r?.instance_id === instanceId)
            const next = row ? pick(row, state) : state
            if (row) setState(prev => pick(row, prev))
            // Done once a shared GFS turned up or the switch went off.
            if (next.gfs_routes > 0 || !next.gfs_relay) return
            step(i + 1)
          })
          .catch(() => { if (!cancelled) step(i + 1) })
      }, GFS_FALLBACK_RECHECK_DELAYS_MS[i])
    }
    step(0)
    return () => { cancelled = true; if (timer !== undefined) clearTimeout(timer) }
    // ``state`` is read only as a fallback for a missing row.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      // ``ApiError.message`` is already the translated line for a coded
      // refusal (``apiErrors.ts``); anything else (offline, a bug) gets
      // the friendly fallback, never a raw error string.
      showToast(
        e instanceof ApiError && e.message ? e.message : t('gfs_fallback.failed'),
        'error',
      )
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
