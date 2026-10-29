/**
 * AppHost — tests for the sandboxed iframe container.
 *
 * Key invariants verified here (§2 security):
 *   - An <iframe> is rendered once the runtime resolves.
 *   - sandbox="allow-scripts" ONLY — never allow-same-origin.
 *   - mountBridge is called exactly once.
 * And the states that say why an app isn't showing (turned off, not for
 * this account, removed, slow, didn't start), each with its next step.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/preact'

const nav = vi.hoisted(() => ({ route: vi.fn() }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: nav.route, path: '/apps/chess', url: '/apps/chess', query: {} }),
}))

const user = vi.hoisted(() => ({ currentUser: { value: { is_admin: false } as { is_admin: boolean } | null } }))
vi.mock('@/store/auth', () => ({ currentUser: user.currentUser }))

// ── Mock @/store/apps ─────────────────────────────────────────────────────
// Default: resolved runtime. Individual tests override with
// mockRejectedValueOnce for the error states.
// The catalog names the app in the viewer's language (``i18n``).
const cat = vi.hoisted(() => ({
  catalog: { value: [] as { app_id: string, name: string, i18n?: Record<string, { name: string }> }[] },
  catalogLoaded: { value: true },
}))
vi.mock('@/store/apps', () => ({
  getRuntime: vi.fn(),
  setEnabled: vi.fn().mockResolvedValue(undefined),
  catalog: cat.catalog,
  catalogLoaded: cat.catalogLoaded,
  loadCatalog: vi.fn().mockResolvedValue(undefined),
  localized: (e: { name: string, i18n?: Record<string, { name: string }> }, lang: string) => ({
    name: e.i18n?.[lang]?.name ?? e.name,
    description: '',
  }),
}))

// ── Mock @/features/apps/bridge ──────────────────────────────────────────
// mountBridge must be called once and returns a no-op cleanup.
vi.mock('@/features/apps/bridge', () => ({
  mountBridge: vi.fn().mockReturnValue(() => {}),
}))

// ── Mock the invite sheet: the host's own picker (D3) ──────────────────
// A stub that shows what it was asked and lets a test Send or Cancel.
vi.mock('@/features/apps/InviteSheet', () => ({
  InviteSheet: ({ appName, request, onDone }: {
    appName: string
    request: { max: number }
    onDone: (p: unknown[]) => void
  }) => (
    <div data-testid="invite-sheet" data-app={appName} data-max={request.max}>
      <button onClick={() => onDone([{ instance_id: 'brookside', user_ref: 'ben', is_local: false, display_name: 'Ben', session_id: 's9' }])}>Send</button>
      <button onClick={() => onDone([])}>Cancel</button>
    </div>
  ),
}))

// ── Mock @/ws ─────────────────────────────────────────────────────────────
// Captures the host's ``app.uninstalled`` / ``app.disabled`` listeners so a
// test can play the frame an admin's action sends.
type Frame = { type: string, data: Record<string, unknown> }
const wsh = vi.hoisted(() => ({ handlers: {} as Record<string, Set<(e: Frame) => void>> }))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: Frame) => void) => {
      (wsh.handlers[type] ??= new Set()).add(h)
      return () => { wsh.handlers[type]?.delete(h) }
    },
  },
}))
function playFrame(type: string, appId: string) {
  wsh.handlers[type]?.forEach(h => h({ type, data: { type, app_id: appId } }))
}

const back = vi.hoisted(() => ({ from: null as string | null }))
vi.mock('@/features/apps/appReturn', () => ({ appOpenedFrom: () => back.from }))

// ── Mock @/baseUrl ────────────────────────────────────────────────────────
// addBase is used for src and the back-button href. Return the input
// unchanged so the iframe src is predictable in assertions.
vi.mock('@/baseUrl', () => ({
  addBase: (path: string) => path,
  basePath: '/',
}))

// ── Mock @/components ─────────────────────────────────────────────────────
vi.mock('@/components/Spinner', () => ({
  Spinner: ({ label }: { label?: string }) => <span role="status" aria-label={label} />,
}))
vi.mock('@/components/Button', () => ({
  Button: ({ children, onClick }: { children?: import('preact').ComponentChildren; onClick?: () => void }) => (
    <button onClick={onClick}>{children}</button>
  ),
}))

import { AppHostInner, FAIL_MS, LOAD_GRACE_MS, SLOW_MS, launchSessionFrom } from './AppHost'
import { mountBridge, type BridgeContext } from '@/features/apps/bridge'
import { getRuntime, loadCatalog, setEnabled } from '@/store/apps'
import { locale } from '@/i18n/i18n'
import { ApiError } from '@/api'

const RUNTIME = {
  app_id: 'chess',
  name: 'Chess',
  entry_url: '/api/apps/chess/bundle/index.html?exp=1&sig=x',
  self_user_id: 'u1',
  capabilities: ['storage', 'federation'],
  permissions: ['storage', 'people', 'federation'],
}

/** The ``onReady`` the host handed the bridge on its latest mount. */
function bridgeReady(): () => void {
  const calls = vi.mocked(mountBridge).mock.calls
  const ctx = calls[calls.length - 1][1] as BridgeContext
  return ctx.onReady!
}

