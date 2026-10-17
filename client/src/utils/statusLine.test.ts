import { describe, it, expect } from 'vitest'
import { statusLine } from './statusLine'

const NOW = Date.parse('2026-10-17T12:00:00Z')

describe('statusLine', () => {
  it('joins emoji and text', () => {
    expect(statusLine({ emoji: '🌴', text: 'On leave', expires_at: null }, NOW)).toBe('🌴 On leave')
  })

  it('shows an emoji-only or text-only status on its own', () => {
    expect(statusLine({ emoji: '🎧', text: null, expires_at: null }, NOW)).toBe('🎧')
    expect(statusLine({ emoji: null, text: 'Focus', expires_at: null }, NOW)).toBe('Focus')
  })

  it('is null when unset', () => {
    expect(statusLine(null, NOW)).toBeNull()
    expect(statusLine(undefined, NOW)).toBeNull()
    expect(statusLine({ emoji: null, text: null, expires_at: null }, NOW)).toBeNull()
  })

  it('is null once the deadline has passed, shown while it is ahead', () => {
    expect(statusLine({ emoji: null, text: 'Lunch', expires_at: '2026-10-17T11:59:59Z' }, NOW)).toBeNull()
    expect(statusLine({ emoji: null, text: 'Lunch', expires_at: '2026-10-17T12:30:00Z' }, NOW)).toBe('Lunch')
  })
})
