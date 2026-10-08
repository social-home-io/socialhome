import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, waitFor, act } from '@testing-library/preact'
import {
  GfsFallbackToggle,
  GFS_FALLBACK_RECHECK_MS,
  type GfsFallbackState,
} from './GfsFallbackToggle'

const mockPatch = vi.fn()
const mockGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    patch: (...a: unknown[]) => mockPatch(...a),
    get: (...a: unknown[]) => mockGet(...a),
  },
}))

const mockShowToast = vi.fn()

vi.mock('./Toast', () => ({
  showToast: (...a: unknown[]) => mockShowToast(...a),
}))

const OFF: GfsFallbackState = {
  gfs_relay: false,
  gfs_routes: 0,
  peer_keywrap_known: false,
  gfs_relay_available: true,
}

function renderToggle(initial: Partial<GfsFallbackState> = {}) {
  return render(
    <GfsFallbackToggle
      instanceId="peer-1"
      peerName="The Smiths"
      initial={{ ...OFF, ...initial }}
    />,
  )
}

const checkbox = () => screen.getByRole('checkbox') as HTMLInputElement
const status = () => screen.getByRole('status').textContent

beforeEach(() => {
  mockPatch.mockReset()
  mockGet.mockReset().mockResolvedValue([])
  mockShowToast.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('GfsFallbackToggle', () => {
  it('off: unchecked, says messages only travel directly, shows the privacy note', () => {
    const { container } = renderToggle()
    expect(checkbox().checked).toBe(false)
    expect(checkbox().disabled).toBe(false)
    expect(container.textContent).toContain('Use the GFS as a fallback')
    expect(status()).toBe('Off — messages to The Smiths only travel directly.')
    expect(screen.getByRole('note').textContent).toContain(
      'though never what',
    )
  })

  it('on with one shared GFS: reachable through 1 GFS', () => {
    renderToggle({ gfs_relay: true, gfs_routes: 1, peer_keywrap_known: true })
    expect(checkbox().checked).toBe(true)
    expect(status()).toBe('On — The Smiths can be reached through 1 GFS you both use.')
  })

  it('on with several shared GFSes: uses the plural', () => {
    renderToggle({ gfs_relay: true, gfs_routes: 3, peer_keywrap_known: true })
    expect(status()).toBe('On — The Smiths can be reached through 3 GFSes you both use.')
  })

  it('on, other household has not turned it on: waiting for them', () => {
    renderToggle({ gfs_relay: true })
    expect(status()).toBe('On — waiting for The Smiths to turn it on too.')
  })

  it('on, both opted in but no shared GFS yet: says a common GFS is needed', () => {
    renderToggle({ gfs_relay: true, peer_keywrap_known: true })
    expect(status()).toContain('no GFS you both use found yet')
    expect(status()).toContain('The Smiths needs to be connected to one of yours')
  })

  it('not available: disabled, says the other household needs a newer Social Home', () => {
    renderToggle({ gfs_relay_available: false })
    expect(checkbox().disabled).toBe(true)
    expect(status()).toBe('Not available: The Smiths needs a newer Social Home.')
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('not available but already on: can still be turned off', () => {
    renderToggle({ gfs_relay: true, gfs_relay_available: false })
    expect(checkbox().disabled).toBe(false)
    expect(checkbox().checked).toBe(true)
  })

  it('switching on PATCHes gfs_relay and shows the server answer', async () => {
    mockPatch.mockResolvedValue({ ...OFF, gfs_relay: true })
    renderToggle()

    fireEvent.change(checkbox())

    expect(mockPatch).toHaveBeenCalledWith('/api/pairing/connections/peer-1', {
      gfs_relay: true,
    })
    await waitFor(() => {
      expect(status()).toBe('On — waiting for The Smiths to turn it on too.')
    })
    expect(checkbox().checked).toBe(true)
  })

  it('re-reads the status once after switching on (routes form in the background)', async () => {
    vi.useFakeTimers()
    mockPatch.mockResolvedValue({ ...OFF, gfs_relay: true, peer_keywrap_known: true })
    mockGet.mockResolvedValue([
      { instance_id: 'other', gfs_routes: 9 },
      { instance_id: 'peer-1', gfs_relay: true, gfs_routes: 1, peer_keywrap_known: true },
    ])
    renderToggle()

    fireEvent.change(checkbox())
    await act(async () => { await Promise.resolve() })
    expect(mockGet).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(GFS_FALLBACK_RECHECK_MS)
    })

    expect(mockGet).toHaveBeenCalledWith('/api/connections')
    expect(status()).toBe('On — The Smiths can be reached through 1 GFS you both use.')
  })

  it('switching off PATCHes false and drops the route count', async () => {
    mockPatch.mockResolvedValue({ ...OFF })
    renderToggle({ gfs_relay: true, gfs_routes: 2, peer_keywrap_known: true })

    fireEvent.change(checkbox())

    expect(mockPatch).toHaveBeenCalledWith('/api/pairing/connections/peer-1', {
      gfs_relay: false,
    })
    await waitFor(() => {
      expect(status()).toBe('Off — messages to The Smiths only travel directly.')
    })
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('reverts and toasts when the PATCH fails', async () => {
    mockPatch.mockRejectedValue(new Error(''))
    renderToggle()

    fireEvent.change(checkbox())

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        "Couldn't change the GFS fallback",
        'error',
      )
    })
    expect(checkbox().checked).toBe(false)
    expect(status()).toBe('Off — messages to The Smiths only travel directly.')
  })
})
