import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me' } },
}))

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

import {
  applyConversationRows, conversationRows, dmUnreadTotal, inbox, isSystemChatFrame,
  messagesByConversation, noteConversationRead, resetDmStoreForTests, wireDmWs,
} from './dms'

beforeEach(() => {
  apiGet.mockReset()
  apiGet.mockResolvedValue([{ unread: 3, muted_until: null }])
  resetDmStoreForTests()
  inbox.value = {}
  messagesByConversation.value = {}
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

  it('a frame for a conversation not loaded yet updates the inbox and refreshes the badge', async () => {
    emit('dm.message', {
      conversation_id: 'dm-1', system_scope: null, space_id: null,
      message: { id: 'm2', sender_user_id: 'u-bob', content: 'hi you' },
    })
    expect(inbox.value['dm-1']?.content).toBe('hi you')
    expect(apiGet).toHaveBeenCalledWith('/api/conversations')
    await vi.waitFor(() => expect(dmUnreadTotal.value).toBe(3))
  })
})

describe('wireDmWs — local unread count', () => {
  const frame = (conv: string, id: string, sender = 'u-bob', at = '2026-11-21T10:00:00+00:00') => ({
    conversation_id: conv, system_scope: null, space_id: null,
    message: { id, sender_user_id: sender, content: 'hi', created_at: at },
  })

  beforeEach(() => {
    vi.useFakeTimers()
    applyConversationRows([
      { id: 'dm-a', unread: 0, muted_until: null, last_message_at: '2026-11-20T10:00:00+00:00' },
      { id: 'dm-b', unread: 1, muted_until: null, last_message_at: '2026-11-21T09:00:00+00:00' },
      { id: 'dm-m', unread: 0, muted_until: '9999-12-31T23:59:59+00:00', last_message_at: null },
    ])
    apiGet.mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('counts a message from someone else without refetching the list', () => {
    expect(dmUnreadTotal.value).toBe(1)
    for (let i = 0; i < 19; i++) emit('dm.message', frame('dm-a', `m-${i}`))
    expect(dmUnreadTotal.value).toBe(20)
    expect(apiGet).not.toHaveBeenCalled()
    // The inbox row follows: its count, its time, and it moves to the top.
    expect(conversationRows.value[0]).toMatchObject({
      id: 'dm-a', unread: 19, last_message_at: '2026-11-21T10:00:00+00:00',
    })
  })

  it('never counts the viewer\'s own message, nor the same message twice', () => {
    emit('dm.message', frame('dm-a', 'mine-1', 'u-me'))
    emit('dm.message', frame('dm-a', 'm-1'))
    emit('dm.message', frame('dm-a', 'm-1'))
    expect(dmUnreadTotal.value).toBe(2)
  })

  it('a muted conversation keeps its row count but stays out of the badge', () => {
    emit('dm.message', frame('dm-m', 'm-1'))
    expect(dmUnreadTotal.value).toBe(1)
    expect(conversationRows.value.find(r => r.id === 'dm-m')?.unread).toBe(1)
  })

  it('reading a thread zeroes its count', () => {
    emit('dm.message', frame('dm-a', 'm-1'))
    noteConversationRead('dm-a')
    noteConversationRead('dm-b')
    expect(dmUnreadTotal.value).toBe(0)
  })

  it('unknown conversations refetch at most once per 5 s, with one trailing refetch', async () => {
    // A burst of new conversations (first messages) …
    for (let i = 0; i < 10; i++) emit('dm.message', frame(`new-${i}`, `m-${i}`))
    emit('dm.conversation.created', { conversation_id: 'new-x' })
    expect(apiGet).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(4_900)
    expect(apiGet).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(200)
    expect(apiGet).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(apiGet).toHaveBeenCalledTimes(2)
  })
})
