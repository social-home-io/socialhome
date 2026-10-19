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
  it('the level is a radio group in the posts style, and saves on pick', async () => {
    api.put.mockResolvedValue({ level: 'all' })
    const c = chat()
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    const group = view.getByRole('group', { name: 'Chat' })
    expect(group.classList.contains('sh-space-notif-levels')).toBe(true)
    expect((view.getByRole('radio', { name: /Only @mentions/ }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(view.getByRole('radio', { name: /All messages/ }))
    await waitFor(() => expect(c.onLevel).toHaveBeenCalledWith('all'))
  })

  it('unmuted: the lengths are a labelled "Mute the chat for…" group', async () => {
    api.put.mockResolvedValue({ muted_until: '2026-10-20T10:00:00+00:00' })
    const c = chat()
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    const group = view.getByRole('group', { name: 'Mute the chat for…' })
    fireEvent.click(group.querySelector('button')!)
    await waitFor(() => expect(c.onMute).toHaveBeenCalled())
    expect(view.queryByRole('button', { name: 'Unmute' })).toBeNull()
  })

  it('muted: says until when, and Unmute turns it back on', async () => {
    api.delete.mockResolvedValue(undefined)
    const c = chat({ mutedUntil: '9999-12-31T23:59:59+00:00' })
    const view = render(<ChatNotifChoices chat={c} legend="Chat" />)
    expect(view.getByRole('status').textContent).toMatch(/Muted/)
    expect(view.queryByRole('group', { name: 'Mute the chat for…' })).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Unmute' }))
    await waitFor(() => expect(c.onMute).toHaveBeenCalledWith(null))
  })
})
