import { describe, it, expect } from 'vitest'
import {
  MUTE_DURATIONS,
  isMuteActive,
  isMutedForever,
  muteDurationLabel,
  mutedLabel,
} from './mute'

const NOW = Date.parse('2026-09-29T10:00:00Z')

describe('mute helpers', () => {
  it('a mute in the future is on, one in the past or missing is off', () => {
    expect(isMuteActive('2026-09-29T11:00:00+00:00', NOW)).toBe(true)
    expect(isMuteActive('2026-09-29T09:00:00+00:00', NOW)).toBe(false)
    expect(isMuteActive('9999-12-31T23:59:59+00:00', NOW)).toBe(true)
    expect(isMuteActive(null, NOW)).toBe(false)
    expect(isMuteActive(undefined, NOW)).toBe(false)
    expect(isMuteActive('garbage', NOW)).toBe(false)
  })

  it('recognises the until-I-unmute sentinel', () => {
    expect(isMutedForever('9999-12-31T23:59:59+00:00')).toBe(true)
    expect(isMutedForever('2026-09-29T11:00:00+00:00')).toBe(false)
    expect(isMutedForever(null)).toBe(false)
  })

  it('labels a forever mute without a time, a timed one with its end', () => {
    expect(mutedLabel('9999-12-31T23:59:59+00:00', NOW)).toBe('Muted until you turn it back on')
    expect(mutedLabel('2026-09-29T11:00:00+00:00', NOW)).toMatch(/^Muted until \S/)
    // A week out names the day too, not just a clock time.
    const week = mutedLabel('2026-10-06T10:00:00+00:00', NOW)
    expect(week).toMatch(/^Muted until /)
    expect(week).toMatch(/6/)
  })

  it('has an English label for every length', () => {
    expect(MUTE_DURATIONS.map(muteDurationLabel)).toEqual([
      'For 1 hour', 'For 8 hours', 'For 1 week', 'Until I turn it back on',
    ])
  })
})
