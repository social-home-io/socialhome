import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { User } from '@/types'

type Handler = (e: { data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
const emit = (type: string, data: Record<string, unknown>) =>
  handlers.get(type)?.forEach(h => h({ data }))

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))

import { currentUser } from '@/store/auth'
import { wireMyStatusWs } from './myStatus'

const me: User = {
  user_id: 'u1', username: 'anna', display_name: 'Anna', is_admin: false,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  status: { emoji: null, text: null, expires_at: null },
}

describe('wireMyStatusWs', () => {
  let off: () => void
  beforeEach(() => {
    handlers.clear()
    currentUser.value = { ...me }
    off = wireMyStatusWs()
  })

  it('our own frame lands on currentUser (another device, the expiry sweep)', () => {
    emit('user.status_changed', { user_id: 'u1', status: { emoji: '🍽️', text: 'Lunch', expires_at: null } })
    expect(currentUser.value?.status).toEqual({ emoji: '🍽️', text: 'Lunch', expires_at: null })
    emit('user.status_changed', { user_id: 'u1', status: null })
    expect(currentUser.value?.status).toEqual({ emoji: null, text: null, expires_at: null })
  })

  it("someone else's frame leaves currentUser alone", () => {
    emit('user.status_changed', { user_id: 'u2', status: { emoji: '🎧', text: 'Focus', expires_at: null } })
    expect(currentUser.value?.status?.text).toBeNull()
  })

  it('does nothing while signed out', () => {
    currentUser.value = null
    emit('user.status_changed', { user_id: 'u1', status: { emoji: '🎧', text: 'Focus', expires_at: null } })
    expect(currentUser.value).toBeNull()
  })

  it('returns an unsubscribe', () => {
    off()
    emit('user.status_changed', { user_id: 'u1', status: { emoji: '🎧', text: 'Focus', expires_at: null } })
    expect(currentUser.value?.status?.text).toBeNull()
  })
})
