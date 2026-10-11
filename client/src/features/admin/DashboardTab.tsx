/**
 * Admin › Dashboard — the household wall-tablet dashboard (its own port,
 * a household-wide password, server-rendered; see
 * ``socialhome/routes/admin_dashboard.py`` and docs/api.md "HFS —
 * Household dashboard").
 *
 * First run reads top to bottom: 1 set a password (the on/off switch
 * stays greyed until then) → 2 open it on the tablet (address, a
 * one-time sign-in QR, the port) → 3 an optional privacy-lock PIN. Then
 * the display settings, "sign out all tablets" and who appears on it.
 *
 * Settings save as they change (optimistic, reverted on error). Every
 * call goes through the ``api`` client; the dashboard's own URL is a
 * different origin/port, so it is shown and linked as-is.
 */
import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { QrCodeImg } from '@/components/QrCodeImg'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'
import localeMeta from '@/i18n/locales/_meta.json'
import { relativeDocsTime } from '@/utils/relativeTime'
import { DashboardPasswordDialog, DashboardPinDialog } from './DashboardDialogs'

/** One household member as ``GET /api/admin/dashboard`` lists them. */
export interface DashboardPerson {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
  show: boolean
  /** Stored choice; ``null`` = the default (on for child-protected minors). */
  picture: boolean | null
  picture_effective: boolean
}

/** ``GET /api/admin/dashboard``. */
export interface DashboardState {
  enabled: boolean
  running: boolean
  running_port: number | null
  port: number
  port_fixed: boolean
  password_set: boolean
  password_set_at: number | null
  pin_set: boolean
  address: string | null
  url: string
  url_host_allowed: boolean
  locale: string
  idle_return_minutes: number
  night_start: string
  night_end: string
  keep_screen_on: boolean
  show_status_text: boolean
  last_seen_at: string | null
  people: DashboardPerson[]
}

interface SetupLink { url: string, expires_at: string }

type Patch = Partial<Omit<DashboardState, 'people'>> & {
  people?: Record<string, { show: boolean, picture: boolean | null }>
}

const ENDPOINT = '/api/admin/dashboard'
/** ``SUPPORTED_DASHBOARD_LOCALES`` on the server. */
const DASHBOARD_LOCALES = ['en', 'de', 'es', 'fr', 'nl'] as const
/** ``IDLE_RETURN_CHOICES`` on the server (0 = off). */
const IDLE_CHOICES = [0, 1, 3, 10] as const
const MIN_PORT = 1024
const MAX_PORT = 65535

function nativeName(code: string): string {
  const info = (localeMeta.locales as Record<string, { native_name: string }>)[code]
  return info?.native_name ?? code
}

/** Apply ``patch`` to ``state`` locally — the optimistic half of a save. */
function applyPatch(state: DashboardState, patch: Patch): DashboardState {
  const { people, ...rest } = patch
  const next: DashboardState = { ...state, ...rest }
  if (people) {
    next.people = state.people.map((p) => {
      const change = people[p.user_id]
      if (!change) return p
      // The server resolves a cleared (``null``) picture; keep showing the
      // last effective value until its answer lands.
      const effective = change.picture === null ? p.picture_effective : change.picture
      return { ...p, ...change, picture_effective: effective }
    })
  }
  return next
}

