/**
 * AppHost — tests for the sandboxed iframe container.
 *
 * Key invariants verified here (§2 security):
 *   - An <iframe> is rendered once the runtime resolves.
 *   - sandbox="allow-scripts" ONLY — never allow-same-origin.
 *   - mountBridge is called exactly once.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const nav = vi.hoisted(() => ({ route: vi.fn() }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: nav.route, path: '/apps/chess', url: '/apps/chess', query: {} }),
}))

// ── Mock @/store/apps ─────────────────────────────────────────────────────
// getRuntime is the only export AppHostInner calls. Default: resolved runtime.
// Individual tests override with mockRejectedValueOnce for error paths.
vi.mock('@/store/apps', () => ({
  getRuntime: vi.fn().mockResolvedValue({
    app_id: 'chess',
    name: 'Chess',
    entry_url: '/api/apps/chess/bundle/index.html?exp=1&sig=x',
    self_user_id: 'u1',
    capabilities: [],
  }),
}))

// ── Mock @/features/apps/bridge ──────────────────────────────────────────
// mountBridge must be called once and returns a no-op cleanup.
vi.mock('@/features/apps/bridge', () => ({
  mountBridge: vi.fn().mockReturnValue(() => {}),
}))

// ── Mock @/baseUrl ────────────────────────────────────────────────────────
// addBase is used for src and the back-button href. Return the input
// unchanged so the iframe src is predictable in assertions.
vi.mock('@/baseUrl', () => ({
  addBase: (path: string) => path,
  basePath: '/',
}))

// ── Mock @/components ─────────────────────────────────────────────────────
vi.mock('@/components/Spinner', () => ({
  Spinner: ({ label }: { label: string }) => <span>{label}</span>,
}))
vi.mock('@/components/Button', () => ({
  Button: ({ children, onClick }: { children?: import('preact').ComponentChildren; onClick?: () => void }) => (
    <button onClick={onClick}>{children}</button>
  ),
}))

import { AppHostInner } from './AppHost'
import { mountBridge } from '@/features/apps/bridge'
import { getRuntime } from '@/store/apps'
import { ApiError } from '@/api'

describe('AppHostInner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

  it('Back navigates in the app (no page load) and leaving tears the app down', async () => {
    // A reload used to "tear down" the sandbox. Unmounting already does:
    // the iframe leaves the DOM (its browsing context is discarded) and
    // the bridge cleanup drops the message listener + the WS relay.
    const cleanup = vi.fn()
    vi.mocked(mountBridge).mockReturnValue(cleanup)
    const view = render(<AppHostInner appId="chess" />)
    await waitFor(() => expect(vi.mocked(mountBridge)).toHaveBeenCalled())
    fireEvent.click(view.getByText(/Back/))
    expect(nav.route).toHaveBeenCalledWith('/apps')
    view.unmount()
    await waitFor(() => expect(cleanup).toHaveBeenCalled())
    expect(document.querySelector('iframe.sh-app-frame')).toBeNull()
    vi.mocked(mountBridge).mockReturnValue(() => {})
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

    expect(post).toHaveBeenCalledWith(
      { id: 1, ok: true, result: { appId: 'chess', selfUserId: 'u1' } },
      '*',
    )
  })

  it('shows age-restricted message and no iframe when getRuntime rejects with 403', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(403, '/api/apps/locked/runtime', { code: 'FORBIDDEN', detail: 'Age restricted' }),
    )
    const { container, getByText } = render(<AppHostInner appId="locked" />)

    await waitFor(() => {
      expect(getByText("This app isn't available for your account.")).toBeTruthy()
    })

    // No iframe must be mounted.
    expect(container.querySelector('iframe')).toBeNull()
    // mountBridge must NOT have been called.
    expect(vi.mocked(mountBridge)).not.toHaveBeenCalled()
  })

  it('shows generic error and no iframe for non-403 rejections', async () => {
    vi.mocked(getRuntime).mockRejectedValueOnce(
      new ApiError(500, '/api/apps/broken/runtime', { code: 'INTERNAL_ERROR', detail: 'Unexpected error' }),
    )
    const { container, getByText } = render(<AppHostInner appId="broken" />)

    await waitFor(() => {
      // A 5xx shows the translated server line, not the raw detail.
      expect(getByText('Something went wrong on the server. Try again in a moment.')).toBeTruthy()
    })

    expect(container.querySelector('iframe')).toBeNull()
    // Generic error: age-restricted message must NOT appear.
    expect(container.textContent).not.toContain("This app isn't available for your account.")
  })
})
