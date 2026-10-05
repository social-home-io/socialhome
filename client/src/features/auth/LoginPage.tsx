import { useState } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { loadCurrentUser, setToken } from '@/store/auth'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import { Wordmark } from '@/components/Wordmark'

/**
 * LoginPage — standalone-mode credential form (§23.3).
 *
 * Posts `{username, password}` to /api/auth/token and stashes the
 * returned bearer token via setToken(). Inside Home Assistant, ingress
 * already supplies auth headers — this form is shown only when the
 * server is running with `SOCIAL_HOME_MODE=standalone` and the user
 * isn't already carrying a session token.
 *
 * The §25.7 IP rate-limit on /api/auth/token (5/15 min) protects this
 * endpoint from brute-force; the form just surfaces the 429.
 */
export function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: Event) {
    e.preventDefault()
    if (!username || !password) {
      setError(t('login.required'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/auth/token', { username, password }) as
        { token: string }
      setToken(resp.token)
      // Without this the SPA stays stuck on the login form:
      // ``isAuthed`` is ``currentUser != null``, and ``currentUser``
      // stays null until ``/api/me`` resolves.
      await loadCurrentUser()
      showToast(t('login.welcome_back'), 'success')
    } catch (err: any) {
      const status = err?.status
      if (status === 401) {
        setError(t('login.invalid'))
      } else if (status === 404) {
        setError(t('login.disabled'))
      } else if (status === 429) {
        setError(t('login.rate_limit'))
      } else {
        setError(err?.message || t('login.failed'))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} tagline={t('login.tagline')} />
      </div>
      <form onSubmit={submit} class="sh-login-form">
        <label>
          {t('login.username')}
          <input
            name="username"
            type="text"
            autoComplete="username"
            required
            aria-required="true"
            aria-invalid={error ? 'true' : undefined}
            aria-describedby={error ? 'login-error' : undefined}
            value={username}
            onInput={(e) =>
              setUsername((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          {t('login.password')}
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            aria-required="true"
            aria-invalid={error ? 'true' : undefined}
            aria-describedby={error ? 'login-error' : undefined}
            value={password}
            onInput={(e) =>
              setPassword((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="login-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? t('login.signing_in') : t('login.submit')}
        </Button>
      </form>
      <p class="sh-muted" style={{ textAlign: 'center', marginTop: 'var(--sh-space-md)' }}>
        <a class="sh-link" href={addBase('/forgot-password')}>{t('login.forgot_password')}</a>
      </p>
    </div>
  )
}
