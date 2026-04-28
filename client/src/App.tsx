import type { JSX } from 'preact'
import { Router } from 'preact-iso'
import { useComputed, signal } from '@preact/signals'
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { isAuthed, currentUser, setToken } from '@/store/auth'
import { routes } from './router'
import { Button } from '@/components/Button'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { NotificationBell, startNotificationPolling } from '@/components/NotificationBell'
import { SearchBar } from '@/components/SearchBar'
import { QuickSwitcher } from '@/components/QuickSwitcher'
import { ToastContainer, showToast } from '@/components/Toast'
import { OnboardingFlow } from '@/components/OnboardingFlow'
import { SpaceCreateDialog } from '@/components/SpaceCreateDialog'
import { NewDmDialog } from '@/components/NewDmDialog'
import { RejectReasonDialog } from '@/components/RejectReasonDialog'
import { ReportDialog } from '@/components/ReportDialog'
import { InstallPrompt } from '@/components/InstallPrompt'
import { SpaceInviteDialog } from '@/components/SpaceInviteDialog'
import IncomingCallDialog from '@/features/calls/IncomingCallDialog'
import { FormError } from '@/components/FormError'
import { Wordmark } from '@/components/Wordmark'

const showOnboarding = signal(false)

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
function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: Event) {
    e.preventDefault()
    if (!username || !password) {
      setError('Username and password are required.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const resp = await api.post('/api/auth/token', { username, password }) as
        { token: string }
      setToken(resp.token)
      showToast('Welcome back', 'success')
    } catch (err: any) {
      const status = err?.status
      if (status === 401) {
        setError('Invalid credentials.')
      } else if (status === 404) {
        setError('Token login is disabled — log in via Home Assistant.')
      } else if (status === 429) {
        setError('Too many attempts — wait a few minutes.')
      } else {
        setError(err?.message || 'Login failed.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} tagline="The social home for your household." />
      </div>
      <form onSubmit={submit} class="sh-login-form">
        <label>
          Username
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
          Password
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
          {busy ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </div>
  )
}

function SideNav() {
  const user = currentUser.value
  return (
    <nav class="sh-sidenav" role="navigation" aria-label="Main navigation">
      <Wordmark as="a" href="/" size={28} className="sh-sidenav-brand" />
      <a href="/">Feed</a>
      <a href="/spaces">Spaces</a>
      <a href="/dms">Messages</a>
      <a href="/calls">Calls</a>
      <a href="/notifications">Notifications</a>
      <a href="/calendar">Calendar</a>
      <a href="/tasks">Tasks</a>
      <a href="/pages">Pages</a>
      <a href="/gallery">Gallery</a>
      <a href="/shopping">Shopping</a>
      <a href="/stickies">Stickies</a>
      <a href="/bazaar">Bazaar</a>
      <a href="/presence">Presence</a>
      <a href="/family">Family</a>
      <a href="/search">Search</a>
      <a href="/dashboard">Dashboard</a>
      <hr />
      <a href="/settings">Settings</a>
      <a href="/connections">Connections</a>
      {user?.is_admin && <a href="/admin">Admin</a>}
    </nav>
  )
}

function TopBar() {
  return (
    <header class="sh-topbar" role="banner">
      <SearchBar />
      <NotificationBell />
    </header>
  )
}

export function App() {
  const authed = useComputed(() => isAuthed.value)
  if (!authed.value) return <LoginPage />

  const user = currentUser.value
  if (user?.is_new_member && !showOnboarding.value) {
    showOnboarding.value = true
  }

  if (showOnboarding.value) {
    return <OnboardingFlow onComplete={() => { showOnboarding.value = false }} />
  }

  startNotificationPolling()

  return (
    <ErrorBoundary>
      <a href="#main" class="sh-skip-link">Skip to main content</a>
      <InstallPrompt />
      <div class="sh-layout">
        <SideNav />
        <div class="sh-content">
          <TopBar />
          <main class="sh-main" id="main" role="main" tabIndex={-1}>
            <Router>
              {Object.entries(routes).map(([path, Component]) => {
                // preact-iso's ``lazy()`` returns an AsyncComponent with
                // a ``.preload`` property; TypeScript's JSX checker
                // doesn't recognise it as a valid element constructor.
                // Cast through ``any`` only at the JSX site.
                const C = Component as unknown as (props: { path: string }) => JSX.Element
                return <C path={path} key={path} />
              })}
            </Router>
          </main>
        </div>
        <QuickSwitcher />
        <ToastContainer />
        <SpaceCreateDialog />
        <NewDmDialog />
        <SpaceInviteDialog />
        <RejectReasonDialog />
        <ReportDialog />
        <IncomingCallDialog />
      </div>
    </ErrorBoundary>
  )
}
