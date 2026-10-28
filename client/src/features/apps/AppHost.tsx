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
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { getRuntime, type AppRuntime } from '@/store/apps'
import { mountBridge } from '@/features/apps/bridge'
import { addBase } from '@/baseUrl'
import { ApiError } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { t } from '@/i18n/i18n'

export default function AppHost() {
  const { params } = useRoute()
  const appId = params.appId ?? ''
  return <AppHostInner appId={appId} />
}

/** Exported for overlay / in-page embedding (AppsPage overlay path). */
export function AppHostInner({ appId }: { appId: string }) {
  const [runtime, setRuntime]         = useState<AppRuntime | null>(null)
  const [error, setError]             = useState<string | null>(null)
  const [ageRestricted, setAgeRestricted] = useState(false)
  const iframeRef                     = useRef<HTMLIFrameElement | null>(null)
  const { route }                     = useLocation()

  // Fetch the signed runtime descriptor on mount / appId change.
  useEffect(() => {
    setRuntime(null)
    setError(null)
    setAgeRestricted(false)
    let cancelled = false
    getRuntime(appId).then(rt => {
      if (!cancelled) setRuntime(rt)
    }).catch((err: unknown) => {
      if (cancelled) return
      if (err instanceof ApiError && err.status === 403) {
        setAgeRestricted(true)
      } else {
        setError((err as Error).message ?? t('apps.host.load_failed'))
      }
    })
    return () => { cancelled = true }
  }, [appId])

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
    })
    return cleanup
  }, [runtime]) // iframeRef.current is stable once rendered; runtime changes re-mount

  // Router navigation (it keeps the HA ingress prefix), not a page load:
  // leaving unmounts this component, which removes the sandboxed iframe —
  // the browser discards its browsing context with it — and the bridge
  // effect's cleanup drops the message listener and the WS relay. A page
  // reload adds nothing to that teardown (AppHost.test.tsx pins it).
  function handleBack() {
    route('/apps')
  }

  return (
    <div class="sh-app-host">
      <header class="sh-app-host__bar">
        <Button variant="secondary" onClick={handleBack}>
          ← {t('common.back')}
        </Button>
        {runtime && (
          <span class="sh-app-host__title">{runtime.name}</span>
        )}
      </header>

      {!runtime && !error && !ageRestricted && (
        <div class="sh-app-host__status" aria-live="polite">
          <Spinner label={t('apps.host.loading')} />
        </div>
      )}

      {ageRestricted && (
        <div class="sh-app-host__status sh-app-host__status--age-restricted" role="alert">
          <p>{t('apps.host.age_restricted')}</p>
          <Button onClick={handleBack}>
            {t('apps.host.back_to_apps')}
          </Button>
        </div>
      )}

      {error && (
        <div class="sh-app-host__status sh-app-host__status--error" role="alert">
          <p>{error}</p>
          <Button onClick={handleBack}>
            {t('apps.host.back_to_apps')}
          </Button>
        </div>
      )}

      {runtime && (
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
         */
        <iframe
          ref={iframeRef}
          class="sh-app-frame"
          src={addBase(runtime.entry_url)}
          title={runtime.name}
          sandbox="allow-scripts"
        />
      )}
    </div>
  )
}
