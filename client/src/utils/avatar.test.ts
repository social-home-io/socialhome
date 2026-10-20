/**
 * Name resolution for a bare user id (post / comment / highlight authors).
 * A raw user id must never reach the screen: a remote space member who set
 * no space name resolves to their household display name, and an unknown
 * id resolves to "Someone".
 */
import { afterEach, describe, expect, it } from 'vitest'
import { householdUsers } from '@/store/householdUsers'
import { spaceAuthors, spaceMembers } from '@/store/spaceMembers'
import type { SpaceMemberProfile, User } from '@/types'
import { resolveAvatar, resolveDisplayName } from './avatar'

function member(p: Partial<SpaceMemberProfile> & { user_id: string }): SpaceMemberProfile {
  return {
    role: 'member', joined_at: '2026-01-01', space_display_name: null,
    picture_hash: null, picture_url: null, ...p,
  }
}

afterEach(() => {
  householdUsers.value = new Map()
  spaceMembers.value = {}
  spaceAuthors.value = {}
})

describe('resolveDisplayName', () => {
  it('prefers the space name, then the household user', () => {
    householdUsers.value = new Map([['u1', { user_id: 'u1', display_name: 'Pascal' } as User]])
    spaceMembers.value = { s1: new Map([['u1', member({ user_id: 'u1', space_display_name: 'Papa' })]]) }
    expect(resolveDisplayName('s1', 'u1')).toBe('Papa')
    expect(resolveDisplayName(null, 'u1')).toBe('Pascal')
  })

  it('a remote space member with no space name shows their display name, not the id', () => {
    spaceMembers.value = {
      s1: new Map([['remote1', member({ user_id: 'remote1', display_name: 'Oma Vizeli', instance_id: 'peer' })]]),
    }
    expect(resolveDisplayName('s1', 'remote1')).toBe('Oma Vizeli')
  })

  it('an unknown id falls back to "Someone", never the id', () => {
    expect(resolveDisplayName('s1', 'tvnxzlljlae437w5fmwriqqvakgfynks')).toBe('Someone')
    expect(resolveDisplayName(null, 'tvnxzlljlae437w5fmwriqqvakgfynks')).toBe('Someone')
  })

  it('an explicit fallback still wins over "Someone"', () => {
    expect(resolveDisplayName(null, 'x', 'Home Assistant')).toBe('Home Assistant')
  })
})

describe('a GFS follower names an author from their published profile (v_60)', () => {
  const noor = {
    user_id: 'u-noor', display_name: 'Noor Haddad', bio: 'Beekeeper.',
    picture_url: 'api/spaces/s1/authors/u-noor/picture?v=1&sig=s',
  }

  it('names and pictures an author who is on no roster', () => {
    spaceAuthors.value = { s1: new Map([['u-noor', noor]]) }
    expect(resolveDisplayName('s1', 'u-noor')).toBe('Noor Haddad')
    expect(resolveAvatar('s1', 'u-noor')).toBe(noor.picture_url)
    // Another space's profile is not used.
    expect(resolveDisplayName('s2', 'u-noor')).toBe('Someone')
  })

  it('the roster and the household always win over a profile', () => {
    spaceAuthors.value = { s1: new Map([['u1', { ...noor, user_id: 'u1', display_name: 'Mallory' }]]) }
    spaceMembers.value = { s1: new Map([['u1', member({ user_id: 'u1', display_name: 'Pascal' })]]) }
    expect(resolveDisplayName('s1', 'u1')).toBe('Pascal')
  })
})