describe('AppHostInner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getRuntime).mockResolvedValue(RUNTIME)
    vi.mocked(mountBridge).mockReturnValue(() => {})
    user.currentUser.value = { is_admin: false }
    back.from = null
    cat.catalog.value = []
    cat.catalogLoaded.value = true
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("names the app in the viewer's language everywhere in its chrome", async () => {
    cat.catalog.value = [{ app_id: 'chess', name: 'Chess', i18n: { de: { name: 'Schach' } } }]
    const was = locale.value
    locale.value = 'de'
    try {
      const { container } = render(<AppHostInner appId="chess" />)
      await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
      expect(container.querySelector('.sh-app-host__title')!.textContent).toBe('Schach')
      expect(container.querySelector('iframe')!.getAttribute('title')).toBe('Schach')
    } finally {
      locale.value = was
    }
  })

  it('keeps the installed name when the catalog has no entry, and loads it once', async () => {
    cat.catalogLoaded.value = false
    const { container } = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    expect(container.querySelector('.sh-app-host__title')!.textContent).toBe('Chess')
    expect(vi.mocked(loadCatalog)).toHaveBeenCalledOnce()
  })

  it('renders an <iframe> once the runtime resolves', async () => {
    const { container } = render(<AppHostInner appId="chess" />)

    // Wait for getRuntime promise to resolve and iframe to appear.
    await waitFor(() => {
      const iframe = container.querySelector('iframe')
      expect(iframe).not.toBeNull()
    })
  })

  it('sandbox attribute is "allow-scripts" only — never allow-same-origin (§2 invariant)', async () => {
    const { container } = render(<AppHostInner appId="chess" />)

    await waitFor(() => {
      const iframe = container.querySelector('iframe')
      expect(iframe).not.toBeNull()
      const sandbox = iframe!.getAttribute('sandbox') ?? ''
      // Must contain allow-scripts.
      expect(sandbox).toContain('allow-scripts')
      // CRITICAL: must NOT contain allow-same-origin — it would collapse
      // the iframe origin boundary, exposing localStorage (bearer token).
      expect(sandbox).not.toContain('allow-same-origin')
    })
  })

  it('calls mountBridge exactly once after runtime resolves', async () => {
    render(<AppHostInner appId="chess" />)

    await waitFor(() => {
      expect(vi.mocked(mountBridge)).toHaveBeenCalledOnce()
    })
  })

  it('hands the bridge the permissions the household agreed to', async () => {
    render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalledOnce())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    expect(ctx.permissions).toEqual(['storage', 'people', 'federation'])
  })

  it('a runtime answer without permissions grants the bridge nothing', async () => {
    const { permissions: _drop, ...older } = RUNTIME
    vi.mocked(getRuntime).mockResolvedValue(older as typeof RUNTIME)
    render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalledOnce())
    expect((vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext).permissions).toEqual([])
  })

  it('hands the bridge the look: language, theme, tokens and a watcher (D14)', async () => {
    render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalledOnce())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    expect(ctx.hostContext!()).toMatchObject({ locale: 'en', dir: 'ltr', theme: 'light' })
    expect(typeof ctx.watchHostContext).toBe('function')
  })

  it('the frame’s color-scheme follows the SPA’s light/dark, live — native controls in the app follow', async () => {
    const root = document.documentElement
    root.classList.add('sh-theme-light')
    try {
      const { container } = render(<AppHostInner appId="chess" />)
      await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
      expect(container.querySelector('iframe')!.style.colorScheme).toBe('light')
      await act(async () => {
        root.classList.replace('sh-theme-light', 'sh-theme-dark')
        await new Promise(r => setTimeout(r, 0))
      })
      expect(container.querySelector('iframe')!.style.colorScheme).toBe('dark')
    } finally {
      root.classList.remove('sh-theme-light', 'sh-theme-dark')
    }
  })

  it('the app name is the host heading, under the topbar\'s h1', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.getByRole('heading', { level: 2, name: 'Chess' })).toBeTruthy())
  })

  it('Back from a deep link goes to Apps in place (no history entry to come back to)', async () => {
    // A reload used to "tear down" the sandbox. Unmounting already does:
    // the iframe leaves the DOM (its browsing context is discarded) and
    // the bridge cleanup drops the message listener + the WS relay.
    const cleanup = vi.fn()
    vi.mocked(mountBridge).mockReturnValue(cleanup)
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalled())
    fireEvent.click(view.getByText(/Back/))
    expect(nav.route).toHaveBeenCalledWith('/apps', true)
    view.unmount()
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    expect(document.querySelector('iframe.sh-app-frame')).toBeNull()
  })

  it('Back returns to the page the app was opened from — one history step, not a push', async () => {
    // Pushing /apps left the app one browser-Back away: Back, then the
    // browser's Back, landed in the app again.
    back.from = '/notifications'
    const historyBack = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalled())
    fireEvent.click(view.getByText(/Back/))
    expect(historyBack).toHaveBeenCalledOnce()
    expect(nav.route).not.toHaveBeenCalled()
    historyBack.mockRestore()
  })

  it('answers a bridge request the app sends as soon as its frame is in the page (cached bundle race)', async () => {
    // A cached bundle (or arriving from another page) can run the app's
    // code before a deferred effect fires. Its first request — the SDK
    // never retries it — must still be answered, so the host listens from
    // the moment the frame is committed, not after the next paint.
    const actual = await vi.importActual<typeof import('@/features/apps/bridge')>(
      '@/features/apps/bridge',
    )
    vi.mocked(mountBridge).mockImplementationOnce(actual.mountBridge)
    const { container } = render(<AppHostInner appId="chess" />)

    // Only microtasks: the runtime resolves and the frame commits, but no
    // paint / timer has run yet — exactly when a cached app starts.
    let iframe: HTMLIFrameElement | null = null
    for (let i = 0; i < 50 && !iframe; i++) {
      await Promise.resolve()
      iframe = container.querySelector('iframe')
    }
    expect(iframe).not.toBeNull()
    const appWindow = iframe!.contentWindow!
    const post = vi.spyOn(appWindow, 'postMessage').mockImplementation(() => {})

    window.dispatchEvent(
      new MessageEvent('message', {
        source: appWindow,
        data: { id: 1, method: 'app.context' },
      }),
    )

    await waitFor(() => expect(post).toHaveBeenCalledWith(
      {
        id: 1,
        ok: true,
        result: expect.objectContaining({ appId: 'chess', selfUserId: 'u1', permissions: ['storage', 'people', 'federation'], launch: {}, theme: 'light' }),
      },
      '*',
    ))
  })

  // ── The invite sheet (D3): drawn by the host, outside the frame ───────

  it('people.pick shows Social Home\'s sheet outside the frame and hands back who was picked', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    let picked: Promise<unknown> | undefined
    act(() => { picked = ctx.pickPeople!({ purpose: 'play', max: 1 }) })
    const sheet = await view.findByTestId('invite-sheet')
    // Not inside the app's frame: the host draws it, the app can't touch it.
    expect(view.container.querySelector('iframe')!.contains(sheet)).toBe(false)
    expect(sheet.dataset.app).toBe('Chess')
    fireEvent.click(view.getByText('Send'))
    await expect(picked).resolves.toEqual([
      { instance_id: 'brookside', user_ref: 'ben', is_local: false, display_name: 'Ben', session_id: 's9' },
    ])
    expect(view.queryByTestId('invite-sheet')).toBeNull()
  })

  it('a sheet goes with its frame: leaving the app hands back nobody', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    let picked: Promise<unknown> | undefined
    act(() => { picked = ctx.pickPeople!({ purpose: 'play', max: 1 }) })
    await view.findByTestId('invite-sheet')
    view.unmount()
    await expect(picked).resolves.toEqual([])
  })

  // ── Launch context: Play opens the game you were invited to ───────────

  it('hands the bridge the ?session= invite to launch into', async () => {
    const { container } = render(<AppHostInner appId="chess" sessionId="s1" />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    expect(ctx.launchSession).toBe('s1')
  })

  it('opened without ?session= launches into nothing', async () => {
    const { container } = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const ctx = vi.mocked(mountBridge).mock.calls[0][1] as BridgeContext
    expect(ctx.launchSession).toBeUndefined()
  })

  it('Play for another game while the app is open starts a fresh frame for it', async () => {
    const view = render(<AppHostInner appId="chess" sessionId="s1" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    const first = view.container.querySelector('iframe')
    view.rerender(<AppHostInner appId="chess" sessionId="s2" />)
    await waitFor(() => {
      const calls = vi.mocked(mountBridge).mock.calls
      expect((calls[calls.length - 1][1] as BridgeContext).launchSession).toBe('s2')
    })
    // A new element (no iframe history entry), on a fresh signed runtime.
    expect(view.container.querySelector('iframe')).not.toBe(first)
    expect(getRuntime).toHaveBeenCalledTimes(2)
  })

  it('reads the launch session from ?session= — a session id only', () => {
    expect(launchSessionFrom({ session: '0f3a9c2b' })).toBe('0f3a9c2b')
    expect(launchSessionFrom({})).toBeUndefined()
    expect(launchSessionFrom({ session: '' })).toBeUndefined()
    expect(launchSessionFrom({ session: '../x' })).toBeUndefined()
    expect(launchSessionFrom({ session: '<b>' })).toBeUndefined()
    expect(launchSessionFrom({ session: 'a'.repeat(129) })).toBeUndefined()
  })

  // ── Starting → ready / slow / failed ──────────────────────────────────

  it('keeps the frame hidden until the app speaks, then shows it', async () => {
    // A bundle error body (raw JSON / "not found") served in the frame's
    // place must never be what the user sees.
    const { container, getByText } = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const iframe = container.querySelector('iframe')!
    expect(iframe.classList.contains('sh-app-frame--pending')).toBe(true)
    expect(iframe.getAttribute('aria-hidden')).toBe('true')
    expect(getByText('Starting Chess…')).toBeTruthy()

    act(() => bridgeReady()())

    expect(iframe.classList.contains('sh-app-frame--pending')).toBe(false)
    expect(iframe.getAttribute('aria-hidden')).toBeNull()
    expect(container.querySelector('.sh-app-host__status')).toBeNull()
  })

  it('says "taking longer than usual" with Try again after SLOW_MS, and still opens if the app then speaks', async () => {
    vi.useFakeTimers()
    const view = render(<AppHostInner appId="chess" />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(view.container.querySelector('iframe')).not.toBeNull()

    await act(async () => { await vi.advanceTimersByTimeAsync(SLOW_MS) })
    expect(view.getByRole('heading', { name: 'Chess is taking longer than usual' })).toBeTruthy()
    expect(view.getByRole('button', { name: 'Try again' })).toBeTruthy()

    act(() => bridgeReady()())
    expect(view.queryByText('Chess is taking longer than usual')).toBeNull()
    // No late "didn't start" once it is running.
    await act(async () => { await vi.advanceTimersByTimeAsync(FAIL_MS) })
    expect(view.queryByText("Chess didn't start")).toBeNull()
  })

  it('says "didn\'t start" after FAIL_MS with Try again + Back to Apps, and drops the silent frame', async () => {
    vi.useFakeTimers()
    const view = render(<AppHostInner appId="chess" />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { await vi.advanceTimersByTimeAsync(FAIL_MS) })
    expect(view.getByRole('heading', { name: "Chess didn't start" })).toBeTruthy()
    expect(view.getByRole('alert').textContent).toContain('an admin can reinstall it from Apps')
    expect(view.container.querySelector('iframe')).toBeNull()

    fireEvent.click(view.getByRole('button', { name: 'Back to Apps' }))
    expect(nav.route).toHaveBeenCalledWith('/apps')
  })

  it('a frame that loaded but never spoke (a bundle error page) fails after LOAD_GRACE_MS', async () => {
    vi.useFakeTimers()
    const view = render(<AppHostInner appId="chess" />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    fireEvent.load(view.container.querySelector('iframe')!)
    await act(async () => { await vi.advanceTimersByTimeAsync(LOAD_GRACE_MS) })
    expect(view.getByRole('heading', { name: "Chess didn't start" })).toBeTruthy()
  })

  it('Try again fetches a fresh runtime and mounts a new frame', async () => {
    vi.useFakeTimers()
    const view = render(<AppHostInner appId="chess" />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { await vi.advanceTimersByTimeAsync(FAIL_MS) })
    expect(getRuntime).toHaveBeenCalledTimes(1)

    fireEvent.click(view.getByRole('button', { name: 'Try again' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(getRuntime).toHaveBeenCalledTimes(2)
    expect(view.container.querySelector('iframe')).not.toBeNull()
    expect(view.getByText('Starting Chess…')).toBeTruthy()
  })

  // ── Why it won't open ────────────────────────────────────────────────

  it('turned off, seen by a member: says an admin turned it off — not the age text', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(403, '/api/apps/chess/runtime', { code: 'APP_NOT_ENABLED', detail: 'App is disabled.', name: 'Chess' }),
    )
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.getByRole('heading', { name: 'An admin turned Chess off' })).toBeTruthy())
    expect(view.container.textContent).not.toContain("isn't available for your account")
    expect(view.queryByRole('button', { name: /Turn on/ })).toBeNull()
    expect(view.container.querySelector('iframe')).toBeNull()
    expect(vi.mocked(mountBridge)).not.toHaveBeenCalled()
  })

  it('turned off, seen by an admin: offers Turn on, which enables it and starts the app', async () => {
    user.currentUser.value = { is_admin: true }
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(403, '/api/apps/chess/runtime', { code: 'APP_NOT_ENABLED', detail: 'App is disabled.', name: 'Chess' }),
    )
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.getByRole('heading', { name: 'Chess is turned off for this household' })).toBeTruthy())

    fireEvent.click(view.getByRole('button', { name: 'Turn on Chess' }))
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    expect(setEnabled).toHaveBeenCalledWith('chess', true)
  })

  it('age-restricted: keeps the "not available for your account" text, no frame', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(403, '/api/apps/locked/runtime', { code: 'APP_AGE_RESTRICTED', detail: 'Age restricted' }),
    )
    const { container, getByRole } = render(<AppHostInner appId="locked" />)

    await waitFor(() => {
      expect(getByRole('heading', { name: "This app isn't available for your account." })).toBeTruthy()
    })

    // No iframe must be mounted.
    expect(container.querySelector('iframe')).toBeNull()
    // mountBridge must NOT have been called.
    expect(vi.mocked(mountBridge)).not.toHaveBeenCalled()
  })

  it('removed (404, a stale link): says it isn\'t installed any more, with Back to Apps', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(404, '/api/apps/gone/runtime', { code: 'NOT_FOUND', detail: 'App not found.' }),
    )
    const view = render(<AppHostInner appId="gone" />)
    await waitFor(() => expect(view.getByRole('heading', { name: "This app isn't installed any more" })).toBeTruthy())
    expect(view.container.textContent).not.toContain('App not found.')
    expect(view.getByRole('button', { name: 'Back to Apps' })).toBeTruthy()
  })

  // ── Live: an admin removes or turns off the open app ──────────────────

  it('uninstalled while open: the frame goes and the tab says it isn\'t installed any more', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(404, '/api/apps/chess/runtime', { code: 'NOT_FOUND', detail: 'App not found.' }),
    )
    act(() => playFrame('app.uninstalled', 'chess'))
    await waitFor(() => expect(view.getByRole('heading', { name: "This app isn't installed any more" })).toBeTruthy())
    expect(view.container.querySelector('iframe')).toBeNull()
  })

  it('turned off while open: a member sees that an admin turned it off', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(403, '/api/apps/chess/runtime', { code: 'APP_NOT_ENABLED', detail: 'App is disabled.', name: 'Chess' }),
    )
    act(() => playFrame('app.disabled', 'chess'))
    await waitFor(() => expect(view.getByRole('heading', { name: 'An admin turned Chess off' })).toBeTruthy())
    expect(view.container.querySelector('iframe')).toBeNull()
  })

  it('a frame about another app leaves the open one alone', async () => {
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(view.container.querySelector('iframe')).not.toBeNull())
    act(() => playFrame('app.uninstalled', 'go'))
    expect(getRuntime).toHaveBeenCalledTimes(1)
    expect(view.container.querySelector('iframe')).not.toBeNull()
  })

  it('a failed runtime fetch is the "didn\'t start" state, with the translated server line', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(500, '/api/apps/broken/runtime', { code: 'INTERNAL_ERROR', detail: 'Unexpected error' }),
    )
    const { container, getByText, getByRole } = render(<AppHostInner appId="broken" />)

    await waitFor(() => {
      // A 5xx shows the translated server line, not the raw detail.
      expect(getByText('Something went wrong on the server. Try again in a moment.')).toBeTruthy()
    })
    expect(getByRole('heading', { name: "The app didn't start" })).toBeTruthy()
    expect(getByRole('button', { name: 'Try again' })).toBeTruthy()

    expect(container.querySelector('iframe')).toBeNull()
    // Generic error: age-restricted message must NOT appear.
    expect(container.textContent).not.toContain("This app isn't available for your account.")
  })
})
