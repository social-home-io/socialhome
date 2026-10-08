import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}))

import {
  householdChat,
  householdChatError,
  isHouseholdChatFrame,
  loadHouseholdChat,
  patchHouseholdChat,
} from './householdChat'

beforeEach(() => {
  apiGet.mockReset()
  householdChat.value = null
  householdChatError.value = false
})

describe('loadHouseholdChat', () => {
  it('stores the summary from GET /api/household/chat', async () => {
    apiGet.mockResolvedValue({
      enabled: true, conversation_id: 'hc-1', unread: 2,
      notif_level: 'mentions', muted_until: null,
    })
    await loadHouseholdChat()
    expect(apiGet).toHaveBeenCalledWith('/api/household/chat')
    expect(householdChat.value).toEqual({
      enabled: true, conversation_id: 'hc-1', unread: 2,
      notif_level: 'mentions', muted_until: null,
    })
    expect(householdChatError.value).toBe(false)
  })

  it('normalises a disabled / partial answer', async () => {
    apiGet.mockResolvedValue({ enabled: false, conversation_id: null, unread: -1 })
    await loadHouseholdChat()
    expect(householdChat.value).toEqual({
      enabled: false, conversation_id: null, unread: 0,
      notif_level: null, muted_until: null,
    })
  })

  it('a failure flags the error and keeps the previous summary', async () => {
    householdChat.value = {
      enabled: true, conversation_id: 'hc-1', unread: 1, notif_level: 'all', muted_until: null,
    }
    apiGet.mockRejectedValue(new Error('offline'))
    await loadHouseholdChat()
    expect(householdChatError.value).toBe(true)
    expect(householdChat.value?.conversation_id).toBe('hc-1')
  })
})

describe('patchHouseholdChat', () => {
  it('merges into a loaded summary and ignores an empty one', () => {
    patchHouseholdChat({ unread: 5 })
    expect(householdChat.value).toBeNull()
    householdChat.value = {
      enabled: true, conversation_id: 'hc-1', unread: 1, notif_level: 'all', muted_until: null,
    }
    patchHouseholdChat({ unread: 0, notif_level: 'mentions' })
    expect(householdChat.value).toMatchObject({ unread: 0, notif_level: 'mentions' })
  })
})

describe('isHouseholdChatFrame', () => {
  it('matches only the household scope', () => {
    expect(isHouseholdChatFrame({ system_scope: 'household' })).toBe(true)
    expect(isHouseholdChatFrame({ system_scope: 'space', space_id: 's1' })).toBe(false)
    expect(isHouseholdChatFrame({ system_scope: null })).toBe(false)
    expect(isHouseholdChatFrame(undefined)).toBe(false)
  })
})
