import { describe, it, expect, beforeEach } from 'vitest'
import { spaceMembers } from '@/store/spaceMembers'
import { householdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import type { SpaceMemberProfile, User } from '@/types'
import { personName, spacePeople } from './scope'

const UID = 'u7x2k9qmzt4wbe5n'

beforeEach(() => {
  spaceMembers.value = {}
  householdUsers.value = new Map()
  currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me' } as User
})

describe('task scope — never a user id as a name', () => {
  it('a space member with no name reads "Someone"', () => {
    spaceMembers.value = {
      sp: new Map([[UID, { user_id: UID, role: 'member' } as unknown as SpaceMemberProfile]]),
    }
    const [p] = spacePeople('sp')
    expect(p.name).toBe('Someone')
    expect(p.name).not.toContain(UID.slice(0, 6))
  })

  it('a named member keeps their alias / space name / name, in that order', () => {
    spaceMembers.value = {
      sp: new Map([[UID, {
        user_id: UID, role: 'member', display_name: 'Mia', space_display_name: 'Mia B.', personal_alias: 'Mimi',
      } as unknown as SpaceMemberProfile]]),
    }
    expect(spacePeople('sp')[0].name).toBe('Mimi')
  })

  it('personName: You, a known person, else "Someone"', () => {
    householdUsers.value = new Map([['u-leo', { user_id: 'u-leo', username: 'leo', display_name: 'Leo' } as User]])
    expect(personName([], 'u-me')).toBe('You')
    expect(personName([], 'u-leo')).toBe('Leo')
    expect(personName([], UID)).toBe('Someone')
  })
})
