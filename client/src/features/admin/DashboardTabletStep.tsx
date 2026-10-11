/**
 * Step 2 of Admin › Dashboard — "Open on the tablet": the address tablets
 * use (copy / open / edit), a one-time sign-in QR code and the port.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { QrCodeImg } from '@/components/QrCodeImg'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import type { DashboardState, SaveFn } from './dashboardTypes'

/** ``POST /api/admin/dashboard/setup-link``. */
interface SetupLink { url: string, expires_at: string }

/** ``SETUP_TOKEN_TTL_SECONDS`` in ``domain/household_dashboard.py``. */
export const SETUP_LINK_TTL_SECONDS = 600
const MIN_PORT = 1024
const MAX_PORT = 65535

export function TabletStep({ state, save, reload }: {
  state: DashboardState
  save: SaveFn
  /** Re-read the tab state (a sign-in link found the dashboard not running). */
  reload: () => Promise<void>
}) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(state.url)
      showToast(t('admin.dashboard.tablet.copied'), 'success')
    } catch {
      showToast(state.url, 'info')
    }
  }
  return (
    <section class="sh-admin-section sh-dashboard-step" aria-labelledby="sh-dash-step2">
      <h3 id="sh-dash-step2">
        <span class="sh-dashboard-step__num" aria-hidden="true">2</span>
        {t('admin.dashboard.tablet.heading')}
      </h3>
      <div class="sh-dashboard-url">
        <code class="sh-dashboard-url__text">{state.url}</code>
        <div class="sh-dashboard-url__actions">
          <Button variant="secondary" onClick={() => void copy()}>{t('admin.dashboard.tablet.copy')}</Button>
          <a class="sh-btn sh-btn--secondary" href={state.url} target="_blank" rel="noopener noreferrer">
            {t('admin.dashboard.tablet.open')}
          </a>
        </div>
      </div>
      <AddressEditor state={state} save={save} />
      {/* Keyed on where tablets reach it: a new address or port drops a
       *  QR code that points at the old one. */}
      <SetupLinkPanel key={`${state.url}|${state.port}`}
        running={state.running} reload={reload} />
      {/* Keyed on the saved port: a new port starts a fresh, clean field. */}
      <PortField key={state.port} state={state} save={save} />
    </section>
  )
}

