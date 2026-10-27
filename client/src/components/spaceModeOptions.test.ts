import { describe, it, expect, afterEach } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import {
  visibilityOptions, joinModeOptions, SPACE_CATEGORIES, categoryLabel, hasJoinModeChoice,
  joinOptionsForVisibility,
} from './spaceModeOptions'

describe('spaceModeOptions', () => {
  it('lists four tiers in ladder order', () => {
    expect(visibilityOptions().map(o => o.value)).toEqual([
      'private', 'household', 'public', 'global',
    ])
  })

  it('labels tiers and join modes from the catalog', () => {
    expect(visibilityOptions()[0].title).toBe('Private')
    expect(visibilityOptions()[3].subtitle).toBe('Listed on GFSs the owner picks, for anyone to find.')
    expect(joinModeOptions().map(o => o.title)).toEqual([
      'Invite only', 'Request to join', 'Open',
    ])
  })

  it('a local community says who can join: households in the area, the edge to an admin', () => {
    const local = joinModeOptions({ local: true })
    expect(local.map(o => o.value)).toEqual(['invite_only', 'request', 'open'])
    expect(local.map(o => o.subtitle)).toEqual([
      'You send invites. An invited household has to live in the area.',
      'Households in the area ask to join, and an admin says yes.',
      'Households in the area join right away. Near the edge, an admin decides.',
    ])
    expect(local.map(o => o.subtitle).join(' ')).not.toMatch(/Anyone/)
    // Not a local community: unchanged.
    expect(joinModeOptions()[2].subtitle).toBe('Anyone can join right away.')
    expect(joinOptionsForVisibility('public', { local: true })[2].subtitle)
      .toBe('Households in the area join right away. Near the edge, an admin decides.')
  })

  it('has twenty discovery categories starting with general', () => {
    expect(SPACE_CATEGORIES[0]).toEqual({ value: 'general', label: 'General' })
    expect(SPACE_CATEGORIES).toHaveLength(20)
    expect(SPACE_CATEGORIES.map(c => c.value)).toEqual([
      'general', 'local', 'community', 'non_profit', 'family_parenting',
      'learning', 'books_writing', 'hobby_crafts', 'music_arts', 'food_drink',
      'sports_outdoors', 'health_wellbeing', 'nature_garden', 'pets_animals',
      'travel', 'gaming', 'tech', 'faith_spirituality', 'politics_activism',
      'work_career',
    ])
    // Every one has a real English label (no raw key leaking through).
    for (const c of SPACE_CATEGORIES) expect(c.label).not.toMatch(/^space\./)
  })

  it('categoryLabel maps known values and falls back to General', () => {
    expect(categoryLabel('gaming')).toBe('Gaming')
    expect(categoryLabel('nonsense')).toBe('General')
    expect(categoryLabel(undefined)).toBe('General')
    expect(categoryLabel(null)).toBe('General')
  })

  describe('in German', () => {
    afterEach(async () => { await setLocale('en') })

    it('translates the category labels but keeps the stored values', async () => {
      await setLocale('de')
      expect(SPACE_CATEGORIES.find(c => c.value === 'gaming')?.label).toBe('Spiele')
      expect(SPACE_CATEGORIES.map(c => c.value)).toContain('food_drink')
      expect(categoryLabel('food_drink')).toBe('Essen & Trinken')
      expect(categoryLabel('nonsense')).toBe('Allgemein')
    })
  })
})

describe('hasJoinModeChoice', () => {
  it('a household space has no join mode to pick — everyone at home is in', () => {
    expect(hasJoinModeChoice('household')).toBe(false)
    for (const tier of ['private', 'public', 'global']) {
      expect(hasJoinModeChoice(tier)).toBe(true)
    }
  })
})
