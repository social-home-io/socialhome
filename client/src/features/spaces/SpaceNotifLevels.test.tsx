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

import {
  SpaceNotifLevelPicker, SpaceNotificationChoices, useSpaceChatNotif, useSpaceNotifLevel,
} from './SpaceNotifLevels'

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
  const state = (over: Record<string, unknown> = {}) => ({
    level: 'mentions', is_default: true, default_level: 'mentions',
    muted_until: null, then_level: null, ...over,
  })

  it('is a radio group with the current level checked and the default tagged', async () => {
    mockGet.mockResolvedValue(state())
    const { getByRole, findByText } = render(
      <SpaceNotifLevelPicker spaceId="s1" level="mentions" onSaved={vi.fn()} />,
    )
    expect((getByRole('radio', { name: /^Replies and mentions/ }) as HTMLInputElement).checked).toBe(true)
    expect((getByRole('radio', { name: /^Everything/ }) as HTMLInputElement).checked).toBe(false)
    expect(getByRole('group', { name: 'Your notifications' })).toBeTruthy()
    expect(await findByText('Default')).toBeTruthy()
  })

  it('Nothing asks for how long, then saves with the length', async () => {
    mockGet.mockResolvedValue(state())
    const until = new Date(Date.now() + 8 * 3600e3).toISOString()
    mockPut.mockResolvedValue(state({
      level: 'muted', is_default: false, muted_until: until, then_level: 'mentions',
    }))
    const onSaved = vi.fn()
    const { getByRole, findByRole, findByText } = render(
      <SpaceNotifLevelPicker spaceId="s1" level="mentions" onSaved={onSaved} />,
    )
    fireEvent.click(getByRole('radio', { name: /^Nothing/ }))
    expect(mockPut).not.toHaveBeenCalled()
    const ask = await findByRole('group', { name: 'For how long?' })
    expect(Array.from(ask.querySelectorAll('button')).map(b => b.textContent)).toEqual([
      'Until I turn it back on', '1 hour', '8 hours', '1 week',
    ])
    fireEvent.click(ask.querySelectorAll('button')[2])
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('muted'))
    expect(mockPut.mock.calls[0][0]).toBe('/api/spaces/s1/notif-prefs')
    expect(mockPut.mock.calls[0][1]).toEqual({ level: 'muted', duration: '8h' })
    expect(await findByText(/^Quiet until .*, then back to Replies and mentions$/)).toBeTruthy()
  })

  it('Undo on a timed mute brings the level from before back', async () => {
    const until = new Date(Date.now() + 3600e3).toISOString()
    mockGet.mockResolvedValue(state({ level: 'muted', muted_until: until, then_level: 'all' }))
    mockPut.mockResolvedValue(state({ level: 'all', is_default: false }))
    const onSaved = vi.fn()
    const { findByRole } = render(
      <SpaceNotifLevelPicker spaceId="s1" level="muted" onSaved={onSaved} />,
    )
    fireEvent.click(await findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('all'))
    expect(mockPut).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs', { level: 'all' })
  })

  it('a failed save keeps the level and says so', async () => {
    mockGet.mockResolvedValue(state({ level: 'all', default_level: 'all' }))
    mockPut.mockRejectedValue(new Error('offline'))
    const onSaved = vi.fn()
    const { getByRole } = render(<SpaceNotifLevelPicker spaceId="s1" level="all" onSaved={onSaved} />)
    fireEvent.click(getByRole('radio', { name: /^Replies and mentions/ }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't save: offline", 'error'))
    expect(onSaved).not.toHaveBeenCalled()
  })
})

describe('SpaceNotificationChoices — the one place both levels are set', () => {
  it('posts only, without the chat', () => {
    const { getByRole, queryByText } = render(
      <SpaceNotificationChoices spaceId="s1" level="all" onSaved={vi.fn()} chatNotif={null} />,
    )
    expect(getByRole('group', { name: 'Your notifications' })).toBeTruthy()
    expect(queryByText('Chat')).toBeNull()
  })

  it('posts and chat side by side when the viewer has the chat', () => {
    const { getByText } = render(
      <SpaceNotificationChoices spaceId="s1" level="all" onSaved={vi.fn()} chatNotif={{
        convId: 'c1', mutedUntil: null, level: 'mentions', onMute: vi.fn(), onLevel: vi.fn(),
      }} />,
    )
    expect(getByText('Posts')).toBeTruthy()
    expect(getByText('Chat')).toBeTruthy()
  })
})

describe('useSpaceChatNotif', () => {
  it('reads the space chat for a member, and nothing while disabled', async () => {
    mockGet.mockResolvedValue({
      enabled: true, conversation_id: 'c9', notif_level: 'all', muted_until: null, unread: 0,
    })
    const off = renderHook(() => useSpaceChatNotif('s1', false))
    expect(off.result.current).toBeNull()
    expect(mockGet).not.toHaveBeenCalled()
    const { result } = renderHook(() => useSpaceChatNotif('s1', true))
    await waitFor(() => expect(result.current?.convId).toBe('c9'))
    expect(mockGet).toHaveBeenCalledWith('/api/spaces/s1/chat')
    expect(result.current?.level).toBe('all')
  })
})
