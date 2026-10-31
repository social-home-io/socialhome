import { describe, it, expect } from 'vitest'
import {
  legacySettingsSection, parseSettingsSection, resolveSettingsSection, settingsSectionLabel,
  settingsSectionPath, settingsSections,
} from './spaceSettingsSections'

describe('space settings sections', () => {
  it('admins get every section, Danger last; never a one-item index', () => {
    expect(settingsSections('admin')).toEqual([
      'basics', 'find', 'members', 'sections', 'notifications',
      'location', 'moderation', 'bots', 'danger',
    ])
    for (const v of ['admin', 'remote-admin', 'member', 'follower'] as const) {
      expect(settingsSections(v).length).toBeGreaterThan(1)
    }
  })

  it('a member gets their own surface; a follower notifications + Unfollow only', () => {
    expect(settingsSections('member')).toEqual(['notifications', 'you', 'bots', 'leave'])
    expect(settingsSections('follower')).toEqual(['notifications', 'unfollow'])
  })

  it('Games — for an admin after Sections, for a moderator after Notifications (D9)', () => {
    expect(settingsSections('admin', { games: true })).toEqual([
      'basics', 'find', 'members', 'sections', 'games', 'notifications',
      'location', 'moderation', 'bots', 'danger',
    ])
    expect(settingsSections('remote-admin', { games: true })).toContain('games')
    expect(settingsSections('member', { games: true }))
      .toEqual(['notifications', 'games', 'you', 'bots', 'leave'])
    // A follower never; nobody without the flag (a plain member, a
    // global space).
    expect(settingsSections('follower', { games: true })).not.toContain('games')
    expect(settingsSections('admin')).not.toContain('games')
    expect(resolveSettingsSection('games', 'member', { games: true })).toBe('games')
    expect(resolveSettingsSection('games', 'member')).toBe('notifications')
    expect(parseSettingsSection('games')).toBe('games')
    expect(settingsSectionLabel('games')).toBe('Games allowed here')
  })

  it('has one Bots section at most (no repeated "Bots & automations")', () => {
    for (const v of ['admin', 'remote-admin', 'member', 'follower'] as const) {
      expect(settingsSections(v).filter(s => s === 'bots').length).toBeLessThanOrEqual(1)
    }
  })

  it('maps an old ?household= link onto Members & joining', () => {
    expect(legacySettingsSection({ household: 'inst-1' })).toBe('members')
    expect(legacySettingsSection({})).toBeNull()
    expect(legacySettingsSection(undefined)).toBeNull()
  })

  it('parses a path segment, refusing unknown ones', () => {
    expect(parseSettingsSection('members')).toBe('members')
    expect(parseSettingsSection('general')).toBeNull()
    expect(parseSettingsSection(undefined)).toBeNull()
  })

  it('resolves to the viewer’s own first section when the asked one is not theirs', () => {
    expect(resolveSettingsSection('sections', 'admin')).toBe('sections')
    expect(resolveSettingsSection(undefined, 'admin')).toBe('basics')
    expect(resolveSettingsSection('danger', 'member')).toBe('notifications')
    expect(resolveSettingsSection('nope', 'follower')).toBe('notifications')
  })

  it('every section has its own stripped path and a label', () => {
    expect(settingsSectionPath('s1', 'location')).toBe('/spaces/s1/settings/location')
    expect(settingsSectionLabel('find')).toBe('Who can find it')
    expect(settingsSectionLabel('you')).toBe('You in this space')
  })
})
