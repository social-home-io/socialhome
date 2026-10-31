/**
 * inviteCaps — what both invite sheets share: a person's key and the
 * server's invite caps in plain words.
 */
import { describe, it, expect } from 'vitest'
import { ApiError } from '@/api'
import { keyOf, limitFrom, limitOf, limitText, SEND_ARM_MS } from './inviteCaps'

const COUNTS = { open: 0, max_open: 10, today: 0, max_per_day: 30 }

describe('inviteCaps', () => {
  it('Send arms after 600 ms', () => {
    expect(SEND_ARM_MS).toBe(600)
  })

  it('keys a person by household and user, never colliding across the two', () => {
    expect(keyOf({ instance_id: 'a', user_ref: 'bc' })).not.toBe(keyOf({ instance_id: 'ab', user_ref: 'c' }))
  })

  it('the counts name the cap in the way, open first', () => {
    expect(limitFrom(null)).toBeNull()
    expect(limitFrom(COUNTS)).toBeNull()
    expect(limitFrom({ ...COUNTS, open: 10, today: 30 })).toBe('open')
    expect(limitFrom({ ...COUNTS, today: 30 })).toBe('daily')
  })

  it('a refusal names its cap; anything else is not a cap', () => {
    expect(limitOf(new ApiError(429, '/x', { code: 'APP_INVITES_OPEN_LIMIT' }))).toBe('open')
    expect(limitOf(new ApiError(429, '/x', { code: 'APP_INVITES_DAILY_LIMIT' }))).toBe('daily')
    expect(limitOf(new ApiError(429, '/x', { code: 'APP_GROUP_OPEN_LIMIT' }))).toBeNull()
    expect(limitOf(new Error('x'))).toBeNull()
  })

  it('says the cap in plain words with the server\'s numbers', () => {
    expect(limitText('open', { ...COUNTS, max_open: 4 }, 'Chess'))
      .toBe('You have 4 invites nobody has answered yet. Wait for an answer, or take one back in Chess.')
    expect(limitText('daily', null, 'Chess')).toBe('You\'ve sent 30 invites today. Try again tomorrow.')
  })
})
