/**
 * The Games section: waiting / being played / finished; "Start a game"
 * only where it can work; an empty list shows "Choose games" to the
 * people who choose them (owner, admins, moderators) and a quiet note to
 * everyone else; a failed read offers Retry; an old host household says
 * so.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render } from '@testing-library/preact'

vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: { post: vi.fn(), get: vi.fn().mockResolvedValue({ apps: [] }) },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: vi.fn(), path: '/', url: '/', query: {} }),
}))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u1', is_admin: false } } }))

import { SpaceGamesSection } from './SpaceGamesSection'
import type { SpaceGames, SpaceTable } from './spaceGames'
import type { SpaceGamesState } from './useSpaceGames'
import { installedApps } from '@/store/apps'

function table(id: string, over: Partial<SpaceTable> = {}): SpaceTable {
  return {
    session_id: id, app_id: 'quiz', host_name: 'Anna', host_is_local: false, note: null,
    min: 3, max: 8, seated: 1, state: 'open', late_join: false,
    created_at: '2026-10-31T10:00:00Z', expires_at: '2099-11-03T12:00:00Z',
    app: { installed: true, enabled: true }, group: null, ...over,
  }
}

function state(over: Partial<SpaceGames> = {}, extra: Partial<SpaceGamesState> = {}): SpaceGamesState {
  return {
    games: {
      space_id: 'sp-1', allowed_apps: ['quiz'], games_possible: true, can_edit_games: false,
      host_supports: true, tables: [], ...over,
    },
    loading: false, failed: false, reload: vi.fn().mockResolvedValue(undefined),
    retry: vi.fn().mockResolvedValue(undefined), ...extra,
  }
}

beforeEach(() => {
  installedApps.value = [{
    app_id: 'quiz', name: 'Quiz', version: '1', enabled: true, capabilities: [], icon: null,
    min_age: 0, tables: true, players: { min: 3, max: 8 },
  }]
})

describe('SpaceGamesSection', () => {
  it('lists waiting, being played and finished games', () => {
    const v = render(<SpaceGamesSection spaceId="sp-1" state={state({
      tables: [table('a'), table('b', { state: 'playing' }), table('c', { state: 'closed' })],
    })} />)
    expect(v.getByRole('heading', { level: 3, name: 'Open games' })).toBeTruthy()
    expect(v.getByRole('heading', { level: 3, name: 'Being played' })).toBeTruthy()
    expect(v.getByRole('heading', { level: 3, name: 'Finished' })).toBeTruthy()
    expect(v.getAllByRole('article')).toHaveLength(3)
  })

  it('nothing waiting: says so and invites to start one', () => {
    const v = render(<SpaceGamesSection spaceId="sp-1" state={state()} />)
    expect(v.getByText(/No open games right now\. Start one/)).toBeTruthy()
    fireEvent.click(v.getByRole('button', { name: 'Start a game' }))
    expect(v.getByRole('dialog', { name: 'Start a game' })).toBeTruthy()
  })

  it('an empty list: the people who choose see "Choose games"', () => {
    const v = render(<SpaceGamesSection spaceId="sp-1"
                                        state={state({ allowed_apps: [], can_edit_games: true })} />)
    expect(v.getByText('No games here yet')).toBeTruthy()
    expect(v.getByRole('link', { name: 'Choose games' }).getAttribute('href'))
      .toBe('/spaces/sp-1/settings/games')
    expect(v.queryByRole('button', { name: 'Start a game' })).toBeNull()
    expect(v.container.textContent).toMatch(/owner, admins and moderators/)
  })

  it('a member sees no "Choose games" link', () => {
    const v = render(<SpaceGamesSection spaceId="sp-1" state={state({ tables: [table('a')] })} />)
    expect(v.queryByRole('link', { name: /Choose games/ })).toBeNull()
  })

  it('an old host household: a note and no Start', () => {
    const v = render(<SpaceGamesSection spaceId="sp-1" state={state({ host_supports: false })} />)
    expect(v.getByText(/host household needs an update/)).toBeTruthy()
    expect(v.queryByRole('button', { name: 'Start a game' })).toBeNull()
  })

  it('a failed read: an error with Retry, never "no games"', () => {
    const st = state({}, { games: null, failed: true })
    const v = render(<SpaceGamesSection spaceId="sp-1" state={st} />)
    expect(v.getByRole('alert').textContent).toMatch(/Couldn't load the games/)
    fireEvent.click(v.getByRole('button', { name: /Retry/ }))
    expect(st.retry).toHaveBeenCalled()
  })
})
