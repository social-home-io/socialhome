import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, waitFor, fireEvent } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
  },
  ApiError: class ApiError extends Error {},
}))

const wsListeners = vi.hoisted(() => ({} as Record<string, ((e: { data: Record<string, unknown> }) => void)[]>))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      const list = (wsListeners[type] ??= [])
      list.push(h)
      return () => { list.splice(list.indexOf(h), 1) }
    },
  },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'me', username: 'me', display_name: 'Me', is_admin: true } },
}))

import { setLocale } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { catalog, catalogLoaded } from '@/store/apps'
import type { Notification } from '@/types'
import { NeedsYouSection, pinnedIds } from './NeedsYouSection'
import { acting, mustReview, needsYou, waitingDeclines } from './needsYou'

function frame(type: string, data: Record<string, unknown>) {
  for (const h of wsListeners[type] ?? []) h({ data })
}

function row(id: string, over: Partial<Notification> = {}): Notification {
  return {
    id,
    type: 'space_join_requested',
    title: `${id} wants to join`,
    body: null,
    link_url: '/spaces/s1/moderation',
    read_at: null,
    created_at: '2026-10-28T10:00:00+00:00',
    object_key: `join_request:${id}`,
    space_id: `space-${id}`,
    params: { actors: [{ id: `u-${id}`, name: `Person ${id}` }], actor_count: 1, new: 1, total: 1, space_name: 'Salon' },
    ...over,
  } as Notification
}

function serve(items: Notification[]) {
  apiGet.mockImplementation(async (path: string) => (
    path === '/api/notifications/needs-you' ? { items, count: items.length } : []
  ))
}

beforeEach(async () => {
  await setLocale('en')
  apiGet.mockReset()
  apiPost.mockReset().mockResolvedValue({})
  needsYou.value = []
  acting.value = new Set()
  mustReview.value = new Map()
  for (const k of Object.keys(wsListeners)) delete wsListeners[k]
  document.body.innerHTML = ''
})

async function mount(items: Notification[], props: Parameters<typeof NeedsYouSection>[0] = {}) {
  serve(items)
  const r = render(<NeedsYouSection {...props} />)
  await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/notifications/needs-you'))
  if (items.length) await waitFor(() => expect(r.container.querySelector('.sh-needs')).not.toBeNull())
  return r
}

