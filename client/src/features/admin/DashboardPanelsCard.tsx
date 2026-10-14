/**
 * Admin › Dashboard › Wall panels — small always-on screens (an ESPHome
 * panel) that use the dashboard's JSON API with their own device token.
 *
 * A panel pairs the other way round from a tablet: it shows a 6-character
 * code, the admin types it here with a name (``POST
 * /api/admin/dashboard/displays``) and the panel then collects its token
 * by itself — the token never reaches this page. Rename / remove go
 * through ``PATCH`` / ``DELETE /api/admin/dashboard/displays/{id}``.
 * A panel privacy-locked with the household PIN shows "Locked"; Unlock
 * (``PATCH … {"locked": false}``) lifts it from here — no confirm, the
 * panel can lock itself again.
 * "Sign out all tablets" unpairs every panel too.
 *
 * On ha / the Home Assistant App (``state.ha_display_discovery``) a
 * "Found in Home Assistant" list shows the panels adopted there
 * (``GET /api/admin/dashboard/displays/ha``). *Connect* (``POST
 * …/ha/{id}/connect``) sends the panel a single-use claim through Home
 * Assistant; the panel pairs itself, so the list polls until it shows as
 * connected (or gives up after a minute and says so).
 */
import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { DashboardDisplay, DashboardState, HaPanel } from './dashboardTypes'

const ENDPOINT = '/api/admin/dashboard/displays'
const HA_ENDPOINT = '/api/admin/dashboard/displays/ha'
/** While a connected panel pairs itself: how often the list is re-read,
 *  and for how long before we say it didn't answer. */
export const HA_POLL_MS = 3000
export const HA_WAIT_MS = 60_000
/** ``PAIRING_CODE_ALPHABET`` / ``PAIRING_CODE_LENGTH`` in
 *  ``domain/household_dashboard.py`` — no look-alikes (0/O, 1/I/L, U/V). */
export const PAIRING_CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTWXYZ'
export const PAIRING_CODE_LENGTH = 6
/** ``MAX_DISPLAY_NAME_LENGTH`` there. */
const MAX_NAME = 40

/** What the admin typed, as a code: upper-case, only code characters
 *  (spaces, dashes and look-alikes dropped), at most 6 of them. */
export function formatPairingCode(raw: string): string {
  return [...raw.toUpperCase()]
    .filter((ch) => PAIRING_CODE_ALPHABET.includes(ch))
    .join('')
    .slice(0, PAIRING_CODE_LENGTH)
}

