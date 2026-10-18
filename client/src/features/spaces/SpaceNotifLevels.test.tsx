/**
 * "Your notifications" for one space: the level read, and the radio
 * choice that saves on pick (the per-space bell is gone — one bell).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, renderHook, fireEvent, waitFor } from '@testing-library/preact'

const { mockGet, mockPut, toast } = vi.hoisted(() => ({
  mockGet: vi.fn(), mockPut: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { get: mockGet, put: mockPut } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import { SpaceNotifLevelPicker, useSpaceNotifLevel } from './SpaceNotifLevels'

beforeEach(() => {
  mockGet.mockReset()
  mockPut.mockReset()
  toast.mockReset()
})

describe('useSpaceNotifLevel', () => {
  it('reads the level once enabled', async () => {
    mockGet.mockResolvedValue({ level: 'mentions' })
    const { result } = renderHook(() => useSpaceNotifLevel('s1', true))
    await waitFor(() => expect(result.current[0]).toBe('mentions'))
    expect(mockGet).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs')
  })

  it('asks nothing while disabled, and stays unknown on a failure', async () => {
    renderHook(() => useSpaceNotifLevel('s1', false))
    expect(mockGet).not.toHaveBeenCalled()
    mockGet.mockRejectedValue(new Error('403'))
    const { result } = renderHook(() => useSpaceNotifLevel('s1', true))
    await waitFor(() => expect(mockGet).toHaveBeenCalled())
    expect(result.current[0]).toBeNull()
  })
})

describe('SpaceNotifLevelPicker', () => {
  it('is a radio group with the current level checked', () => {
    const { getByRole } = render(<SpaceNotifLevelPicker spaceId="s1" level="mentions" onSaved={vi.fn()} />)
    expect((getByRole('radio', { name: /^Only @mentions/ }) as HTMLInputElement).checked).toBe(true)
    expect((getByRole('radio', { name: /^All posts/ }) as HTMLInputElement).checked).toBe(false)
    expect(getByRole('group', { name: 'Your notifications' })).toBeTruthy()
  })

  it('saves a pick and reports the saved level', async () => {
    mockPut.mockResolvedValue({ level: 'muted' })
    const onSaved = vi.fn()
    const { getByRole } = render(<SpaceNotifLevelPicker spaceId="s1" level="all" onSaved={onSaved} />)
    fireEvent.click(getByRole('radio', { name: /^Muted/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('muted'))
    expect(mockPut).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs', { level: 'muted' })
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('Muted'), 'success')
  })

  it('a failed save keeps the level and says so', async () => {
    mockPut.mockRejectedValue(new Error('offline'))
    const onSaved = vi.fn()
    const { getByRole } = render(<SpaceNotifLevelPicker spaceId="s1" level="all" onSaved={onSaved} />)
    fireEvent.click(getByRole('radio', { name: /^Muted/ }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't save: offline", 'error'))
    expect(onSaved).not.toHaveBeenCalled()
  })
})
