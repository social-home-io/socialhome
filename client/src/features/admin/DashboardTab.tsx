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
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'
import localeMeta from '@/i18n/locales/_meta.json'
import { relativeDocsTime } from '@/utils/relativeTime'
import { DashboardPasswordDialog, DashboardPinDialog } from './DashboardDialogs'
import { TabletStep } from './DashboardTabletStep'
import type { DashboardPerson, DashboardState, Patch, SaveFn, SaveOpts } from './dashboardTypes'

export type { DashboardPerson, DashboardState } from './dashboardTypes'

const ENDPOINT = '/api/admin/dashboard'
/** ``SUPPORTED_DASHBOARD_LOCALES`` on the server. */
const DASHBOARD_LOCALES = ['en', 'de', 'es', 'fr', 'nl'] as const
/** ``IDLE_RETURN_CHOICES`` on the server (0 = off). */
const IDLE_CHOICES = [0, 1, 3, 10] as const

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

/** An optimistic patch still waiting for the server. */
interface Pending { seq: number, patch: Patch }

export function DashboardTab() {
  const [state, setState] = useState<DashboardState | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  // Every request gets a monotonic id. ``confirmed`` is the newest server
  // answer (by request id, not arrival order); what the tab shows is that
  // answer with every still-pending optimistic patch replayed on top — so
  // an older save failing (or answering late) never undoes a newer one.
  const seq = useRef(0)
  const confirmed = useRef<{ seq: number, state: DashboardState } | null>(null)
  const pending = useRef<Pending[]>([])

  const render = useCallback(() => {
    const base = confirmed.current?.state
    if (base) setState(pending.current.reduce((s, p) => applyPatch(s, p.patch), base))
  }, [])

  /** Adopt a server answer to request ``id`` unless a newer one landed. */
  const adopt = useCallback((id: number, fresh: DashboardState) => {
    if (!confirmed.current || id > confirmed.current.seq) {
      confirmed.current = { seq: id, state: fresh }
    }
  }, [])

  const load = useCallback(async () => {
    const id = ++seq.current
    setLoadFailed(false)
    try {
      adopt(id, await api.get<DashboardState>(ENDPOINT))
      render()
    } catch {
      setLoadFailed(true)
    }
  }, [adopt, render])
  useEffect(() => { void load() }, [load])

  /** Save a settings patch (optimistically unless told otherwise); revert
   *  + toast on failure. Returns the error so a caller can show it inline. */
  const save: SaveFn = async (patch: Patch, opts: SaveOpts = {}) => {
    const id = ++seq.current
    if (opts.optimistic !== false) {
      pending.current = [...pending.current, { seq: id, patch }]
      render()
    }
    const done = () => { pending.current = pending.current.filter((p) => p.seq !== id) }
    try {
      const fresh = await api.put<DashboardState>(ENDPOINT, patch)
      done()
      adopt(id, fresh)
      render()
      return null
    } catch (e: unknown) {
      done()
      render()
      if (opts.toast !== false) showToast((e as Error).message, 'error')
      return e as Error
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
      <TabletStep state={state} save={save} reload={load} />
      <PinStep state={state} reload={load} />
      <DisplaySection state={state} save={save} />
      <PeopleSection state={state} save={save} />
    </div>
  )
}


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
  // The hint is a description, not part of the switch's name.
  const hintId = hint ? `${id}-hint` : undefined
  return (
    <div class={`sh-dashboard-row${disabled ? ' sh-dashboard-row--disabled' : ''}`}>
      <div class="sh-dashboard-row__label">
        <label for={id}>{label}</label>
        {hint && <span id={hintId} class="sh-form-hint">{hint}</span>}
      </div>
      <span class="sh-switch">
        <input id={id} type="checkbox" role="switch" checked={checked} disabled={disabled}
          aria-describedby={hintId} onChange={() => onChange(!checked)} />
        <span class="sh-switch-track" />
      </span>
    </div>
  )
}

function DisplaySection({ state, save }: { state: DashboardState, save: SaveFn }) {
  const [signingOut, setSigningOut] = useState(false)
  // A ref, not just the state: a second click can land before the
  // re-render that disables the button.
  const busy = useRef(false)
  const signOutAll = async () => {
    if (busy.current) return
    // Busy from the first click, so a double click can't open two confirms
    // or send the DELETE twice.
    busy.current = true
    setSigningOut(true)
    try {
      if (!await confirmDialog(t('admin.dashboard.sessions.confirm'), {
        title: t('admin.dashboard.sessions.confirm_title'),
        confirmLabel: t('admin.dashboard.sessions.sign_out'),
        destructive: true,
      })) return
      await api.delete('/api/admin/dashboard/sessions')
      showToast(t('admin.dashboard.sessions.done'), 'success')
    } catch (e: unknown) {
      showToast((e as Error).message, 'error')
    } finally {
      busy.current = false
      setSigningOut(false)
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
        <div class="sh-dashboard-row__label">
          <label for="sh-dash-idle">{t('admin.dashboard.display.idle')}</label>
          <span id="sh-dash-idle-hint" class="sh-form-hint">{t('admin.dashboard.display.idle_hint')}</span>
        </div>
        <select id="sh-dash-idle" class="sh-dashboard-row__control" value={String(state.idle_return_minutes)}
          aria-describedby="sh-dash-idle-hint"
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
      <SwitchRow id="sh-dash-messages" label={t('admin.dashboard.display.messages')}
        hint={state.household_chat_enabled
          ? t('admin.dashboard.display.messages_hint')
          : `${t('admin.dashboard.display.messages_chat_off')} ${t('admin.dashboard.display.messages_hint')}`}
        checked={state.show_household_messages} disabled={!state.household_chat_enabled}
        onChange={(v) => void save({ show_household_messages: v })} />

      <div class="sh-dashboard-sessions">
        <p class="sh-muted">
          {state.last_seen_at
            ? t('admin.dashboard.sessions.last_seen', { when: relativeDocsTime(state.last_seen_at) })
            : t('admin.dashboard.sessions.never')}
        </p>
        <Button variant="danger" disabled={!state.password_set || signingOut}
          onClick={() => void signOutAll()}>
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
        <>
          <p id="sh-dash-picture-hint" class="sh-form-hint">
            {t('admin.dashboard.people.picture_hint')}
          </p>
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
                            aria-describedby="sh-dash-picture-hint"
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
        </>
      )}
    </section>
  )
}
