import { describe, it, expect, vi, beforeEach } from 'vitest'

const get = vi.fn()

beforeEach(() => {
  vi.resetModules()
  get.mockReset()
  get.mockImplementation(async () => [
    { user_id: 'u-me', display_name: 'Me', mention: 'me', is_self: true },
    { user_id: 'u-anna', display_name: 'Anna', mention: 'anna', is_self: false },
    { user_id: 'u-x', display_name: 'X', mention: null, is_self: false },
  ])
  vi.doMock('@/api', () => ({ api: { get } }))
  vi.doMock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u-me' } } }))
})

describe('conversation member cache', () => {
  it('loads once per conversation and exposes the render tokens', async () => {
    const m = await import('./conversationMembers')
    expect(m.conversationMentionRender('c1').mentions.size).toBe(0)
    await m.loadConversationMembers('c1')
    await m.loadConversationMembers('c1')
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/api/conversations/c1/members')
    const r = m.conversationMentionRender('c1')
    expect([...r.mentions].sort()).toEqual(['anna', 'me'])
    expect(r.selfMention).toBe('me')
  })

  it('seeds from a roster the thread already fetched (no second request)', async () => {
    const m = await import('./conversationMembers')
    m.setConversationMembers('c2', [{ user_id: 'u-bob', mention: 'bob' }])
    await m.loadConversationMembers('c2')
    expect(get).not.toHaveBeenCalled()
    expect(m.conversationMentionRender('c2').mentions.has('bob')).toBe(true)
    expect(m.conversationMentionRender(null).mentions.size).toBe(0)
  })

  it('a failed fetch is retried on the next call', async () => {
    const m = await import('./conversationMembers')
    get.mockRejectedValueOnce(new Error('offline'))
    await m.loadConversationMembers('c3')
    expect(m.conversationMembers.value.c3).toBeUndefined()
    await m.loadConversationMembers('c3')
    expect(m.conversationMembers.value.c3?.size).toBe(3)
    m.invalidateConversationMembers('c3')
    await m.loadConversationMembers('c3')
    expect(get).toHaveBeenCalledTimes(3)
  })
})
