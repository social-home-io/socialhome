import { type ComponentChildren } from 'preact'
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { basePath, addBase } from '@/baseUrl'
import { loadCurrentUser, setToken } from '@/store/auth'
import { instanceConfig, loadInstanceConfig } from '@/store/instance'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import { Wordmark } from '@/components/Wordmark'
import { t } from '@/i18n/i18n'

interface HaPerson {
  username: string
  /** Public ``@``-handle; falls back to ``username`` when unset. */
  handle?: string | null
  display_name: string
  picture_url: string | null
}

const haPersons = signal<HaPerson[] | null>(null)
const haPersonsError = signal<string | null>(null)

async function fetchHaPersons(): Promise<HaPerson[]> {
  if (haPersons.value) return haPersons.value
  try {
    const resp = await api.get('/api/setup/ha/persons') as { persons: HaPerson[] }
    haPersons.value = resp.persons
    return resp.persons
  } catch (err: any) {
    haPersonsError.value = err?.message || t('setup.ha.persons_failed')
    throw err
  }
}

/**
 * SetupPage — first-boot wizard.
 *
 * Every mode starts with the same warm welcome card (👋 + "Welcome to
 * your home" + a 3-bullet "what's about to happen") so the operator
 * has a clear "this is the first-boot screen" moment regardless of
 * deployment shape. Continue advances:
 *
 * * standalone → username + password form
 * * ha → pick HA person + password form
 * * haos → silent supervisor handshake + redirect (ingress signs in)
 *
 * The caller (App.tsx) only renders this when
 * `instanceConfig.value?.setup_required === true`.
 */
export function SetupPage() {
  const cfg = instanceConfig.value
  if (!cfg) {
    // App.tsx fetches before rendering, so this is defensive only.
    return (
      <SetupShell>
        <SetupSpinner label={t('setup.loading')} />
      </SetupShell>
    )
  }
  if (cfg.mode === 'haos') return <HaosFlow />
  if (cfg.mode === 'ha') return <HaFlow />
  return <StandaloneFlow />
}

// ── Shared shell + bits ──────────────────────────────────────────────────────

interface SetupShellProps {
  step?: { current: number; total: number }
  children: any
}

function SetupShell({ step, children }: SetupShellProps) {
  return (
    <div class="sh-setup" role="main">
      <div class="sh-setup-card">
        <Wordmark size={32} className="sh-setup-brand" />
        {step && (
          <ol class="sh-setup-steps" aria-label={t('setup.step_aria', { current: String(step.current), total: String(step.total) })}>
            {Array.from({ length: step.total }, (_, i) => (
              <li
                key={i}
                aria-current={i + 1 === step.current ? 'step' : undefined}
                class={
                  i + 1 < step.current
                    ? 'sh-setup-step sh-setup-step--done'
                    : i + 1 === step.current
                    ? 'sh-setup-step sh-setup-step--active'
                    : 'sh-setup-step'
                }
              />
            ))}
          </ol>
        )}
        {children}
      </div>
    </div>
  )
}

function SetupSpinner({ label }: { label: string }) {
  return (
    <div class="sh-setup-spinner-wrap">
      <div class="sh-setup-spinner" aria-hidden="true" />
      <p class="sh-muted">{label}</p>
    </div>
  )
}

const STRENGTH_LABEL_KEYS = [
  '', 'setup.strength.too_short', 'setup.strength.fair', 'setup.strength.good', 'setup.strength.strong',
]

function PasswordStrength({ value }: { value: string }) {
  const score = useMemo(() => {
    let s = 0
    if (value.length >= 8) s += 1
    if (value.length >= 12) s += 1
    if (/[A-Z]/.test(value) && /[a-z]/.test(value)) s += 1
    if (/[0-9]/.test(value) && /[^A-Za-z0-9]/.test(value)) s += 1
    return s
  }, [value])
  const filled = value.length === 0 ? 0 : Math.max(score, 1)
  const labelKey = STRENGTH_LABEL_KEYS[filled] ?? ''
  const label = labelKey ? t(labelKey) : ''
  return (
    <div
      class="sh-setup-strength-wrap"
      aria-label={t('setup.strength.aria', { label: label || t('setup.strength.empty') })}
      role="status"
      aria-live="polite"
    >
      <div class="sh-setup-strength" data-score={filled}>
        <span /><span /><span /><span />
      </div>
      {label && (
        <span
          class={`sh-setup-strength-label sh-setup-strength-label--${filled}`}
        >
          {label}
        </span>
      )}
    </div>
  )
}

