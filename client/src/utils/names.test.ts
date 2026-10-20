import { describe, it, expect } from 'vitest'
import { firstName, householdLabel, personLabel } from './names'

describe('names — never an id as a label', () => {
  it('firstName takes the first non-blank candidate, trimmed', () => {
    expect(firstName(null, '', '  ', ' Anna ', 'Bob')).toBe('Anna')
    expect(firstName(undefined, null)).toBeNull()
  })

  it('personLabel falls back to "Someone"', () => {
    expect(personLabel('Mia')).toBe('Mia')
    expect(personLabel(null, '')).toBe('Someone')
  })

  it('householdLabel falls back to "another household"', () => {
    expect(householdLabel(null, 'Keller household')).toBe('Keller household')
    expect(householdLabel(undefined)).toBe('another household')
  })
})
