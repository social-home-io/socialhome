/**
 * A row's "…" menu: mute this post, change the space's / chat's level,
 * turn the kind down — each with an Undo toast (redesign §4, D4, D9, D10).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/preact'

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
  NotificationRowMenu, chatOf, closestDuration, menuPlacement, turnDown,
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

describe('menuPlacement', () => {
  const area = { top: 64, bottom: 772 }

  it('opens below the "…" when the menu fits there', () => {
    expect(menuPlacement({ top: 200, bottom: 236 }, 300, area))
      .toEqual({ up: false, maxHeight: 772 - 236 - 4 })
  })

  it('opens upward when it would run under the bottom bar and there is more room above', () => {
    const p = menuPlacement({ top: 700, bottom: 736 }, 300, area)
    expect(p.up).toBe(true)
    expect(p.maxHeight).toBe(700 - 64 - 4)
  })

  it('stays below when it does not fit either way but below has more room', () => {
    expect(menuPlacement({ top: 120, bottom: 156 }, 900, area).up).toBe(false)
  })

  it('caps the height to the room on its side, never below a usable minimum', () => {
    expect(menuPlacement({ top: 100, bottom: 136 }, 900, { top: 90, bottom: 200 }).maxHeight).toBe(120)
  })
})

describe('NotificationRowMenu near the bottom of a phone screen', () => {
  const rect = (top: number, height: number, left = 300, width = 36) => ({
    top, bottom: top + height, height, left, right: left + width, width, x: left, y: top,
    toJSON: () => ({}),
  }) as DOMRect
  let nav: HTMLElement | null = null
  let bar: HTMLElement | null = null

  /** A 390×844 phone: top bar 0–56, bottom tab bar from y 780. */
  function phone(triggerTop: number) {
    vi.stubGlobal('innerHeight', 844)
    vi.stubGlobal('innerWidth', 390)
    vi.stubGlobal('visualViewport', undefined)
    nav = document.createElement('nav')
    nav.className = 'sh-mobile-nav'
    bar = document.createElement('header')
    bar.className = 'sh-topbar'
    document.body.append(nav, bar)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this === nav) return rect(780, 64, 0, 390)
      if (this === bar) return rect(0, 56, 0, 390)
      if (this.classList.contains('sh-notif-menu__btn')) return rect(triggerTop, 36)
      return rect(0, 0)
    })
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('sh-notif-menu__panel') ? 260 : 0
    })
  }

  afterEach(() => {
    cleanup()
    nav?.remove(); nav = null
    bar?.remove(); bar = null
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('a row near the bottom opens its menu upward, above the bottom bar', () => {
    phone(700)
    const { container } = open(row())
    const panel = container.querySelector<HTMLElement>('.sh-notif-menu__panel')!
    expect(panel.classList.contains('sh-notif-menu__panel--up')).toBe(true)
    // Room above: from under the top bar (56 + 8) to the "…" (700 − 4).
    expect(panel.style.getPropertyValue('--sh-notif-menu-room')).toBe(`${700 - 64 - 4}px`)
  })

  it('a row near the top opens downward and stops above the bottom bar', () => {
    phone(120)
    const { container } = open(row())
    const panel = container.querySelector<HTMLElement>('.sh-notif-menu__panel')!
    expect(panel.classList.contains('sh-notif-menu__panel--up')).toBe(false)
    expect(panel.style.getPropertyValue('--sh-notif-menu-room')).toBe(`${780 - 8 - 156 - 4}px`)
  })

  it('with the keyboard up the visible bottom is the visual viewport', () => {
    phone(380)
    nav!.remove()
    vi.stubGlobal('visualViewport', { height: 450, offsetTop: 0, width: 390, addEventListener: vi.fn(), removeEventListener: vi.fn() })
    const { container } = open(row())
    expect(container.querySelector('.sh-notif-menu__panel')!.classList.contains('sh-notif-menu__panel--up')).toBe(true)
  })

  it('focus lands on the first item without scrolling the page, and Esc gives it back', () => {
    phone(700)
    const focus = vi.spyOn(HTMLElement.prototype, 'focus')
    const view = open(row())
    const first = view.getAllByRole('menuitem')[0]
    expect(document.activeElement).toBe(first)
    const call = focus.mock.calls.find((_, i) => focus.mock.instances[i] === (first as unknown))
    expect(call?.[0]).toEqual({ preventScroll: true })
    fireEvent.keyDown(first, { key: 'Escape' })
    expect(document.activeElement).toBe(view.getByRole('button', { name: 'More for Ben commented on your post' }))
  })
})
