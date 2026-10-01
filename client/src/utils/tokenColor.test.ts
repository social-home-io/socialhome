import { describe, it, expect } from 'vitest'
import { colorClass, hashColor, normalizeTokenKey, TOKEN_COLORS } from './tokenColor'

describe('tokenColor', () => {
  it('normalises case, accents and spacing', () => {
    expect(normalizeTokenKey('  Música  Class ')).toBe('musica class')
  })
  it('the same name in any spelling gets the same palette token', () => {
    expect(hashColor('Garden')).toBe(hashColor(' garden '))
    expect(TOKEN_COLORS).toContain(hashColor('School'))
  })
  it('colorClass maps a token (or none) to its CSS class', () => {
    expect(colorClass('teal')).toBe('sh-timetable-c--teal')
    expect(colorClass(null)).toBe('sh-timetable-c--neutral')
  })
})
