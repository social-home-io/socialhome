import { describe, it, expect } from 'vitest'
import { visibilityOptions, joinModeOptions, SPACE_CATEGORIES, categoryLabel } from './spaceModeOptions'

describe('spaceModeOptions', () => {
  it('lists four tiers in ladder order', () => {
    expect(visibilityOptions().map(o => o.value)).toEqual([
      'private', 'household', 'public', 'global',
    ])
  })

  it('labels tiers and join modes from the catalog', () => {
    expect(visibilityOptions()[0].title).toBe('Private')
    expect(visibilityOptions()[3].subtitle).toBe('Published worldwide through your GFS.')
    expect(joinModeOptions().map(o => o.title)).toEqual([
      'Invite only', 'Request to join', 'Open',
    ])
  })

  it('has ten discovery categories starting with general', () => {
    expect(SPACE_CATEGORIES[0]).toEqual({ value: 'general', label: 'General' })
    expect(SPACE_CATEGORIES).toHaveLength(10)
    expect(SPACE_CATEGORIES.map(c => c.value)).toEqual([
      'general', 'hobby_crafts', 'sports_outdoors', 'gaming', 'music_arts',
      'food_drink', 'tech', 'local', 'family_parenting', 'learning',
    ])
  })

  it('categoryLabel maps known values and falls back to General', () => {
    expect(categoryLabel('gaming')).toBe('Gaming')
    expect(categoryLabel('nonsense')).toBe('General')
    expect(categoryLabel(undefined)).toBe('General')
    expect(categoryLabel(null)).toBe('General')
  })
})
