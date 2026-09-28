import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import { signal } from '@preact/signals'
import type { User } from '@/types'

vi.mock('@/components/LocationMap', () => ({ LocationMap: () => null }))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
const emit = (type: string, data: Record<string, unknown>) =>
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))

const me: User = {
  user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  status: { emoji: null, text: null, expires_at: null },
}

const row = (status: Record<string, unknown> | null) => ({
  username: 'bob', user_id: 'u2', display_name: 'Bob', picture_url: null,
  state: 'home', zone_name: null, is_online: true, is_idle: false,
  last_seen_at: null, status,
})

describe('PresencePage', () => {
  const currentUser = signal<User | null>(null)

  beforeEach(() => {
    vi.resetModules()
    handlers.clear()
    currentUser.value = { ...me }
    vi.doMock('@/store/auth', () => ({ currentUser }))
    vi.doMock('@/ws', () => ({
      ws: {
        on: (type: string, h: Handler) => {
          if (!handlers.has(type)) handlers.set(type, new Set())
          handlers.get(type)!.add(h)
          return () => { handlers.get(type)?.delete(h) }
        },
      },
    }))
  })

  it("shows each member's status", async () => {
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(async () => [row({ emoji: '🌴', text: 'On leave', expires_at: null })]) },
    }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    expect(await view.findByText('🌴 On leave')).toBeTruthy()
    expect(view.getByText('No status set')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Set status' })).toBeTruthy()
  })

  it('user.status_changed refetches the roster and updates our own status', async () => {
    let rows = [row(null)]
    const get = vi.fn(async () => rows)
    vi.doMock('@/api', () => ({ api: { get } }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    await view.findByText('Bob')

    rows = [row({ emoji: '🎧', text: 'Focus', expires_at: null })]
    emit('user.status_changed', { user_id: 'u2', status: rows[0].status })
    expect(await view.findByText('🎧 Focus')).toBeTruthy()

    emit('user.status_changed', {
      user_id: 'u1', status: { emoji: '🍽️', text: 'Lunch', expires_at: null },
    })
    await waitFor(() => expect(currentUser.value?.status?.text).toBe('Lunch'))
    expect(await view.findByText('🍽️ Lunch')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Edit status' })).toBeTruthy()

    emit('user.status_changed', { user_id: 'u1', status: null })
    await waitFor(() => expect(view.getByText('No status set')).toBeTruthy())
  })
})
