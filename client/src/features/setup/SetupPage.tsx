import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { setToken } from '@/store/auth'
import { instanceConfig, loadInstanceConfig } from '@/store/instance'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

interface HaPerson {
  username: string
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
    haPersonsError.value = err?.message || 'Failed to load HA persons.'
    throw err
  }
}

/**
 * SetupPage — first-boot wizard.
 *
 * Mode-aware: standalone shows a username+password form, ha shows a
 * person-pick + password form, haos auto-completes silently. The
 * caller (App.tsx) only renders this when
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
  if (cfg.mode === 'haos') return <HaosAutoComplete />
  if (cfg.mode === 'ha') return <HaOwnerForm />
  return <StandaloneSetupForm />
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
        <header class="sh-setup-brand">
          <span class="sh-setup-brand-mark" aria-hidden="true">🏠</span>
          <span class="sh-setup-brand-name">Social Home</span>
        </header>
        {step && (
          <ol class="sh-setup-steps" aria-label={`Step ${step.current} of ${step.total}`}>
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
  return (
    <div
      class="sh-setup-strength"
      aria-label={`Password strength ${filled} of 4`}
      data-score={filled}
    >
      <span /><span /><span /><span />
    </div>
  )
}

// ── Standalone: username + password ─────────────────────────────────────────

function StandaloneSetupForm() {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
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
      const resp = await api.post('/api/setup/standalone', { username, password }) as
        { token: string }
      setToken(resp.token)
      // Refresh the instance config so setup_required flips to false
      // and the SPA stops redirecting here.
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      window.location.href = '/'
    } catch (err: any) {
      setError(err?.message || t('setup.error.generic'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <SetupShell>
      <h1 class="sh-setup-title">{t('setup.standalone.title')}</h1>
      <p class="sh-setup-intro">{t('setup.standalone.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
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
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.password')}</span>
          <input
            name="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
          />
          <PasswordStrength value={password} />
        </label>
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.password_confirm')}</span>
          <input
            name="password_confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onInput={(e) => setConfirm((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </SetupShell>
  )
}

// ── ha: pick HA person + password ───────────────────────────────────────────

function HaOwnerForm() {
  const [persons, setPersons] = useState<HaPerson[] | null>(haPersons.value)
  const [loading, setLoading] = useState(persons === null)
  const [picked, setPicked] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (persons !== null) return
    fetchHaPersons().then(
      (p) => { setPersons(p); setLoading(false) },
      () => { setLoading(false) },
    )
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
      }) as { token: string }
      setToken(resp.token)
      await loadInstanceConfig()
      showToast(t('setup.success'), 'success')
      window.location.href = '/'
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
      </SetupShell>
    )
  }
  if (!persons || persons.length === 0) {
    return (
      <SetupShell>
        <h1 class="sh-setup-title">{t('setup.ha.title')}</h1>
        <p class="sh-setup-intro">{t('setup.ha.no_persons')}</p>
      </SetupShell>
    )
  }

  // Two visual steps: pick person, then set password. We surface them as
  // a progress indicator without splitting the form (a single submit keeps
  // the flow snappy and avoids a backwards-arrow dance for the operator).
  const step = picked ? 2 : 1

  return (
    <SetupShell step={{ current: step, total: 2 }}>
      <h1 class="sh-setup-title">{t('setup.ha.title')}</h1>
      <p class="sh-setup-intro">{t('setup.ha.intro')}</p>
      <form onSubmit={submit} class="sh-setup-form">
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
                    ? <img src={p.picture_url} alt="" />
                    : <span aria-hidden="true">{initials(p.display_name)}</span>}
                </span>
                <span class="sh-setup-person-name">{p.display_name}</span>
                <span class="sh-setup-person-username">@{p.username}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.password')}</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
          />
          <PasswordStrength value={password} />
        </label>
        <label class="sh-setup-field">
          <span class="sh-setup-label">{t('setup.password_confirm')}</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onInput={(e) => setConfirm((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="setup-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('setup.submitting') : t('setup.submit')}
        </Button>
      </form>
    </SetupShell>
  )
}

// ── haos: silent auto-complete via Supervisor ──────────────────────────────

function HaosAutoComplete() {
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    api.post('/api/setup/haos/complete').then(
      async () => {
        if (cancelled) return
        await loadInstanceConfig()
        // Ingress already provides the auth headers, so we don't need
        // a token — bounce straight into the app.
        window.location.href = '/'
      },
      (err) => {
        if (cancelled) return
        setError(err?.message || t('setup.error.generic'))
      },
    )
    return () => { cancelled = true }
  }, [])

  return (
    <SetupShell>
      <h1 class="sh-setup-title">{t('setup.haos.title')}</h1>
      {error
        ? <FormError id="setup-error" message={error} />
        : <SetupSpinner label={t('setup.haos.completing')} />}
    </SetupShell>
  )
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2)
  return parts.map((p) => p[0]?.toUpperCase() ?? '').join('') || '?'
}

export default SetupPage