/**
 * PasswordField — password input with a 👁 show/hide toggle.
 *
 * Keeps Setup's three password fields (standalone password, ha
 * password, the matching ``password_confirm`` for both) consistent
 * and lets the operator verify what they typed without the typical
 * "type twice into a hidden box" dance.  ``autoComplete`` stays
 * ``new-password`` so password managers offer to save / fill.
 */
function PasswordField({
  label, value, onInput, autoComplete = 'new-password', children,
}: {
  label: string
  value: string
  onInput: (v: string) => void
  autoComplete?: string
  children?: ComponentChildren
}) {
  const [shown, setShown] = useState(false)
  return (
    <label class="sh-setup-field">
      <span class="sh-setup-label">{label}</span>
      <span class="sh-setup-pw">
        <input
          type={shown ? 'text' : 'password'}
          autoComplete={autoComplete}
          required
          value={value}
          onInput={(e) => onInput((e.target as HTMLInputElement).value)}
        />
        <button
          type="button"
          class="sh-setup-pw-toggle"
          aria-pressed={shown}
          aria-label={shown ? t('setup.password.hide') : t('setup.password.show')}
          title={shown ? t('setup.password.hide') : t('setup.password.show')}
          onClick={() => setShown(s => !s)}
        >
          {shown ? '🙈' : '👁'}
        </button>
      </span>
      {children}
    </label>
  )
}

interface WelcomeCardProps {
  /** Mode picks the i18n bullets / intro variant. */
  mode: 'standalone' | 'ha' | 'haos'
  /** Optional step indicator — standalone/ha use 1 of 2; haos has none. */
  step?: { current: number; total: number }
  /** "Continue" or "Let's go" — defaults to setup.welcome.continue. */
  ctaLabelKey?: string
  busy?: boolean
  error?: string | null
  onContinue: () => void
  /** Reveal the mode-agnostic "Restore from a Recovery Kit" flow.
   *  Rendered as a calm secondary link below the primary CTA so it's
   *  discoverable without competing with normal first-boot setup. */
  onRestore?: () => void
}

function WelcomeCard({
  mode, step, ctaLabelKey = 'setup.welcome.continue',
  busy = false, error = null, onContinue, onRestore,
}: WelcomeCardProps) {
  return (
    <SetupShell step={step}>
      <div class="sh-setup-welcome" aria-hidden="true">
        <span class="sh-setup-welcome-icon">👋</span>
      </div>
      <h1 class="sh-setup-title">{t('setup.welcome.title')}</h1>
      <p class="sh-setup-intro">{t(`setup.${mode}.welcome_intro`)}</p>
      <ul class="sh-setup-checklist">
        <li>{t(`setup.${mode}.bullet_1`)}</li>
        <li>{t(`setup.${mode}.bullet_2`)}</li>
        <li>{t(`setup.${mode}.bullet_3`)}</li>
      </ul>
      <FormError id="setup-error" message={error} />
      <Button onClick={onContinue} disabled={busy}>
        {busy ? t('setup.submitting') : t(ctaLabelKey)}
      </Button>
      {onRestore && (
        <button
          type="button"
          class="sh-link sh-setup-recovery-cta"
          onClick={onRestore}
          disabled={busy}
        >
          {t('setup.recovery.cta')}
        </button>
      )}
    </SetupShell>
  )
}

/**
 * RecoveryRestoreFlow — mode-agnostic "Restore from a Recovery Kit"
 * path off the welcome step.
 *
 * Restore reconstitutes the whole household identity from a sealed
 * ``.shrk`` Kit, so it's the same regardless of deployment mode. The
 * backend endpoint (``POST /api/setup/recovery/restore``) is
 * setup-gated and takes no bearer. On success it RESTARTS and returns
 * no token — so we land on a terminal "restored, restarting; wait and
 * reload" state instead of redirecting (an immediate redirect would
 * just hit the restarting backend, and the identity has changed).
 */
