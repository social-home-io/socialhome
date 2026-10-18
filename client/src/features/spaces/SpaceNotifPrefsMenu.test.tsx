import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// vi.mock factory bodies are hoisted above plain const declarations,
// so the mocks must be defined via vi.hoisted to be reachable.
const { mockGet, mockPut, mockPatch } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
  mockPatch: vi.fn(),
}))

vi.mock('@/api', () => ({
  api: {
    get: mockGet,
    put: mockPut,
    patch: mockPatch,
    post: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
}))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

import { SpaceNotifPrefsMenu } from './SpaceNotifPrefsMenu'

beforeEach(() => {
  mockGet.mockReset()
  mockPut.mockReset()
  mockPatch.mockReset()
  mockPut.mockResolvedValue({ level: 'all' })
  mockPatch.mockResolvedValue({})
})

describe('SpaceNotifPrefsMenu', () => {
  it('module exports exist', async () => {
    const mod = await import('./SpaceNotifPrefsMenu')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceNotifPrefsMenu).toBe('function')
  })

  it('hides the location toggle when feature_location is false', async () => {
    mockGet.mockResolvedValue({
      level: 'all',
      feature_location: false,
      location_share_enabled: false,
    })
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    // Open the menu by clicking the trigger.
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    // Location toggle must NOT be rendered when the space has the
    // feature off — there is nothing for the member to opt into.
    expect(result.queryByTestId('space-location-toggle')).toBeNull()
  })

  it('offers a follower no location toggle — they may not share (location_share_allowed=false)', async () => {
    mockGet.mockResolvedValue({
      level: 'all',
      feature_location: true,
      location_share_enabled: false,
      location_share_allowed: false,
    })
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    await waitFor(() => expect(result.getByRole('menu')).toBeTruthy())
    expect(result.queryByTestId('space-location-toggle')).toBeNull()
  })

  it('shows the location toggle reflecting location_share_enabled=true', async () => {
    mockGet.mockResolvedValue({
      level: 'mentions',
      feature_location: true,
      location_share_allowed: true,
      location_share_enabled: true,
    })
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    await waitFor(() => {
      const toggle = result.getByTestId('space-location-toggle')
      expect(toggle).toBeTruthy()
      expect(toggle.getAttribute('aria-checked')).toBe('true')
    })
  })

  it('shows the location toggle reflecting location_share_enabled=false', async () => {
    mockGet.mockResolvedValue({
      level: 'all',
      feature_location: true,
      location_share_allowed: true,
      location_share_enabled: false,
    })
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    await waitFor(() => {
      const toggle = result.getByTestId('space-location-toggle')
      expect(toggle.getAttribute('aria-checked')).toBe('false')
    })
  })

  it('clicking the toggle PATCHes the endpoint and flips the UI', async () => {
    mockGet.mockResolvedValue({
      level: 'all',
      feature_location: true,
      location_share_allowed: true,
      location_share_enabled: false,
    })
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    const toggle = await waitFor(() => result.getByTestId('space-location-toggle'))
    fireEvent.click(toggle)
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        '/api/spaces/s1/members/me/location-sharing',
        { enabled: true },
      )
    })
    await waitFor(() => {
      // Optimistic — UI flips before/after the PATCH resolves.
      expect(result.getByTestId('space-location-toggle').getAttribute('aria-checked')).toBe('true')
    })
  })

  it('reverts the toggle when the PATCH fails', async () => {
    mockGet.mockResolvedValue({
      level: 'all',
      feature_location: true,
      location_share_allowed: true,
      location_share_enabled: true,
    })
    mockPatch.mockRejectedValueOnce(new Error('boom'))
    const result = render(<SpaceNotifPrefsMenu spaceId="s1" />)
    await waitFor(() => expect(result.container.querySelector('button')).toBeTruthy())
    fireEvent.click(result.container.querySelector('button') as HTMLButtonElement)
    const toggle = await waitFor(() => result.getByTestId('space-location-toggle'))
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    await waitFor(() => expect(mockPatch).toHaveBeenCalled())
    await waitFor(() => {
      // After the PATCH rejects, the UI reverts to the pre-click state.
      expect(result.getByTestId('space-location-toggle').getAttribute('aria-checked')).toBe('true')
    })
  })
})
