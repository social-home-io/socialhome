import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { rateLimitWaitMessage } from './rateLimitWait'

describe('rateLimitWaitMessage — login / reset 429', () => {
  afterEach(async () => { await setLocale('en') })

  it('rounds the wait up to whole minutes', () => {
    expect(rateLimitWaitMessage(840, 'fallback')).toBe(
      'Too many tries. Wait 14 minutes, then try again.')
    expect(rateLimitWaitMessage(61, 'fallback')).toBe(
      'Too many tries. Wait 2 minutes, then try again.')
  })

  it('says "a minute" for a minute or less', () => {
    expect(rateLimitWaitMessage(1, 'fallback')).toBe(
      'Too many tries. Wait a minute, then try again.')
    expect(rateLimitWaitMessage(60, 'fallback')).toBe(
      'Too many tries. Wait a minute, then try again.')
  })

  it('reads a Retry-After header string', () => {
    expect(rateLimitWaitMessage('900', 'fallback')).toBe(
      'Too many tries. Wait 15 minutes, then try again.')
  })

  it('falls back when the wait is missing or odd', () => {
    for (const odd of [undefined, null, 0, -5, 'soon', Number.NaN]) {
      expect(rateLimitWaitMessage(odd, 'fallback')).toBe('fallback')
    }
  })

  it('is translated', async () => {
    await setLocale('de')
    expect(rateLimitWaitMessage(900, 'fallback')).toContain('15')
    expect(rateLimitWaitMessage(900, 'fallback')).not.toContain('Too many')
  })
})
