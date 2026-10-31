/**
 * GameLobby — Social Home's own "Who's in" screen for a group game.
 *
 * Pinned here: what each person sees by their place (invited, joining,
 * seated, the person who started it) and by the game's state (waiting,
 * playing, ended); every action hits its own route; Start ignores taps for
 * its first 600 ms and asks before starting with free places; Remove is
 * held behind an Undo toast and sent only when the toast goes; refusals
 * read in plain words; live ``app.group`` frames keep the screen current;
 * a game that's gone (404) or a failed load says so with a way on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'
import { signal } from '@preact/signals'

const h = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  confirm: vi.fn(),
  showToast: vi.fn(),
  dismissToast: vi.fn(),
  handlers: {} as Record<string, Set<(e: { type: string, data: Record<string, unknown> }) => void>>,
}))

vi.mock('@/api', async (orig) => ({
  ...(await orig<typeof import('@/api')>()),
  api: { get: h.get, post: h.post },
}))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: (e: { type: string, data: Record<string, unknown> }) => void) => {
      (h.handlers[type] ??= new Set()).add(fn)
      return () => { h.handlers[type]?.delete(fn) }
    },
  },
}))
vi.mock('@/components/confirm', () => ({ confirmDialog: h.confirm }))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast, dismissToast: h.dismissToast }))
vi.mock('@/store/apps', () => ({
  catalog: signal([{
    app_id: 'gift-draw', name: 'Gift draw', icon_url: null,
    i18n: { de: { name: 'Wichteln', description: null } },
  }]),
  catalogLoaded: signal(true),
  installedApps: signal([]),
  loadCatalog: vi.fn(),
  localized: (e: { name: string, i18n?: Record<string, { name: string | null }> }, lang: string) =>
    ({ name: e.i18n?.[lang]?.name || e.name, description: '' }),
}))

import { ApiError } from '@/api'
import { GameLobby, START_ARM_MS } from './GameLobby'
import type { GroupView } from '@/features/apps/groupBridge'

const NOW = new Date('2026-10-31T12:00:00Z')

function view(over: Partial<GroupView> = {}): GroupView {
  return {
    session_id: 's1',
    app_id: 'gift-draw',
    state: 'lobby',
    end_reason: null,
    space_id: null,
    me: 2,
    my_state: 'seated',
    host: 1,
    is_host: false,
    min: 3,
    max: 12,
    late_join: false,
    seq: 0,
    note: 'Wichteln 2026',
    created_at: '2026-10-30T12:00:00+00:00',
    started_at: null,
    expires_at: '2026-11-03T12:00:00+00:00',
    invited: null,
    players: [
      { seat: 1, name: 'Anna', household: 'Müller', is_local: false, state: 'seated' },
      { seat: 2, name: 'Ben', household: null, is_local: true, state: 'seated' },
    ],
    ...over,
  }
}

/** The starter's own view with three people in. */
function starterView(over: Partial<GroupView> = {}): GroupView {
  return view({
    me: 1, is_host: true, invited: 1,
    players: [
      { seat: 1, name: 'Anna', household: null, is_local: true, state: 'seated' },
      { seat: 2, name: 'Ben', household: 'Okafor', is_local: false, state: 'seated' },
      { seat: 3, name: 'Cleo', household: 'Krämer', is_local: false, state: 'seated' },
    ],
    ...over,
  })
}

function apiError(status: number, code: string, params?: Record<string, unknown>): ApiError {
  return new ApiError(status, '/x', { code, detail: code, ...(params ? { params } : {}) } as never)
}

function frame(data: Record<string, unknown>) {
  act(() => { h.handlers['app.group']?.forEach(fn => fn({ type: 'app.group', data })) })
}

async function open(v: GroupView = view(), props: { appName?: string, appId?: string } = {}) {
  h.get.mockResolvedValue({ group: v })
  const onClosed = vi.fn()
  const r = render(
    <GameLobby appId={props.appId ?? 'gift-draw'} sessionId="s1" appName={props.appName} onClosed={onClosed} />,
  )
  await r.findByText("Who's in")
  return { ...r, onClosed }
}

const BASE = '/api/apps/gift-draw/groups/s1'

