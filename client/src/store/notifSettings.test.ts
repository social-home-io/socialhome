import { describe, it, expect, vi, beforeEach } from 'vitest'

const api = vi.hoisted(() => ({
  get: vi.fn(), put: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn(),
}))
vi.mock('@/api', () => ({ api }))

import {
  KINDS, isPostKey, kindChoice, loadNotifSettings, mutePost, notifSettings,
  notifSettingsError, parseSpaceLevel, setKind, setSpaceLevel, unmutePost,
  type NotifChange, type NotifSettingsState,
} from './notifSettings'

const STATE: NotifSettingsState = {
  kinds: [
    { kind: 'reactions', choice: 'in_app', default: 'in_app', choices: ['push', 'in_app', 'off'] },
    { kind: 'replies', choice: 'push', default: 'push', choices: ['push', 'in_app', 'off'] },
  ],
  changed: [
    {
      target: 'space', id: 's1', level: 'mentions', default_level: 'all', place: 'household',
      name: 'Fam', muted_until: null, then_level: null, space_id: null, space_name: null,
      snippet: null,
    },
    {
      target: 'post', id: 'post:p1', level: 'muted', default_level: 'all', place: 'post',
      name: null, muted_until: null, then_level: null, space_id: null, space_name: null,
      snippet: 'hi',
    },
  ] as NotifChange[],
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  notifSettings.value = null
  notifSettingsError.value = false
})

describe('notifSettings store', () => {
  it('eight kinds in Settings order', () => {
    expect(KINDS).toEqual([
      'messages', 'replies', 'needs_you', 'plans', 'about_you', 'games', 'reactions', 'activity',
    ])
  })

  it('loads, and flags a failure', async () => {
    api.get.mockResolvedValue(STATE)
    await loadNotifSettings()
    expect(api.get).toHaveBeenCalledWith('/api/me/notification-settings')
    expect(kindChoice('reactions')).toBe('in_app')
    expect(kindChoice('plans')).toBeNull()
    api.get.mockRejectedValue(new Error('offline'))
    await loadNotifSettings()
    expect(notifSettingsError.value).toBe(true)
    expect(notifSettings.value).not.toBeNull() // the last good copy stays
  })

  it('setKind is optimistic and rolls back on a failure', async () => {
    notifSettings.value = STATE
    api.patch.mockResolvedValue({ kinds: [{ ...STATE.kinds[0], choice: 'push' }, STATE.kinds[1]] })
    await setKind('reactions', 'push')
    expect(api.patch).toHaveBeenCalledWith('/api/me/notification-settings', {
      kind: 'reactions', choice: 'push',
    })
    expect(kindChoice('reactions')).toBe('push')
    api.patch.mockRejectedValue(new Error('422'))
    await expect(setKind('replies', 'off')).rejects.toThrow('422')
    expect(kindChoice('replies')).toBe('push')
  })

  it('mutes and unmutes a post; unmuting drops its line', async () => {
    notifSettings.value = STATE
    await mutePost('space_post:x')
    expect(api.post).toHaveBeenCalledWith('/api/me/notification-settings/muted-posts', {
      object_key: 'space_post:x',
    })
    await unmutePost('post:p1')
    expect(api.delete).toHaveBeenCalledWith('/api/me/notification-settings/muted-posts/post%3Ap1')
    expect(notifSettings.value!.changed.map(c => c.id)).toEqual(['s1'])
  })

  it('isPostKey', () => {
    expect(isPostKey('post:p1')).toBe(true)
    expect(isPostKey('space_post:a-b_c')).toBe(true)
    expect(isPostKey('dm:c1')).toBe(false)
    expect(isPostKey(null)).toBe(false)
  })

  it('setSpaceLevel sends a length only with Nothing, and keeps the list in step', async () => {
    notifSettings.value = STATE
    api.put.mockResolvedValue({
      level: 'all', is_default: false, default_level: 'all', muted_until: null, then_level: null,
    })
    await setSpaceLevel('s1', 'all')
    expect(api.put).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs', { level: 'all' })
    expect(notifSettings.value!.changed.map(c => c.id)).toEqual(['post:p1'])
    notifSettings.value = STATE
    api.put.mockResolvedValue({
      level: 'muted', default_level: 'all', muted_until: '2026-10-28T10:00:00Z', then_level: 'mentions',
    })
    await setSpaceLevel('s1', 'muted', '8h')
    expect(api.put).toHaveBeenLastCalledWith('/api/spaces/s1/notif-prefs', { level: 'muted', duration: '8h' })
    expect(notifSettings.value!.changed[0]).toMatchObject({ level: 'muted', then_level: 'mentions' })
    await setSpaceLevel('s1', 'muted')
    expect(api.put).toHaveBeenLastCalledWith('/api/spaces/s1/notif-prefs', { level: 'muted', duration: 'forever' })
  })

  it('parseSpaceLevel reads garbage as the default', () => {
    expect(parseSpaceLevel(null)).toEqual({
      level: 'all', is_default: false, default_level: 'all', muted_until: null, then_level: null,
    })
    expect(parseSpaceLevel({ level: 'x' as never, default_level: 'mentions' }).level).toBe('mentions')
  })
})
