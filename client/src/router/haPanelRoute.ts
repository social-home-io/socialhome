import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { routes } from '@/router'
import { servedThroughIngress } from '@/platform'

/**
 * Open the in-app page Home Assistant's App panel was asked for.
 *
 * Under ``haos`` Social Home runs inside HA's App panel. A tap on a
 * Companion push opens ``/<app slug>/<encoded in-app path>`` in HA's
 * frontend (``HaosAdapter.companion_click_url``), but the panel's iframe
 * always loads the App's ingress root — HA does not forward the tail into
 * the iframe URL. What HA does provide (home-assistant/frontend
 * ``src/panels/app/ha-panel-app.ts`` @ 53dc14f4):
 *
 *  - L431-441: an iframe that posts ``{type:
 *    "home-assistant/subscribe-properties"}`` to its parent is answered
 *    with ``home-assistant/properties`` at once;
 *  - L74-83: and again whenever the panel's ``route`` (or ``narrow``)
 *    changes; also on window resize (L456-460);
 *  - L462-496: the message carries ``route`` — for an App's own sidebar
 *    panel (``config.addon`` set, home-assistant/core
 *    ``hassio/addon_panel.py``) the panel route unchanged, i.e.
 *    ``{prefix: "/<slug>", path: <tail after the slug>}``
 *    (``src/layouts/partial-panel-resolver.ts`` ``routeTail``);
 *  - ``Route.path`` is ``location.pathname`` only
 *    (``src/layouts/home-assistant.ts`` ``updateRoute``), so the server
 *    folds a query / hash into the path, percent-encoded, and we decode
 *    it once here.
 *
 * We subscribe on mount, follow the route only when it **changes** (HA
 * re-sends the same route on every resize — that must never pull the user
 * back), replace the history entry for the first one (the tap's target
 * stands in for the root the iframe loaded) and push for later ones (a tap
 * while the panel is already open). Every target goes through
 * {@link parseHaPanelTarget}: same-app, root-relative, a known route.
 */

/** First path segments the SPA routes (``''`` is the root ``/``). */
export const HA_PANEL_ROUTE_PREFIXES: ReadonlySet<string> = new Set(
  Object.keys(routes).map((p) => p.split('/')[1] ?? ''),
)

// C0 controls + DEL — never part of an in-app path.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/

/** The in-app URL (path + query + hash) for a route tail HA posted, or
 *  ``null`` when it is empty or not a same-app path to a known route.
 *  Guards the redirect: only ``/x…`` (never ``//host`` or ``/\host``),
 *  no control characters, and the resolved path's first segment must be
 *  one the SPA routes. */
export function parseHaPanelTarget(
  tail: string,
  prefixes: ReadonlySet<string> = HA_PANEL_ROUTE_PREFIXES,
): string | null {
  if (!tail) return null
  let decoded: string
  try {
    decoded = decodeURIComponent(tail)
  } catch {
    return null
  }
  if (
    !decoded.startsWith('/') ||
    decoded.startsWith('//') ||
    decoded.includes('\\') ||
    CONTROL_CHARS.test(decoded)
  ) {
    return null
  }
  const probe = 'http://in-app.invalid'
  let url: URL
  try {
    url = new URL(decoded, probe)
  } catch {
    return null
  }
  if (url.origin !== probe) return null
  if (!prefixes.has(url.pathname.split('/')[1] ?? '')) return null
  return url.pathname + url.search + url.hash
}

interface Route { prefix?: unknown; path?: unknown }

/** Subscribe to HA's panel properties and call ``navigate`` with each new
 *  in-app target. Returns the unsubscribe. A no-op outside a frame. */
export function startHaPanelRoute(
  navigate: (target: string, replace: boolean) => void,
  { parent = window.parent, prefixes = HA_PANEL_ROUTE_PREFIXES }: {
    parent?: Window
    prefixes?: ReadonlySet<string>
  } = {},
): () => void {
  if (!parent || parent === window) return () => {}
  // The ingress iframe is same-origin with HA's frontend (its src is
  // ``/api/hassio_ingress/<token>/`` on HA's own host), so we talk to our
  // own origin only — never ``'*'``.
  const origin = location.origin
  let last: string | null = null
  const onMessage = (ev: MessageEvent) => {
    if (ev.source !== parent || ev.origin !== origin) return
    const data = ev.data as { type?: unknown; route?: Route } | null
    if (!data || data.type !== 'home-assistant/properties') return
    const route = data.route
    if (!route || typeof route !== 'object' || typeof route.path !== 'string') return
    const key = `${String(route.prefix ?? '')}\u0000${route.path}`
    const first = last === null
    if (key === last) return
    last = key
    const target = parseHaPanelTarget(route.path, prefixes)
    if (target) navigate(target, first)
  }
  window.addEventListener('message', onMessage)
  parent.postMessage({ type: 'home-assistant/subscribe-properties' }, origin)
  return () => {
    window.removeEventListener('message', onMessage)
    parent.postMessage({ type: 'home-assistant/unsubscribe-properties' }, origin)
  }
}

/** Mount inside the ``LocationProvider``: follows the HA App panel's route
 *  while Social Home is served through ingress. Renders nothing. */
export function HaPanelRoute({ parent }: { parent?: Window } = {}) {
  const { route } = useLocation()
  const enabled = servedThroughIngress()
  useEffect(() => {
    if (!enabled) return
    return startHaPanelRoute(route, { parent })
  }, [enabled, route, parent])
  return null
}