function RecoveryRestoreFlow({ onBack }: { onBack: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  async function submit(e: Event) {
    e.preventDefault()
    if (!file) {
      setError(t('setup.recovery.err_no_file'))
      return
    }
    if (!passphrase) {
      setError(t('setup.recovery.err_no_passphrase'))
      return
    }
    setBusy(true)
    setError(null)
    let kitB64: string
    try {
      // ``readAsDataURL`` handles any size; strip the
      // ``data:...;base64,`` prefix to get the bare base64 payload.
      // ``btoa(String.fromCharCode(...))`` overflows the call stack
      // on a large Kit, so we never go that route.
      kitB64 = await readKitBase64(file)
    } catch {
      setError(t('setup.recovery.err_read'))
      setBusy(false)
      return
    }
    try {
      const resp = await api.post('/api/setup/recovery/restore', {
        kit_b64: kitB64,
        passphrase,
      }) as { instance_id: string; restart_required: boolean }
      if (resp?.restart_required) {
        // Backend is restarting + identity changed — do NOT redirect to
        // ``basePath`` (it would just error). Show the terminal state.
        setDone(true)
        return
      }
      // Defensive: a non-restart success isn't expected, but reload
      // so the SPA re-reads the fresh instance config.
      window.location.reload()
    } catch (err: any) {
      if (err?.code === 'BAD_KIT') {
        setError(t('setup.recovery.err_bad_kit'))
      } else if (err?.code === 'RESTORE_FAILED') {
        setError(err?.message || t('setup.recovery.err_generic'))
      } else {
        setError(err?.message || t('setup.recovery.err_generic'))
      }
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <SetupShell>
        <div class="sh-setup-welcome" aria-hidden="true">
          <span class="sh-setup-welcome-icon">🛟</span>
        </div>
        <h1 class="sh-setup-title">{t('setup.recovery.done_title')}</h1>
        <p class="sh-setup-intro">{t('setup.recovery.done_intro')}</p>
        <p class="sh-setup-hint">{t('setup.recovery.done_hint')}</p>
        <Button onClick={() => { window.location.href = basePath }}>
          {t('setup.recovery.reload')}
        </Button>
      </SetupShell>
    )
  }

  return (
    <SetupShell>
      <h1 class="sh-setup-title">{t('setup.recovery.title')}</h1>
      <p class="sh-setup-intro">{t('setup.recovery.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.recovery.file_label')}</span>
          <input
            type="file"
            accept=".shrk"
            onChange={(e) => setFile((e.target as HTMLInputElement).files?.[0] ?? null)}
          />
          <span class="sh-setup-hint">{t('setup.recovery.file_hint')}</span>
        </label>
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.recovery.passphrase_label')}</span>
          <input
            type="password"
            autoComplete="off"
            placeholder={t('setup.recovery.passphrase_placeholder')}
            value={passphrase}
            onInput={(e) => setPassphrase((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.recovery.restoring') : t('setup.recovery.restore')}
        </Button>
        <button
          type="button"
          class="sh-link sh-setup-recovery-cta"
          onClick={onBack}
          disabled={busy}
        >
          {t('setup.recovery.back')}
        </button>
      </form>
    </SetupShell>
  )
}

/** Read a File as bare base64 via ``readAsDataURL`` (no stack
 *  overflow on large Kits), stripping the ``data:...;base64,``
 *  prefix the browser prepends. */
function readKitBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => {
      const result = String(r.result)
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    r.onerror = () => reject(r.error)
    r.readAsDataURL(file)
  })
}

// ── Standalone: welcome → form ─────────────────────────────────────────────

function StandaloneFlow() {
  const [started, setStarted] = useState(false)
  const [restoring, setRestoring] = useState(false)
  if (restoring) return <RecoveryRestoreFlow onBack={() => setRestoring(false)} />
  if (!started) {
    return (
      <WelcomeCard
        mode="standalone"
        step={{ current: 1, total: 2 }}
        onContinue={() => setStarted(true)}
        onRestore={() => setRestoring(true)}
      />
    )
  }
  return <StandaloneSetupForm />
}

function StandaloneSetupForm() {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [householdName, setHouseholdName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: Event) {
    e.preventDefault()
    if (password !== confirm) {
      setError(t('setup.error.password_mismatch'))
      return
    }
    if (password.length < 8) {
      setError(t('setup.error.password_too_short'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/setup/standalone', {
        username, password, household_name: householdName.trim() || undefined,
      }) as { token: string }
      setToken(resp.token)
      // Populate currentUser so isAuthed flips true on the next render
      // (otherwise the SPA bounces straight to the login screen).
      await loadCurrentUser()
      // Refresh the instance config so setup_required flips to false
      // and the SPA stops redirecting here.
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      // ``basePath`` is the document base (``/`` standalone, or the
      // HA Supervisor ingress prefix for ha/haos). Hard-coding ``/``
      // here bounced the iframe to HA Core's frontend root.
      window.location.href = basePath
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SetupShell step={{ current: 2, total: 2 }}>
      <h1 class="sh-setup-title">{t('setup.standalone.title')}</h1>
      <p class="sh-setup-intro">{t('setup.standalone.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.household_name')}</span>
          <input
            name="household_name"
            type="text"
            maxLength={80}
            placeholder={t('setup.household_name_placeholder')}
            value={householdName}
            onInput={(e) => setHouseholdName((e.target as HTMLInputElement).value)}
          />
          <span class="sh-setup-hint">{t('setup.household_name_hint')}</span>
        </label>
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.username')}</span>
          <input
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onInput={(e) => setUsername((e.target as HTMLInputElement).value)}
          />
        </label>
        <PasswordField
          label={t('setup.password')}
          value={password}
          onInput={setPassword}
        >
          <PasswordStrength value={password} />
        </PasswordField>
        <PasswordField
          label={t('setup.password_confirm')}
          value={confirm}
          onInput={setConfirm}
        />
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </SetupShell>
  )
}

// ── ha: welcome → pick HA person + password ────────────────────────────────

function HaFlow() {
  const [started, setStarted] = useState(false)
  const [restoring, setRestoring] = useState(false)
  if (restoring) return <RecoveryRestoreFlow onBack={() => setRestoring(false)} />
  if (!started) {
    return (
      <WelcomeCard
        mode="ha"
        step={{ current: 1, total: 2 }}
        onContinue={() => setStarted(true)}
        onRestore={() => setRestoring(true)}
      />
    )
  }
  return <HaOwnerForm />
}

function HaOwnerForm() {
  const [persons, setPersons] = useState<HaPerson[] | null>(haPersons.value)
  const [loading, setLoading] = useState(persons === null)
  const [picked, setPicked] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [householdName, setHouseholdName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = () => {
    haPersonsError.value = null
    setLoading(true)
    fetchHaPersons().then(
      (p) => { setPersons(p); setLoading(false) },
      () => { setLoading(false) },
    )
  }

  useEffect(() => {
    if (persons !== null) return
    reload()
  }, [])

  async function submit(e: Event) {
    e.preventDefault()
    if (!picked) {
      setError(t('setup.error.no_person'))
      return
    }
    if (password !== confirm) {
      setError(t('setup.error.password_mismatch'))
      return
    }
    if (password.length < 8) {
      setError(t('setup.error.password_too_short'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/setup/ha/owner', {
        username: picked, password,
        household_name: householdName.trim() || undefined,
      }) as { token: string }
      setToken(resp.token)
      await loadCurrentUser()
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      // ``basePath`` is the document base (``/`` standalone, or the
      // HA Supervisor ingress prefix for ha/haos). Hard-coding ``/``
      // here bounced the iframe to HA Core's frontend root.
      window.location.href = basePath
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <SetupShell>
        <SetupSpinner label={t('setup.loading')} />
      </SetupShell>
    )
  }
  if (haPersonsError.value) {
    return (
      <SetupShell>
        <h1 class="sh-setup-title">{t('setup.ha.title')}</h1>
        <FormError id="setup-error" message={haPersonsError.value} />
        <Button onClick={reload}>{t('common.try_again')}</Button>
      </SetupShell>
    )
  }
  if (!persons || persons.length === 0) {
    return (
      <SetupShell>
        <div class="sh-setup-welcome" aria-hidden="true">
          <span class="sh-setup-welcome-icon">👥</span>
        </div>
        <h1 class="sh-setup-title">{t('setup.ha.no_people_title')}</h1>
        <p class="sh-setup-intro">
          {t('setup.ha.no_people_body')}
        </p>
        <a
          class="sh-link"
          href={addBase('/profile/person')}
          target="_blank"
          rel="noopener"
        >{t('setup.ha.open_person_settings')}</a>
        <div style={{ marginTop: 'var(--sh-space-md)' }}>
          <Button onClick={reload}>{t('common.try_again')}</Button>
        </div>
      </SetupShell>
    )
  }

  return (
    <SetupShell step={{ current: 2, total: 2 }}>
      <h1 class="sh-setup-title">{t('setup.ha.title')}</h1>
      <p class="sh-setup-intro">{t('setup.ha.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.household_name')}</span>
          <input
            name="household_name"
            type="text"
            maxLength={80}
            placeholder={t('setup.household_name_placeholder')}
            value={householdName}
            onInput={(e) => setHouseholdName((e.target as HTMLInputElement).value)}
          />
          <span class="sh-setup-hint">{t('setup.household_name_hint')}</span>
        </label>
        <fieldset class="sh-setup-persons">
          <legend class="sh-setup-label">{t('setup.ha.pick_owner')}</legend>
          <div class="sh-setup-persons-grid">
            {persons.map((p) => (
              <label
                key={p.username}
                class={
                  picked === p.username
                    ? 'sh-setup-person sh-setup-person--picked'
                    : 'sh-setup-person'
                }
              >
                <input
                  type="radio"
                  name="picked"
                  value={p.username}
                  checked={picked === p.username}
                  onChange={() => setPicked(p.username)}
                />
                <span class="sh-setup-person-avatar">
                  {p.picture_url
                    ? <img src={p.picture_url} alt="" loading="lazy" />
                    : <span aria-hidden="true">{initials(p.display_name)}</span>}
                </span>
                <span class="sh-setup-person-name">{p.display_name}</span>
                <span class="sh-setup-person-username">@{p.handle ?? p.username}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <PasswordField
          label={t('setup.password')}
          value={password}
          onInput={setPassword}
        >
          <PasswordStrength value={password} />
        </PasswordField>
        <PasswordField
          label={t('setup.password_confirm')}
          value={confirm}
          onInput={setConfirm}
        />
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </SetupShell>
  )
}

// ── haos: welcome → supervisor handshake → redirect ────────────────────────

function HaosFlow() {
  const [stage, setStage] = useState<'welcome' | 'name'>('welcome')
  const [restoring, setRestoring] = useState(false)
  const [householdName, setHouseholdName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (restoring) return <RecoveryRestoreFlow onBack={() => setRestoring(false)} />

  async function continueSetup() {
    setBusy(true)
    setError(null)
    try {
      const body = householdName.trim()
        ? { household_name: householdName.trim() }
        : {}
      await api.post('/api/setup/haos/complete', body)
      await loadInstanceConfig()
      // Ingress already provides the auth headers, so we don't need
      // a token — bounce straight into the app.
      // ``basePath`` is the document base (``/`` standalone, or the
      // HA Supervisor ingress prefix for ha/haos). Hard-coding ``/``
      // here bounced the iframe to HA Core's frontend root.
      window.location.href = basePath
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
      setBusy(false)
    }
  }

  if (busy && !error) {
    return (
      <SetupShell>
        <h1 class="sh-setup-title">{t('setup.haos.completing_title')}</h1>
        <SetupSpinner label={t('setup.haos.completing')} />
      </SetupShell>
    )
  }

  if (stage === 'welcome') {
    return (
      <WelcomeCard
        mode="haos"
        step={{ current: 1, total: 2 }}
        busy={busy}
        error={error}
        ctaLabelKey="setup.haos.continue"
        onContinue={() => setStage('name')}
        onRestore={() => setRestoring(true)}
      />
    )
  }

  return (
    <SetupShell step={{ current: 2, total: 2 }}>
      <h1 class="sh-setup-title">{t('setup.haos.name_title')}</h1>
      <p class="sh-setup-intro">{t('setup.haos.name_intro')}</p>
      <form
        onSubmit={(e) => { e.preventDefault(); void continueSetup() }}
        class="sh-setup-form"
      >
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.household_name')}</span>
          <input
            name="household_name"
            type="text"
            maxLength={80}
            placeholder={t('setup.household_name_placeholder')}
            value={householdName}
            onInput={(e) => setHouseholdName((e.target as HTMLInputElement).value)}
          />
          <span class="sh-setup-hint">{t('setup.household_name_hint')}</span>
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </SetupShell>
  )
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2)
  return parts.map((p) => p[0]?.toUpperCase() ?? '').join('') || '?'
}

export default SetupPage
