/**
 * A row's "…" menu: mute this post, change the space's / chat's level,
 * turn the kind down — each with an Undo toast (redesign §4, D4, D9, D10).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({
  get: vi.fn(), put: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn(),
}))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import type { Notification, Space } from '@/types'
import { spaces } from '@/store/spaces'
import { notifSettings, type KindSetting } from '@/store/notifSettings'
import {
  NotificationRowMenu, chatOf, closestDuration, turnDown,
} from './NotificationRowMenu'

const ALL: KindSetting['choices'] = ['push', 'in_app', 'off']

function row(extra: Partial<Notification> = {}): Notification {
  return {
    id: 'n1', type: 'post_replied', title: 't', body: null, link_url: '/post/p1',
    read_at: null, created_at: new Date().toISOString(), object_key: 'post:p1',
    space_id: null, params: null, kind: 'replies', ...extra,
  }
}

function settings(choice: KindSetting['choice'] = 'push') {
  notifSettings.value = {
    kinds: [
      { kind: 'replies', choice, default: 'push', choices: ALL },
      { kind: 'needs_you', choice: 'push', default: 'push', choices: ['push', 'in_app'] },
    ],
    changed: [],
  }
}

function open(n: Notification) {
  const view = render(<NotificationRowMenu n={n} label="Ben commented on your post" />)
  fireEvent.click(view.getByRole('button', { name: 'More for Ben commented on your post' }))
  return view
}

/** The Undo of the last toast. */
function undo() {
  const opts = toast.mock.calls.at(-1)![2] as { action: { label: string, onClick: () => void } }
  expect(opts.action.label).toBe('Undo')
  opts.action.onClick()
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset().mockResolvedValue({})
  toast.mockReset()
  spaces.value = []
  settings()
})

describe('helpers', () => {
  it('turnDown: Push → In app → Off → nothing', () => {
    expect(turnDown('push')).toBe('in_app')
    expect(turnDown('in_app')).toBe('off')
    expect(turnDown('off')).toBeNull()
    expect(turnDown(null)).toBeNull()
  })

  it('chatOf reads a chat row', () => {
    expect(chatOf(row({ object_key: 'dm:c1' }))).toBe('c1')
    expect(chatOf(row())).toBeNull()
  })

  it('closestDuration restores a mute about as long as it was', () => {
    expect(closestDuration(null)).toBe('forever')
    expect(closestDuration('9999-12-31T23:59:59+00:00')).toBe('forever')
    expect(closestDuration(new Date(Date.now() + 3600e3).toISOString())).toBe('1h')
    expect(closestDuration(new Date(Date.now() + 6 * 3600e3).toISOString())).toBe('8h')
    expect(closestDuration(new Date(Date.now() + 5 * 24 * 3600e3).toISOString())).toBe('1w')
  })
})

