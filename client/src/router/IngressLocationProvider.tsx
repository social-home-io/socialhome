import type { ComponentChildren } from 'preact'
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { LocationProvider } from 'preact-iso'
import { addBase, basePath, stripBase } from '@/baseUrl'
import { confirmLeave, shouldConfirmLeave } from './navigationGuard'

/**
 * Drop-in replacement for ``preact-iso``'s ``<LocationProvider>`` that
 * is aware of the ingress URL prefix injected by HA Supervisor.
 *
 * Why a replacement rather than a wrapper
 * ---------------------------------------
 *
 * ``preact-iso``'s ``LocationProvider`` reads ``location.pathname``
 * raw and exposes it as the ``path`` consumed by ``<Router>``. Under
 * HA Supervisor ingress the pathname is
 * ``/api/hassio_ingress/<token>/feed`` — but routes are defined as
 * ``/feed``. Without intercepting the read, every in-app link
 * resolves to the wrong route (the ``default`` one).
 *
 * Wrapping doesn't help because ``preact-iso`` registers its own
 * ``click`` and ``popstate`` listeners that mutate ``location`` based
 * on un-stripped reads. The cleanest fix is to provide the same
 * context shape ``preact-iso`` consumes
 * (``LocationProvider.ctx.Provider``) ourselves and own the event
 * wiring end-to-end:
 *
 *  - We track the URL in the **stripped** form (``"/feed"``) in React
 *    state, derive ``path`` / ``query`` from it, and expose
 *    ``route(strippedUrl, replace?)``.
 *  - ``route`` writes the **prefixed** form via ``history.pushState``
 *    so the browser address bar matches what the user actually
 *    typed (the ingress URL).
 *  - On click of an in-scope ``<a>`` we ``preventDefault`` and call
 *    ``route`` with the stripped form.
 *  - On ``popstate`` we re-read ``location`` and re-strip.
 *
 * In the no-prefix case (standalone / Vite dev) ``basePath`` is
 * ``"/"``, ``addBase`` / ``stripBase`` are identity, and this
 * component behaves exactly like ``preact-iso``'s own
 * ``LocationProvider``.
 */
export function IngressLocationProvider({
  children,
}: {
  children: ComponentChildren
}) {
  // ``url`` is the stripped form (e.g. "/feed?q=1"). ``path`` /
  // ``query`` are derived from it.
  const [url, setUrl] = useState(
    () => stripBase(location.pathname) + location.search,
  )

  // The URL we're showing, readable from the popstate listener without
  // re-binding it on every navigation.
  const urlRef = useRef(url)
  urlRef.current = url

  const go = useCallback((target: string, replace?: boolean) => {
    // ``target`` arrives stripped from callers and from our own click
    // interceptor. Add the ingress prefix back for the browser URL.
    const prefixed = addBase(target)
    if (replace) history.replaceState(null, '', prefixed)
    else history.pushState(null, '', prefixed)
    setUrl(target)
  }, [])

  // A page with unsaved changes (``navigationGuard``) gets to ask first;
  // the move happens only once the user agrees to leave.
  const route = useCallback((target: string, replace?: boolean) => {
    if (!shouldConfirmLeave(target)) {
      go(target, replace)
      return
    }
    void confirmLeave().then((ok) => { if (ok) go(target, replace) })
  }, [go])

  const value = useMemo(() => {
    const u = new URL(url, location.origin)
    const path = u.pathname.replace(/\/+$/g, '') || '/'
    return {
      url,
      path,
      query: Object.fromEntries(u.searchParams),
      route,
      // ``back`` / ``forward`` are part of preact-iso's ``LocationHook``
      // contract (added in 2.12). They just drive browser history; our
      // ``popstate`` listener below re-strips the URL into ``url`` state.
      back: () => history.back(),
      forward: () => history.forward(),
      // ``wasPush`` is part of the contract but only consumed by
      // preact-iso-internal effects we no longer run; ``false`` is
      // safe.
      wasPush: false,
    }
  }, [url, route])

  useEffect(() => {
    const onPopState = () => {
      const target = stripBase(location.pathname) + location.search
      if (!shouldConfirmLeave(target)) {
        setUrl(target)
        return
      }
      // Back / Forward already moved the address bar. Put the page we
      // are leaving back, and only follow the user's choice once they
      // agree to drop their changes.
      const staying = urlRef.current
      history.pushState(null, '', addBase(staying))
      void confirmLeave().then((ok) => {
        if (!ok) return
        history.replaceState(null, '', addBase(target))
        setUrl(target)
      })
    }

    const onClick = (ev: MouseEvent) => {
      if (
        ev.ctrlKey ||
        ev.metaKey ||
        ev.altKey ||
        ev.shiftKey ||
        ev.button !== 0
      ) {
        return
      }
      const link = (ev.composedPath() as Element[]).find(
        (el) => el.nodeName === 'A',
      ) as HTMLAnchorElement | undefined
      if (!link) return
      const href = link.getAttribute('href')
      if (!href) return
      // Skip cross-origin, target=_blank, hash-only, download, and
      // non-http(s) links — the browser's default behaviour is
      // correct for those.
      if (
        link.origin !== location.origin ||
        link.target ||
        link.hasAttribute('download') ||
        href.startsWith('#') ||
        /^[a-z][a-z0-9+.-]*:/i.test(href)
      ) {
        return
      }
      ev.preventDefault()
      // ``link.pathname`` is the browser-resolved absolute path,
      // including the ingress prefix when ``<base href>`` and a
      // relative href cooperate. Strip the prefix back off for the
      // router.
      route(stripBase(link.pathname) + link.search + link.hash, false)
    }

    addEventListener('popstate', onPopState)
    addEventListener('click', onClick)
    return () => {
      removeEventListener('popstate', onPopState)
      removeEventListener('click', onClick)
    }
  }, [route])

  // ``LocationProvider.ctx`` is the context preact-iso's ``<Router>``
  // consumes via ``useLocation()``. Providing our own value with the
  // expected shape gives us a complete swap — preact-iso's own
  // ``LocationProvider`` never runs.
  return <LocationProvider.ctx.Provider value={value}>{children}</LocationProvider.ctx.Provider>
}

/** Exposed for the no-prefix fast-path assertion in tests. */
export const _basePath = basePath
