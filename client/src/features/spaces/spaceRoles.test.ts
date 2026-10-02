/**
 * Space role helpers — the SPA mirror of ``domain/space.py``'s authority
 * sets and ``role_change_allowed``. UI hints only: the server re-checks.
 */
import { describe, it, expect } from 'vitest'
import {
  canModerate,
  hasSettingsAuthority,
  isWriterRole,
  parseSpaceRole,
  roleChangeOptions,
} from './spaceRoles'

describe('authority sets', () => {
  it.each([
    ['owner', true, true, true],
    ['admin', true, true, true],
    ['moderator', false, true, true],
    ['member', false, false, true],
    ['subscriber', false, false, false],
    [undefined, false, false, false],
  ] as const)('%s → settings=%s content=%s writer=%s', (role, settings, content, writer) => {
    expect(hasSettingsAuthority(role)).toBe(settings)
    expect(canModerate(role)).toBe(content)
    expect(isWriterRole(role)).toBe(writer)
  })
})

describe('parseSpaceRole', () => {
  it('admits the five roles and nothing else', () => {
    for (const r of ['owner', 'admin', 'moderator', 'member', 'subscriber']) {
      expect(parseSpaceRole(r)).toBe(r)
    }
    expect(parseSpaceRole('overlord')).toBeUndefined()
    expect(parseSpaceRole(undefined)).toBeUndefined()
  })
})

describe('roleChangeOptions', () => {
  it('the owner offers admin / moderator / member, minus the current role', () => {
    expect(roleChangeOptions('owner', 'member')).toEqual(['admin', 'moderator'])
    expect(roleChangeOptions('owner', 'moderator')).toEqual(['admin', 'member'])
    expect(roleChangeOptions('owner', 'admin')).toEqual(['moderator', 'member'])
  })

  it('an admin moves a seat only between member and moderator', () => {
    expect(roleChangeOptions('admin', 'member')).toEqual(['moderator'])
    expect(roleChangeOptions('admin', 'moderator')).toEqual(['member'])
    expect(roleChangeOptions('admin', 'admin')).toEqual([])
    expect(roleChangeOptions('admin', 'subscriber')).toEqual([])
  })

  it('nobody changes the owner, and other roles change nothing', () => {
    expect(roleChangeOptions('owner', 'owner')).toEqual([])
    for (const actor of ['moderator', 'member', 'subscriber', undefined] as const) {
      expect(roleChangeOptions(actor, 'member')).toEqual([])
    }
  })
})
