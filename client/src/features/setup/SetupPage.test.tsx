/**
 * Tests for the first-boot setup wizard. The headline assertion across
 * every mode: the operator sees a real welcome card BEFORE any
 * mode-specific form / handshake — no silent spinners, no instant
 * forms, just a "this is what's about to happen" preface.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

function buildCfg(mode: 'standalone' | 'ha' | 'haos') {
  return {
    value: {
      mode,
      instance_name: 'Home',
      capabilities: ['ingress', 'push', 'ai', 'ha_person_directory'],
      setup_required: true,
    },
  }
}

function commonMocks(cfg: ReturnType<typeof buildCfg>) {
  vi.doMock('@/store/instance', () => ({
    instanceConfig: cfg,
    loadInstanceConfig: vi.fn(async () => cfg.value),
  }))
  vi.doMock('@/store/auth', () => ({ setToken: vi.fn() }))
  vi.doMock('@/components/Toast', () => ({ showToast: vi.fn() }))
}

// ── haos ────────────────────────────────────────────────────────────────────

describe('SetupPage haos welcome', () => {
  it('renders the welcome card, then a name step, and only POSTs from the name step', async () => {
    const post = vi.fn(async () => ({ username: 'pascal' }))
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    commonMocks(buildCfg('haos'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, queryByText, container } = render(<SetupPage />)

    expect(await findByText('Welcome to your home')).toBeTruthy()
    expect(post).not.toHaveBeenCalled()
    expect(queryByText(/Detecting your Home Assistant owner/)).toBeNull()

    // Welcome → "Let’s go" → name step (no POST yet).
    fireEvent.click(await findByText("Let’s go"))
    expect(await findByText('Name your home')).toBeTruthy()
    expect(post).not.toHaveBeenCalled()

    // Type a name and submit.
    const input = container.querySelector('input[name="household_name"]') as HTMLInputElement
    expect(input).toBeTruthy()
    fireEvent.input(input, { target: { value: 'Hearth' } })
    fireEvent.click(await findByText('Continue'))
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith('/api/setup/haos/complete', { household_name: 'Hearth' })
  })

  it('omits household_name when the operator leaves it blank', async () => {
    const post = vi.fn(async () => ({ username: 'pascal' }))
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    commonMocks(buildCfg('haos'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText } = render(<SetupPage />)
    fireEvent.click(await findByText("Let’s go"))
    fireEvent.click(await findByText('Continue'))
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith('/api/setup/haos/complete', {})
  })

  it('shows the error inline when the supervisor handshake fails', async () => {
    const post = vi.fn(async () => { throw new Error('boom') })
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    commonMocks(buildCfg('haos'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText } = render(<SetupPage />)
    fireEvent.click(await findByText("Let’s go"))
    fireEvent.click(await findByText('Continue'))
    await new Promise((r) => setTimeout(r, 20))
    expect(await findByText('boom')).toBeTruthy()
  })
})

// ── standalone ──────────────────────────────────────────────────────────────

describe('SetupPage standalone welcome', () => {
  it('shows welcome first, advances to the admin form on Continue', async () => {
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
    }))
    commonMocks(buildCfg('standalone'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, queryByLabelText } = render(<SetupPage />)

    expect(await findByText('Welcome to your home')).toBeTruthy()
    // The username form is NOT visible yet.
    expect(queryByLabelText('Username')).toBeNull()

    fireEvent.click(await findByText("Let’s get started"))
    // After the welcome, the username field appears.
    expect(await findByText('Set up Social Home')).toBeTruthy()
  })

  it('renders a household name field and forwards it to the setup endpoint', async () => {
    const post = vi.fn(async () => ({ token: 'tok' }))
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(), post, delete: vi.fn() },
    }))
    vi.doMock('@/store/auth', () => ({
      setToken: vi.fn(),
      loadCurrentUser: vi.fn(async () => null),
    }))
    commonMocks(buildCfg('standalone'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, container } = render(<SetupPage />)

    fireEvent.click(await findByText("Let’s get started"))
    const nameInput = container.querySelector('input[name="household_name"]') as HTMLInputElement
    expect(nameInput).toBeTruthy()
    fireEvent.input(nameInput, { target: { value: 'The Rivendells' } })

    const userInput = container.querySelector('input[name="username"]') as HTMLInputElement
    fireEvent.input(userInput, { target: { value: 'admin' } })
    const pwInputs = container.querySelectorAll('input[type="password"]')
    fireEvent.input(pwInputs[0], { target: { value: 'hunter2-pw' } })
    fireEvent.input(pwInputs[1], { target: { value: 'hunter2-pw' } })

    fireEvent.click(await findByText('Continue'))
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith('/api/setup/standalone', {
      username: 'admin',
      password: 'hunter2-pw',
      household_name: 'The Rivendells',
    })
  })
})

// ── ha ──────────────────────────────────────────────────────────────────────

describe('SetupPage ha welcome', () => {
  it('shows welcome first, advances to the person picker on Continue', async () => {
    const get = vi.fn(async () => ({
      persons: [{ username: 'alice', display_name: 'Alice', picture_url: null }],
    }))
    vi.doMock('@/api', () => ({
      api: { get, post: vi.fn(), delete: vi.fn() },
    }))
    commonMocks(buildCfg('ha'))
    const { SetupPage } = await import('./SetupPage')
    const { findByText, queryByText } = render(<SetupPage />)

    expect(await findByText('Welcome to your home')).toBeTruthy()
    // The HA persons endpoint is NOT hit during the welcome step.
    expect(get).not.toHaveBeenCalled()
    // The picker title isn't visible yet.
    expect(queryByText('Pick your Home Assistant user')).toBeNull()

    fireEvent.click(await findByText("Let’s get started"))
    expect(await findByText('Pick your Home Assistant user')).toBeTruthy()
    expect(get).toHaveBeenCalledWith('/api/setup/ha/persons')
  })
})