describe('NotificationRowMenu', () => {
  it('a post row: mute the post, turn replies push off, settings; focus on the first item', async () => {
    const { getAllByRole, getByRole } = open(row())
    const items = getAllByRole('menuitem').map(i => i.querySelector('.sh-notif-menu__label')!.textContent)
    expect(items).toEqual([
      'Mute this post', 'Turn off “Replies and mentions” push', 'Notification settings',
    ])
    await waitFor(() => expect(document.activeElement).toBe(getAllByRole('menuitem')[0]))
    expect(getByRole('menuitem', { name: /Notification settings/ }).getAttribute('href'))
      .toMatch(/\/settings#notifications$/)
  })

  it('Mute this post, then Undo', async () => {
    const { getByRole, queryByRole } = open(row())
    fireEvent.click(getByRole('menuitem', { name: /^Mute this post/ }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/me/notification-settings/muted-posts', { object_key: 'post:p1' },
    ))
    expect(queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(getByRole('button', { name: /^More for/ }))
    expect(toast.mock.calls.at(-1)![0]).toBe('Post muted — no more notifications about it')
    undo()
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
      '/api/me/notification-settings/muted-posts/post%3Ap1',
    ))
  })

  it('turn the kind down: Push → In app, then Undo puts Push back', async () => {
    api.patch.mockResolvedValue({})
    const { getByRole } = open(row())
    fireEvent.click(getByRole('menuitem', { name: /Turn off “Replies and mentions” push/ }))
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      '/api/me/notification-settings', { kind: 'replies', choice: 'in_app' },
    ))
    expect(toast.mock.calls.at(-1)![0]).toBe('“Replies and mentions”: in the app only now')
    undo()
    await waitFor(() => expect(api.patch).toHaveBeenLastCalledWith(
      '/api/me/notification-settings', { kind: 'replies', choice: 'push' },
    ))
  })

  it('a kind already In app turns Off', () => {
    settings('in_app')
    const { getByRole } = open(row())
    expect(getByRole('menuitem', { name: /^Turn off “Replies and mentions”No more/ })).toBeTruthy()
  })

  it('Needs you: no turning down, and says how they stop', () => {
    const { queryByRole, getByText } = open(row({
      type: 'space_join_requested', kind: 'needs_you', object_key: 'space:s1',
    }))
    expect(queryByRole('menuitem', { name: /Turn off/ })).toBeNull()
    expect(getByText(/Stepping down as admin stops them/)).toBeTruthy()
  })

  it('a space row: its level, Nothing for a while, and Undo back', async () => {
    spaces.value = [{ id: 's1', name: 'Familie Alpha' } as Space]
    api.get.mockResolvedValue({ level: 'all', default_level: 'all', is_default: true })
    api.put.mockResolvedValue({ level: 'muted', default_level: 'all', muted_until: '2026-10-28T20:00:00Z', then_level: 'all' })
    const n = row({ type: 'space_post_created', kind: 'activity', object_key: 'space_feed:s1', space_id: 's1' })
    const { getByRole, findByRole } = open(n)
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/spaces/s1/notif-prefs'))
    fireEvent.click(await findByRole('menuitem', { name: /^Familie Alpha notifications…Now: Everything/ }))
    expect((getByRole('menuitemradio', { name: /Everything/ })).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(getByRole('menuitem', { name: '8 hours' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/spaces/s1/notif-prefs', { level: 'muted', duration: '8h' },
    ))
    expect(toast.mock.calls.at(-1)![0]).toBe('Familie Alpha: quiet for 8 hours')
    undo()
    await waitFor(() => expect(api.put).toHaveBeenLastCalledWith(
      '/api/spaces/s1/notif-prefs', { level: 'all' },
    ))
  })

  it('Back returns to the main list', async () => {
    spaces.value = [{ id: 's1', name: 'Familie Alpha' } as Space]
    api.get.mockResolvedValue({ level: 'mentions', default_level: 'mentions' })
    const { getByRole, findByRole } = open(row({ space_id: 's1', object_key: 'space_post:p9' }))
    fireEvent.click(await findByRole('menuitem', { name: /^Familie Alpha notifications/ }))
    fireEvent.click(getByRole('menuitem', { name: 'Back' }))
    expect(getByRole('menuitem', { name: /^Mute this post/ })).toBeTruthy()
  })

  it('a chat row: mute it for a length, Undo unmutes', async () => {
    api.put.mockResolvedValue({ muted_until: '2026-10-28T20:00:00Z' })
    const n = row({ type: 'dm_mention', object_key: 'dm:c1', kind: 'replies' })
    const { getByRole, queryByRole } = open(n)
    expect(queryByRole('menuitem', { name: /^Mute this post/ })).toBeNull()
    fireEvent.click(getByRole('menuitem', { name: /^Mute this chat…/ }))
    fireEvent.click(getByRole('menuitem', { name: 'Until I turn it back on' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/conversations/c1/mute', { duration: 'forever' },
    ))
    expect(toast.mock.calls.at(-1)![0]).toBe('Chat muted until you turn it back on')
    undo()
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/conversations/c1/mute'))
  })

  it('arrow keys move, Esc closes and gives focus back', async () => {
    const { getAllByRole, getByRole, queryByRole } = open(row())
    const items = getAllByRole('menuitem')
    await waitFor(() => expect(document.activeElement).toBe(items[0]))
    fireEvent.keyDown(items[0], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(items[1], { key: 'ArrowUp' })
    fireEvent.keyDown(items[0], { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[items.length - 1])
    fireEvent.keyDown(items[0], { key: 'Escape' })
    expect(queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(getByRole('button', { name: /^More for/ }))
  })

  it('a failed change says so, with no Undo', async () => {
    api.post.mockRejectedValue(new Error('500'))
    const { getByRole } = open(row())
    fireEvent.click(getByRole('menuitem', { name: /^Mute this post/ }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't change that. Try again.", 'error'))
  })

  it('loads the settings when the menu opens without them', async () => {
    notifSettings.value = null
    api.get.mockResolvedValue({ kinds: [], changed: [] })
    open(row())
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/me/notification-settings'))
  })
})
