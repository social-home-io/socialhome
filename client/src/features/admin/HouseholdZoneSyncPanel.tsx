/**
 * HouseholdZoneSyncPanel — Admin › Zones: the "Sync zones from Home
 * Assistant" switch (``/api/household/zones/sync``).
 *
 * On (the default) the household's zones follow Home Assistant's: they
 * show up read-only next to the zones added here and refresh every few
 * minutes. Turning it off asks first, inline — the synced zones go, the
 * household's own zones stay. A failed last sync shows as a quiet line
 * (the synced zones are kept; the server retries on its own).
 *
 * Renders nothing when the platform has no zones to sync, or when the
 * status can't be read (the zone list itself still shows).
 */
import { useEffect, useRef, useState } from 'preact/hooks'

import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t, formatLocale } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ws } from '@/ws'

const SYNC_PATH = '/api/household/zones/sync'

/** How often the "Last synced …" line re-reads the status. */
export const ZONE_SYNC_STATUS_REFRESH_MS = 60_000

export interface ZoneSyncStatus {
  available: boolean
  enabled: boolean
  last_synced_at: string | null
  last_error: string | null
}

export interface HouseholdZoneSyncPanelProps {
  /** The sync changed the zones (or may have): refetch the list. */
  onChanged: () => void
}

export function HouseholdZoneSyncPanel({ onChanged }: HouseholdZoneSyncPanelProps) {
  const [status, setStatus] = useState<ZoneSyncStatus | null>(null)
  const [confirmingOff, setConfirmingOff] = useState(false)
  const [busy, setBusy] = useState(false)
  const confirmRef = useRef<HTMLDivElement | null>(null)

  // Move focus to the question so a keyboard / screen-reader user lands
  // on it (and its buttons) instead of staying on the switch.
  useEffect(() => {
    if (confirmingOff) confirmRef.current?.focus()
  }, [confirmingOff])

  useEffect(() => {
    let cancelled = false
    const load = () => {
      api.get<ZoneSyncStatus>(SYNC_PATH)
        .then((body) => { if (!cancelled) setStatus(body) })
        // Quiet: no status → no switch; the zone list carries on.
        .catch(() => {})
    }
    load()
    const timer = setInterval(load, ZONE_SYNC_STATUS_REFRESH_MS)
    // A sync that changed the zones also moved "last synced".
    const off = ws.on('household_zones_changed', load)
    return () => { cancelled = true; clearInterval(timer); off() }
  }, [])

  if (status === null || !status.available) return null

  const save = async (enabled: boolean) => {
    setBusy(true)
    try {
      const body = await api.patch<ZoneSyncStatus>(SYNC_PATH, { enabled })
      setStatus(body)
      setConfirmingOff(false)
      onChanged()
    } catch (e) {
      showToast(
        e instanceof ApiError && e.message ? e.message : t('zones.sync.failed'),
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const onToggle = () => {
    if (busy) return
    // Flipping the switch again while the question is open backs out.
    if (confirmingOff) setConfirmingOff(false)
    else if (status.enabled) setConfirmingOff(true)
    else void save(true)
  }

  const stateLine = !status.enabled
    ? t('zones.sync.off')
    : status.last_synced_at
      ? t('zones.sync.last_synced', { when: relativeDocsTime(status.last_synced_at) })
      : t('zones.sync.pending')

  return (
    <div class="sh-zone-sync">
      <label class="sh-conn-setting sh-zone-sync__row">
        <span class="sh-conn-setting__text">
          <b id="sh-zone-sync-label">{t('zones.sync.label')}</b>
          <span id="sh-zone-sync-explain">{t('zones.sync.explain')}</span>
          <span
            id="sh-zone-sync-state"
            role="status"
            title={status.enabled && status.last_synced_at
              ? new Date(status.last_synced_at).toLocaleString(formatLocale())
              : undefined}
          >
            {stateLine}
          </span>
          {status.enabled && status.last_error && (
            <span class="sh-zone-sync__error" data-testid="zone-sync-error">
              {t('zones.sync.error')}
            </span>
          )}
        </span>
        <span class="sh-switch">
          <input
            type="checkbox"
            role="switch"
            checked={status.enabled}
            disabled={busy}
            aria-labelledby="sh-zone-sync-label"
            aria-describedby="sh-zone-sync-explain sh-zone-sync-state"
            onChange={(e) => {
              // Controlled: the server's answer flips it, not the click.
              ;(e.currentTarget as HTMLInputElement).checked = status.enabled
              onToggle()
            }}
          />
          <span class="sh-switch-track" />
        </span>
      </label>
      {confirmingOff && (
        <div
          ref={confirmRef}
          class="sh-zone-sync__confirm"
          role="group"
          aria-labelledby="sh-zone-sync-confirm"
          tabIndex={-1}
        >
          <p id="sh-zone-sync-confirm">{t('zones.sync.confirm_off')}</p>
          <div class="sh-zone-sync__confirm-actions">
            <Button variant="secondary" onClick={() => setConfirmingOff(false)} disabled={busy}>
              {t('common.cancel')}
            </Button>
            <Button variant="danger" onClick={() => void save(false)} loading={busy}>
              {t('zones.sync.confirm_off_button')}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
