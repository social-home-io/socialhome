/**
 * Ingress-aware URL helpers — anchor every runtime URL on the
 * document's base.
 *
 * Why this exists
 * ---------------
 *
 * The Social Home SPA can be served at one of three document bases
 * depending on the deployment shape:
 *
 *  - Standalone Docker / dev (Vite proxy): the document loads at
 *    ``http://host:port/`` and every API path is rooted at ``/api/...``.
 *  - HA add-on behind HA Supervisor ingress: the document loads at
 *    ``http://<ha>/api/hassio_ingress/<token>/`` and every API path
 *    on the add-on container ends up reachable through
 *    ``/api/hassio_ingress/<token>/api/...``.
 *
 * Hard-coding ``/api/me`` or ``new WebSocket(\`ws://${location.host}/api/ws\`)``
 * worked for the first shape and silently broke under ingress —
 * fetches would go to ``/api/me`` on the HA host (which is HA Core,
 * not the add-on) and 404 / 401 / redirect.
 *
 * Fix: backend's ``SpaIndexView`` rewrites ``<base href>`` in
 * ``index.html`` from the Supervisor-injected ``X-Ingress-Path``
 * header at request time; the SPA reads ``document.baseURI`` (which
 * honours ``<base href>``) and builds every fetch / WebSocket / link
 * relative to it.
 *
 * ``document.baseURI`` is evaluated **once** at module load so it
 * survives a programmatic ``history.pushState`` (the document's base
 * doesn't change mid-session).
 */

/** Path portion of the document base with leading ``/`` and trailing
 *  ``/``, e.g. ``"/api/hassio_ingress/abc/"`` or ``"/"``. Used by
 *  the router to strip the prefix before matching. */
export const basePath: string = new URL('./', document.baseURI).pathname

/** Strip ``basePath`` from a pathname for client-side router matching.
 *
 *  ``preact-iso``'s ``<Router>`` matches against
 *  ``location.pathname`` directly. Under ingress that's the full
 *  ``/api/hassio_ingress/<token>/feed`` — we need to feed
 *  ``/feed`` into the matcher. */
export function stripBase(pathname: string): string {
  // ``basePath`` always ends in ``/``. To preserve the leading slash
  // in the result we keep the trailing slash on the prefix when
  // stripping ("foo/" off "/foo/feed" gives "feed" — we want "/feed",
  // so strip without the trailing slash and let the leading "/" of
  // the suffix carry through).
  const prefix = basePath.replace(/\/+$/, '')
  if (!prefix) return pathname
  if (pathname === prefix) return '/'
  if (pathname.startsWith(prefix + '/')) {
    return pathname.slice(prefix.length) || '/'
  }
  return pathname
}

/** Add ``basePath`` to a path for ``history.pushState`` / link clicks.
 *
 *  Idempotent — passing a path that already starts with ``basePath``
 *  returns it unchanged. */
export function addBase(path: string): string {
  // Strip query / hash first so we don't break composition.
  const [pathOnly, ...rest] = path.split(/(?=[?#])/, 2)
  const suffix = rest.join('')
  const prefix = basePath.replace(/\/+$/, '')
  if (!prefix) return path
  if (pathOnly.startsWith(basePath) || pathOnly === prefix) return path
  const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
  return prefix + normalised + suffix
}

/** ``addBase`` for an ``<a href>`` whose value may or may not be an
 *  in-app path (a server-sent ``link_url``, a caller-supplied ``href``).
 *  Root-relative paths (``/spaces/x``) get the ingress prefix; anything
 *  else (``https://…``, ``//host``, ``#frag``, ``api/…``) is returned
 *  as-is. */
export function appHref(url: string): string {
  return url.startsWith('/') && !url.startsWith('//') ? addBase(url) : url
}
