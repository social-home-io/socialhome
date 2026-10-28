/**
 * Settings → Notifications (D4): this device, the seven kinds, and
 * "Muted and changed" with its way back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({
  get: vi.fn(), put: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn(),
}))
const toast = vi.hoisted(() => vi.fn())
const push = vi.hoisted(() => ({
  currentPushSubscription: vi.fn().mockResolvedValue(null),
  enableWebPush: vi.fn().mockResolvedValue(true),
  disableWebPush: vi.fn().mockResolvedValue(undefined),
  webPushSupported: vi.fn(() => true),
}))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/utils/webPush', () => push)

import { DeviceBlock, KindsBlock, MutedAndChanged } from './NotificationSettings'
import { notifSettings, type NotifChange, type KindSetting } from '@/store/notifSettings'

const ALL: KindSetting['choices'] = ['push', 'in_app', 'off']
const KIND_ROWS: KindSetting[] = [
  { kind: 'messages', choice: 'push', default: 'push', choices: ['push', 'off'] },
  { kind: 'replies', choice: 'push', default: 'push', choices: ALL },
  { kind: 'needs_you', choice: 'push', default: 'push', choices: ['push', 'in_app'] },
  { kind: 'plans', choice: 'push', default: 'push', choices: ALL },
  { kind: 'about_you', choice: 'push', default: 'push', choices: ALL },
  { kind: 'reactions', choice: 'in_app', default: 'in_app', choices: ALL },
  { kind: 'activity', choice: 'in_app', default: 'in_app', choices: ALL },
]

function line(over: Partial<NotifChange>): NotifChange {
  return {
    target: 'space', id: 's1', level: 'mentions', default_level: 'all', place: 'household',
    name: 'Familie Alpha', muted_until: null, then_level: null, space_id: null,
    space_name: null, snippet: null, ...over,
  }
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  toast.mockReset()
  push.currentPushSubscription.mockReset().mockResolvedValue(null)
  push.webPushSupported.mockReset().mockReturnValue(true)
  notifSettings.value = null
})

describe('DeviceBlock', () => {
  it('push off: says so and offers Turn on', async () => {
    const { findByText, getByRole } = render(<DeviceBlock />)
    expect(await findByText('Push is off for this browser')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(push.enableWebPush).toHaveBeenCalled())
    expect(await findByText('Push is on for this browser')).toBeTruthy()
  })

  it('a browser without push says what still works', async () => {
    push.webPushSupported.mockReturnValue(false)
    const { findByText, queryByRole } = render(<DeviceBlock />)
    expect(await findByText("This browser can't show push")).toBeTruthy()
    expect(queryByRole('button')).toBeNull()
  })

  it('more device rows go inside', () => {
    const { getByText } = render(<DeviceBlock><p>HA app row</p></DeviceBlock>)
    expect(getByText('HA app row')).toBeTruthy()
  })
})

describe('KindsBlock', () => {
  it('seven kinds, each one choice with an example; a pick saves', async () => {
    notifSettings.value = { kinds: KIND_ROWS, changed: [] }
    api.patch.mockResolvedValue({ kinds: KIND_ROWS.map(k => (k.kind === 'reactions' ? { ...k, choice: 'push' } : k)) })
    const { getAllByRole, getByRole, getByText } = render(<KindsBlock />)
    const groups = getAllByRole('radiogroup')
    expect(groups).toHaveLength(7)
    expect(getByText('Messages and calls')).toBeTruthy()
    expect(getByText('“25 new posts in Familie Alpha”')).toBeTruthy()
    // Needs you has no Off; Messages and calls no In app.
    const needs = getByRole('radiogroup', { name: 'Needs you' })
    expect(Array.from(needs.querySelectorAll('[role="radio"]')).map(r => r.textContent))
      .toEqual(['Push', 'In app'])
    const reactions = getByRole('radiogroup', { name: 'Reactions and follows' })
    const pushOpt = Array.from(reactions.querySelectorAll<HTMLElement>('[role="radio"]'))
      .find(r => r.textContent === 'Push')!
    fireEvent.click(pushOpt)
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      '/api/me/notification-settings', { kind: 'reactions', choice: 'push' },
    ))
  })

  it('a failed save rolls back and says so', async () => {
    notifSettings.value = { kinds: KIND_ROWS, changed: [] }
    api.patch.mockRejectedValue(new Error('500'))
    const { getByRole } = render(<KindsBlock />)
    const plans = getByRole('radiogroup', { name: 'Plans and tasks' })
    const off = Array.from(plans.querySelectorAll<HTMLElement>('[role="radio"]'))
      .find(r => r.textContent === 'Off')!
    fireEvent.click(off)
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't save. Try again.", 'error'))
    expect(notifSettings.value!.kinds.find(k => k.kind === 'plans')!.choice).toBe('push')
  })
})

describe('MutedAndChanged', () => {
  it('says when nothing is muted or changed', () => {
    notifSettings.value = { kinds: KIND_ROWS, changed: [] }
    const { getByText } = render(<MutedAndChanged />)
    expect(getByText(/Nothing muted or changed/)).toBeTruthy()
  })

  it('lists spaces, chats and posts; Change links, Unmute undoes', async () => {
    const soon = new Date(Date.now() + 3600e3).toISOString()
    notifSettings.value = {
      kinds: KIND_ROWS,
      changed: [
        line({}),
        line({ id: 's2', name: 'Nachbarschaft', place: 'public', level: 'muted', default_level: 'mentions', muted_until: soon, then_level: 'mentions' }),
        line({ target: 'chat', id: 'g1', name: 'Weekend plans', place: 'group', level: 'muted', muted_until: '9999-12-31T23:59:59+00:00', then_level: 'all' }),
        line({ target: 'chat', id: 'hc', name: null, place: 'household', level: 'mentions' }),
        line({ target: 'post', id: 'post:p1', name: null, place: 'post', level: 'muted', snippet: 'Flohmarkt am Samstag' }),
      ],
    }
    api.put.mockResolvedValue({ level: 'mentions', default_level: 'mentions', muted_until: null })
    api.delete.mockResolvedValue(undefined)
    const { getByRole, getByText, getAllByRole } = render(<MutedAndChanged />)
    expect(getAllByRole('listitem')).toHaveLength(5)
    expect(getByText('Household space')).toBeTruthy()
    expect(getByText(/then back to Replies and mentions/)).toBeTruthy()
    expect(getByText('“Flohmarkt am Samstag”')).toBeTruthy()
    expect(getByText('Post muted')).toBeTruthy()
    expect(getByText('Household chat', { selector: 'b' })).toBeTruthy()
    // Change goes to the space's own notification settings (base-anchored).
    const change = getByRole('link', { name: 'Change Familie Alpha' }) as HTMLAnchorElement
    expect(change.getAttribute('href')).toMatch(/\/spaces\/s1\/settings\/notifications$/)
    expect(getByRole('link', { name: 'Change Household chat' }).getAttribute('href')).toBe('#household-chat')
    // Unmute: a timed space mute goes back to its level from before.
    fireEvent.click(getByRole('button', { name: 'Unmute Nachbarschaft' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/spaces/s2/notif-prefs', { level: 'mentions' }))
    fireEvent.click(getByRole('button', { name: 'Unmute “Flohmarkt am Samstag”' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
      '/api/me/notification-settings/muted-posts/post%3Ap1',
    ))
    fireEvent.click(getByRole('button', { name: 'Unmute Weekend plans' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/conversations/g1/mute'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Weekend plans: notifications back on', 'success'))
  })
})
