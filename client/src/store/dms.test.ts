import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers: Record<string, Handler[]> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      ;(handlers[type] ??= []).push(h)
      return () => {}
    },
  },
}))
const emit = (type: string, data: Record<string, unknown>) => {
  for (const h of handlers[type] ?? []) h({ type, data })
}

import { dmUnreadTotal, inbox, isSystemChatFrame, wireDmWs } from './dms'

beforeEach(() => {
  apiGet.mockReset()
  apiGet.mockResolvedValue([{ unread: 3, muted_until: null }])
  inbox.value = {}
  dmUnreadTotal.value = 0
  for (const k of Object.keys(handlers)) delete handlers[k]
  wireDmWs()
})

describe('isSystemChatFrame', () => {
  it('is true only for a frame carrying a system_scope', () => {
    expect(isSystemChatFrame({ system_scope: 'household' })).toBe(true)
    expect(isSystemChatFrame({ system_scope: 'space', space_id: 's1' })).toBe(true)
    expect(isSystemChatFrame({ system_scope: null })).toBe(false)
    expect(isSystemChatFrame({})).toBe(false)
    expect(isSystemChatFrame(null)).toBe(false)
  })
})

describe('wireDmWs — dm.message', () => {
  it('a household-chat frame never reaches the inbox or the Chats badge', async () => {
    emit('dm.message', {
      conversation_id: 'hc-1', system_scope: 'household', space_id: null,
      message: { id: 'm1', sender_user_id: 'u-bob', content: 'hi all' },
    })
    await Promise.resolve()
    expect(inbox.value).toEqual({})
    expect(apiGet).not.toHaveBeenCalled()
    expect(dmUnreadTotal.value).toBe(0)
  })

  it('a person-made DM frame updates the inbox and refreshes the badge', async () => {
    emit('dm.message', {
      conversation_id: 'dm-1', system_scope: null, space_id: null,
      message: { id: 'm2', sender_user_id: 'u-bob', content: 'hi you' },
    })
    expect(inbox.value['dm-1']?.content).toBe('hi you')
    expect(apiGet).toHaveBeenCalledWith('/api/conversations')
    await vi.waitFor(() => expect(dmUnreadTotal.value).toBe(3))
  })
})
