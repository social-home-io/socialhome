/**
 * Tests for the presence store's online-status handlers.
 *
 * The store mounts WS handlers via ``wirePresenceWs()``; we mock the
 * ``ws`` module so we can drive synthetic frames at the registered
 * callbacks and observe the resulting signal mutations.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

import { presence, wirePresenceWs } from './presence'

describe('presence store — online status', () => {
  beforeEach(() => {
    presence.value = {}
    Object.keys(handlers).forEach(k => delete handlers[k])
    wirePresenceWs()
  })

  it('user.online flips is_online true on the matching row', () => {
    presence.value = { anna: { username: 'anna', user_id: 'u-anna', state: 'home' } }
    handlers['user.online']({ data: { user_id: 'u-anna' } })
    expect(presence.value.anna.is_online).toBe(true)
    expect(presence.value.anna.is_idle).toBe(false)
  })

  it('user.idle flips is_idle true while keeping is_online true', () => {
    presence.value = {
      anna: { username: 'anna', user_id: 'u-anna', state: 'home', is_online: true },
    }
    handlers['user.idle']({ data: { user_id: 'u-anna' } })
    expect(presence.value.anna.is_online).toBe(true)
    expect(presence.value.anna.is_idle).toBe(true)
  })

  it('user.offline clears the flags and stamps last_seen_at', () => {
    presence.value = {
      anna: { username: 'anna', user_id: 'u-anna', state: 'home', is_online: true },
    }
    handlers['user.offline']({
      data: { user_id: 'u-anna', last_seen_at: '2026-05-02T08:00:00Z' },
    })
    expect(presence.value.anna.is_online).toBe(false)
    expect(presence.value.anna.is_idle).toBe(false)
    expect(presence.value.anna.last_seen_at).toBe('2026-05-02T08:00:00Z')
  })

  it('frames for unknown user_ids are dropped silently', () => {
    presence.value = { anna: { username: 'anna', user_id: 'u-anna', state: 'home' } }
    handlers['user.online']({ data: { user_id: 'u-other' } })
    expect(presence.value.anna.is_online).toBeUndefined()
  })
})
