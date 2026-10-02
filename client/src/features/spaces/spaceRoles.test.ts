/**
 * Space role helpers — the SPA mirror of ``domain/space.py``'s authority
 * sets and ``role_change_allowed``. UI hints only: the server re-checks.
 */
import { describe, it, expect } from 'vitest'
import {
  canContribute,
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


describe('canContribute — the §4.3 access level for one feature', () => {
  it.each([
    // level, role, may write
    ['open', 'member', true],
    ['open', 'moderator', true],
    ['open', 'subscriber', false],
    ['open', undefined, false],
    ['moderated', 'member', true], // a member's post queues — still a writer
    ['moderated', 'subscriber', false],
    ['admin_only', 'owner', true],
    ['admin_only', 'admin', true],
    ['admin_only', 'moderator', false],
    ['admin_only', 'member', false],
    ['admin_only', 'subscriber', false],
    [undefined, 'member', true], // a stub before the host config: open
  ] as const)('%s × %s → %s', (level, role, ok) => {
    expect(canContribute(level, role)).toBe(ok)
  })
})