describe('GameLobby', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    h.get.mockReset()
    h.post.mockReset()
    h.confirm.mockReset()
    h.showToast.mockReset()
    h.dismissToast.mockReset()
    h.handlers = {}
  })
  afterEach(() => { vi.useRealTimers() })

  it('shows the app by its catalog name, the note, who is in and when it closes', async () => {
    const { getByRole, getByText, container } = await open()
    expect(h.get).toHaveBeenCalledWith(BASE)
    expect(getByRole('heading', { level: 2 }).textContent).toBe('Gift draw')
    expect(getByText('Wichteln 2026')).toBeTruthy()
    expect(getByText('2 of 12 · closes in 3 days')).toBeTruthy()
    const bar = getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('2')
    expect(bar.getAttribute('aria-valuemax')).toBe('12')
    expect(bar.getAttribute('aria-label')).toBe('2 of 12 places taken')
    // Never a username, never "host" / "lobby" / "table".
    expect(container.textContent).not.toMatch(/host|lobby|table/i)
  })

  it('without a catalog entry: the name passed in, else the app id', async () => {
    const a = await open(view({ app_id: 'go' }), { appId: 'go', appName: 'Go' })
    expect(a.getByRole('heading', { level: 2 }).textContent).toBe('Go')
    a.unmount()
    const b = await open(view({ app_id: 'go' }), { appId: 'go' })
    expect(b.getByRole('heading', { level: 2 }).textContent).toBe('go')
  })

  describe('invited', () => {
    const invited = () => view({ me: null, my_state: 'invited' })

    it('Join asks to join, then waits for the starter\'s household', async () => {
      const { getByRole, findByText } = await open(invited())
      expect(getByRole('button', { name: 'Decline' })).toBeTruthy()
      h.post.mockResolvedValue({ group: view({ me: null, my_state: 'joining' }) })
      fireEvent.click(getByRole('button', { name: 'Join' }))
      await findByText('Waiting for Anna\'s household to confirm…')
      expect(h.post).toHaveBeenCalledWith(`${BASE}/join`)
    })

    it('Decline says no', async () => {
      const { getByRole, findByText } = await open(invited())
      h.post.mockResolvedValue({ ok: true })
      fireEvent.click(getByRole('button', { name: 'Decline' }))
      await findByText('You said no to this game.')
      expect(h.post).toHaveBeenCalledWith(`${BASE}/decline`)
    })

    it.each([
      ['APP_GROUP_FULL', 409, 'This game is full.'],
      ['APP_GROUP_CLOSED', 409, 'This game isn\'t taking anyone any more.'],
      ['APP_GROUP_CANT_JOIN', 403, 'You can\'t join this game.'],
    ])('a refused Join (%s) reads in plain words', async (code, status, text) => {
      const { getByRole, findByRole } = await open(invited())
      h.post.mockRejectedValue(apiError(status, code))
      fireEvent.click(getByRole('button', { name: 'Join' }))
      expect((await findByRole('alert')).textContent).toBe(text)
    })
  })

  describe('seated, not the starter', () => {
    it('waits for the starter, and says how many more are needed', async () => {
      const { getByText, queryByRole } = await open()
      expect(getByText('Waiting for Anna to start')).toBeTruthy()
      expect(getByText('Waiting for 1 more person to start')).toBeTruthy()
      expect(queryByRole('button', { name: /^Start/ })).toBeNull()
      expect(queryByRole('button', { name: /^Remove/ })).toBeNull()
    })

    it('offers to open the app for its own setup', async () => {
      const { getByRole } = await open()
      expect(getByRole('link', { name: 'Open Gift draw' }).getAttribute('href')).toBe('/apps/gift-draw?session=s1')
    })

    it('Leave asks first; yes leaves', async () => {
      const { getByRole, findByText } = await open()
      h.confirm.mockResolvedValue(true)
      h.post.mockResolvedValue({ ok: true })
      fireEvent.click(getByRole('button', { name: 'Leave' }))
      await findByText('You left this game.')
      expect(h.confirm.mock.calls[0][1].title).toBe('Leave this game?')
      expect(h.post).toHaveBeenCalledWith(`${BASE}/leave`)
    })

    it('Leave, then cancel: nothing is sent', async () => {
      const { getByRole } = await open()
      h.confirm.mockResolvedValue(false)
      fireEvent.click(getByRole('button', { name: 'Leave' }))
      await waitFor(() => expect(h.confirm).toHaveBeenCalled())
      expect(h.post).not.toHaveBeenCalled()
    })
  })

  describe('the person who started it', () => {
    it('below the minimum: how many more, Start off', async () => {
      const { getByText, getByRole } = await open(starterView({
        players: starterView().players.slice(0, 2),
      }))
      expect(getByText('Waiting for 1 more person to start')).toBeTruthy()
      expect((getByRole('button', { name: 'Start with 2 people' }) as HTMLButtonElement).disabled).toBe(true)
    })

    it('enough people: you can start; nobody else can join after', async () => {
      const { getByText, getByRole } = await open(starterView())
      expect(getByText('You can start now. Once you start, nobody else can join.')).toBeTruthy()
      expect((getByRole('button', { name: 'Start with 3 people' }) as HTMLButtonElement).disabled).toBe(false)
      expect(getByText('1 invite not answered yet')).toBeTruthy()
    })

    it('an app that takes people late says so', async () => {
      const { getByText } = await open(starterView({ late_join: true }))
      expect(getByText('You can start now. People can still join after you start.')).toBeTruthy()
    })

    it('Start ignores taps for its first 600 ms, then asks before starting with free places', async () => {
      const { getByRole, findByText } = await open(starterView())
      const start = getByRole('button', { name: 'Start with 3 people' })
      fireEvent.click(start)
      expect(h.confirm).not.toHaveBeenCalled()
      vi.setSystemTime(NOW.getTime() + START_ARM_MS)
      h.confirm.mockResolvedValue(true)
      h.post.mockResolvedValue({ group: starterView({ state: 'playing' }) })
      fireEvent.click(start)
      await findByText('Playing')
      expect(h.confirm.mock.calls[0][1].title).toBe('Start with 3 people?')
      expect(h.confirm.mock.calls[0][0]).toBe('9 places stay free. Once you start, nobody else can join.')
      expect(h.post).toHaveBeenCalledWith(`${BASE}/start`)
    })

    it('a full game starts without asking', async () => {
      const { getByRole, findByText } = await open(starterView({ max: 3 }))
      vi.setSystemTime(NOW.getTime() + START_ARM_MS)
      h.post.mockResolvedValue({ group: starterView({ max: 3, state: 'playing' }) })
      fireEvent.click(getByRole('button', { name: 'Start with 3 people' }))
      await findByText('Playing')
      expect(h.confirm).not.toHaveBeenCalled()
    })

    it('the 600 ms count from when Start turns on, not from the page load', async () => {
      const { getByRole } = await open(starterView({ players: starterView().players.slice(0, 2) }))
      vi.setSystemTime(NOW.getTime() + 5_000)
      // Cleo joins: Start turns on under the finger.
      frame({ app_id: 'gift-draw', session_id: 's1', group: starterView() })
      const start = getByRole('button', { name: 'Start with 3 people' })
      fireEvent.click(start)
      expect(h.confirm).not.toHaveBeenCalled()
      vi.setSystemTime(NOW.getTime() + 5_000 + START_ARM_MS)
      h.confirm.mockResolvedValue(false)
      fireEvent.click(start)
      await waitFor(() => expect(h.confirm).toHaveBeenCalledOnce())
    })

    it('a refused Start (too few) reads in plain words', async () => {
      const { getByRole, findByRole } = await open(starterView({ max: 3 }))
      vi.setSystemTime(NOW.getTime() + START_ARM_MS)
      h.post.mockRejectedValue(apiError(409, 'APP_GROUP_TOO_FEW', { min: 3 }))
      fireEvent.click(getByRole('button', { name: 'Start with 3 people' }))
      expect((await findByRole('alert')).textContent).toBe('At least 3 people need to be in before you can start.')
    })

    it('Remove hides the person at once and sends only when the Undo toast goes', async () => {
      const { getByRole, queryByText } = await open(starterView())
      expect(() => getByRole('button', { name: 'Remove Anna' })).toThrow()
      fireEvent.click(getByRole('button', { name: 'Remove Ben' }))
      expect(queryByText('Ben')).toBeNull()
      expect(h.post).not.toHaveBeenCalled()
      const [msg, , opts] = h.showToast.mock.calls[0]
      expect(msg).toBe('Ben is out of the game.')
      expect(opts.action.label).toBe('Undo')
      h.post.mockResolvedValue({ group: starterView() })
      await act(async () => { opts.onExpire() })
      expect(h.post).toHaveBeenCalledWith(`${BASE}/remove`, { seat: 2 })
    })

    it('Undo brings the person back and sends nothing', async () => {
      const { getByRole, findByText } = await open(starterView())
      fireEvent.click(getByRole('button', { name: 'Remove Ben' }))
      const opts = h.showToast.mock.calls[0][2]
      act(() => { opts.action.onClick() })
      await findByText('Ben')
      expect(h.post).not.toHaveBeenCalled()
    })

    it('a Remove still waiting is sent when the screen goes', async () => {
      const { getByRole, unmount } = await open(starterView())
      fireEvent.click(getByRole('button', { name: 'Remove Ben' }))
      h.post.mockResolvedValue({ group: starterView() })
      unmount()
      await waitFor(() => expect(h.post).toHaveBeenCalledWith(`${BASE}/remove`, { seat: 2 }))
      expect(h.dismissToast).toHaveBeenCalled()
    })

    it('Close this game asks first, then closes', async () => {
      const { getByRole, findByText, onClosed } = await open(starterView())
      h.confirm.mockResolvedValue(true)
      h.post.mockResolvedValue({ ok: true })
      h.get.mockResolvedValue({ group: starterView({ state: 'ended', end_reason: 'cancelled' }) })
      fireEvent.click(getByRole('button', { name: 'Close this game' }))
      await findByText('This game was closed.')
      expect(h.confirm.mock.calls[0][1]).toMatchObject({ title: 'Close this game?', destructive: true })
      expect(h.post).toHaveBeenCalledWith(`${BASE}/close`)
      expect(onClosed).toHaveBeenCalledOnce()
    })
  })

  it('playing: says so and opens the app', async () => {
    const { getByText, getByRole, queryByRole } = await open(view({ state: 'playing' }))
    expect(getByText('Playing')).toBeTruthy()
    expect(getByRole('link', { name: 'Open Gift draw' }).getAttribute('href')).toBe('/apps/gift-draw?session=s1')
    // Anyone may leave a game being played (design 2.5); nobody closes it.
    expect(getByRole('button', { name: 'Leave' })).toBeTruthy()
    expect(queryByRole('button', { name: 'Close this game' })).toBeNull()
  })

  it('playing: the person who started it may leave too (it passes on)', async () => {
    const { getByRole, queryByRole } = await open(starterView({ state: 'playing' }))
    expect(getByRole('button', { name: 'Leave' })).toBeTruthy()
    expect(queryByRole('button', { name: 'Close this game' })).toBeNull()
  })

  it.each([
    ['finished', 'This game is over.'],
    ['expired', 'Not enough people joined in time, so this game closed.'],
    ['host_gone', 'The person who started this game left, so it ended.'],
    ['app_removed', 'Gift draw was removed, so this game ended.'],
    ['closed_by_moderator', 'A moderator closed this game.'],
    ['something_new', 'This game has ended.'],
  ])('ended (%s): one line why and the way back', async (reason, text) => {
    h.get.mockResolvedValue({ group: view({ state: 'ended', end_reason: reason }) })
    const { findByText, getByRole } = render(<GameLobby appId="gift-draw" sessionId="s1" />)
    await findByText(text)
    expect(getByRole('link', { name: 'Back to Apps' }).getAttribute('href')).toBe('/apps')
  })

  it('removed by the starter: says so', async () => {
    h.get.mockResolvedValue({ group: view({ me: null, my_state: 'removed' }) })
    const { findByText } = render(<GameLobby appId="gift-draw" sessionId="s1" />)
    await findByText('You were removed from this game.')
  })

  describe('live', () => {
    it('a frame with the game replaces the view', async () => {
      const { findByText } = await open()
      const more = view({
        players: [...view().players, { seat: 3, name: 'Cleo', household: 'Krämer', is_local: false, state: 'seated' }],
      })
      frame({ app_id: 'gift-draw', session_id: 's1', group: more })
      await findByText('Cleo')
      await findByText('3 of 12 · closes in 3 days')
    })

    it('frames for another game or app are ignored', async () => {
      const { queryByText } = await open()
      const more = view({
        players: [...view().players, { seat: 3, name: 'Cleo', household: 'Krämer', is_local: false, state: 'seated' }],
      })
      frame({ app_id: 'gift-draw', session_id: 's2', group: more })
      frame({ app_id: 'chess', session_id: 's1', group: more })
      expect(queryByText('Cleo')).toBeNull()
    })

    it('a change frame asks again; a game that is gone for me says why', async () => {
      const { findByText } = await open()
      h.get.mockRejectedValue(apiError(404, 'APP_GROUP_NOT_FOUND'))
      frame({ app_id: 'gift-draw', session_id: 's1', change: 'removed', reason: 'removed' })
      await findByText('You were removed from this game.')
      expect(h.get).toHaveBeenCalledTimes(2)
    })

    it('stops listening when it goes', async () => {
      const { unmount } = await open()
      unmount()
      await waitFor(() => expect(h.handlers['app.group']?.size ?? 0).toBe(0))
    })
  })

  it('404: the game isn\'t here any more, with the way back', async () => {
    h.get.mockRejectedValue(apiError(404, 'APP_GROUP_NOT_FOUND'))
    const { findByText, getByRole } = render(<GameLobby appId="gift-draw" sessionId="s1" />)
    await findByText('This game isn\'t here any more')
    expect(getByRole('link', { name: 'Back to Apps' })).toBeTruthy()
  })

  it('a failed load offers Try again', async () => {
    h.get.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { findByRole } = render(<GameLobby appId="gift-draw" sessionId="s1" />)
    h.get.mockResolvedValue({ group: view() })
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await findByRole('heading', { name: "Who's in" })
  })
})
