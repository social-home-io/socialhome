/**
 * RouteProgress — a thin top progress bar shown while a lazily-loaded
 * route chunk is in flight.
 *
 * preact-iso keeps the *previous* route mounted while the next route's
 * `lazy()` chunk downloads (and, under Vite dev, while it is compiled
 * on-demand — which for a heavy page like ``SpaceFeedPage`` can take a
 * second or more). Without any feedback the app looks frozen: you click
 * a space, nothing changes, and it reads as "the page doesn't work".
 *
 * The `<Router>` in `App.tsx` drives this via `onLoadStart` / `onLoadEnd`.
 * We count in-flight loads (overlapping navigations are possible) and
 * render the bar whenever the count is > 0.
 */
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'

const inflight = signal(0)

// preact-iso invokes these from inside the Router's *synchronous* suspend
// handler (`_childDidSuspend`), which runs mid-diff while a lazy route chunk
// is in flight. Mutating a signal there re-enters Preact's render
// synchronously, which re-renders the still-suspended route, which re-throws
// its load promise, which re-invokes `onLoadStart` → an infinite synchronous
// loop that starves the microtask queue so the dynamic `import()` never
// resolves and the tab hard-freezes (the #473 regression). Deferring the
// mutation to a microtask moves the signal write *after* the diff completes,
// so the bar still toggles but the suspend cycle can settle. See
// `RouteProgress.test.tsx` for the regression guard.
/** Called from the Router's `onLoadStart`. */
export function routeLoadStart() {
  queueMicrotask(() => { inflight.value += 1 })
}

/** Called from the Router's `onLoadEnd`. */
export function routeLoadEnd() {
  // Never drop below zero — a stray `onLoadEnd` without a matching
  // `onLoadStart` shouldn't wedge the bar in a negative state.
  queueMicrotask(() => { inflight.value = Math.max(0, inflight.value - 1) })
}

export function RouteProgress() {
  if (inflight.value <= 0) return null
  return (
    <div
      class="sh-route-progress"
      role="progressbar"
      aria-label={t('route.loading')}
      aria-busy="true"
    >
      <div class="sh-route-progress__bar" />
    </div>
  )
}
