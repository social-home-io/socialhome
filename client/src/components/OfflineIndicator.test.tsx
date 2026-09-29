import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, cleanup } from '@testing-library/preact'
import { isOnline, OfflineIndicator, UNREACHABLE_GRACE_MS } from './OfflineIndicator'
import { connectionState, ws } from '@/ws'
import { toasts } from './Toast'

describe('OfflineIndicator', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    isOnline.value = true
    connectionState.value = 'open'
    toasts.value = []
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
    connectionState.value = 'closed'
    isOnline.value = true
  })

  it('isOnline defaults to true in test env', () => {
    expect(isOnline.value).toBe(true)
  })

  it('renders nothing while online and connected', () => {
    const { container } = render(<OfflineIndicator />)
    expect(container.textContent).toBe('')
  })

  it('shows the browser-offline banner when navigator goes offline', () => {
    const { getByRole } = render(<OfflineIndicator />)
    act(() => { isOnline.value = false })
    expect(getByRole('alert').textContent).toContain("You're offline")
  })

  it('waits out the grace period before showing the unreachable banner', () => {
    const { queryByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS - 1) })
    expect(queryByRole('alert')).toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    expect(queryByRole('alert')?.textContent).toContain("Can't reach Social Home")
  })

  it('a connecting → reconnecting hop does not restart the grace timer', () => {
    const { queryByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'connecting' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS - 1000) })
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(queryByRole('alert')).not.toBeNull()
  })

  it('a quick reconnect inside the grace period never shows the banner or a toast', () => {
    const { queryByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(1000) })
    act(() => { connectionState.value = 'open' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS * 2) })
    expect(queryByRole('alert')).toBeNull()
    expect(toasts.value).toHaveLength(0)
  })

  it('Retry now asks the socket to reconnect immediately', () => {
    const spy = vi.spyOn(ws, 'retryNow').mockImplementation(() => {})
    const { getByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS) })
    fireEvent.click(getByRole('button', { name: 'Retry now' }))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('hides the banner and toasts "Reconnected" once the socket opens', () => {
    const { queryByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS) })
    expect(queryByRole('alert')).not.toBeNull()
    act(() => { connectionState.value = 'open' })
    expect(queryByRole('alert')).toBeNull()
    expect(toasts.value.map(t => t.message)).toEqual(['Reconnected'])
  })

  it('an intentional disconnect hides the banner without a Reconnected toast', () => {
    const { queryByRole } = render(<OfflineIndicator />)
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS) })
    act(() => { connectionState.value = 'closed' })
    expect(queryByRole('alert')).toBeNull()
    expect(toasts.value).toHaveLength(0)
  })

  it('the browser-offline banner wins over the unreachable banner', () => {
    const { getAllByRole } = render(<OfflineIndicator />)
    act(() => {
      isOnline.value = false
      connectionState.value = 'reconnecting'
    })
    act(() => { vi.advanceTimersByTime(UNREACHABLE_GRACE_MS) })
    const alerts = getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0].textContent).toContain("You're offline")
  })

  it('coming back online retries the socket right away', () => {
    const spy = vi.spyOn(ws, 'retryNow').mockImplementation(() => {})
    render(<OfflineIndicator />)
    act(() => { window.dispatchEvent(new Event('online')) })
    expect(spy).toHaveBeenCalledTimes(1)
  })
})
