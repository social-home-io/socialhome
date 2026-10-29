/**
 * AppHost — sandboxed iframe container for a Social Home App.
 *
 * Security model (CRITICAL — do not relax):
 *   sandbox="allow-scripts"  ONLY — never add allow-same-origin.
 *   If allow-same-origin were present the iframe would share the
 *   parent's origin, giving the app direct access to localStorage
 *   (where the bearer token lives) and to window.parent.fetch with
 *   full credentials. The bridge.ts postMessage channel is the
 *   intentional, audited API surface; the iframe origin boundary
 *   enforces that.
 *
 * The entry_url from /api/apps/{id}/runtime starts with /api/...
 * and MUST go through addBase() so it resolves correctly under HA
 * Supervisor ingress (where the document base is not /).
 *
 * States — each says why the app isn't showing and what to do next:
 *   loading   runtime fetch, then the frame starts (hidden)
 *   slow      no word from the app after SLOW_MS → "taking longer" + Try again
 *   failed    no word after FAIL_MS, the frame loaded but never spoke
 *             (a bundle error page, a crashed script), or the runtime
 *             fetch failed → "didn't start" + Try again / Back to Apps
 *   off       403 APP_NOT_ENABLED → admin: Turn on; member: an admin turned it off
 *   age       403 APP_AGE_RESTRICTED → "isn't available for your account"
 *   gone      404 → "isn't installed any more"
 *   ready     the app's first bridge request (``app.ready`` or any RPC)
 *
 * An ``app.uninstalled`` / ``app.disabled`` frame for the open app asks
 * the server again, so the tab lands on ``gone`` / ``off`` live.
 *
 * The frame stays invisible until the app speaks, so a server error body
 * served in its place (raw JSON / text) is never shown to anyone.
 *
 * ``?session=<sid>`` ("Play" in Needs you) is the launch context: the
 * bridge takes in that one invite and hands it to the app (see bridge.ts).
 * A new ``?session=`` while the app is open (Play from the bell) starts a
 * fresh frame, so the app launches into that game.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { getRuntime, setEnabled, type AppRuntime } from '@/store/apps'
import { currentUser } from '@/store/auth'
import { useTitle } from '@/store/pageTitle'
import { mountBridge } from '@/features/apps/bridge'
import { appOpenedFrom } from '@/features/apps/appReturn'
import { addBase } from '@/baseUrl'
import { ws, type WsEvent } from '@/ws'
import { ApiError } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

/** No word from the app after this long → "taking longer than usual". */
export const SLOW_MS = 8_000
/** No word from the app after this long → "didn't start". */
export const FAIL_MS = 20_000
/** The frame finished loading but the app never spoke → "didn't start".
 *  The SDK asks the host for its context as soon as its script runs, so
 *  a frame that loaded and stayed silent this long is an error page or a
 *  script that crashed — no need to wait the full FAIL_MS. */
export const LOAD_GRACE_MS = 5_000

type HostState =
  | { kind: 'loading' }
  | { kind: 'starting'; slow: boolean }
  | { kind: 'ready' }
  | { kind: 'failed'; detail: string | null }
  | { kind: 'off'; name: string | null }
  | { kind: 'age' }
  | { kind: 'gone' }

/** The game invite a ``?session=`` names ("Play" in Needs you) — only a
 *  session id, never markup or a path. */
export function launchSessionFrom(query: Record<string, string | undefined>): string | undefined {
  const session = query.session
  return session && /^[A-Za-z0-9_-]{1,128}$/.test(session) ? session : undefined
}

export default function AppHost() {
  const { params, query } = useRoute()
  const appId = params.appId ?? ''
  return <AppHostInner appId={appId} sessionId={launchSessionFrom(query)} />
}

