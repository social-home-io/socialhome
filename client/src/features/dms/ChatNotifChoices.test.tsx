import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn() }))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import { ChatNotifChoices, type ChatNotif } from './ChatNotifChoices'

function chat(over: Partial<ChatNotif> = {}): ChatNotif {
  return { convId: 'c1', mutedUntil: null, level: 'mentions', onMute: vi.fn(), onLevel: vi.fn(), ...over }
}

beforeEach(() => { api.put.mockReset(); api.delete.mockReset(); toast.mockReset() })

describe('ChatNotifChoices', () => {
  it('the level is the shared radio group, and saves on pick', async () => {
    api.put.mockResolvedValue({ level: 'all' })
    const c = chat({ defaultLevel: 'mentions' })
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    const group = view.getByRole('group', { name: 'Chat' })
    expect(group.classList.contains('sh-space-notif-levels')).toBe(true)
    expect((view.getByRole('radio', { name: /^Replies and mentions/ }) as HTMLInputElement).checked).toBe(true)
    expect(view.getByText('Default')).toBeTruthy()
    fireEvent.click(view.getByRole('radio', { name: /^Everything/ }))
    await waitFor(() => expect(c.onLevel).toHaveBeenCalledWith('all'))
    expect(api.put).toHaveBeenCalledWith('/api/conversations/c1/notif-prefs', { level: 'all' })
  })

  it('Nothing asks for how long, and mutes for that long', async () => {
    api.put.mockResolvedValue({ muted_until: '2026-10-20T10:00:00+00:00' })
    const c = chat()
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    expect(view.queryByRole('group', { name: 'For how long?' })).toBeNull()
    fireEvent.click(view.getByRole('radio', { name: /^Nothing/ }))
    const ask = view.getByRole('group', { name: 'For how long?' })
    fireEvent.click(ask.querySelectorAll('button')[1])
    await waitFor(() => expect(c.onMute).toHaveBeenCalledWith('2026-10-20T10:00:00+00:00'))
    expect(api.put).toHaveBeenCalledWith('/api/conversations/c1/mute', { duration: '1h' })
  })

  it('muted: says until when and what comes back; Undo turns it back on', async () => {
    api.delete.mockResolvedValue(undefined)
    const until = new Date(Date.now() + 3600e3).toISOString()
    const c = chat({ mutedUntil: until })
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    expect((view.getByRole('radio', { name: /^Nothing/ }) as HTMLInputElement).checked).toBe(true)
    expect(view.getByRole('status').textContent).toMatch(/Quiet until .*, then back to Replies and mentions/)
    fireEvent.click(view.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(c.onMute).toHaveBeenCalledWith(null))
    expect(c.onLevel).not.toHaveBeenCalled() // the level from before stays
  })

  it('muted forever says so', () => {
    const view = render(
      <ChatNotifChoices chat={chat({ mutedUntil: '9999-12-31T23:59:59+00:00' })} legend="Chat" />,
    )
    expect(view.getByRole('status').textContent).toMatch(/Quiet until you turn it back on/)
  })
})
