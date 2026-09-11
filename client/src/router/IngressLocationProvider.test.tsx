/**
 * IngressLocationProvider tests.
 *
 * JSDOM's ``document.baseURI`` is ``http://localhost:3000/``, so the
 * module-level ``basePath`` is ``/`` and ``addBase`` / ``stripBase`` are
 * identity. That exercises the no-prefix wiring end-to-end (click
 * interception, popstate, route()).
 *
 * The prefixed path (where ``basePath`` is
 * ``/api/hassio_ingress/<token>/``) is covered by a second describe block
 * that mocks ``@/baseUrl`` before the provider import, so the helpers
 * report the ingress prefix.
 */
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { useEffect } from 'preact/hooks'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useLocation } from 'preact-iso'

import { IngressLocationProvider } from './IngressLocationProvider'

interface LocationSnapshot {
  url: string
  path: string
  query: Record<string, string>
  route: (target: string, replace?: boolean) => void
}

function Probe({ sink }: { sink: { current?: LocationSnapshot } }) {
  const loc = useLocation()
  useEffect(() => {
    sink.current = loc
  })
  return (
    <>
      <a href="/feed" data-testid="feed">feed</a>
      <a href="/spaces/abc" target="_blank" data-testid="blank">blank</a>
      <a href="#frag" data-testid="hash">hash</a>
      <a href="mailto:a@b" data-testid="mail">mail</a>
      <a href="/download.zip" data-testid="ext-dot">dot</a>
      <a href="/file.txt" download data-testid="dl">dl</a>
      <a href="http://other.example/feed" data-testid="cross">cross</a>
    </>
  )
}

function resetLocation(url: string) {
  history.replaceState(null, '', url)
}

describe('IngressLocationProvider (no-prefix, JSDOM default)', () => {
  let sink: { current?: LocationSnapshot }

  beforeEach(() => {
    resetLocation('/feed?x=1')
    sink = {}
  })

  afterEach(() => {
    cleanup()
  })

  it('reads initial pathname + query into the context value', () => {
    render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    expect(sink.current?.path).toBe('/feed')
    expect(sink.current?.query).toEqual({ x: '1' })
    expect(sink.current?.url).toBe('/feed?x=1')
  })

  it('route() pushes a new pathname and updates the context', () => {
    render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    sink.current!.route('/spaces/abc')
    expect(pushSpy).toHaveBeenCalledWith(null, '', '/spaces/abc')
    pushSpy.mockRestore()
  })

  it('route(target, replace=true) uses replaceState instead of pushState', () => {
    render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    const replaceSpy = vi.spyOn(history, 'replaceState')
    sink.current!.route('/setup', true)
    expect(pushSpy).not.toHaveBeenCalled()
    expect(replaceSpy).toHaveBeenCalledWith(null, '', '/setup')
    pushSpy.mockRestore()
    replaceSpy.mockRestore()
  })

  it('intercepts internal <a> clicks and routes via pushState', () => {
    const { getByTestId } = render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    getByTestId('feed').dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(true)
    expect(pushSpy).toHaveBeenCalledWith(null, '', '/feed')
    pushSpy.mockRestore()
  })

/**
 * Swallow the browser's native link follow-through for one test.
 *
 * These "skips interception" cases dispatch clicks the provider is
 * *supposed* to ignore, so nothing calls ``preventDefault`` and jsdom
 * proceeds to its default anchor activation — which it cannot implement,
 * logging "Not implemented: navigation to another Document" through its
 * virtual console, asynchronously.
 *
 * That noise is not harmless. When one of those lands after the test file
 * has finished, vitest attributes it to nothing and reports it as an
 * unhandled error, failing the whole run with every test passing — a
 * genuine CI flake that cost a re-run on #658 (1221 passed, "Errors 1
 * error", exit 1).
 *
 * Suppressing it does not weaken what these tests assert: the assertion is
 * that OUR handler did not route (``history.pushState`` untouched). What
 * the browser would then do natively is the browser's business, and jsdom
 * has no implementation of it either way. Deliberately NOT installed
 * globally — the sibling "intercepts internal <a> clicks" test asserts
 * ``evt.defaultPrevented``, and a blanket suppressor would keep that green
 * even if the provider stopped preventing the default itself.
 */
function suppressNativeLinkNavigation(): () => void {
  const stop = (ev: Event) => ev.preventDefault()
  // Bubble phase on ``window``, same as the provider's own listener. It
  // never reads ``defaultPrevented``, so this cannot change its decisions.
  window.addEventListener('click', stop)
  return () => window.removeEventListener('click', stop)
}

  it.each([
    ['ctrlKey', { ctrlKey: true }],
    ['metaKey', { metaKey: true }],
    ['shiftKey', { shiftKey: true }],
    ['altKey', { altKey: true }],
    ['middle-click', { button: 1 }],
  ])('skips interception when %s is set', (_label, init) => {
    const { getByTestId } = render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    const restoreNav = suppressNativeLinkNavigation()
    const evt = new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      button: 0,
      ...init,
    })
    getByTestId('feed').dispatchEvent(evt)
    expect(pushSpy).not.toHaveBeenCalled()
    restoreNav()
    pushSpy.mockRestore()
  })

  it('skips target=_blank, download, hash, mailto, and cross-origin links', () => {
    const { getByTestId } = render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    const restoreNav = suppressNativeLinkNavigation()
    for (const id of ['blank', 'hash', 'mail', 'dl', 'cross']) {
      const evt = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
      getByTestId(id).dispatchEvent(evt)
    }
    expect(pushSpy).not.toHaveBeenCalled()
    restoreNav()
    pushSpy.mockRestore()
  })

  it('updates the context on popstate (back/forward navigation)', () => {
    render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
    history.replaceState(null, '', '/spaces/abc?z=2')
    fireEvent(window, new PopStateEvent('popstate'))
    expect(sink.current?.path).toBe('/spaces/abc')
    expect(sink.current?.query).toEqual({ z: '2' })
  })
})

