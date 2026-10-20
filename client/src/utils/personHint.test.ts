import { describe, it, expect, beforeEach } from 'vitest'
import { householdUsers } from '@/store/householdUsers'
import { spaceAuthors, spaceMembers } from '@/store/spaceMembers'
import type { SpaceMemberProfile, User } from '@/types'
import { personHint, rosterPersonHint } from './personHint'

function user(over: Partial<User>): User {
  return {
    user_id: 'u-x', username: 'x', display_name: 'X', is_admin: false,
    picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    ...over,
  } as User
}

function member(over: Partial<SpaceMemberProfile>): SpaceMemberProfile {
  return {
    user_id: 'u-x', role: 'member', joined_at: '2026-01-01T00:00:00Z',
    space_display_name: null, picture_hash: null, picture_url: null,
    ...over,
  }
}

describe('rosterPersonHint', () => {
  it('a remote member: real name, alias, picture, household — never is_local', () => {
    const hint = rosterPersonHint({
      user_id: 'ru-chidi',
      display_name: 'Chidi Okafor',
      space_display_name: 'Coach',
      personal_alias: 'Chidi (football)',
      picture_url: 'api/pic/chidi',
      instance_id: 'p-okafor',
      household_name: 'Okafor family',
      username: 'chidi',
    })
    expect(hint).toEqual({
      display_name: 'Chidi Okafor',
      personal_alias: 'Chidi (football)',
      picture_url: 'api/pic/chidi',
      household: { instance_id: 'p-okafor', display_name: 'Okafor family', is_own: false },
    })
    // Never "complete": the sheet still fetches status / presence.
    expect(hint.is_local).toBeUndefined()
  })

  it('a remote member without a household name gets no household row', () => {
    const hint = rosterPersonHint({ user_id: 'ru', instance_id: 'p-x', display_name: 'R' })
    expect(hint.household).toBeUndefined()
  })

  it('a member of this household with a username is local (Message works at once)', () => {
    const hint = rosterPersonHint({
      user_id: 'u-oma', username: 'oma', display_name: 'Oma',
      instance_id: null, is_online: true, is_idle: false,
    })
    expect(hint).toEqual({
      display_name: 'Oma', is_local: true, username: 'oma',
      is_online: true, is_idle: false,
    })
  })

  it('falls back to the space name when the roster has no household name', () => {
    expect(rosterPersonHint({ user_id: 'u', space_display_name: 'Spacey' }).display_name)
      .toBe('Spacey')
  })
})

describe('personHint', () => {
  beforeEach(() => {
    householdUsers.value = new Map()
    spaceMembers.value = {}
  })

  it('unknown everywhere → empty hint (the sheet fetches it all)', () => {
    expect(personHint('s1', 'u-nobody')).toEqual({})
    expect(personHint(null, 'u-nobody')).toEqual({})
  })

  it('a household user: name, picture, username, local', () => {
    householdUsers.value = new Map([['u-oma', user({
      user_id: 'u-oma', username: 'oma', display_name: 'Oma', picture_url: 'api/u/oma',
    })]])
    expect(personHint(null, 'u-oma')).toEqual({
      display_name: 'Oma', picture_url: 'api/u/oma', is_local: true, username: 'oma',
    })
  })

  it('in a space the roster row wins, the household cache fills the gaps', () => {
    householdUsers.value = new Map([['u-oma', user({
      user_id: 'u-oma', username: 'oma', display_name: 'Oma', picture_url: 'api/u/oma',
    })]])
    spaceMembers.value = {
      s1: new Map([
        ['u-oma', member({ user_id: 'u-oma', display_name: 'Oma Vizeli', personal_alias: 'Granny' })],
        ['ru-chidi', member({
          user_id: 'ru-chidi', display_name: 'Chidi', picture_url: 'api/s/chidi',
          instance_id: 'p-okafor', household_name: 'Okafor family',
        })],
      ]),
    }
    expect(personHint('s1', 'u-oma')).toEqual({
      display_name: 'Oma Vizeli', personal_alias: 'Granny',
      picture_url: 'api/u/oma', is_local: true, username: 'oma',
    })
    expect(personHint('s1', 'ru-chidi')).toEqual({
      display_name: 'Chidi', picture_url: 'api/s/chidi',
      household: { instance_id: 'p-okafor', display_name: 'Okafor family', is_own: false },
    })
  })
})

describe('personHint for an author known only from their profile (v_60)', () => {
  it('carries the name, picture and public bio', () => {
    spaceAuthors.value = {
      s1: new Map([['u-noor', {
        user_id: 'u-noor', display_name: 'Noor Haddad', bio: 'Beekeeper.',
        picture_url: 'api/spaces/s1/authors/u-noor/picture?v=1',
      }]]),
    }
    expect(personHint('s1', 'u-noor')).toEqual({
      display_name: 'Noor Haddad',
      picture_url: 'api/spaces/s1/authors/u-noor/picture?v=1',
      bio: 'Beekeeper.',
    })
    spaceAuthors.value = {}
  })
})
