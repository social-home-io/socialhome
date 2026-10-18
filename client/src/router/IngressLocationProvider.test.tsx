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
import { setNavigationGuard } from './navigationGuard'

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

describe('IngressLocationProvider — unsaved-change guard', () => {
  let sink: { current?: LocationSnapshot }
  let release: () => void
  let answer: (ok: boolean) => void
  const confirm = vi.fn(() => new Promise<boolean>((resolve) => { answer = resolve }))

  beforeEach(() => {
    resetLocation('/settings')
    sink = {}
    confirm.mockClear()
    release = setNavigationGuard({ path: '/settings', isDirty: () => true, confirm })
  })

  afterEach(() => {
    release()
    cleanup()
  })

  function mount() {
    render(
      <IngressLocationProvider>
        <Probe sink={sink} />
      </IngressLocationProvider>,
    )
  }

  it('route() waits for the page to agree before leaving', async () => {
    mount()
    sink.current!.route('/feed')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(sink.current?.path).toBe('/settings')
    answer(true)
    await vi.waitFor(() => expect(sink.current?.path).toBe('/feed'))
    expect(location.pathname).toBe('/feed')
  })

  it('stays put when the user keeps editing', async () => {
    mount()
    fireEvent.click(document.querySelector('[data-testid="feed"]')!)
    expect(confirm).toHaveBeenCalledTimes(1)
    answer(false)
    await Promise.resolve()
    expect(sink.current?.path).toBe('/settings')
    expect(location.pathname).toBe('/settings')
  })

  it('a hop within the same page (hash / query) never asks', async () => {
    mount()
    sink.current!.route('/settings?tab=privacy')
    expect(confirm).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(sink.current?.url).toBe('/settings?tab=privacy'))
  })

  it('Back puts the page back in the address bar until the user agrees', async () => {
    mount()
    history.replaceState(null, '', '/feed')
    fireEvent(window, new PopStateEvent('popstate'))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(location.pathname).toBe('/settings')
    expect(sink.current?.path).toBe('/settings')
    answer(true)
    await vi.waitFor(() => expect(sink.current?.path).toBe('/feed'))
    expect(location.pathname).toBe('/feed')
  })

  it('a clean page leaves without asking', async () => {
    release()
    release = setNavigationGuard({ path: '/settings', isDirty: () => false, confirm })
    mount()
    sink.current!.route('/feed')
    expect(confirm).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(sink.current?.path).toBe('/feed'))
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

  it('unsaved-change guard: Back re-pushes the ingress-prefixed page, then follows once agreed', async () => {
    resetLocation('/api/hassio_ingress/tok/settings')
    vi.doMock('@/baseUrl', () => {
      const PREFIX = '/api/hassio_ingress/tok/'
      return {
        basePath: PREFIX,
        addBase: (p: string) => PREFIX.replace(/\/+$/, '') + (p.startsWith('/') ? p : '/' + p),
        stripBase: (p: string) => {
          const prefix = PREFIX.replace(/\/+$/, '')
          if (p === prefix) return '/'
          if (p.startsWith(prefix + '/')) return p.slice(prefix.length) || '/'
          return p
        },
      }
    })
    const mod = await import('./IngressLocationProvider')
    const guard = await import('./navigationGuard')
    let answer: (ok: boolean) => void = () => {}
    const release = guard.setNavigationGuard({
      path: '/settings',
      isDirty: () => true,
      confirm: () => new Promise<boolean>((resolve) => { answer = resolve }),
    })
    const sink: { current?: LocationSnapshot } = {}
    render(
      <mod.IngressLocationProvider>
        <Probe sink={sink} />
      </mod.IngressLocationProvider>,
    )
    history.replaceState(null, '', '/api/hassio_ingress/tok/feed')
    fireEvent(window, new PopStateEvent('popstate'))
    // Never the bare '/settings' — that would bounce the iframe out of ingress.
    expect(location.pathname).toBe('/api/hassio_ingress/tok/settings')
    expect(sink.current?.path).toBe('/settings')
    answer(true)
    await vi.waitFor(() => expect(sink.current?.path).toBe('/feed'))
    expect(location.pathname).toBe('/api/hassio_ingress/tok/feed')
    release()
  })
})

describe('space sections under the ingress prefix (/spaces/:id/:section?)', () => {
  const PREFIX = '/api/hassio_ingress/tok/'
  beforeEach(() => {
    vi.resetModules()
    vi.doMock('@/baseUrl', () => ({
      basePath: PREFIX,
      addBase: (p: string) => PREFIX.replace(/\/+$/, '') + (p.startsWith('/') ? p : '/' + p),
      stripBase: (p: string) => {
        const prefix = PREFIX.replace(/\/+$/, '')
        if (p === prefix) return '/'
        if (p.startsWith(prefix + '/')) return p.slice(prefix.length) || '/'
        return p
      },
    }))
  })

  afterEach(() => {
    cleanup()
    vi.resetModules()
    vi.doUnmock('@/baseUrl')
  })

  /** The app's route order for a space: fixed sub-pages first, then the
   *  one optional-section route the page itself reads. */
  async function mountSpace(at: string) {
    resetLocation(at)
    const mod = await import('./IngressLocationProvider')
    const iso = await import('preact-iso')
    const base = await import('@/baseUrl')
    const mounts = { space: 0 }
    function Space(_: { path: string }) {
      const { params } = iso.useRoute()
      useEffect(() => { mounts.space += 1 }, [])
      return (
        <div>
          <p data-testid="section">{`${params.id}:${params.section ?? 'feed'}`}</p>
          <a href={base.addBase(`/spaces/${params.id}/chat`)} data-testid="to-chat">Chat</a>
        </div>
      )
    }
    const About = (_: { path: string }) => <p data-testid="section">about</p>
    const r = render(
      <mod.IngressLocationProvider>
        <iso.Router>
          <About path="/spaces/:id/about" />
          <Space path="/spaces/:id/:section?" />
        </iso.Router>
      </mod.IngressLocationProvider>,
    )
    return { r, mounts }
  }

  it('a cold load of an ingress-prefixed section path opens that section', async () => {
    const { r } = await mountSpace('/api/hassio_ingress/tok/spaces/abc/calendar')
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('abc:calendar'))
  })

  it('the bare space path under the prefix is the feed; a fixed sub-page still wins', async () => {
    let { r } = await mountSpace('/api/hassio_ingress/tok/spaces/abc')
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('abc:feed'))
    cleanup()
    vi.resetModules()
    ;({ r } = await mountSpace('/api/hassio_ingress/tok/spaces/abc/about'))
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('about'))
  })

  it('a section link keeps the ingress prefix in the address bar and re-renders the same page', async () => {
    const { r, mounts } = await mountSpace('/api/hassio_ingress/tok/spaces/abc')
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('abc:feed'))
    const link = r.getByTestId('to-chat')
    expect(link.getAttribute('href')).toBe('/api/hassio_ingress/tok/spaces/abc/chat')
    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('abc:chat'))
    expect(location.pathname).toBe('/api/hassio_ingress/tok/spaces/abc/chat')
    // Back keeps the section too.
    history.replaceState(null, '', '/api/hassio_ingress/tok/spaces/abc')
    fireEvent(window, new PopStateEvent('popstate'))
    await vi.waitFor(() => expect(r.getByTestId('section').textContent).toBe('abc:feed'))
    // One mount: switching sections never remounts (and refetches) the page.
    expect(mounts.space).toBe(1)
  })
})
