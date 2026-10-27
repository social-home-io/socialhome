import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

import type { Notification } from '@/types'
import NotificationsPage, { PAGE_SIZE, mergeNewestPage } from './NotificationsPage'

/** ``count`` rows, newest first, starting ``offset`` minutes back. */
function page(count: number, offset = 0, read = false): Notification[] {
  return Array.from({ length: count }, (_, i) => {
    const m = offset + i
    const ts = new Date(Date.UTC(2026, 9, 27, 12, 0, 0) - m * 60_000).toISOString()
    return {
      id: `n${m}`, type: 'post_created', title: `Row ${m}`, body: null,
      link_url: m % 2 === 0 ? `/post/p${m}` : null,
      read_at: read ? ts : null, created_at: ts,
    } as Notification
  })
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset().mockResolvedValue({})
  document.body.innerHTML = ''
})

async function mount(first: Notification[]) {
  apiGet.mockResolvedValueOnce(first)
  const r = render(<NotificationsPage />, { container: document.body.appendChild(document.createElement('div')) })
  await waitFor(() => expect(r.container.querySelector('.sh-notifications-page[aria-busy]')).toBeNull())
  return r
}

describe('NotificationsPage rows', () => {
  it('each row is one link (or button) named by its sentence; no nameless arrow link', async () => {
    const { container, getByRole } = await mount(page(2))
    const rows = [...container.querySelectorAll<HTMLElement>('.sh-notif-row')]
    expect(rows.map(r => r.tagName)).toEqual(['A', 'BUTTON'])
    expect(rows[0].getAttribute('href')).toBe('/post/p0')
    // The accessible name is the sentence (and its time), led by "Unread".
    expect(getByRole('link', { name: /^Unread, Row 0 \S/ })).toBe(rows[0])
    expect(getByRole('button', { name: /^Unread, Row 1 \S/ })).toBe(rows[1])
    expect(container.querySelector('.sh-notif-link')).toBeNull()
    expect([...container.querySelectorAll('a')].some(a => a.textContent?.trim() === '→')).toBe(false)
    // The visual dot is decoration; the words carry the state.
    expect(rows[0].querySelector('.sh-notif-icon')!.getAttribute('aria-hidden')).toBe('true')
  })

  it('read rows don\'t say "Unread"', async () => {
    const { container } = await mount(page(1, 0, true))
    const row = container.querySelector<HTMLElement>('.sh-notif-row')!
    expect(row.querySelector('.sr-only')).toBeNull()
    expect(row.classList.contains('sh-notif-row--unread')).toBe(false)
  })

  it('activating a row marks it read', async () => {
    const { container } = await mount(page(2))
    const btn = container.querySelectorAll<HTMLElement>('.sh-notif-row')[1]
    await act(async () => { btn.click() })
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/n1/read')
    expect(btn.classList.contains('sh-notif-row--unread')).toBe(false)
  })

  it('"Mark all read" only shows while something is unread', async () => {
    const { container } = await mount(page(2, 0, true))
    expect(container.textContent).not.toContain('Mark all read')
  })

  it('empty: the caught-up copy, and no "Mark all read"', async () => {
    const { container } = await mount([])
    expect(container.textContent).toContain("You're all caught up")
    expect(container.textContent).not.toContain('Mark all read')
    expect(container.textContent).not.toContain('Show older')
  })
})

describe('NotificationsPage — Show older', () => {
  it('a full first page offers "Show older", which fetches before the oldest row', async () => {
    const first = page(PAGE_SIZE)
    const { container } = await mount(first)
    const more = [...container.querySelectorAll('button')].find(b => b.textContent === 'Show older')!
    expect(more).toBeTruthy()
    apiGet.mockResolvedValueOnce(page(3, PAGE_SIZE))
    await act(async () => { more.click() })
    const oldest = first[first.length - 1].created_at
    expect(apiGet).toHaveBeenLastCalledWith(
      `/api/notifications?limit=${PAGE_SIZE}&before=${encodeURIComponent(oldest)}`,
    )
    await waitFor(() => expect(container.querySelectorAll('.sh-notif-row').length).toBe(PAGE_SIZE + 3))
    // A short page is the end: the button goes away.
    expect([...container.querySelectorAll('button')].some(b => b.textContent === 'Show older')).toBe(false)
    // …and focus lands on the first row it added, not on <body>.
    await waitFor(() => expect(document.activeElement?.getAttribute('data-notif-id')).toBe(`n${PAGE_SIZE}`))
  })

  it('a short first page has no "Show older"', async () => {
    const { container } = await mount(page(3))
    expect([...container.querySelectorAll('button')].some(b => b.textContent === 'Show older')).toBe(false)
  })
})

describe('mergeNewestPage', () => {
  it('keeps older pages already loaded behind a refreshed newest page', () => {
    const loaded = page(PAGE_SIZE + 10)
    const fresh = [page(1, -1)[0], ...page(PAGE_SIZE - 1)]
    const r = mergeNewestPage(fresh, loaded, true)
    expect(r.list.length).toBe(PAGE_SIZE + 11)
    expect(new Set(r.list.map(n => n.id)).size).toBe(r.list.length)
    expect(r.hasMore).toBe(true)
  })

  it('a short newest page is everything', () => {
    expect(mergeNewestPage(page(2), page(60), true)).toEqual({ list: page(2), hasMore: false })
  })

  it('a full newest page with nothing older loaded may have more', () => {
    expect(mergeNewestPage(page(PAGE_SIZE), [], false).hasMore).toBe(true)
  })
})