describe('IngressLocationProvider (prefixed — simulated ingress)', () => {
  beforeEach(() => {
    vi.resetModules()
    resetLocation('/api/hassio_ingress/tok/feed')
  })

  afterEach(() => {
    cleanup()
    vi.resetModules()
    vi.doUnmock('@/baseUrl')
  })

  it('strips the ingress prefix off location.pathname for routing', async () => {
    vi.doMock('@/baseUrl', () => {
      const PREFIX = '/api/hassio_ingress/tok/'
      return {
        basePath: PREFIX,
        addBase: (p: string) => {
          const [pathOnly, ...rest] = p.split(/(?=[?#])/, 2)
          const suffix = rest.join('')
          const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
          return PREFIX.replace(/\/+$/, '') + normalised + suffix
        },
        stripBase: (p: string) => {
          const prefix = PREFIX.replace(/\/+$/, '')
          if (p === prefix) return '/'
          if (p.startsWith(prefix + '/')) return p.slice(prefix.length) || '/'
          return p
        },
      }
    })

    const mod = await import('./IngressLocationProvider')
    const sink: { current?: LocationSnapshot } = {}
    render(
      <mod.IngressLocationProvider>
        <Probe sink={sink} />
      </mod.IngressLocationProvider>,
    )
    expect(sink.current?.path).toBe('/feed')
    expect(sink.current?.url).toBe('/feed')
  })

  it('route() writes the prefixed URL to history.pushState', async () => {
    vi.doMock('@/baseUrl', () => {
      const PREFIX = '/api/hassio_ingress/tok/'
      return {
        basePath: PREFIX,
        addBase: (p: string) => {
          const [pathOnly, ...rest] = p.split(/(?=[?#])/, 2)
          const suffix = rest.join('')
          const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
          return PREFIX.replace(/\/+$/, '') + normalised + suffix
        },
        stripBase: (p: string) => {
          const prefix = PREFIX.replace(/\/+$/, '')
          if (p === prefix) return '/'
          if (p.startsWith(prefix + '/')) return p.slice(prefix.length) || '/'
          return p
        },
      }
    })

    const mod = await import('./IngressLocationProvider')
    const sink: { current?: LocationSnapshot } = {}
    render(
      <mod.IngressLocationProvider>
        <Probe sink={sink} />
      </mod.IngressLocationProvider>,
    )
    const pushSpy = vi.spyOn(history, 'pushState')
    sink.current!.route('/spaces/abc')
    expect(pushSpy).toHaveBeenCalledWith(null, '', '/api/hassio_ingress/tok/spaces/abc')
    pushSpy.mockRestore()
  })
})