function AddressEditor({ state, save }: { state: DashboardState, save: SaveFn }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(state.address ?? '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A host the dashboard would refuse keeps the editor open until fixed.
  const open = editing || !state.url_host_allowed

  const submit = async (address: string | null) => {
    setBusy(true)
    setError(null)
    const err = await save({ address }, { toast: false, optimistic: false })
    setBusy(false)
    if (err) {
      setError(err instanceof ApiError && err.code === 'DASHBOARD_SETTINGS_INVALID'
        ? t('admin.dashboard.tablet.address_invalid')
        : err.message)
      return
    }
    setValue(address ?? '')
    setEditing(false)
  }

  return (
    <div class="sh-dashboard-address">
      {state.host_port_mapped === false && (
        <p class="sh-dashboard-admin__warn" role="note">⚠ {t('admin.dashboard.tablet.port_not_mapped')}</p>
      )}
      {!state.url_host_allowed && (
        <p class="sh-dashboard-admin__warn" role="note">⚠ {t('admin.dashboard.tablet.host_refused')}</p>
      )}
      {state.url_host_allowed && state.address === null && (
        <p class="sh-muted">
          {state.port_fixed ? t('admin.dashboard.tablet.guess_app') : t('admin.dashboard.tablet.guess')}
        </p>
      )}
      {open ? (
        <form class="sh-dashboard-address__form" onSubmit={(e) => { e.preventDefault(); void submit(value.trim() || null) }}>
          <label for="sh-dash-address">{t('admin.dashboard.tablet.address_label')}</label>
          <div class="sh-dashboard-inline">
            <input
              id="sh-dash-address"
              type="url"
              inputMode="url"
              placeholder="http://192.168.1.20:8100"
              value={value}
              disabled={busy}
              aria-describedby="sh-dash-address-hint sh-dash-address-error"
              onInput={(e) => setValue((e.target as HTMLInputElement).value)}
            />
            <Button type="submit" disabled={busy}>{t('common.save')}</Button>
          </div>
          <span id="sh-dash-address-hint" class="sh-form-hint">{t('admin.dashboard.tablet.address_hint')}</span>
          <FormError id="sh-dash-address-error" message={error} />
          <div class="sh-admin-actions">
            {state.address !== null && (
              <Button type="button" variant="ghost" disabled={busy} onClick={() => void submit(null)}>
                {t('admin.dashboard.tablet.address_reset')}
              </Button>
            )}
            {state.url_host_allowed && (
              <Button type="button" variant="ghost" disabled={busy}
                onClick={() => { setValue(state.address ?? ''); setError(null); setEditing(false) }}>
                {t('common.cancel')}
              </Button>
            )}
          </div>
        </form>
      ) : (
        <div class="sh-admin-actions">
          <Button variant="ghost" onClick={() => { setValue(state.address ?? ''); setEditing(true) }}>
            {t('admin.dashboard.tablet.edit_address')}
          </Button>
        </div>
      )}
    </div>
  )
}

/** Seconds left until ``deadline`` (client clock, ms). Ticks once a
 *  second and stops at 0, calling ``onExpire`` once. */
function useSecondsLeft(deadline: number | null, onExpire: () => void): number {
  const [, setTick] = useState(0)
  const onExpireRef = useRef(onExpire)
  onExpireRef.current = onExpire
  useEffect(() => {
    if (deadline === null) return
    const id = setInterval(() => {
      if (Date.now() >= deadline) {
        clearInterval(id)
        onExpireRef.current()
      } else {
        setTick((n) => n + 1)
      }
    }, 1000)
    return () => clearInterval(id)
  }, [deadline])
  // Read the clock while rendering, so a fresh link never shows a stale value.
  if (deadline === null) return 0
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
}

function SetupLinkPanel({ running, reload }: { running: boolean, reload: () => Promise<void> }) {
  const [link, setLink] = useState<string | null>(null)
  // Anchored on when the link arrived (+ the server's fixed lifetime), not
  // on the server's ``expires_at`` — a tablet-admin clock skew would
  // otherwise show a fresh link as expired, or an expired one as valid.
  const [deadline, setDeadline] = useState<number | null>(null)
  const [expired, setExpired] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const secs = useSecondsLeft(deadline, () => {
    setLink(null)
    setDeadline(null)
    setExpired(true)
  })

  // A link for a listener that stopped would only fail on the tablet.
  useEffect(() => {
    if (!running) { setLink(null); setDeadline(null) }
  }, [running])

  const issue = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.post<SetupLink>('/api/admin/dashboard/setup-link')
      setLink(res.url)
      setDeadline(Date.now() + SETUP_LINK_TTL_SECONDS * 1000)
      setExpired(false)
    } catch (e: unknown) {
      setLink(null)
      setDeadline(null)
      setError((e as Error).message)
      // The listener stopped behind our back: refresh the switch + status.
      if (e instanceof ApiError && e.code === 'DASHBOARD_NOT_RUNNING') void reload()
    } finally {
      setBusy(false)
    }
  }

  const mmss = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`
  return (
    <div class="sh-dashboard-qr">
      {link ? (
        <>
          <QrCodeImg data={link} size={180} alt={t('admin.dashboard.qr.alt')} />
          <div class="sh-dashboard-qr__text">
            <p>{t('admin.dashboard.qr.scan')}</p>
            <p class="sh-muted" aria-live="off">{t('admin.dashboard.qr.valid_for', { time: mmss })}</p>
            <Button variant="secondary" disabled={busy || !running} onClick={() => void issue()}>
              {t('admin.dashboard.qr.new')}
            </Button>
          </div>
        </>
      ) : (
        <div class="sh-dashboard-qr__text">
          <p class="sh-muted">
            {expired ? t('admin.dashboard.qr.expired') : t('admin.dashboard.qr.intro')}
          </p>
          {!running && <p class="sh-muted">{t('admin.dashboard.qr.needs_running')}</p>}
          <Button variant={expired ? 'secondary' : 'primary'} disabled={busy || !running}
            aria-describedby="sh-dash-qr-error" onClick={() => void issue()}>
            {expired ? t('admin.dashboard.qr.new') : t('admin.dashboard.qr.show')}
          </Button>
        </div>
      )}
      <FormError id="sh-dash-qr-error" message={error} />
    </div>
  )
}

function PortField({ state, save }: { state: DashboardState, save: SaveFn }) {
  const [value, setValue] = useState(String(state.port))
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (state.port_fixed) {
    return (
      <div class="sh-dashboard-port">
        <label for="sh-dash-port">{t('admin.dashboard.port.label')}</label>
        <input id="sh-dash-port" type="number" value={state.port} readOnly
          aria-describedby="sh-dash-port-hint" class="sh-dashboard-port__input" />
        <span id="sh-dash-port-hint" class="sh-form-hint">{t('admin.dashboard.port.fixed')}</span>
      </div>
    )
  }

  const dirty = value.trim() !== String(state.port)
  const submit = async (ev: Event) => {
    ev.preventDefault()
    const n = Number(value)
    if (!Number.isInteger(n) || n < MIN_PORT || n > MAX_PORT) {
      setError(t('admin.dashboard.port.range', { min: String(MIN_PORT), max: String(MAX_PORT) }))
      return
    }
    setBusy(true)
    setError(null)
    const err = await save({ port: n }, { toast: false, optimistic: false })
    setBusy(false)
    if (err) {
      setError(err instanceof ApiError && err.code === 'DASHBOARD_SETTINGS_INVALID'
        ? t('admin.dashboard.port.invalid')
        : err.message)
    }
  }
  return (
    <form class="sh-dashboard-port" onSubmit={(e) => void submit(e)} noValidate>
      <label for="sh-dash-port">{t('admin.dashboard.port.label')}</label>
      <div class="sh-dashboard-inline">
        <input
          id="sh-dash-port"
          class="sh-dashboard-port__input"
          type="number"
          inputMode="numeric"
          min={MIN_PORT}
          max={MAX_PORT}
          value={value}
          disabled={busy}
          aria-describedby="sh-dash-port-hint sh-dash-port-error"
          onInput={(e) => setValue((e.target as HTMLInputElement).value)}
        />
        {dirty && <Button type="submit" disabled={busy}>{t('admin.dashboard.port.save')}</Button>}
      </div>
      <span id="sh-dash-port-hint" class="sh-form-hint">
        {t('admin.dashboard.port.hint', { min: String(MIN_PORT), max: String(MAX_PORT) })}
      </span>
      <FormError id="sh-dash-port-error" message={error} />
    </form>
  )
}
