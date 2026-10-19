import { describe, it, expect, beforeEach } from 'vitest'
import { householdUsers } from '@/store/householdUsers'
import { setConversationMembers } from '@/store/conversationMembers'
import { callerName } from './callerName'

const RAW = 'tvnxzlljlae437w5fmwriqqvakgfynks'

beforeEach(() => {
  householdUsers.value = new Map()
})

describe('callerName', () => {
  it('a caller from this household: display name, then username', () => {
    householdUsers.value = new Map([
      [RAW, { user_id: RAW, username: 'pascal', display_name: 'Pascal' }],
      ['u2', { user_id: 'u2', username: 'lina', display_name: '' }],
    ] as never)
    expect(callerName(RAW)).toBe('Pascal')
    expect(callerName('u2')).toBe('lina')
  })

  it('a caller from another household: named by the conversation roster', () => {
    setConversationMembers('c1', [{ user_id: 'remote-1', display_name: 'Sara' }])
    expect(callerName('remote-1', 'c1')).toBe('Sara')
  })

  it('anyone unknown is "Someone", never the raw user id', () => {
    expect(callerName(RAW)).toBe('Someone')
    expect(callerName(RAW, 'not-loaded')).toBe('Someone')
    expect(callerName(null)).toBe('Someone')
  })
})
