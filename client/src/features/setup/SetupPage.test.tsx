/**
 * Tests for the haos welcome flow — the operator should see a real
 * welcome card before the supervisor handshake fires, not a silent
 * spinner. The other two variants (standalone, ha) have happy-path
 * coverage on the backend; the SetupPage shell is just composition
 * around the same Wordmark/Button/FormError components covered
 * elsewhere.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

const haosInstanceCfg = {
  value: {
    mode: 'haos' as const,
    instance_name: 'Home',
    capabilities: ['ingress', 'push', 'ai', 'ha_person_directory'],
    setup_required: true,
  },
}

describe('SetupPage haos welcome', () => {
  it('renders the welcome screen and only POSTs after Continue', async () => {
    const post = vi.fn(async () => ({ username: 'pascal' }))
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    vi.doMock('@/store/instance', () => ({
      instanceConfig: haosInstanceCfg,
      loadInstanceConfig: vi.fn(async () => haosInstanceCfg.value),
    }))
    vi.doMock('@/store/auth', () => ({ setToken: vi.fn() }))
    vi.doMock('@/components/Toast', () => ({ showToast: vi.fn() }))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, queryByText } = render(<SetupPage />)

    expect(await findByText('Welcome to your home')).toBeTruthy()
    expect(post).not.toHaveBeenCalled()
    expect(queryByText(/Detecting your Home Assistant owner/)).toBeNull()

    const cta = await findByText("Let’s go")
    fireEvent.click(cta)
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith('/api/setup/haos/complete')
  })

  it('shows the error inline when the supervisor handshake fails', async () => {
    const post = vi.fn(async () => { throw new Error('boom') })
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    vi.doMock('@/store/instance', () => ({
      instanceConfig: haosInstanceCfg,
      loadInstanceConfig: vi.fn(async () => haosInstanceCfg.value),
    }))
    vi.doMock('@/store/auth', () => ({ setToken: vi.fn() }))
    vi.doMock('@/components/Toast', () => ({ showToast: vi.fn() }))
    const { SetupPage } = await import('./SetupPage')
    const { findByText } = render(<SetupPage />)
    fireEvent.click(await findByText("Let’s go"))
    await new Promise((r) => setTimeout(r, 20))
    expect(await findByText('boom')).toBeTruthy()
  })
})
