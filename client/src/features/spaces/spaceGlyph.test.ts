import { describe, it, expect } from 'vitest'
import { HA_SPACE_SYMBOL, isHaSymbol, spaceGlyph, tierIcon, titleEmoji } from './spaceGlyph'

describe('spaceGlyph', () => {
  it('the emoji wins; else the first letter, never a globe or a house', () => {
    expect(spaceGlyph('🥕', 'Garden')).toBe('🥕')
    expect(spaceGlyph(null, 'Book club')).toBe('B')
    expect(spaceGlyph('', '  élternrat')).toBe('É')
    expect(spaceGlyph(null, '')).toBe('•')
    expect(spaceGlyph(undefined, undefined)).toBe('•')
  })

  it('takes a whole character, not half a surrogate pair', () => {
    expect(spaceGlyph(null, '𝒜lpha')).toBe('𝒜')
  })
})

describe('tierIcon', () => {
  it('marks each tier; nothing for an unknown one', () => {
    expect(['private', 'household', 'public', 'global'].map(tierIcon))
      .toEqual(['🔐', '🏠', '🤝', '🌐'])
    expect(tierIcon('weird')).toBeNull()
    expect(tierIcon(undefined)).toBeNull()
  })
})

describe('the Home Assistant symbol', () => {
  it('is a house with the text-presentation selector — what older households show', () => {
    expect(Array.from(HA_SPACE_SYMBOL).map(c => c.codePointAt(0)!.toString(16)))
      .toEqual(['1f3e0', 'fe0e'])
  })

  it('is told apart from a picked house', () => {
    expect(isHaSymbol(HA_SPACE_SYMBOL)).toBe(true)
    expect(isHaSymbol('🏠')).toBe(false)
    expect(isHaSymbol('🏠\uFE0F')).toBe(false)
    expect(isHaSymbol(null)).toBe(false)
  })

  it('never goes into a plain-text title; a real emoji does', () => {
    expect(titleEmoji(HA_SPACE_SYMBOL)).toBeNull()
    expect(titleEmoji('🥕')).toBe('🥕')
    expect(titleEmoji('')).toBeNull()
    expect(titleEmoji(null)).toBeNull()
  })
})
