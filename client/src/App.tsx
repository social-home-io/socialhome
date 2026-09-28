import type { JSX } from 'preact'
import { Router } from 'preact-iso'
import { IngressLocationProvider as LocationProvider } from '@/router/IngressLocationProvider'
import { useComputed, signal } from '@preact/signals'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { basePath } from '@/baseUrl'
import { isAuthed, currentUser, loadCurrentUser, setToken, token } from '@/store/auth'
import { instanceConfig, loadInstanceConfig } from '@/store/instance'
import { usesIngressAuth } from '@/platform'
import { isGuardian, loadGuardian } from '@/store/guardian'
import { loadDmUnread } from '@/store/dms'
import { loadUserPreferences } from '@/store/userPreferences'
import { pageTitle, pageTitleAvatar } from '@/store/pageTitle'
import { Avatar } from '@/components/Avatar'
import { toggles, loadToggles } from '@/components/HouseholdToggles'
import { SetupPage } from '@/features/setup/SetupPage'
import { ForgotPasswordPage } from '@/features/auth/ForgotPasswordPage'
import { routes } from './router'
import { Button } from '@/components/Button'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { NotificationBell, startNotificationPolling, stopNotificationPolling } from '@/components/NotificationBell'
import { SearchBar } from '@/components/SearchBar'
import { QuickSwitcher } from '@/components/QuickSwitcher'
import { ToastContainer, showToast } from '@/components/Toast'
import { OnboardingFlow } from '@/components/OnboardingFlow'
import { SpaceCreateDialog } from '@/components/SpaceCreateDialog'
import { NewDmDialog } from '@/components/NewDmDialog'
import { CommentOverlay } from '@/components/CommentOverlay'
import { ImageLightbox } from '@/components/ImageLightbox'
import { StickyDialog } from '@/components/StickyDialog'
import { HighlightPickerDialog } from '@/components/HighlightPickerDialog'
import { CallTypePickerDialog } from '@/components/CallTypePickerDialog'
import { CallEmbedBlockedDialog } from '@/features/calls/CallEmbedBlockedDialog'
import { ConfirmDialogHost } from '@/components/confirm'
import { UserActionsMenu } from '@/components/UserActionsMenu'
import { HighlightPublishMenu } from '@/features/highlights/HighlightPublishMenu'
import { RejectReasonDialog } from '@/components/RejectReasonDialog'
import { ReportDialog } from '@/components/ReportDialog'
import { InstallPrompt } from '@/components/InstallPrompt'
import { OfflineIndicator } from '@/components/OfflineIndicator'
import { RouteProgress, routeLoadStart, routeLoadEnd } from '@/components/RouteProgress'
import { BackToTop } from '@/components/BackToTop'
import { SpaceInviteDialog } from '@/components/SpaceInviteDialog'
import { RemoteInviteDialog } from '@/components/RemoteInviteDialog'
import { SpaceJoinByCodeDialog } from '@/features/spaces/SpaceJoinByCodeDialog'
import IncomingCallDialog from '@/features/calls/IncomingCallDialog'
import { FormError } from '@/components/FormError'
import { Wordmark } from '@/components/Wordmark'
import { SideNav } from '@/components/SideNav'
import { MobileNav } from '@/components/MobileNav'

const showOnboarding = signal(false)

// Latches on the first ``loadCurrentUser()`` resolution (success or
// failure). The App shell uses it to avoid flashing the login form
// before the haos ingress probe has finished — without the gate the
// SPA paints LoginPage for one tick on every cold start while
// ``/api/me`` is in flight.
const authProbeAttempted = signal(false)

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
      // Without this the SPA stays stuck on the login form:
      // ``isAuthed`` is ``currentUser != null``, and ``currentUser``
      // stays null until ``/api/me`` resolves.
      await loadCurrentUser()
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
      <p class="sh-muted" style={{ textAlign: 'center', marginTop: 'var(--sh-space-md)' }}>
        <a class="sh-link" href="/forgot-password">Forgot password?</a>
      </p>
    </div>
  )
}

