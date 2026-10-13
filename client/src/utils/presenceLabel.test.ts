import { describe, it, expect } from 'vitest'
import { presenceLabel } from './presenceLabel'

describe('presenceLabel', () => {
  it('translates the known presence states', () => {
    expect(presenceLabel('home')).toBe('Home')
    expect(presenceLabel('away')).toBe('Away')
    expect(presenceLabel('not_home')).toBe('Not home')
    expect(presenceLabel('zone')).toBe('In zone')
  })

  it('names the zone for a zone state when the zone is known', () => {
    expect(presenceLabel('zone', 'Work')).toBe('Work')
    expect(presenceLabel('home', 'Work')).toBe('Home')
  })

  it('reads a missing state as unknown and keeps an unexpected one as sent', () => {
    expect(presenceLabel(null)).toBe('Unknown')
    expect(presenceLabel('')).toBe('Unknown')
    expect(presenceLabel('unavailable')).toBe('Unknown')
    expect(presenceLabel('sleeping')).toBe('sleeping')
  })
})