/** Exported for overlay / in-page embedding (AppsPage overlay path). */
export function AppHostInner({ appId, sessionId }: {
  appId: string
  /** The game invite "Play" opened the app for (``?session=``). */
  sessionId?: string
}) {
  const [runtime, setRuntime] = useState<AppRuntime | null>(null)
  /** The ``sessionId`` the current runtime (and frame) was started for:
   *  until the runtime for a new Play arrives, no frame shows (never the
   *  previous game's frame under the new key). */
  const [frameSession, setFrameSession] = useState<string | undefined>(undefined)
  const [state, setState]     = useState<HostState>({ kind: 'loading' })
  /** Bumped by Try again / Turn on: refetch the runtime (a fresh signed
   *  URL) and mount a new frame — a new element, so the retry adds no
   *  entry to the browser history. */
  const [attempt, setAttempt] = useState(0)
  const [turningOn, setTurningOn] = useState(false)
  const iframeRef             = useRef<HTMLIFrameElement | null>(null)
  const timers                = useRef<number[]>([])
  const { route }             = useLocation()
  const isAdmin               = !!currentUser.value?.is_admin

  const name = runtime?.name ?? (state.kind === 'off' ? state.name : null)
  // The topbar names the section; the host bar's heading names the app
  // (three "Chess" lines stacked on a phone otherwise). Set here so a
  // deep link doesn't keep the previous page's title.
  useTitle(t('nav.apps'))

  const clearTimers = () => {
    timers.current.forEach(id => window.clearTimeout(id))
    timers.current = []
  }
  const later = (ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms))
  }

  // Fetch the signed runtime descriptor on mount / appId change / retry.
  useEffect(() => {
    setRuntime(null)
    setState({ kind: 'loading' })
    let cancelled = false
    getRuntime(appId).then(rt => {
      if (cancelled) return
      setRuntime(rt)
      setFrameSession(sessionId)
      setState({ kind: 'starting', slow: false })
    }).catch((err: unknown) => {
      if (cancelled) return
      setState(stateForError(err))
    })
    return () => { cancelled = true }
  }, [appId, attempt, sessionId])

  // An admin uninstalled or turned off this app while it's open: ask the
  // server again, so the tab shows "isn't installed any more" / "turned
  // off" (the frame goes) instead of an app whose calls start failing.
  useEffect(() => {
    const recheck = (e: WsEvent) => {
      if (e.data.app_id === appId) setAttempt(n => n + 1)
    }
    const offs = [ws.on('app.uninstalled', recheck), ws.on('app.disabled', recheck)]
    return () => offs.forEach(off => off())
  }, [appId])

  // While the app starts: slow after SLOW_MS, failed after FAIL_MS.
  const starting = state.kind === 'starting'
  useEffect(() => {
    if (!starting) return
    later(SLOW_MS, () => setState(s => s.kind === 'starting' ? { kind: 'starting', slow: true } : s))
    later(FAIL_MS, () => setState(s => s.kind === 'starting' ? { kind: 'failed', detail: null } : s))
    return clearTimers
  }, [starting, runtime])

  // Mount the postMessage bridge in the same commit that puts the iframe
  // in the page — a layout effect, never a plain effect. Preact defers a
  // plain effect until after the next paint (requestAnimationFrame, or a
  // timeout in a background tab); a cached bundle can start the app and
  // post its first request before then, and the SDK does not resend it,
  // so the app hung and timed out ("Failed to load"). The frame's own
  // messages arrive as later tasks, so a listener attached during this
  // commit sees every one of them. Works for every SDK version.
  useLayoutEffect(() => {
    const el = iframeRef.current
    if (!el || !runtime) return
    const cleanup = mountBridge(el, {
      appId: runtime.app_id,
      selfUserId: runtime.self_user_id,
      launchSession: frameSession,
      onReady: () => {
        clearTimers()
        setState(s => s.kind === 'starting' ? { kind: 'ready' } : s)
      },
    })
    return cleanup
  }, [runtime, frameSession]) // iframeRef.current is stable once rendered; runtime changes re-mount

  function handleFrameLoad() {
    later(LOAD_GRACE_MS, () => setState(s => s.kind === 'starting' ? { kind: 'failed', detail: null } : s))
  }

  // Router navigation (it keeps the HA ingress prefix), not a page load:
  // leaving unmounts this component, which removes the sandboxed iframe —
  // the browser discards its browsing context with it — and the bridge
  // effect's cleanup drops the message listener and the WS relay. A page
  // reload adds nothing to that teardown (AppHost.test.tsx pins it).
  // Back returns to the page the app was opened from (one history step,
  // so the browser's own Back doesn't land in the app again); a deep link
  // has none and goes to the Apps page in its place.
  function handleBack() {
    if (appOpenedFrom() !== null) window.history.back()
    else route('/apps', true)
  }
  function backToApps() {
    route('/apps')
  }
  function retry() {
    clearTimers()
    setAttempt(n => n + 1)
  }
  async function turnOn() {
    setTurningOn(true)
    try {
      await setEnabled(appId, true)
      retry()
    } catch {
      showToast(t('apps.enable_failed'), 'error')
    } finally {
      setTurningOn(false)
    }
  }

  const label = name ?? appId
  const startLabel = runtime ? t('apps.host.starting', { name: runtime.name }) : t('apps.host.loading')

  return (
    <div class="sh-app-host">
      <header class="sh-app-host__bar">
        <Button variant="secondary" onClick={handleBack}>
          <span aria-hidden="true" class="sh-flip-rtl">←</span> {t('common.back')}
        </Button>
        {name && <h2 class="sh-app-host__title">{name}</h2>}
      </header>

      <div class="sh-app-host__stage">
        {runtime && frameSession === sessionId && state.kind !== 'failed' && (
          /*
           * SECURITY: sandbox="allow-scripts" ONLY.
           * NEVER add allow-same-origin — that collapses the origin
           * boundary and lets the app read localStorage (bearer token)
           * and call window.parent.fetch with host credentials.
           * The postMessage bridge (bridge.ts) is the only sanctioned
           * host↔app communication channel.
           *
           * src goes through addBase() so the signed bundle URL resolves
           * against the HA Supervisor ingress base, not the HA host root.
           *
           * Hidden (and out of the tab order / a11y tree) until the app
           * speaks, so an error body served in its place never shows.
           */
          <iframe
            key={`${attempt}:${frameSession ?? ''}`}
            ref={iframeRef}
            class={`sh-app-frame${state.kind === 'ready' ? '' : ' sh-app-frame--pending'}`}
            src={addBase(runtime.entry_url)}
            title={runtime.name}
            sandbox="allow-scripts"
            aria-hidden={state.kind === 'ready' ? undefined : 'true'}
            onLoad={handleFrameLoad}
          />
        )}

        {(state.kind === 'loading' || (state.kind === 'starting' && !state.slow)) && (
          <div class="sh-app-host__status" aria-live="polite">
            <Spinner size={48} label={startLabel} />
            {/* The spinner says it to a screen reader already. */}
            <p class="sh-app-host__status-body" aria-hidden="true">{startLabel}</p>
          </div>
        )}

        {state.kind === 'starting' && state.slow && (
          <Status
            title={t('apps.host.slow_title', { name: label })}
            body={t('apps.host.slow_body')}
            polite
            busy
          >
            <Button onClick={retry}>{t('common.try_again')}</Button>
          </Status>
        )}

        {state.kind === 'failed' && (
          <Status
            title={name
              ? t('apps.host.failed_title', { name })
              : t('apps.host.failed_title_generic')}
            body={state.detail ?? t('apps.host.failed_body')}
          >
            <Button variant="secondary" onClick={backToApps}>{t('apps.host.back_to_apps')}</Button>
            <Button onClick={retry}>{t('common.try_again')}</Button>
          </Status>
        )}

        {state.kind === 'off' && (isAdmin ? (
          <Status
            title={t('apps.host.off_admin_title', { name: label })}
            body={t('apps.host.off_admin_body')}
          >
            <Button variant="secondary" onClick={backToApps}>{t('apps.host.back_to_apps')}</Button>
            <Button loading={turningOn} onClick={() => { void turnOn() }}>
              {t('apps.host.turn_on', { name: label })}
            </Button>
          </Status>
        ) : (
          <Status
            title={t('apps.host.off_member_title', { name: label })}
            body={t('apps.host.off_member_body')}
          >
            <Button onClick={backToApps}>{t('apps.host.back_to_apps')}</Button>
          </Status>
        ))}

        {state.kind === 'age' && (
          <Status title={t('apps.host.age_restricted')}>
            <Button onClick={backToApps}>{t('apps.host.back_to_apps')}</Button>
          </Status>
        )}

        {state.kind === 'gone' && (
          <Status title={t('apps.host.gone_title')} body={t('apps.host.gone_body')}>
            <Button onClick={backToApps}>{t('apps.host.back_to_apps')}</Button>
          </Status>
        )}
      </div>
    </div>
  )
}