/**
 * IngressAuthFailed — terminal state for a haos cold-start that
 * couldn't authenticate via HA Supervisor ingress headers.
 *
 * In ``haos`` mode the SPA never carries a bearer token: ingress
 * adds ``X-Hass-Source: core.ingress`` + ``X-Remote-User-Name`` to
 * every request as it proxies through HA Core, and the backend's
 * :class:`HaIngressStrategy` accepts that as the auth handshake. If
 * those headers don't arrive (Supervisor restart mid-flight, the
 * panel was loaded from a stale URL outside the ingress prefix, or
 * the add-on was rebuilt and HA hasn't reconnected yet) the cold-
 * start probe of ``/api/me`` 401s and we land here.
 *
 * The fix is always "reload from the Home Assistant sidebar
 * entry" — that re-runs the ingress dance and gets the headers
 * back. The Reload button bounces the document to ``basePath``
 * (which under ingress is ``/api/hassio_ingress/<token>/``); if the
 * SPA was reached via a stale URL, the new URL is the canonical
 * one HA constructs for the sidebar entry.
 */
function IngressAuthFailed() {
  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} tagline="The social home for your household." />
      </div>
      <h1 style={{ textAlign: 'center' }}>Couldn't reach Social Home</h1>
      <p class="sh-muted" style={{ maxWidth: '34em', margin: '0 auto var(--sh-space-md)' }}>
        Home Assistant didn't pass through the authentication
        headers Social Home needs to sign you in. This usually means
        the panel was opened from a stale link, or the add-on was
        restarted mid-session. Open the <strong>Social Home</strong>
        sidebar entry again — that re-runs the ingress handshake.
      </p>
      <div style={{ textAlign: 'center' }}>
        <Button onClick={() => { window.location.assign(basePath) }}>
          Reload
        </Button>
      </div>
    </div>
  )
}

function TopBar() {
  const avatar = pageTitleAvatar.value
  return (
    <header class="sh-topbar" role="banner">
      {pageTitle.value && (
        <div class="sh-topbar-heading">
          {avatar && (
            <Avatar
              src={avatar.src}
              name={avatar.name || pageTitle.value}
              size={28}
            />
          )}
          <h1 class="sh-topbar-title">{pageTitle.value}</h1>
        </div>
      )}
      <SearchBar />
      <NotificationBell />
    </header>
  )
}

