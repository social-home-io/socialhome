import { describe, it, expect, beforeEach } from 'vitest'
import { ApiError } from '@/api'
import { setLocale } from '@/i18n/i18n'
import { refusalText } from './refusal'

const refused = (code: string, params?: Record<string, unknown>) =>
  new ApiError(409, '/api/apps/quiz/groups/t1/join', { code, ...(params ? { params } : {}) })

beforeEach(async () => {
  await setLocale('en')
})

describe('refusalText', () => {
  it('says each group refusal in the person\'s words', () => {
    expect(refusalText(refused('APP_GROUP_FULL'))).toBe('This game is full.')
    expect(refusalText(refused('APP_GROUP_CLOSED'))).toBe('This game isn\'t taking anyone any more.')
    // No reason: whose household refused, and why, stays theirs.
    expect(refusalText(refused('APP_GROUP_CANT_JOIN'))).toBe('You can\'t join this game.')
    expect(refusalText(refused('APP_GROUP_TOO_FEW', { min: 3 }))).toContain('3')
  })

  it('anything else is a plain "didn\'t work"', () => {
    expect(refusalText(refused('SOMETHING_ELSE'))).toBe(refusalText(new Error('offline')))
  })
})