export function DashboardTab() {
  const [state, setState] = useState<DashboardState | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  // The last state the server confirmed — what an error reverts to.
  const confirmed = useRef<DashboardState | null>(null)
  const inFlight = useRef(0)

  // Only touches setters and a ref, so one instance serves every render.
  const load = useCallback(async () => {
    setLoadFailed(false)
    try {
      const fresh = await api.get<DashboardState>(ENDPOINT)
      confirmed.current = fresh
      setState(fresh)
    } catch {
      setLoadFailed(true)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  /** Save a settings patch (optimistically unless told otherwise); revert
   *  + toast on failure. Returns the error so a caller can show it inline. */
  const save = async (patch: Patch, opts: SaveOpts = {}): Promise<Error | null> => {
    if (opts.optimistic !== false) setState((s) => (s ? applyPatch(s, patch) : s))
    inFlight.current += 1
    try {
      const fresh = await api.put<DashboardState>(ENDPOINT, patch)
      confirmed.current = fresh
      // A later save is still on its way: keep its optimistic value.
      if (inFlight.current === 1) setState(fresh)
      return null
    } catch (e: unknown) {
      setState(confirmed.current)
      if (opts.toast !== false) showToast((e as Error).message, 'error')
      return e as Error
    } finally {
      inFlight.current -= 1
    }
  }

  if (!state) {
    if (loadFailed) {
      return (
        <section class="sh-admin-section">
          <h2>{t('admin.dashboard.title')}</h2>
          <p class="sh-muted">{t('admin.dashboard.load_failed')}</p>
          <Button variant="secondary" onClick={() => void load()}>{t('common.retry')}</Button>
        </section>
      )
    }
    return <Spinner />
  }

  return (
    <div class="sh-dashboard-admin">
      <HeaderSection state={state} save={save} />
      <PasswordStep state={state} reload={load} />
      <TabletStep state={state} save={save} />
      <PinStep state={state} reload={load} />
      <DisplaySection state={state} save={save} />
      <PeopleSection state={state} save={save} />
    </div>
  )
}

interface SaveOpts {
  /** ``false``: report the error to the caller (shown inline) instead. */
  toast?: boolean
  /** ``false``: wait for the server — for changes that move the listener
   *  (on/off, port) or the URL, where a guess would mislead. */
  optimistic?: boolean
}
type SaveFn = (patch: Patch, opts?: SaveOpts) => Promise<Error | null>

// ─── Header: on/off + listener state ───────────────────────────────────────

function HeaderSection({ state, save }: { state: DashboardState, save: SaveFn }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const locked = !state.password_set

  const toggle = async () => {
    setBusy(true)
    setError(null)
    // Inline, not a toast: "Port 8100 is in use" belongs next to the switch.
    const err = await save({ enabled: !state.enabled }, { toast: false, optimistic: false })
    if (err) setError(err.message)
    setBusy(false)
  }

  let status: string
  if (!state.enabled) status = t('admin.dashboard.status.off')
  else if (state.running) {
    status = t('admin.dashboard.status.running', { port: String(state.running_port ?? state.port) })
  } else status = t('admin.dashboard.status.not_running')

  return (
    <section class="sh-admin-section sh-dashboard-admin__header" aria-labelledby="sh-dash-title">
      <div class="sh-dashboard-admin__title-row">
        <h2 id="sh-dash-title">{t('admin.dashboard.title')}</h2>
        <label class={`sh-dashboard-admin__switch ${locked ? 'sh-dashboard-admin__switch--locked' : ''}`}>
          <span class="sh-switch">
            <input
              type="checkbox"
              role="switch"
              checked={state.enabled}
              disabled={locked || busy}
              aria-label={t('admin.dashboard.enable_aria')}
              aria-describedby="sh-dash-status sh-dash-enable-error"
              onChange={() => void toggle()}
            />
            <span class="sh-switch-track" />
          </span>
          <span aria-hidden="true">{state.enabled ? t('admin.dashboard.on') : t('admin.dashboard.off')}</span>
        </label>
      </div>
      <p class="sh-muted">{t('admin.dashboard.intro')}</p>
      {locked
        ? <p id="sh-dash-status" class="sh-dashboard-admin__note">{t('admin.dashboard.password_first')}</p>
        : (
          <p id="sh-dash-status" class="sh-dashboard-admin__status" role="status">
            <span class={`sh-dashboard-admin__dot ${state.enabled && state.running ? 'sh-dashboard-admin__dot--on' : ''}`} aria-hidden="true" />
            {status}
          </p>
        )}
      <FormError id="sh-dash-enable-error" message={error} />
    </section>
  )
}

// ─── Step 1: password ──────────────────────────────────────────────────────

function PasswordStep({ state, reload }: { state: DashboardState, reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  return (
    <section class="sh-admin-section sh-dashboard-step" aria-labelledby="sh-dash-step1">
      <h3 id="sh-dash-step1">
        <span class="sh-dashboard-step__num" aria-hidden="true">1</span>
        {t('admin.dashboard.password.heading')}
      </h3>
      <p class="sh-muted">
        {state.password_set ? t('admin.dashboard.password.is_set') : t('admin.dashboard.password.not_set')}
      </p>
      <div class="sh-admin-actions">
        <Button variant={state.password_set ? 'secondary' : 'primary'} onClick={() => setOpen(true)}>
          {state.password_set ? t('admin.dashboard.password.change') : t('admin.dashboard.password.set')}
        </Button>
      </div>
      <DashboardPasswordDialog
        open={open}
        replacing={state.password_set}
        onClose={() => setOpen(false)}
        onSaved={() => {
          setOpen(false)
          showToast(t('admin.dashboard.password.saved'), 'success')
          void reload()
        }}
      />
    </section>
  )
}

// ─── Step 2: open on the tablet ────────────────────────────────────────────

function TabletStep({ state, save }: { state: DashboardState, save: SaveFn }) {
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
      <SetupLinkPanel running={state.running} />
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

/** Seconds left until ``expiresAt`` (ISO), ticking once a second. */
function useSecondsLeft(expiresAt: string | null): number {
  // Read the clock while rendering (a fresh link is never shown as expired
  // for a frame); the interval only asks for the next render.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!expiresAt) return
    const id = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [expiresAt])
  if (!expiresAt) return 0
  return Math.max(0, Math.round((Date.parse(expiresAt) - Date.now()) / 1000))
}

function SetupLinkPanel({ running }: { running: boolean }) {
  const [link, setLink] = useState<SetupLink | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const secs = useSecondsLeft(link?.expires_at ?? null)
  const expired = link !== null && secs <= 0

  // A link for a listener that stopped would only fail on the tablet.
  useEffect(() => { if (!running) setLink(null) }, [running])

  const issue = async () => {
    setBusy(true)
    setError(null)
    try {
      setLink(await api.post<SetupLink>('/api/admin/dashboard/setup-link'))
    } catch (e: unknown) {
      setLink(null)
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const mmss = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`
  return (
    <div class="sh-dashboard-qr">
      {link && !expired ? (
        <>
          <QrCodeImg data={link.url} size={180} alt={t('admin.dashboard.qr.alt')} />
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

// ─── Step 3: privacy lock PIN ──────────────────────────────────────────────

function PinStep({ state, reload }: { state: DashboardState, reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const remove = async () => {
    if (!await confirmDialog(t('admin.dashboard.pin.remove_confirm'), {
      title: t('admin.dashboard.pin.remove_title'),
      confirmLabel: t('admin.dashboard.pin.remove'),
      destructive: true,
    })) return
    setBusy(true)
    try {
      await api.delete('/api/admin/dashboard/pin')
      showToast(t('admin.dashboard.pin.removed'), 'success')
      await reload()
    } catch (e: unknown) {
      showToast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section class="sh-admin-section sh-dashboard-step" aria-labelledby="sh-dash-step3">
      <h3 id="sh-dash-step3">
        <span class="sh-dashboard-step__num" aria-hidden="true">3</span>
        {t('admin.dashboard.pin.heading')}
      </h3>
      <p class="sh-muted">{t('admin.dashboard.pin.intro')}</p>
      <p>{state.pin_set ? t('admin.dashboard.pin.is_set') : t('admin.dashboard.pin.not_set')}</p>
      <div class="sh-admin-actions">
        <Button variant="secondary" disabled={busy} onClick={() => setOpen(true)}>
          {state.pin_set ? t('admin.dashboard.pin.change') : t('admin.dashboard.pin.set')}
        </Button>
        {state.pin_set && (
          <Button variant="ghost" disabled={busy} onClick={() => void remove()}>
            {t('admin.dashboard.pin.remove')}
          </Button>
        )}
      </div>
      <DashboardPinDialog
        open={open}
        replacing={state.pin_set}
        onClose={() => setOpen(false)}
        onSaved={() => {
          setOpen(false)
          showToast(t('admin.dashboard.pin.saved'), 'success')
          void reload()
        }}
      />
    </section>
  )
}

// ─── Display + sessions ────────────────────────────────────────────────────

function SwitchRow({ id, label, hint, checked, disabled, onChange }: {
  id: string, label: string, hint?: string, checked: boolean, disabled?: boolean,
  onChange: (next: boolean) => void
}) {
  return (
    <div class="sh-dashboard-row">
      <label for={id} class="sh-dashboard-row__label">
        {label}
        {hint && <span class="sh-form-hint">{hint}</span>}
      </label>
      <span class="sh-switch">
        <input id={id} type="checkbox" role="switch" checked={checked} disabled={disabled}
          onChange={() => onChange(!checked)} />
        <span class="sh-switch-track" />
      </span>
    </div>
  )
}

function DisplaySection({ state, save }: { state: DashboardState, save: SaveFn }) {
  const signOutAll = async () => {
    if (!await confirmDialog(t('admin.dashboard.sessions.confirm'), {
      title: t('admin.dashboard.sessions.confirm_title'),
      confirmLabel: t('admin.dashboard.sessions.sign_out'),
      destructive: true,
    })) return
    try {
      await api.delete('/api/admin/dashboard/sessions')
      showToast(t('admin.dashboard.sessions.done'), 'success')
    } catch (e: unknown) {
      showToast((e as Error).message, 'error')
    }
  }
  const onTime = (field: 'night_start' | 'night_end') => (e: Event) => {
    const v = (e.target as HTMLInputElement).value
    // An emptied time input is not a setting; snap back to the saved one.
    if (!v) { (e.target as HTMLInputElement).value = state[field]; return }
    if (v !== state[field]) void save({ [field]: v })
  }

  return (
    <section class="sh-admin-section" aria-labelledby="sh-dash-display">
      <h3 id="sh-dash-display">{t('admin.dashboard.display.heading')}</h3>
      <div class="sh-dashboard-row sh-dashboard-row--select">
        <label for="sh-dash-locale" class="sh-dashboard-row__label">{t('admin.dashboard.display.language')}</label>
        <select id="sh-dash-locale" class="sh-dashboard-row__control" value={state.locale}
          onChange={(e) => void save({ locale: (e.target as HTMLSelectElement).value })}>
          {DASHBOARD_LOCALES.map((code) => (
            <option key={code} value={code} lang={code}>{nativeName(code)}</option>
          ))}
        </select>
      </div>
      <div class="sh-dashboard-row sh-dashboard-row--select">
        <label for="sh-dash-idle" class="sh-dashboard-row__label">
          {t('admin.dashboard.display.idle')}
          <span class="sh-form-hint">{t('admin.dashboard.display.idle_hint')}</span>
        </label>
        <select id="sh-dash-idle" class="sh-dashboard-row__control" value={String(state.idle_return_minutes)}
          onChange={(e) => void save({ idle_return_minutes: Number((e.target as HTMLSelectElement).value) })}>
          {IDLE_CHOICES.map((n) => (
            <option key={n} value={String(n)}>
              {n === 0 ? t('admin.dashboard.display.idle_off') : t('admin.dashboard.display.idle_minutes', { n: String(n) })}
            </option>
          ))}
        </select>
      </div>
      <fieldset class="sh-dashboard-row sh-dashboard-night">
        <legend class="sh-dashboard-row__label">
          {t('admin.dashboard.display.night')}
          <span class="sh-form-hint">{t('admin.dashboard.display.night_hint')}</span>
        </legend>
        <div class="sh-dashboard-night__times">
          <label class="sr-only" for="sh-dash-night-start">{t('admin.dashboard.display.night_start')}</label>
          <input id="sh-dash-night-start" type="time" value={state.night_start} onChange={onTime('night_start')} />
          <span aria-hidden="true">–</span>
          <label class="sr-only" for="sh-dash-night-end">{t('admin.dashboard.display.night_end')}</label>
          <input id="sh-dash-night-end" type="time" value={state.night_end} onChange={onTime('night_end')} />
        </div>
      </fieldset>
      <SwitchRow id="sh-dash-keep-on" label={t('admin.dashboard.display.keep_on')}
        hint={t('admin.dashboard.display.keep_on_hint')} checked={state.keep_screen_on}
        onChange={(v) => void save({ keep_screen_on: v })} />
      <SwitchRow id="sh-dash-status-text" label={t('admin.dashboard.display.status_text')}
        hint={t('admin.dashboard.display.status_text_hint')} checked={state.show_status_text}
        onChange={(v) => void save({ show_status_text: v })} />

      <div class="sh-dashboard-sessions">
        <p class="sh-muted">
          {state.last_seen_at
            ? t('admin.dashboard.sessions.last_seen', { when: relativeDocsTime(state.last_seen_at) })
            : t('admin.dashboard.sessions.never')}
        </p>
        <Button variant="danger" disabled={!state.password_set} onClick={() => void signOutAll()}>
          {t('admin.dashboard.sessions.sign_out')}
        </Button>
      </div>
    </section>
  )
}

// ─── People ────────────────────────────────────────────────────────────────

function PeopleSection({ state, save }: { state: DashboardState, save: SaveFn }) {
  const set = (p: DashboardPerson, change: { show?: boolean, picture?: boolean | null }) => {
    void save({ people: { [p.user_id]: { show: p.show, picture: p.picture, ...change } } })
  }
  return (
    <section class="sh-admin-section" aria-labelledby="sh-dash-people">
      <h3 id="sh-dash-people">{t('admin.dashboard.people.heading')}</h3>
      <p class="sh-muted">{t('admin.dashboard.people.intro')}</p>
      {state.people.length === 0 ? (
        <p class="sh-muted">{t('admin.dashboard.people.empty')}</p>
      ) : (
        <ul class="sh-dashboard-people">
          {state.people.map((p) => {
            const name = p.display_name || p.username
            return (
              <li key={p.user_id} class="sh-dashboard-person">
                <Avatar name={name} src={p.picture_url} size={36} />
                <div class="sh-dashboard-person__name">
                  <span class="sh-dashboard-person__display" title={name}>{name}</span>
                  <span class="sh-muted">@{p.username}</span>
                </div>
                <div class="sh-dashboard-person__toggles">
                  <label class="sh-dashboard-person__toggle">
                    <span class="sh-switch">
                      <input type="checkbox" role="switch" checked={p.show}
                        aria-label={t('admin.dashboard.people.show_aria', { name })}
                        onChange={() => set(p, { show: !p.show })} />
                      <span class="sh-switch-track" />
                    </span>
                    <span aria-hidden="true">{t('admin.dashboard.people.show')}</span>
                  </label>
                  <div class="sh-dashboard-person__picture">
                    <label class="sh-dashboard-person__toggle">
                      <span class="sh-switch">
                        <input type="checkbox" role="switch" checked={p.picture_effective}
                          disabled={!p.show}
                          aria-label={t('admin.dashboard.people.picture_aria', { name })}
                          onChange={() => set(p, { picture: !p.picture_effective })} />
                        <span class="sh-switch-track" />
                      </span>
                      <span aria-hidden="true">{t('admin.dashboard.people.picture')}</span>
                    </label>
                    {p.picture === null ? (
                      <span class="sh-form-hint">
                        {p.picture_effective
                          ? t('admin.dashboard.people.default_child')
                          : t('admin.dashboard.people.default')}
                      </span>
                    ) : (
                      <button type="button" class="sh-link-button sh-form-hint"
                        disabled={!p.show}
                        aria-label={t('admin.dashboard.people.reset_aria', { name })}
                        onClick={() => set(p, { picture: null })}>
                        {t('admin.dashboard.people.reset')}
                      </button>
                    )}
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