export function App() {
  const authed = useComputed(() => isAuthed.value)
  const cfg = useComputed(() => instanceConfig.value)

  // Fetch instance config once on cold start. Public endpoint —
  // works without a token. Drives the /setup vs /login choice.
  useEffect(() => {
    if (cfg.value === null) {
      loadInstanceConfig().catch(() => {
        // Silent — surfaces errors via the InstanceConfigError signal.
        // The login form remains the safe fallback.
      })
    }
  }, [])

  // Cold-start auth probe: pick the right /api/me handshake for the
  // current platform mode.
  //
  // * standalone / ha — bearer auth. Skip the probe entirely without
  //   a stashed token (it would 401 and bring the SPA up on the
  //   "Session expired" toast instead of a clean LoginPage); with a
  //   token, rehydrate so a refresh of an already-signed-in session
  //   doesn't bounce through the login screen.
  // * haos — ingress auth. The SPA never sees a token; HA Supervisor
  //   adds ``X-Hass-Source: core.ingress`` + ``X-Remote-User-Name`` to
  //   every request and the backend's ``HaIngressStrategy`` accepts
  //   that as the handshake. Probe ``/api/me`` unconditionally —
  //   success populates ``currentUser`` (and ``isAuthed`` flips
  //   true); failure means the panel was opened from a stale URL or
  //   the Supervisor isn't proxying — we render
  //   :func:`IngressAuthFailed` in that case instead of the bearer-
  //   mode login form.
  //
  // The single-shot ``authProbeAttempted`` latch keeps the render gate
  // below from flashing the login form for one tick while the probe
  // is in flight.
  useEffect(() => {
    if (cfg.value === null) return
    if (cfg.value.setup_required) return
    if (authProbeAttempted.value) return
    if (currentUser.value !== null) {
      authProbeAttempted.value = true
      return
    }
    const shouldProbe = usesIngressAuth() || token.value !== null
    if (!shouldProbe) {
      authProbeAttempted.value = true
      return
    }
    void loadCurrentUser().finally(() => { authProbeAttempted.value = true })
  }, [cfg.value])

  // Cold-start sidebar inputs: household feature toggles drive
  // gating (`feat_feed`, `feat_pages`, …) and `/api/cp/minors`
  // gates the Parent Control link. Both run only when authed; the
  // sidebar treats null/loading as "all visible" / "not a guardian"
  // so the first paint doesn't flicker items in.
  useEffect(() => {
    if (token.value === null) return
    if (toggles.value === null) void loadToggles()
    if (isGuardian.value === null) void loadGuardian()
    // Seed the sidebar Chats badge so the count is correct on cold
    // load — without this it stays at 0 until the user opens /dms or
    // the first ``dm.message`` WS frame triggers a refetch.
    void loadDmUnread()
    // Load per-user section visibility preferences (hide_highlights,
    // hide_momentum, hide_bazaar). Fires once per auth success and is
    // kept live via ``user.preferences_changed`` WS frames wired in
    // main.tsx.
    void loadUserPreferences()
  }, [authed.value])

  // 30 s notifications poll — a belt-and-suspenders fallback for when
  // the WS feed is down. MUST live in an effect, not the render body:
  // ``startNotificationPolling`` arms a ``setInterval``, so calling it
  // inline on every render leaked an orphaned timer per render (each
  // kept fetching ``/unread-count`` forever). The effect starts it once
  // the main shell is up and tears it down on sign-out / onboarding.
  useEffect(() => {
    if (!authed.value || showOnboarding.value) return
    startNotificationPolling()
    return stopNotificationPolling
  }, [authed.value, showOnboarding.value])

  // While the config is loading, render nothing (avoids a flash of
  // login form before we know whether to redirect to /setup).
  if (cfg.value === null) return null

  if (cfg.value.setup_required) return <SetupPage />

  // Public, pre-auth routes. The reset path is reachable from the
  // ``Forgot password?`` link below the login form, AND from a token
  // URL the admin hands the user out-of-band — the latter must work
  // even when the user isn't signed in. Same for the static
  // instructions card at /forgot-password.
  const path = typeof window !== 'undefined' ? window.location.pathname : ''
  if (path === '/forgot-password' || path === '/reset-password') {
    const params = new URLSearchParams(
      typeof window !== 'undefined' ? window.location.search : '',
    )
    return <ForgotPasswordPage token={params.get('token')} />
  }

  // Don't render LoginPage / IngressAuthFailed until the cold-start
  // probe has resolved — otherwise the page paints the wrong shell
  // for a tick while ``/api/me`` is in flight.
  if (!authProbeAttempted.value) return null

  if (!authed.value) {
    return usesIngressAuth() ? <IngressAuthFailed /> : <LoginPage />
  }

  const user = currentUser.value
  if (user?.is_new_member && !showOnboarding.value) {
    showOnboarding.value = true
  }

  if (showOnboarding.value) {
    return <OnboardingFlow onComplete={() => { showOnboarding.value = false }} />
  }

  // <Router> from preact-iso requires a <LocationProvider> ancestor —
  // it reads the current location from that context. Without the
  // wrapper, mounting Router throws "preact-iso's <Router> must be
  // used within a <LocationProvider>", which the ErrorBoundary surfaces
  // as the generic "Something went wrong" page after the operator
  // closes the onboarding wizard.
  return (
    <ErrorBoundary>
      <LocationProvider>
        <a href="#main" class="sh-skip-link">Skip to main content</a>
        <OfflineIndicator />
        <InstallPrompt />
        <div class="sh-layout">
          <SideNav />
          <MobileNav />
          <div class="sh-content">
            <TopBar />
            <main class="sh-main" id="main" role="main" tabIndex={-1}>
              <RouteProgress />
              <Router onLoadStart={routeLoadStart} onLoadEnd={routeLoadEnd}>
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
          <BackToTop />
          <ToastContainer />
          <SpaceCreateDialog />
          <NewDmDialog />
          <CommentOverlay />
          <ImageLightbox />
          <StickyDialog />
          <SpaceInviteDialog />
          <RemoteInviteDialog />
          <SpaceJoinByCodeDialog />
          <RejectReasonDialog />
          <ReportDialog />
          <IncomingCallDialog />
          <HighlightPickerDialog />
          <CallTypePickerDialog />
          <CallEmbedBlockedDialog />
          <ConfirmDialogHost />
          <UserActionsMenu />
          <HighlightPublishMenu />
        </div>
      </LocationProvider>
    </ErrorBoundary>
  )
}
