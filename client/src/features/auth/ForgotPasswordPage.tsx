/**
 * ForgotPasswordPage — admin-issued reset redeem flow (§§auth/standalone).
 *
 * Two modes:
 *
 *   - **No token** (``/forgot-password``): static info card. Standalone
 *     mode has no SMTP — recovery is admin-issued, so the page just
 *     explains how to ask the household admin for a reset link.
 *   - **With token** (``/reset-password?token=…``): the form. Posts
 *     ``{token, new_password}`` to ``/api/auth/redeem-password-reset``.
 *     On 204 toasts + redirects to ``/login`` (i.e. reload the SPA so
 *     it lands on the LoginPage with a fresh state).
 *
 * Public page — mounted before the auth gate in ``App.tsx`` so an
 * unauthenticated reset link works without the login form intercepting.
 */
import { useState } from 'preact/hooks'
import { basePath, addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { showToast } from '@/components/Toast'
import { Wordmark } from '@/components/Wordmark'

interface Props {
  /** ``?token=`` from the current URL. ``null`` → instructions mode. */
  token: string | null
}

export function ForgotPasswordPage({ token }: Props) {
  if (!token) return <InstructionsCard />
  return <ResetForm token={token} />
}

function InstructionsCard() {
  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} />
      </div>
      <div class="sh-card sh-forgot-instructions">
        <h2>Forgot your password?</h2>
        <p>
          Social Home recovers passwords through your household admin —
          there's no email-based reset.
        </p>
        <ol style={{ paddingLeft: '1.25rem', lineHeight: '1.6' }}>
          <li>Ask a household admin for a password-reset link.</li>
          <li>The admin issues a one-time link from the Admin → Members
              tab; it's valid for an hour.</li>
          <li>Open the link they send you and pick a new password.</li>
        </ol>
        <a class="sh-link" href={addBase('/')}>← Back to sign-in</a>
      </div>
    </div>
  )
}

function ResetForm({ token }: { token: string }) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: Event) => {
    e.preventDefault()
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      // Raw ``fetch`` (not the ``api`` client) so the 204/410/422/429
      // status checks below can branch without ``api.post`` calling
      // ``res.json()`` on the empty 204 body. Relative URL (no leading
      // slash) so the browser resolves it against ``<base href>`` —
      // bare ``/api/...`` would bypass the ingress prefix (#303).
      const res = await fetch('api/auth/redeem-password-reset', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ token, new_password: password }),
      })
      if (res.status === 204) {
        showToast('Password updated — please sign in.', 'success')
        // Hard-reload so the SPA lands on LoginPage with a clean state
        // (no stale signals from the reset flow). ``basePath`` is the
        // document base — ``/`` here would skip the ingress prefix
        // when the SPA is served behind HA Supervisor.
        window.location.href = basePath
        return
      }
      if (res.status === 410) {
        setError('This reset link has expired or has already been used. '
          + 'Ask your household admin for a new one.')
      } else if (res.status === 422) {
        setError('Password must be at least 8 characters.')
      } else if (res.status === 429) {
        setError('Too many attempts — wait a few minutes and try again.')
      } else {
        const body = await res.json().catch(() => null)
        setError(body?.error?.message ?? `Reset failed (${res.status}).`)
      }
    } catch (err: unknown) {
      setError((err as Error)?.message ?? 'Reset failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} />
      </div>
      <form onSubmit={submit} class="sh-login-form">
        <h2 style={{ marginTop: 0 }}>Set a new password</h2>
        <p class="sh-muted">
          You're using a one-time reset link. Pick a new password to
          finish signing in.
        </p>
        <label>
          New password
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            value={password}
            onInput={(e) =>
              setPassword((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          Confirm password
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={8}
            value={confirm}
            onInput={(e) =>
              setConfirm((e.target as HTMLInputElement).value)}
          />
        </label>
        <FormError id="reset-error" message={error} />
        <Button type="submit" disabled={busy}>
          {busy ? 'Updating…' : 'Set new password'}
        </Button>
      </form>
    </div>
  )
}