/** The host state a failed runtime fetch maps to. */
function stateForError(err: unknown): HostState {
  if (err instanceof ApiError) {
    if (err.code === 'APP_NOT_ENABLED') {
      const n = err.extra.name
      return { kind: 'off', name: typeof n === 'string' && n ? n : null }
    }
    // Any other 403 is the age gate — the server's only other refusal.
    if (err.status === 403) return { kind: 'age' }
    if (err.status === 404) return { kind: 'gone' }
  }
  const message = err instanceof Error && err.message ? err.message : t('apps.host.load_failed')
  return { kind: 'failed', detail: message }
}

/** One host state: a heading, one sentence, the next step. */
function Status({ title, body, polite, busy, children }: {
  title: string
  body?: string
  /** A passing state (slow) is announced politely; a dead end is an alert. */
  polite?: boolean
  /** Still trying: the spinner stays (decorative — the title says it). */
  busy?: boolean
  children?: ComponentChildren
}) {
  return (
    <div class="sh-app-host__status" role={polite ? 'status' : 'alert'}>
      {busy && <span aria-hidden="true"><Spinner size={48} /></span>}
      <h3 class="sh-app-host__status-title">{title}</h3>
      {body && <p class="sh-app-host__status-body">{body}</p>}
      <div class="sh-app-host__status-actions">{children}</div>
    </div>
  )
}
