import { describe, expect, test } from 'vitest'
import type { SpaceMemberProfile } from '@/types'
import {
  findMentionTrigger,
  mentionCandidates,
  mentionTokenSet,
  splitMentions,
} from './mentions'

function member(p: Partial<SpaceMemberProfile> & { user_id: string }): SpaceMemberProfile {
  return {
    role: 'member',
    joined_at: '',
    space_display_name: null,
    picture_hash: null,
    picture_url: null,
    ...p,
  }
}

const MEMBERS = [
  member({ user_id: 'u-me', display_name: 'Me Myself', mention: 'me' }),
  member({ user_id: 'u-anna', display_name: 'Anna Berg', mention: 'anna' }),
  member({
    user_id: 'u-anna2', display_name: 'Anna Smith', mention: 'anna@k3f9x2',
    instance_id: 'peer', household_name: 'The Smiths',
  }),
  member({ user_id: 'u-bob', display_name: 'Robert', mention: 'bob' }),
  member({ user_id: 'u-zed', display_name: 'Zed', mention: null }),
]

describe('findMentionTrigger', () => {
  test('opens on a bare @ and on a partial token at the cursor', () => {
    expect(findMentionTrigger('hi @', 4)).toEqual({ start: 3, query: '' })
    expect(findMentionTrigger('hi @an', 6)).toEqual({ start: 3, query: 'an' })
    expect(findMentionTrigger('@an', 3)).toEqual({ start: 0, query: 'an' })
  })

  test('ignores e-mail addresses and tokens away from the cursor', () => {
    expect(findMentionTrigger('bob@exa', 7)).toBeNull()
    expect(findMentionTrigger('@anna hi', 8)).toBeNull()
    expect(findMentionTrigger('hi @an', 2)).toBeNull()
  })

  test('accepts unicode handles', () => {
    expect(findMentionTrigger('@jösé', 5)).toEqual({ start: 0, query: 'jösé' })
  })
})

describe('mentionCandidates', () => {
  test('empty query lists everyone mentionable except the viewer', () => {
    const ids = mentionCandidates(MEMBERS, '', 'u-me').map(c => c.userId)
    expect(ids).toEqual(['u-anna', 'u-anna2', 'u-bob'])
  })

  test('prefix-matches token and any word of the display name', () => {
    expect(mentionCandidates(MEMBERS, 'rob', 'u-me').map(c => c.userId))
      .toEqual(['u-bob'])
    expect(mentionCandidates(MEMBERS, 'smi', 'u-me').map(c => c.userId))
      .toEqual(['u-anna2'])
    expect(mentionCandidates(MEMBERS, 'ANN', 'u-me').map(c => c.token))
      .toEqual(['anna', 'anna@k3f9x2'])
  })

  test('labels remote members with their household', () => {
    const [remote] = mentionCandidates(MEMBERS, 'anna s', 'u-me')
    expect(remote?.household).toBe('The Smiths')
  })

  test('prefers the space nickname for the shown name', () => {
    const rows = [member({
      user_id: 'u1', display_name: 'Anna', space_display_name: 'Nana', mention: 'anna',
    })]
    expect(mentionCandidates(rows, 'na', null)[0].name).toBe('Nana')
  })

  test('caps the list', () => {
    const many = Array.from({ length: 20 }, (_, i) =>
      member({ user_id: `u${i}`, display_name: `P${i}`, mention: `p${i}` }))
    expect(mentionCandidates(many, 'p', null)).toHaveLength(8)
  })
})

describe('mentionCandidates — @here', () => {
  test('offered first, only when includeHere, on a prefix of "here"', () => {
    const withHere = mentionCandidates(MEMBERS, 'h', 'u-me', { includeHere: true })
    expect(withHere[0]).toMatchObject({ token: 'here', here: true })
    expect(mentionCandidates(MEMBERS, '', 'u-me', { includeHere: true })[0].token)
      .toBe('here')
    expect(mentionCandidates(MEMBERS, 'an', 'u-me', { includeHere: true })
      .map(c => c.token)).toEqual(['anna', 'anna@k3f9x2'])
    expect(mentionCandidates(MEMBERS, 'h', 'u-me').some(c => c.here)).toBe(false)
  })
})

describe('splitMentions', () => {
  const tokens = mentionTokenSet(MEMBERS)

  test('only known tokens become mention parts', () => {
    expect(splitMentions('hi @anna and @nobody.', tokens)).toEqual([
      'hi ', { token: 'anna', raw: '@anna' }, ' and @nobody.',
    ])
  })

  test('qualified tokens and trailing punctuation', () => {
    expect(splitMentions('@Anna@k3f9x2, ok', tokens)).toEqual([
      { token: 'anna@k3f9x2', raw: '@Anna@k3f9x2' }, ', ok',
    ])
  })

  test('e-mail addresses are not mentions', () => {
    expect(splitMentions('mail bob@anna.org', tokens)).toEqual(['mail bob@anna.org'])
  })

  test('no tokens → single text part', () => {
    expect(splitMentions('@anna', new Set())).toEqual(['@anna'])
  })
})
