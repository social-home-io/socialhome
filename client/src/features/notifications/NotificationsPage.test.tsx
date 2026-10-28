import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const needsYouGet = vi.fn(async (..._a: unknown[]) => ({ items: [] as unknown[], count: 0 }))
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    // "Needs you" loads on its own; the tests here queue the list's pages.
    get: (...a: unknown[]) => String(a[0]).startsWith('/api/notifications/needs-you')
      ? needsYouGet(...a)
      : apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

const wsListeners = vi.hoisted(() => ({} as Record<string, ((e: { data: Record<string, unknown> }) => void)[]>))
const wsHandlers = vi.hoisted(() => ({} as Record<string, (e: { data: Record<string, unknown> }) => void>))
vi.mock('@/ws', () => ({
  ws: {
    // Several components listen to one frame type (the list and "Needs
    // you"): ``wsHandlers[type]`` calls every live listener.
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      const list = (wsListeners[type] ??= [])
      list.push(h)
      wsHandlers[type] = (e) => { for (const f of [...list]) f(e) }
      return () => {
        list.splice(list.indexOf(h), 1)
        if (list.length === 0) delete wsHandlers[type]
      }
    },
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
import NotificationsPage, { PAGE_SIZE, filter, mergeNewestPage } from './NotificationsPage'

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
  filter.value = 'all'
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
    expect(rows[0].querySelector('.sh-notif-dot')!.getAttribute('aria-hidden')).toBe('true')
    expect(rows[0].querySelector('.sh-notif-av')!.getAttribute('aria-hidden')).toBe('true')
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

  it('toolbar: Mark all read and Settings are icon buttons that keep their names', async () => {
    const { container, getByRole } = await mount(page(2))
    const mark = getByRole('button', { name: 'Mark all read' })
    const settings = getByRole('link', { name: 'Settings' })
    expect(settings.getAttribute('href')).toBe('/settings#notifications')
    for (const el of [mark, settings]) {
      // An icon that stays on a phone, the words that hide there (still
      // the accessible name) and a tooltip.
      expect(el.querySelector('svg[aria-hidden="true"]')).not.toBeNull()
      expect(el.querySelector('.sh-notif-toolbar__text')?.textContent).toBe(el.getAttribute('title'))
    }
    // One row, never wrapping: the tabs, then the two actions.
    const bar = container.querySelector('.sh-notif-toolbar')!
    expect(bar.querySelector('.sh-notif-toolbar__actions')?.contains(mark)).toBe(true)
    expect(bar.querySelector('.sh-notif-toolbar__actions')?.contains(settings)).toBe(true)
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

/** A row from after 0129: about a thing, with who acted and the counts. */
function rich(id: string, extra: Partial<Notification> = {}): Notification {
  const ts = new Date().toISOString()
  return {
    id, type: 'space_comment_added', title: 'old title', body: null,
    link_url: `/spaces/s1?post=${id}`, read_at: null, created_at: ts,
    object_key: `space_post:${id}`, space_id: 's1',
    params: {
      actors: [{ id: 'u2', name: 'Ben' }, { id: 'u3', name: 'Anna' }],
      actor_count: 5, new: 2, total: 5, space_name: 'Familie Alpha',
    },
    ...extra,
  } as Notification
}

describe('NotificationsPage — rows from params', () => {
  it('reads the sentence in the viewer\'s language, names bold, and the counts of a re-opened row', async () => {
    const { container, getByRole } = await mount([rich('p1')])
    const row = container.querySelector<HTMLElement>('.sh-notif-row')!
    expect(row.querySelector('.sh-notif-title')!.textContent)
      .toBe('Ben, Anna and 3 others commented in Familie Alpha')
    expect([...row.querySelectorAll('.sh-notif-title b')].map(b => b.textContent)).toEqual(['Ben', 'Anna'])
    expect(row.querySelector('.sh-notif-title i')!.textContent).toBe('Familie Alpha')
    expect(row.querySelector('.sh-notif-meta')!.textContent).toContain('2 new · 5 in all')
    expect(getByRole('link', { name: /^Unread, Ben, Anna and 3 others commented in Familie Alpha/ })).toBe(row)
  })

  it('a name is text, never markup', async () => {
    const evil = rich('p1', {
      params: { actors: [{ id: 'u2', name: '<img src=x onerror=alert(1)>' }], actor_count: 1, new: 1, total: 1, space_name: 'S' },
    })
    const { container } = await mount([evil])
    expect(container.querySelector('.sh-notif-title img')).toBeNull()
    expect(container.querySelector('.sh-notif-title b')!.textContent).toBe('<img src=x onerror=alert(1)>')
  })

  it('rows sit under day headers', async () => {
    const { container } = await mount([rich('p1'), ...page(1, 60 * 24 * 400)])
    const heads = [...container.querySelectorAll('h3.sh-notif-day__h')].map(h => h.textContent)
    expect(heads[0]).toBe('Today')
    expect(heads.length).toBe(2)
  })

  it('opening a row reads every row about the same thing, by object', async () => {
    const a = rich('p1')
    const b = rich('p1b', { type: 'space_mention', object_key: 'space_post:p1' })
    const { container } = await mount([a, b])
    const [ra, rb] = [...container.querySelectorAll<HTMLElement>('.sh-notif-row')]
    ra.addEventListener('click', e => e.preventDefault())
    await act(async () => { ra.click() })
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/read-by-object', { object_keys: ['space_post:p1'] })
    expect(ra.classList.contains('sh-notif-row--unread')).toBe(false)
    expect(rb.classList.contains('sh-notif-row--unread')).toBe(false)
  })
})

describe('NotificationsPage — Needs you', () => {
  it('a request that waits is pinned above the timeline, not in it', async () => {
    const rows = page(3)
    const pinned = { ...rows[1], type: 'space_join_requested', object_key: 'join_request:r1', space_id: 's1', link_url: '/spaces/s1/moderation' }
    needsYouGet.mockResolvedValueOnce({ items: [pinned], count: 1 })
    const { container } = await mount([rows[0], pinned, rows[2]])
    await waitFor(() => expect(container.querySelector('.sh-needs')).not.toBeNull())
    const section = container.querySelector('.sh-needs')!
    const list = container.querySelector('#sh-notif-list')!
    // Pinned on top of the list, its row once.
    expect(section.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(section.querySelectorAll('.sh-notif-row')).toHaveLength(1)
    expect([...list.querySelectorAll('[data-notif-id]')].map(e => e.getAttribute('data-notif-id')))
      .toEqual([rows[0].id, rows[2].id])
  })
})

describe('NotificationsPage — All | Unread', () => {
  it('is a tab list; Unread asks the server for unread rows only', async () => {
    const { getByRole } = await mount(page(2))
    const all = getByRole('tab', { name: 'All' })
    const unread = getByRole('tab', { name: /^Unread/ })
    expect(all.getAttribute('aria-selected')).toBe('true')
    apiGet.mockResolvedValueOnce(page(1))
    await act(async () => { unread.click() })
    expect(apiGet).toHaveBeenLastCalledWith(`/api/notifications?limit=${PAGE_SIZE}&unread=1`)
    await waitFor(() => expect(getByRole('tab', { name: /^Unread/ }).getAttribute('aria-selected')).toBe('true'))
  })

  it('arrow keys move between the tabs', async () => {
    const { getByRole } = await mount(page(2))
    apiGet.mockResolvedValueOnce([])
    await act(async () => {
      getByRole('tab', { name: 'All' }).dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    })
    expect(filter.value).toBe('unread')
  })

  it('an empty Unread view says so and points to All', async () => {
    const { getByRole, container } = await mount(page(2, 0, true))
    apiGet.mockResolvedValueOnce([])
    await act(async () => { getByRole('tab', { name: /^Unread/ }).click() })
    await waitFor(() => expect(container.textContent).toContain('Nothing unread'))
  })
})

describe('NotificationsPage — live arrivals', () => {
  it('a new row waits behind the "new" pill; the list does not move until it is tapped', async () => {
    const { container } = await mount(page(2))
    const calls = apiGet.mock.calls.length
    await act(async () => { wsHandlers['notification.new']({ data: { notification_id: 'x1', notif_type: 'post_created', title: 'X' } }) })
    expect(apiGet.mock.calls.length).toBe(calls)
    const pill = container.querySelector<HTMLButtonElement>('.sh-notif-newpill button')!
    expect(pill.textContent).toBe('↑ 1 new')
    apiGet.mockResolvedValueOnce([...page(1, -5), ...page(2)])
    await act(async () => { pill.click() })
    await waitFor(() => expect(container.querySelector('.sh-notif-newpill')).toBeNull())
    expect(container.querySelectorAll('.sh-notif-row').length).toBe(3)
  })

  it('the live region announces the count, never the sentence; a repeat on an unread row is silent', async () => {
    const { container } = await mount(page(1))
    const region = container.querySelector('[role="status"]')!
    await act(async () => {
      wsHandlers['notification.new']({ data: { notification_id: 'x1', notif_type: 'dm_mention', title: 'Secret text', bumped: true } })
    })
    expect(region.textContent).toBe('')
    await act(async () => { wsHandlers['notification.new']({ data: { notification_id: 'x2', notif_type: 'post_created', title: 'Secret text' } }) })
    expect(region.textContent).toBe('1 new notification')
    expect(region.textContent).not.toContain('Secret')
  })
})
