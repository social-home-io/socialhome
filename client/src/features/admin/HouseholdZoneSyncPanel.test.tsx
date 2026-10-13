/**
 * HouseholdZoneSyncPanel tests — the "Sync zones from Home Assistant"
 * switch: status line, quiet error, the inline turn-off question, and
 * the PATCH + list refetch.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

const mockApi = { get: vi.fn(), patch: vi.fn() }
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  get api() { return mockApi },
}))

const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: () => void) => {
      wsHandlers.set(type, h)
      return () => { wsHandlers.delete(type) }
    },
  },
}))

const showToast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast }))

import { ApiError } from '@/api'
import { HouseholdZoneSyncPanel, ZONE_SYNC_STATUS_REFRESH_MS } from './HouseholdZoneSyncPanel'

const ON = {
  available: true,
  enabled: true,
  last_synced_at: new Date(Date.now() - 3 * 60_000).toISOString(),
  last_error: null,
}
const OFF = { available: true, enabled: false, last_synced_at: null, last_error: null }

const sw = (c: Element) => c.querySelector('input[role="switch"]') as HTMLInputElement

describe('HouseholdZoneSyncPanel', () => {
  beforeEach(() => {
    mockApi.get.mockReset()
    mockApi.patch.mockReset()
    showToast.mockReset()
    wsHandlers.clear()
  })
  afterEach(() => { vi.useRealTimers() })

  it('shows the switch on with the last sync time', async () => {
    mockApi.get.mockResolvedValue(ON)
    const { findByText, container, getByRole } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await findByText('Sync zones from Home Assistant')
    expect(mockApi.get).toHaveBeenCalledWith('/api/household/zones/sync')
    expect(sw(container).checked).toBe(true)
    expect(getByRole('switch', { name: 'Sync zones from Home Assistant' })).toBeTruthy()
    expect(getByRole('status').textContent).toBe('Last synced 3 min ago')
  })

  it('says it is waiting before the first sync', async () => {
    mockApi.get.mockResolvedValue({ ...ON, last_synced_at: null })
    const { findByText } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await findByText('Waiting for the first sync…')
  })

  it('renders nothing when the platform has no zones to sync', async () => {
    mockApi.get.mockResolvedValue({ ...OFF, available: false })
    const { container } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing when the status cannot be read', async () => {
    mockApi.get.mockRejectedValue(new Error('offline'))
    const { container } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('shows a quiet error line after a failed sync', async () => {
    mockApi.get.mockResolvedValue({ ...ON, last_error: 'Home Assistant didn\'t answer' })
    const { findByTestId } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    const line = await findByTestId('zone-sync-error')
    expect(line.textContent).toContain("couldn't reach Home Assistant")
    // Never the raw server text.
    expect(line.textContent).not.toContain('answer')
  })

  it('asks before turning off, and Cancel backs out without a request', async () => {
    mockApi.get.mockResolvedValue(ON)
    const { findByText, container, getByText, queryByText } = render(
      <HouseholdZoneSyncPanel onChanged={() => {}} />,
    )
    await findByText('Sync zones from Home Assistant')
    fireEvent.click(sw(container))
    getByText('Zones from Home Assistant will be removed from Social Home. Your own zones stay.')
    // Still on until confirmed.
    expect(sw(container).checked).toBe(true)
    // Focus lands on the question.
    expect(document.activeElement?.classList.contains('sh-zone-sync__confirm')).toBe(true)
    fireEvent.click(getByText('Cancel'))
    expect(queryByText(/will be removed from Social Home/)).toBeNull()
    expect(mockApi.patch).not.toHaveBeenCalled()
  })

  it('flipping the switch again closes the question', async () => {
    mockApi.get.mockResolvedValue(ON)
    const { findByText, container, queryByText } = render(
      <HouseholdZoneSyncPanel onChanged={() => {}} />,
    )
    await findByText('Sync zones from Home Assistant')
    fireEvent.click(sw(container))
    fireEvent.click(sw(container))
    expect(queryByText(/will be removed from Social Home/)).toBeNull()
    expect(mockApi.patch).not.toHaveBeenCalled()
  })

  it('confirming turns it off and refetches the list', async () => {
    mockApi.get.mockResolvedValue(ON)
    mockApi.patch.mockResolvedValueOnce(OFF)
    const onChanged = vi.fn()
    const { findByText, container, getByText } = render(
      <HouseholdZoneSyncPanel onChanged={onChanged} />,
    )
    await findByText('Sync zones from Home Assistant')
    fireEvent.click(sw(container))
    fireEvent.click(getByText('Turn off and remove'))
    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledWith(
      '/api/household/zones/sync', { enabled: false },
    ))
    await findByText('Off — only the zones you add here are used.')
    expect(sw(container).checked).toBe(false)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('turning on syncs straight away, no question', async () => {
    mockApi.get.mockResolvedValue(OFF)
    mockApi.patch.mockResolvedValueOnce(ON)
    const onChanged = vi.fn()
    const { findByText, container, queryByText } = render(
      <HouseholdZoneSyncPanel onChanged={onChanged} />,
    )
    await findByText('Off — only the zones you add here are used.')
    fireEvent.click(sw(container))
    expect(queryByText(/will be removed/)).toBeNull()
    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledWith(
      '/api/household/zones/sync', { enabled: true },
    ))
    await findByText(/Last synced/)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('toasts a failed change and keeps the switch as it was', async () => {
    mockApi.get.mockResolvedValue(OFF)
    mockApi.patch.mockRejectedValueOnce(new Error('offline'))
    const onChanged = vi.fn()
    const { findByText, container } = render(<HouseholdZoneSyncPanel onChanged={onChanged} />)
    await findByText(/^Off/)
    fireEvent.click(sw(container))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't change the zone sync", 'error',
    ))
    expect(sw(container).checked).toBe(false)
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('shows a coded refusal as its translated message', async () => {
    mockApi.get.mockResolvedValue(OFF)
    mockApi.patch.mockRejectedValueOnce(new ApiError(403, '/api/household/zones/sync', {
      code: 'FORBIDDEN', detail: 'Only household admins can change zone sync.',
    }))
    const { findByText, container } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await findByText(/^Off/)
    fireEvent.click(sw(container))
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(showToast.mock.calls[0][1]).toBe('error')
    expect(showToast.mock.calls[0][0]).not.toBe("Couldn't change the zone sync")
  })

  it('re-reads the status on household_zones_changed and on a timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockApi.get.mockResolvedValue(ON)
    const { findByText } = render(<HouseholdZoneSyncPanel onChanged={() => {}} />)
    await findByText('Sync zones from Home Assistant')
    expect(mockApi.get).toHaveBeenCalledTimes(1)
    act(() => { wsHandlers.get('household_zones_changed')!() })
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledTimes(2))
    await act(async () => { vi.advanceTimersByTime(ZONE_SYNC_STATUS_REFRESH_MS) })
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledTimes(3))
  })
})
