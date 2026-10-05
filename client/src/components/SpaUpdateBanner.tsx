/**
 * SpaUpdateBanner — detect that a backend deploy has shipped a newer
 * SPA bundle than the one this tab booted with, and prompt the user
 * to reload.
 *
 * Mechanism
 *
 *  1. On module load, scrape the entry bundle's content hash from the
 *     ``<script type="module" src="…/assets/index-{hash}.js">`` tag the
 *     current document was served with. That's the "what this tab is
 *     running" reference. Stored once; never changes for the life of
 *     the tab.
 *
 *  2. Periodically refetch ``/api/instance/config`` (every five minutes
 *     while the tab is visible, plus an immediate check whenever
 *     the tab regains visibility). Compare ``spa_bundle_hash`` against
 *     the reference.
 *
 *  3. On mismatch, render a sticky banner at the top of the viewport
 *     with a "Reload" button. Banner stays until the user reloads
 *     (the new bundle re-mounts the SPA and resets the reference) or
 *     until they dismiss it (kept dismissed for the rest of the
 *     session; honoured per-deploy via the hash key).
 *
 *  Dev mode (``pnpm dev``) returns ``spa_bundle_hash: null`` from the
 *  backend because the SPA isn't served from disk; the banner stays
 *  hidden in that case. Same for any bundle without a recognisable
 *  ``index-{hash}.js`` script tag.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { instanceConfig, recheckInstanceConfig } from '@/store/instance'
import { t } from '@/i18n/i18n'

/** Five-minute heartbeat. Long enough that a busy tab isn't hammering
 *  the backend just to learn it's up-to-date; short enough that the
 *  user sees the prompt within a coffee break of a deploy. */
const POLL_INTERVAL_MS = 5 * 60 * 1000

/** Hash this tab booted with — captured at module load by reading the
 *  current document's entry script tag. ``null`` if no recognisable
 *  ``assets/index-{hash}.js`` tag is present (a brand-new template
 *  shape that doesn't match the parser, or the dev-server load path).
 *  The banner only fires when both sides have a non-null hash. */
const _bootBundleHash: string | null = (() => {
  if (typeof document === 'undefined') return null
  // ``getAttribute('src')`` returns the raw attribute (``./assets/…``)
  // instead of the resolved absolute URL ``.src`` gives us, which makes
  // the regex robust to different ``<base href>`` shapes.
  const scripts = Array.from(document.querySelectorAll('script[type="module"]'))
  for (const s of scripts) {
    const src = s.getAttribute('src') ?? ''
    const m = /assets\/index-([A-Za-z0-9_-]+)\.[A-Za-z0-9]+/.exec(src)
    if (m) return m[1]
  }
  return null
})()

/** When the user explicitly dismisses the banner, we remember the
 *  exact backend hash they dismissed for — so a *second* deploy in the
 *  same session re-surfaces the banner. The state is per-tab; we
 *  intentionally don't persist to ``localStorage`` because a stale tab
 *  in another window has its own state to track. */
const dismissedFor = signal<string | null>(null)

/** True when the backend's current bundle hash differs from the one
 *  this tab booted with AND the user hasn't already dismissed the
 *  prompt for that exact hash. */
function _isOutdated(): boolean {
  if (_bootBundleHash === null) return false
  const remote = instanceConfig.value?.spa_bundle_hash
  if (!remote) return false
  if (remote === _bootBundleHash) return false
  if (dismissedFor.value === remote) return false
  return true
}

export function SpaUpdateBanner() {
  useEffect(() => {
    if (_bootBundleHash === null) return  // dev mode / unbuilt SPA

    let timer: ReturnType<typeof setInterval> | null = null
    const tick = () => {
      // Skip the poll when the tab is hidden — saves the backend a
      // tick per stale tab on every laptop in the household. The
      // visibility-change listener fires an immediate check the
      // moment the tab regains focus.
      if (document.hidden) return
      recheckInstanceConfig().catch(() => { /* leave the previous value */ })
    }
    timer = setInterval(tick, POLL_INTERVAL_MS)
    const onVis = () => { if (!document.hidden) tick() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      if (timer) clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])

  // Read both signals INLINE in the component body so Preact's
  // signal-tracking subscribes the component to them. Reading the
  // signals only inside a helper like ``_isOutdated()`` works in
  // dev but the production build's signal-reactivity plugin can
  // skip subscriptions for `.value` accesses that don't appear in
  // the component source — making the banner go silent in prod
  // even though the helper sees the right values.
  const remote = instanceConfig.value?.spa_bundle_hash ?? null
  const dismissed = dismissedFor.value
  if (_bootBundleHash === null) return null
  if (!remote) return null
  if (remote === _bootBundleHash) return null
  if (dismissed === remote) return null
  return (
    <div class="sh-update-banner" role="status" aria-live="polite">
      <span class="sh-update-banner__msg">
        {t('spa_update.message')}
      </span>
      <button
        type="button"
        class="sh-update-banner__btn sh-update-banner__btn--primary"
        onClick={() => window.location.reload()}
      >
        {t('spa_update.reload')}
      </button>
      <button
        type="button"
        class="sh-update-banner__btn sh-update-banner__btn--ghost"
        title={t('spa_update.later_hint')}
        onClick={() => { dismissedFor.value = remote }}
      >
        {t('spa_update.later')}
      </button>
    </div>
  )
}

// ── Test-only helpers ─────────────────────────────────────────────────
// Vitest needs to seed the boot hash + reset dismissed state per test.
// Exposed under ``_test`` so the production code path doesn't read it.
export const _test = {
  bootBundleHash: () => _bootBundleHash,
  dismissedFor,
  isOutdated: _isOutdated,
}