export function DashboardPanelsCard({ state, reload }: {
  state: DashboardState
  reload: () => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [renaming, setRenaming] = useState<DashboardDisplay | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const displays = state.displays

  const remove = async (d: DashboardDisplay) => {
    if (!await confirmDialog(t('admin.dashboard.panels.remove_confirm'), {
      title: t('admin.dashboard.panels.remove_title', { name: d.name }),
      confirmLabel: t('admin.dashboard.panels.remove'),
      destructive: true,
    })) return
    setBusyId(d.id)
    try {
      await api.delete(`${ENDPOINT}/${encodeURIComponent(d.id)}`)
      showToast(t('admin.dashboard.panels.removed', { name: d.name }), 'success')
    } catch (e: unknown) {
      // Already gone (another admin) is fine: the reload shows the truth.
      if (!(e instanceof ApiError && e.code === 'DASHBOARD_DISPLAY_NOT_FOUND')) {
        showToast((e as Error).message, 'error')
      }
    } finally {
      setBusyId(null)
      await reload()
    }
  }

  const unlock = async (d: DashboardDisplay) => {
    setBusyId(d.id)
    try {
      await api.patch(`${ENDPOINT}/${encodeURIComponent(d.id)}`, { locked: false })
      showToast(t('admin.dashboard.panels.unlocked', { name: d.name }), 'success')
    } catch (e: unknown) {
      // Removed meanwhile: nothing left to unlock, the reload shows it.
      if (!(e instanceof ApiError && e.code === 'DASHBOARD_DISPLAY_NOT_FOUND')) {
        showToast((e as Error).message, 'error')
      }
    } finally {
      setBusyId(null)
      await reload()
    }
  }

  return (
    <section class="sh-admin-section" aria-labelledby="sh-dash-panels">
      <h3 id="sh-dash-panels">{t('admin.dashboard.panels.heading')}</h3>
      <p class="sh-muted">{t('admin.dashboard.panels.intro')}</p>
      {displays.length === 0 ? (
        <p class="sh-muted">{t('admin.dashboard.panels.empty')}</p>
      ) : (
        <ul class="sh-dashboard-panels">
          {displays.map((d) => {
            const info = [d.model, d.firmware].filter(Boolean).join(' · ')
            return (
              <li key={d.id} class="sh-dashboard-panel">
                <div class="sh-dashboard-panel__text">
                  <span class="sh-dashboard-panel__title">
                    <span class="sh-dashboard-panel__name" title={d.name}>{d.name}</span>
                    {d.locked && <span class="sh-badge">{t('admin.dashboard.panels.locked')}</span>}
                  </span>
                  {info && <span class="sh-muted sh-dashboard-panel__info" title={info}>{info}</span>}
                  <span class="sh-form-hint">
                    {d.last_seen_at
                      ? t('admin.dashboard.panels.last_seen', { when: relativeDocsTime(d.last_seen_at) })
                      : t('admin.dashboard.panels.never_seen')}
                  </span>
                </div>
                <div class="sh-dashboard-panel__actions">
                  {d.locked && (
                    <Button variant="secondary" disabled={busyId === d.id}
                      aria-label={t('admin.dashboard.panels.unlock_aria', { name: d.name })}
                      onClick={() => void unlock(d)}>
                      {t('admin.dashboard.panels.unlock')}
                    </Button>
                  )}
                  <Button variant="secondary" disabled={busyId === d.id}
                    aria-label={t('admin.dashboard.panels.rename_aria', { name: d.name })}
                    onClick={() => setRenaming(d)}>
                    {t('admin.dashboard.panels.rename')}
                  </Button>
                  <Button variant="ghost" disabled={busyId === d.id}
                    aria-label={t('admin.dashboard.panels.remove_aria', { name: d.name })}
                    onClick={() => void remove(d)}>
                    {t('admin.dashboard.panels.remove')}
                  </Button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {!state.running && (
        <p id="sh-dash-panels-off" class="sh-dashboard-admin__note">{t('admin.dashboard.panels.off')}</p>
      )}
      <div class="sh-admin-actions">
        <Button disabled={!state.running} aria-describedby={state.running ? undefined : 'sh-dash-panels-off'}
          onClick={() => setAdding(true)}>
          {t('admin.dashboard.panels.add')}
        </Button>
      </div>
      {state.ha_display_discovery && <HaPanelsSection state={state} reload={reload} />}
      <AddPanelDialog open={adding} onClose={() => setAdding(false)}
        onPaired={(name) => {
          setAdding(false)
          showToast(t('admin.dashboard.panels.paired', { name }), 'success')
          void reload()
        }} />
      <RenamePanelDialog display={renaming} onClose={() => setRenaming(null)}
        onSaved={() => { setRenaming(null); void reload() }} />
    </section>
  )
}

function HaPanelsSection({ state, reload }: {
  state: DashboardState
  reload: () => Promise<void>
}) {
  const [panels, setPanels] = useState<HaPanel[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  /** Panels sent a connect request: id → when we stop waiting (ms). */
  const [waiting, setWaiting] = useState<Record<string, number>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const load = useCallback(async (): Promise<HaPanel[] | null> => {
    setLoading(true)
    try {
      const body = await api.get<{ panels: HaPanel[] }>(HA_ENDPOINT)
      if (!alive.current) return null
      setPanels(body.panels)
      setLoadError(null)
      return body.panels
    } catch (e: unknown) {
      if (alive.current) setLoadError((e as Error).message)
      return null
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // While a panel pairs itself, re-read the list until it shows as
  // connected (then refresh the paired list above) or the wait runs out.
  useEffect(() => {
    const waitingIds = Object.keys(waiting)
    if (waitingIds.length === 0) return
    const timer = setTimeout(async () => {
      const fresh = await load()
      if (!alive.current) return
      const now = Date.now()
      const next: Record<string, number> = {}
      let paired = false
      for (const id of waitingIds) {
        const panel = fresh?.find((p) => p.id === id)
        if (panel?.status === 'connected') {
          paired = true
          showToast(t('admin.dashboard.panels.ha_connected', {
            name: panel.display_name ?? panel.name,
          }), 'success')
        } else if (now >= waiting[id]) {
          setErrors((prev) => ({
            ...prev,
            [id]: t('admin.dashboard.panels.ha_timeout', { name: panel?.name ?? '' }),
          }))
        } else {
          next[id] = waiting[id]
        }
      }
      setWaiting(next)
      if (paired) await reload()
    }, HA_POLL_MS)
    return () => clearTimeout(timer)
  }, [waiting, load, reload])

  const connect = async (panel: HaPanel) => {
    setBusyId(panel.id)
    setErrors((prev) => {
      const rest = { ...prev }
      delete rest[panel.id]
      return rest
    })
    try {
      await api.post(`${HA_ENDPOINT}/${encodeURIComponent(panel.id)}/connect`, {})
      showToast(t('admin.dashboard.panels.ha_sent', { name: panel.name }), 'success')
      setWaiting((prev) => ({ ...prev, [panel.id]: Date.now() + HA_WAIT_MS }))
    } catch (e: unknown) {
      setErrors((prev) => ({ ...prev, [panel.id]: (e as Error).message }))
      // Gone / offline / updated meanwhile: show what Home Assistant has now.
      if (e instanceof ApiError && e.status !== 409) void load()
    } finally {
      setBusyId(null)
    }
  }

  const statusText = (p: HaPanel): string => {
    if (waiting[p.id] !== undefined) return t('admin.dashboard.panels.ha_waiting')
    switch (p.status) {
      case 'connected':
        return t('admin.dashboard.panels.ha_status_connected', { name: p.display_name ?? p.name })
      case 'offline':
        return t('admin.dashboard.panels.ha_status_offline')
      case 'needs_update':
        return t('admin.dashboard.panels.ha_status_needs_update')
      default:
        return t('admin.dashboard.panels.ha_status_ready')
    }
  }

  return (
    <div class="sh-dashboard-ha" aria-labelledby="sh-dash-ha" role="group">
      <div class="sh-dashboard-ha__head">
        <h4 id="sh-dash-ha">{t('admin.dashboard.panels.ha_heading')}</h4>
        <Button variant="ghost" disabled={loading}
          onClick={() => { setErrors({}); void load() }}>
          {t('admin.dashboard.panels.ha_refresh')}
        </Button>
      </div>
      <p class="sh-muted">{t('admin.dashboard.panels.ha_intro')}</p>
      {loadError !== null && (
        <p class="sh-form-error" role="alert">{loadError}</p>
      )}
      {panels === null && loadError === null && (
        <p class="sh-muted" aria-live="polite">{t('admin.dashboard.panels.ha_loading')}</p>
      )}
      {panels !== null && panels.length === 0 && (
        <p class="sh-muted">{t('admin.dashboard.panels.ha_empty')}</p>
      )}
      {panels !== null && panels.length > 0 && (
        <ul class="sh-dashboard-panels">
          {panels.map((p) => {
            const isWaiting = waiting[p.id] !== undefined
            const error = errors[p.id]
            const errorId = `sh-dash-ha-error-${p.id}`
            const again = p.status === 'connected'
            return (
              <li key={p.id} class="sh-dashboard-panel">
                <div class="sh-dashboard-panel__text">
                  <span class="sh-dashboard-panel__title">
                    <span class="sh-dashboard-panel__name" title={p.name}>{p.name}</span>
                    {again && <span class="sh-badge">{t('admin.dashboard.panels.ha_badge_connected')}</span>}
                  </span>
                  {p.sw_version && (
                    <span class="sh-muted sh-dashboard-panel__info" title={p.sw_version}>{p.sw_version}</span>
                  )}
                  <span class="sh-form-hint" aria-live="polite">{statusText(p)}</span>
                  {error && <span id={errorId} class="sh-form-error" role="alert">{error}</span>}
                </div>
                {p.can_connect && (
                  <div class="sh-dashboard-panel__actions">
                    <Button variant={again ? 'secondary' : 'primary'}
                      disabled={!state.running || busyId === p.id || isWaiting}
                      aria-describedby={error ? errorId : (state.running ? undefined : 'sh-dash-panels-off')}
                      aria-label={t(again
                        ? 'admin.dashboard.panels.ha_reconnect_aria'
                        : 'admin.dashboard.panels.ha_connect_aria', { name: p.name })}
                      onClick={() => void connect(p)}>
                      {isWaiting
                        ? t('admin.dashboard.panels.ha_connecting')
                        : t(again ? 'admin.dashboard.panels.ha_reconnect' : 'admin.dashboard.panels.ha_connect')}
                    </Button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function AddPanelDialog({ open, onClose, onPaired }: {
  open: boolean
  onClose: () => void
  onPaired: (name: string) => void
}) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A fresh, empty form every time the dialog opens.
  useEffect(() => {
    if (open) { setCode(''); setName(''); setError(null); setBusy(false) }
  }, [open])

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (code.length !== PAIRING_CODE_LENGTH) {
      setError(t('admin.dashboard.panels.code_invalid'))
      return
    }
    const trimmed = name.trim()
    if (!trimmed) {
      setError(t('admin.dashboard.panels.name_required'))
      return
    }
    setError(null)
    setBusy(true)
    try {
      await api.post(ENDPOINT, { code, name: trimmed })
      onPaired(trimmed)
    } catch (e: unknown) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t('admin.dashboard.panels.add_title')}>
      <form class="sh-form sh-dashboard-secret" onSubmit={(e) => void submit(e)} noValidate>
        <p class="sh-muted">{t('admin.dashboard.panels.add_intro')}</p>
        <label for="sh-dash-panel-code">{t('admin.dashboard.panels.code')}</label>
        <input
          id="sh-dash-panel-code"
          class="sh-dashboard-code-input"
          type="text"
          inputMode="text"
          autocomplete="off"
          autocapitalize="characters"
          spellcheck={false}
          maxLength={PAIRING_CODE_LENGTH + 4}
          value={code}
          disabled={busy}
          aria-describedby="sh-dash-panel-code-hint sh-dash-panel-error"
          onInput={(e) => {
            const input = e.target as HTMLInputElement
            const next = formatPairingCode(input.value)
            setCode(next)
            // Keep the field in step even when the cleaned value is unchanged.
            input.value = next
          }}
        />
        <span id="sh-dash-panel-code-hint" class="sh-form-hint">{t('admin.dashboard.panels.code_hint')}</span>
        <label for="sh-dash-panel-name">{t('admin.dashboard.panels.name')}</label>
        <input
          id="sh-dash-panel-name"
          type="text"
          autocomplete="off"
          maxLength={MAX_NAME}
          placeholder={t('admin.dashboard.panels.name_placeholder')}
          value={name}
          disabled={busy}
          aria-describedby="sh-dash-panel-error"
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
        />
        <FormError id="sh-dash-panel-error" message={error} />
        <div class="sh-modal-actions">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={busy}>{t('admin.dashboard.panels.pair')}</Button>
        </div>
      </form>
    </Modal>
  )
}

function RenamePanelDialog({ display, onClose, onSaved }: {
  display: DashboardDisplay | null
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (display) { setName(display.name); setError(null); setBusy(false) }
  }, [display])

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (!display) return
    const trimmed = name.trim()
    if (!trimmed) {
      setError(t('admin.dashboard.panels.name_required'))
      return
    }
    setBusy(true)
    try {
      await api.patch(`${ENDPOINT}/${encodeURIComponent(display.id)}`, { name: trimmed })
      onSaved()
    } catch (e: unknown) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={display !== null} onClose={onClose} title={t('admin.dashboard.panels.rename_title')}>
      <form class="sh-form sh-dashboard-secret" onSubmit={(e) => void submit(e)} noValidate>
        <label for="sh-dash-panel-rename">{t('admin.dashboard.panels.name')}</label>
        <input
          id="sh-dash-panel-rename"
          type="text"
          autocomplete="off"
          maxLength={MAX_NAME}
          value={name}
          disabled={busy}
          aria-describedby="sh-dash-panel-rename-error"
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
        />
        <FormError id="sh-dash-panel-rename-error" message={error} />
        <div class="sh-modal-actions">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={busy}>{t('admin.dashboard.panels.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}
