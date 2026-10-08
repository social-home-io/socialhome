import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}))

import {
  isSpaceChatFrame,
  loadSpaceChat,
  patchSpaceChat,
  resetSpaceChat,
  spaceChat,
  spaceChatError,
  spaceChatOf,
  spaceFeedViewFromQuery,
  spaceFeedViews,
} from './spaceChat'

const SUMMARY = {
  enabled: true, conversation_id: 'sc-1', unread: 2,
  notif_level: 'mentions', muted_until: null, last_read_at: '2026-10-08 12:00:00',
}

beforeEach(() => {
  apiGet.mockReset()
  resetSpaceChat(null)
})

describe('loadSpaceChat', () => {
  it('stores the summary of GET /api/spaces/{id}/chat for that space', async () => {
    apiGet.mockResolvedValue(SUMMARY)
    resetSpaceChat('s1')
    await loadSpaceChat('s1')
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/chat')
    expect(spaceChatOf('s1')).toEqual(SUMMARY)
    expect(spaceChatOf('s2')).toBeNull()
    expect(spaceChatError.value).toBeNull()
  })

  it('drops an answer for a space the page already left', async () => {
    let answer!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise((res) => { answer = res }))
    const pending = loadSpaceChat('s1')
    resetSpaceChat('s2')
    answer(SUMMARY)
    await pending
    expect(spaceChat.value).toBeNull()
    expect(spaceChatOf('s1')).toBeNull()
  })

  it('a failure names the space and keeps the previous summary', async () => {
    apiGet.mockResolvedValueOnce(SUMMARY)
    await loadSpaceChat('s1')
    apiGet.mockRejectedValueOnce(new Error('offline'))
    await loadSpaceChat('s1')
    expect(spaceChatError.value).toBe('s1')
    expect(spaceChatOf('s1')?.conversation_id).toBe('sc-1')
    resetSpaceChat('s1')
    expect(spaceChatError.value).toBeNull()
  })

  it('patchSpaceChat merges into a loaded summary only', async () => {
    patchSpaceChat({ unread: 5 })
    expect(spaceChat.value).toBeNull()
    apiGet.mockResolvedValue(SUMMARY)
    await loadSpaceChat('s1')
    patchSpaceChat({ unread: 0 })
    expect(spaceChatOf('s1')?.unread).toBe(0)
  })
})

describe('isSpaceChatFrame', () => {
  it('matches only this space\'s chat frames', () => {
    expect(isSpaceChatFrame({ system_scope: 'space', space_id: 's1' }, 's1')).toBe(true)
    expect(isSpaceChatFrame({ system_scope: 'space', space_id: 's2' }, 's1')).toBe(false)
    expect(isSpaceChatFrame({ system_scope: 'household', space_id: null }, 's1')).toBe(false)
    expect(isSpaceChatFrame({ system_scope: null }, 's1')).toBe(false)
    expect(isSpaceChatFrame(null, 's1')).toBe(false)
  })
})

describe('spaceFeedViews', () => {
  it('chat for a writer while the feature is on and the server agrees', () => {
    for (const role of ['owner', 'admin', 'moderator', 'member'] as const) {
      expect(spaceFeedViews({}, role, null)).toEqual(['posts', 'chat'])
      expect(spaceFeedViews({ chat: true }, role, { enabled: true })).toEqual(['posts', 'chat'])
    }
  })

  it('posts only for a follower, a non-member, chat off or a server "off"', () => {
    expect(spaceFeedViews({}, 'subscriber', null)).toEqual(['posts'])
    expect(spaceFeedViews({}, undefined, null)).toEqual(['posts'])
    expect(spaceFeedViews({ chat: false }, 'owner', { enabled: true })).toEqual(['posts'])
    expect(spaceFeedViews(undefined, 'member', { enabled: false })).toEqual(['posts'])
  })

  it('?view= parsing', () => {
    expect(spaceFeedViewFromQuery('chat')).toBe('chat')
    expect(spaceFeedViewFromQuery('posts')).toBe('posts')
    expect(spaceFeedViewFromQuery(undefined)).toBe('posts')
    expect(spaceFeedViewFromQuery(['chat'])).toBe('posts')
  })
})