describe('NeedsYouSection', () => {
  it('renders nothing while nothing waits', async () => {
    const { container } = await mount([])
    expect(container.querySelector('.sh-needs')).toBeNull()
  })

  it('``only`` keeps the given types (the Apps page: invites and app requests)', async () => {
    const ask = row('r', {
      type: 'app_request', title: 'Milo asked for Chess', object_key: 'app:chess',
      space_id: null, link_url: '/apps/chess/about',
    })
    const { container, getByRole } = await mount([row('a'), ask], { only: new Set(['app_challenge', 'app_request']) })
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(1)
    // An app request is reviewed on the app's detail page.
    expect(getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/apps/chess/about')
  })

  it('is a region named by its heading, with a count of pending items', async () => {
    const { getByRole } = await mount([row('a'), row('b')])
    const region = getByRole('region', { name: /Needs you/ })
    expect(region.querySelector('.sh-needs__count')!.textContent).toBe('2')
    expect(region.querySelector('.sh-needs__count')!.getAttribute('aria-label')).toBe('2 waiting')
  })

  it('shows 3 at a time and "Show N more"', async () => {
    const { container, getByRole } = await mount(['a', 'b', 'c', 'd', 'e'].map(id => row(id)))
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(3)
    const more = getByRole('button', { name: 'Show 2 more' })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    await act(async () => { more.click() })
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(5)
    expect(getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('a plain join request has Approve and Decline that say what they act on', async () => {
    const { getByRole, container } = await mount([row('a')])
    const approve = getByRole('button', { name: 'Approve' })
    const decline = getByRole('button', { name: 'Decline' })
    const sentence = container.querySelector('.sh-needs .sh-notif-title')!
    expect(sentence.id).toBeTruthy()
    expect(approve.getAttribute('aria-describedby')).toBe(sentence.id)
    expect(decline.getAttribute('aria-describedby')).toBe(sentence.id)
    // The row is one link; the actions are siblings, never inside it.
    expect(container.querySelector('a.sh-notif-row button')).toBeNull()
  })

  it('a flagged request only offers Review', async () => {
    const r = row('a')
    r.params = { ...r.params!, flags: ['edge'] }
    const { getByRole, queryByRole, container } = await mount([r])
    expect(queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/spaces/s1/moderation')
    // It says why it needs a closer look.
    expect(container.querySelector('.sh-needs__why')!.textContent).toBe('Near the edge')
  })

  it('Approve refused with REVIEW_REQUIRED turns the entry into Review with the server\'s reasons', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('x'), {
      status: 409, code: 'REVIEW_REQUIRED', extra: { params: { flags: 'edge,unpaired' } },
    }))
    const { getByRole, queryByRole, container } = await mount([row('a')])
    await act(async () => { getByRole('button', { name: 'Approve' }).click() })
    await waitFor(() => expect(queryByRole('button', { name: 'Approve' })).toBeNull())
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/space-a/join-requests/a/approve', { via: 'inline' })
    expect(getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/spaces/s1/moderation')
    expect(container.querySelector('.sh-needs__why')!.textContent)
      .toBe("Near the edge · Household you're not paired with")
    // Decline stays one tap away only on a plain request: here it's Review.
    expect(queryByRole('button', { name: 'Decline' })).toBeNull()
  })

  it('an invite offers View invite (the invite card) and Decline — no one-tap join', async () => {
    const inv = row('i', { type: 'space_invite', object_key: 'space_invite:sp', link_url: '/spaces' })
    const { getByRole, queryByRole } = await mount([inv])
    expect(getByRole('link', { name: 'View invite' }).getAttribute('href')).toBe('/spaces')
    expect(getByRole('button', { name: 'Decline' })).toBeTruthy()
    expect(queryByRole('button', { name: /Join|Accept|Approve/ })).toBeNull()
  })

  it('several requests in one space become one entry with the space and Review', async () => {
    const one = row('a', { space_id: 'salon' })
    const two = row('b', { space_id: 'salon' })
    const queue = row('q', { space_id: 'salon', type: 'moderation_pending', object_key: 'space:salon', params: { actors: [], actor_count: 0, new: 1, total: 1, space_name: 'Salon' } })
    const { container, getByRole } = await mount([one, two, queue])
    const items = container.querySelectorAll('.sh-needs__item')
    expect(items).toHaveLength(1)
    expect(items[0].querySelector('.sh-notif-title')!.textContent).toBe('3 requests wait for you in Salon')
    expect(items[0].querySelector('time')!.getAttribute('datetime')).toBe(one.created_at)
    // The space's avatar, not a person's.
    expect(items[0].querySelector('.sh-notif-av__space')).not.toBeNull()
    expect(getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/spaces/s1/moderation')
    expect(container.querySelector('.sh-needs__count')!.textContent).toBe('3')
  })

  it('Decline hides the item at once and waits for the undo window', async () => {
    const { getByRole, container } = await mount([row('a'), row('b')])
    await act(async () => { fireEvent.click(getByRole('button', { name: 'Decline', description: /Person a/ })) })
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(1)
    expect(apiPost).not.toHaveBeenCalled()
    expect(waitingDeclines()).toBe(1)
  })

  it('leaving the page sends a waiting decline', async () => {
    const r = await mount([row('a')])
    await act(async () => { fireEvent.click(r.getByRole('button', { name: 'Decline' })) })
    r.unmount()
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/spaces/space-a/join-requests/a/deny', undefined, { keepalive: true },
    ))
  })

  it('an item another admin handled leaves the section on this device', async () => {
    const { container } = await mount([row('a'), row('b')])
    await act(async () => { frame('notification.unread_count', { unread_count: 1, handled: ['join_request:a'] }) })
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(1)
    // A new request reloads the list.
    serve([row('b'), row('c')])
    await act(async () => { frame('notification.new', { notif_type: 'space_join_requested' }) })
    await waitFor(() => expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(2))
    // Other news does not.
    const calls = apiGet.mock.calls.length
    await act(async () => { frame('notification.new', { notif_type: 'post_created' }) })
    expect(apiGet.mock.calls.length).toBe(calls)
  })

  it('opening an item reads it but keeps it pinned', async () => {
    const { container } = await mount([row('a')])
    const link = container.querySelector<HTMLAnchorElement>('.sh-needs a.sh-notif-row')!
    link.addEventListener('click', e => e.preventDefault())
    await act(async () => { link.click() })
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/read-by-object', { object_keys: ['join_request:a'] })
    expect(container.querySelectorAll('.sh-needs__item')).toHaveLength(1)
    expect(container.querySelector('.sh-needs .sh-notif-row--unread')).toBeNull()
    expect([...pinnedIds()]).toEqual(['a'])
  })

  it('a link in the bell panel closes it', async () => {
    const onNavigate = vi.fn()
    const r = await mount([row('a', { type: 'pairing_request', object_key: 'pairing_request:a', space_id: null, link_url: '/connections' })], { compact: true, onNavigate })
    const review = r.getByRole('link', { name: 'Review' })
    review.addEventListener('click', e => e.preventDefault())
    await act(async () => { review.click() })
    expect(onNavigate).toHaveBeenCalled()
    expect(r.container.querySelector('.sh-needs--compact')).not.toBeNull()
  })

  it('a game invite offers Play (that game) and Decline', async () => {
    const inv = row('g', {
      type: 'app_challenge',
      object_key: 'app_session:chess/s1',
      space_id: null,
      link_url: '/apps/chess',
    })
    const { getByRole, queryByRole } = await mount([inv])
    // Play opens the game it is on, not the app's lobby (D12).
    expect(getByRole('link', { name: 'Play' }).getAttribute('href')).toBe('/apps/chess?session=s1')
    expect(getByRole('button', { name: 'Decline' })).toBeTruthy()
    expect(queryByRole('link', { name: 'Review' })).toBeNull()
  })

  describe('a group game invite (v_76)', () => {
    const group = () => row('gg', {
      type: 'app_challenge',
      title: 'Anna invited you and 3 others to play Quiz',
      object_key: 'app_session:quiz/t1',
      space_id: null,
      link_url: '/apps/quiz/tables/t1',
      params: { actors: [{ id: 'u-anna', name: 'Anna' }], actor_count: 1, new: 1, total: 1, space_name: 'Quiz', flags: ['group'], others: 3 },
    })

    it('reads "and 3 others" and offers Join to the Who\'s-in page — no Play', async () => {
      const { getByRole, queryByRole, container } = await mount([group()])
      expect(container.textContent).toContain('Anna invited you and 3 others to play Quiz')
      expect(getByRole('link', { name: 'Join' }).getAttribute('href')).toBe('/apps/quiz/tables/t1')
      expect(queryByRole('link', { name: 'Play' })).toBeNull()
      expect(getByRole('button', { name: 'Decline' })).toBeTruthy()
    })

    it('Join takes the seat first, then moves on', async () => {
      apiPost.mockResolvedValue({ group: { session_id: 't1', app_id: 'quiz', state: 'lobby', me: null, my_state: 'joining', host: 1, is_host: false, min: 3, max: 8, late_join: false, players: [] } })
      const { getByRole } = await mount([group()])
      const join = getByRole('link', { name: 'Join' })
      const moves: boolean[] = []
      join.addEventListener('click', e => { moves.push(e.defaultPrevented); e.preventDefault() })
      await act(async () => { join.click() })
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/apps/quiz/groups/t1/join'))
      // The first click is held for the Join; the second is the move.
      await waitFor(() => expect(moves).toEqual([true, false]))
    })

    it('a refused Join stays here and says so, without a reason', async () => {
      const { ApiError } = await import('@/api')
      // The mocked ApiError takes a message only.
      const Mocked = ApiError as unknown as new (message: string) => Error
      const err = Object.assign(new Mocked('no'), { code: 'APP_GROUP_CANT_JOIN' })
      apiPost.mockRejectedValue(err)
      const { getByRole } = await mount([group()])
      const join = getByRole('link', { name: 'Join' })
      const moves: boolean[] = []
      join.addEventListener('click', e => { moves.push(e.defaultPrevented); e.preventDefault() })
      await act(async () => { join.click() })
      const { toasts } = await import('@/components/Toast')
      await waitFor(() => expect(toasts.value.map(x => x.message)).toContain("You can't join this game."))
      expect(moves).toEqual([true])
    })
  })

  it('an app update offers Update (one tap) and What\'s new', async () => {
    const upd = row('u', {
      type: 'app_update',
      title: 'Chess has an update',
      object_key: 'app:chess',
      space_id: null,
      link_url: '/apps/chess/about',
      params: { actors: [], actor_count: 0, new: 1, total: 1, space_name: 'Chess' },
    })
    const { getByRole, queryByRole, container } = await mount([upd])
    expect(container.textContent).toContain('Chess has an update')
    expect(getByRole('link', { name: "What's new" }).getAttribute('href')).toBe('/apps/chess/about#whats-new')
    expect(queryByRole('link', { name: 'Review' })).toBeNull()
    await act(async () => { fireEvent.click(getByRole('button', { name: 'Update Chess' })) })
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/apps/chess/update', { accept_permissions: [] }))
    // Done: it leaves the section.
    await waitFor(() => expect(needsYou.value).toEqual([]))
  })

  describe('a game invite for an app the household doesn\'t have (D10)', () => {
    const waiting = (flag: 'app_missing' | 'app_off' = 'app_missing') => row('w', {
      type: 'app_challenge_waiting',
      title: 'Ben · Lakeside invited you to play Chess',
      object_key: 'app_session:chess/s1',
      space_id: null,
      link_url: '/apps/chess/about',
      params: {
        actors: [{ id: 'r-ben', name: 'Ben · Lakeside' }],
        actor_count: 1, new: 1, total: 1, space_name: 'Chess', flags: [flag],
      },
    })
    const as = (who: { is_admin: boolean, protected?: boolean }) => {
      currentUser.value = { user_id: 'me', username: 'me', display_name: 'Me', ...who } as typeof currentUser.value
    }
    beforeEach(() => {
      catalog.value = [{
        app_id: 'chess', name: 'Chess', latest_version: '2026.10.29', description: 'Play chess',
        icon_url: null, capabilities: [], i18n: { de: { name: 'Schach', description: null } },
      }]
      catalogLoaded.value = true
    })
    afterEach(() => { as({ is_admin: true }) })

    it('a member reads why it waits and asks an admin — no Play', async () => {
      as({ is_admin: false })
      const { getByRole, queryByRole, container } = await mount([waiting()])
      expect(container.textContent).toContain('Ben · Lakeside invited you to play Chess')
      expect(container.querySelector('.sh-needs__why')!.textContent).toBe(
        "Chess isn't in your household yet. Ask an admin to add it, then tap Play.",
      )
      expect(queryByRole('link', { name: 'Play' })).toBeNull()
      await act(async () => { fireEvent.click(getByRole('button', { name: 'Ask an admin for Chess' })) })
      await act(async () => { fireEvent.click(getByRole('button', { name: 'Send request' })) })
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/apps/chess/requests'))
      // A second ask isn't offered: it reads "Asked".
      await waitFor(() => expect(container.querySelector('.sh-app-asked')).not.toBeNull())
    })

    it('a protected account asks a grown-up, and can say Not now', async () => {
      as({ is_admin: false, protected: true })
      const { getByRole, container } = await mount([waiting()])
      expect(container.querySelector('.sh-needs__why')!.textContent).toBe(
        'Ask a grown-up to add Chess, then you can play.',
      )
      expect(getByRole('button', { name: 'Ask a grown-up for Chess' })).toBeTruthy()
      expect(getByRole('button', { name: 'Not now' })).toBeTruthy()
    })

    it('an admin sees the app (to add it) instead of asking', async () => {
      const { getByRole, queryByRole, container } = await mount([waiting()])
      expect(container.querySelector('.sh-needs__why')!.textContent).toBe(
        "Chess isn't in your household yet. You can add it for everyone.",
      )
      expect(getByRole('link', { name: 'See Chess' }).getAttribute('href')).toBe('/apps/chess/about')
      expect(queryByRole('button', { name: /Ask an admin/ })).toBeNull()
    })

    it('a turned-off app says an admin turned it off', async () => {
      as({ is_admin: false })
      const { container } = await mount([waiting('app_off')])
      expect(container.querySelector('.sh-needs__why')!.textContent).toBe(
        'An admin turned Chess off. Ask an admin to turn it on, then tap Play.',
      )
    })

    it('Decline is the game decline (nothing reaches the sender)', async () => {
      as({ is_admin: false })
      const r = await mount([waiting()])
      await act(async () => { fireEvent.click(r.getByRole('button', { name: 'Decline' })) })
      r.unmount()
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
        '/api/apps/chess/sessions/s1/decline', undefined, { keepalive: true },
      ))
    })

    it('names the app in the viewer\'s language (the catalog), in German', async () => {
      await setLocale('de')
      as({ is_admin: false })
      const { getByRole, container } = await mount([waiting()])
      expect(container.querySelector('.sh-notif-title')!.textContent).toContain('Schach')
      expect(container.querySelector('.sh-needs__why')!.textContent).toBe(
        'Schach gibt es in deinem Haushalt noch nicht. Frag einen Admin, ob er es hinzufügt, dann tippe auf Spielen.',
      )
      expect(getByRole('button', { name: /Schach/ }).textContent).toBe('Admin fragen')
    })
  })

  it('reads in German with the glossary words', async () => {
    await setLocale('de')
    const inv = row('i', { type: 'space_invite', object_key: 'space_invite:sp', link_url: '/spaces' })
    const { getByRole } = await mount([row('a'), inv])
    expect(getByRole('region', { name: /Wartet auf dich/ })).toBeTruthy()
    expect(getByRole('button', { name: 'Annehmen' })).toBeTruthy()
    expect(getByRole('link', { name: 'Ansehen' })).toBeTruthy()
  })
})
