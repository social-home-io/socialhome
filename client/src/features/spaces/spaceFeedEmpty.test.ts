import { describe, expect, it } from 'vitest'
import { emptyFeedBodyKey } from './spaceFeedEmpty'
import en from '@/i18n/locales/en.json'

describe('emptyFeedBodyKey', () => {
  it('a private space with just you names no audience', () => {
    expect(emptyFeedBodyKey('private', 1, false)).toBe('space.feed.empty_body_solo')
    expect(emptyFeedBodyKey('household', 1, false)).toBe('space.feed.empty_body_solo')
    // Unknown count: the line that is always true.
    expect(emptyFeedBodyKey('private', null, false)).toBe('space.feed.empty_body_solo')
  })

  it('a private or household space with members says the members read it', () => {
    expect(emptyFeedBodyKey('private', 3, false)).toBe('space.feed.empty_body_members')
    expect(emptyFeedBodyKey('household', 4, undefined)).toBe('space.feed.empty_body_members')
  })

  it('mentions other households only when one has a member here', () => {
    expect(emptyFeedBodyKey('private', 3, true)).toBe('space.feed.empty_body')
    expect(emptyFeedBodyKey('public', 1, true)).toBe('space.feed.empty_body')
  })

  it('a public or global space says everyone who joins reads it', () => {
    expect(emptyFeedBodyKey('public', 1, false)).toBe('space.feed.empty_body_open')
    expect(emptyFeedBodyKey('global', 5, false)).toBe('space.feed.empty_body_open')
  })

  it('only the other-households line mentions other households', () => {
    const t = en as Record<string, string>
    for (const key of ['space.feed.empty_body_solo', 'space.feed.empty_body_members', 'space.feed.empty_body_open']) {
      expect(t[key]).toBeTruthy()
      expect(t[key]).not.toMatch(/household/i)
    }
    expect(t['space.feed.empty_body']).toMatch(/other households/)
  })
})
