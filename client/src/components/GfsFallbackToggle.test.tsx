import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, waitFor, act } from '@testing-library/preact'
import {
  GfsFallbackToggle,
  GFS_FALLBACK_RECHECK_DELAYS_MS,
  type GfsFallbackState,
} from './GfsFallbackToggle'

const mockPatch = vi.fn()
const mockGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    patch: (...a: unknown[]) => mockPatch(...a),
    get: (...a: unknown[]) => mockGet(...a),
  },
  // ``message`` is what ``apiErrors`` already turned into words.
  ApiError: class ApiError extends Error {
    constructor(public status: number, message: string) { super(message) }
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

  it('on but no longer available: says it is on yet cannot work, and can be turned off', () => {
    renderToggle({ gfs_relay: true, gfs_relay_available: false })
    expect(checkbox().disabled).toBe(false)
    expect(checkbox().checked).toBe(true)
    expect(status()).toBe(
      "On, but it can't work right now: The Smiths needs a newer Social Home.",
    )
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

  it('re-reads the status a few times with back-off after switching on', async () => {
    vi.useFakeTimers()
    mockPatch.mockResolvedValue({ ...OFF, gfs_relay: true, peer_keywrap_known: true })
    const noRouteYet = [
      { instance_id: 'peer-1', gfs_relay: true, gfs_routes: 0, peer_keywrap_known: true },
    ]
    mockGet
      .mockResolvedValueOnce(noRouteYet)
      .mockResolvedValueOnce([
        { instance_id: 'other', gfs_routes: 9 },
        { instance_id: 'peer-1', gfs_relay: true, gfs_routes: 1, peer_keywrap_known: true },
      ])
    renderToggle()

    fireEvent.change(checkbox())
    await act(async () => { await Promise.resolve() })
    expect(mockGet).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(GFS_FALLBACK_RECHECK_DELAYS_MS[0])
    })
    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(status()).toContain('no GFS you both use found yet')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(GFS_FALLBACK_RECHECK_DELAYS_MS[1])
    })
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(mockGet).toHaveBeenLastCalledWith('/api/connections')
    expect(status()).toBe('On — The Smiths can be reached through 1 GFS you both use.')

    // A route was found: no further re-reads.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('stops re-reading after the last back-off step', async () => {
    vi.useFakeTimers()
    mockPatch.mockResolvedValue({ ...OFF, gfs_relay: true })
    mockGet.mockResolvedValue([{ instance_id: 'peer-1', gfs_relay: true, gfs_routes: 0 }])
    renderToggle()

    fireEvent.change(checkbox())
    await act(async () => { await Promise.resolve() })
    for (const delay of GFS_FALLBACK_RECHECK_DELAYS_MS) {
      await act(async () => { await vi.advanceTimersByTimeAsync(delay) })
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })

    expect(mockGet).toHaveBeenCalledTimes(GFS_FALLBACK_RECHECK_DELAYS_MS.length)
  })

  it('cancels the re-reads when the panel closes', async () => {
    vi.useFakeTimers()
    mockPatch.mockResolvedValue({ ...OFF, gfs_relay: true })
    const { unmount } = renderToggle()

    fireEvent.change(checkbox())
    await act(async () => { await Promise.resolve() })
    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })

    expect(mockGet).not.toHaveBeenCalled()
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

  it('a coded refusal shows the translated message, never "API 409"', async () => {
    const { ApiError } = await import('@/api') as unknown as {
      ApiError: new (status: number, message: string) => Error
    }
    mockPatch.mockRejectedValue(
      new ApiError(409, 'Only households you paired with directly can use the GFS fallback.'),
    )
    renderToggle()

    fireEvent.change(checkbox())

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        'Only households you paired with directly can use the GFS fallback.',
        'error',
      )
    })
    expect(checkbox().checked).toBe(false)
  })

  it('a network failure shows the friendly line, not the raw error', async () => {
    mockPatch.mockRejectedValue(new TypeError('Failed to fetch'))
    renderToggle()

    fireEvent.change(checkbox())

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        "Couldn't change the GFS fallback",
        'error',
      )
    })
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
