/**
 * AppHost — tests for the sandboxed iframe container.
 *
 * Key invariants verified here (§2 security):
 *   - An <iframe> is rendered once the runtime resolves.
 *   - sandbox="allow-scripts" ONLY — never allow-same-origin.
 *   - mountBridge is called exactly once.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

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
      new ApiError(500, '/api/apps/broken/runtime', { code: 'SERVER_ERROR', detail: 'Unexpected error' }),
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
