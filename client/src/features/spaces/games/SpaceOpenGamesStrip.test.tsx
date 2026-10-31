/**
 * "Open games" on top of a space's feed: nothing at all when no game
 * waits (or while nothing is known), at most three cards, and "All games
 * (n)" to the Games section under the ingress prefix.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/api', async (orig) => ({ ...(await orig<object>()), api: { post: vi.fn(), get: vi.fn() } }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: vi.fn(), path: '/', url: '/', query: {} }),
}))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u1', is_admin: false } } }))
const base = vi.hoisted(() => ({ prefix: '' }))
vi.mock('@/baseUrl', async (orig) => ({
  ...(await orig<object>()),
  addBase: (p: string) => `${base.prefix}${p}`,
}))

import { SpaceOpenGamesStrip } from './SpaceOpenGamesStrip'
import type { SpaceGames, SpaceTable } from './spaceGames'

function table(id: string, over: Partial<SpaceTable> = {}): SpaceTable {
  return {
    session_id: id, app_id: 'quiz', host_name: 'Anna', host_is_local: false, note: null,
    min: 3, max: 8, seated: 1, state: 'open', late_join: false,
    created_at: '2026-10-31T10:00:00Z', expires_at: '2099-11-03T12:00:00Z',
    app: { installed: true, enabled: true }, group: null, ...over,
  }
}

function games(tables: SpaceTable[]): SpaceGames {
  return {
    space_id: 'sp-1', allowed_apps: ['quiz'], games_possible: true, can_edit_games: false,
    host_supports: true, tables,
  }
}

describe('SpaceOpenGamesStrip', () => {
  it('renders nothing while unknown or when no game waits', () => {
    expect(render(<SpaceOpenGamesStrip spaceId="sp-1" games={null} onChanged={() => {}} />)
      .container.innerHTML).toBe('')
    expect(render(<SpaceOpenGamesStrip spaceId="sp-1" onChanged={() => {}}
                                       games={games([table('a', { state: 'playing' })])} />)
      .container.innerHTML).toBe('')
  })

  it('one game: one card and the link to every game', () => {
    const v = render(<SpaceOpenGamesStrip spaceId="sp-1" games={games([table('a')])} onChanged={() => {}} />)
    expect(v.getByRole('heading', { level: 2, name: 'Open games' })).toBeTruthy()
    expect(v.getAllByRole('article')).toHaveLength(1)
    expect(v.getByRole('link', { name: 'All games (1)' }).getAttribute('href')).toBe('/spaces/sp-1/games')
  })

  it('many games: three cards, the count of all, under the ingress prefix', () => {
    base.prefix = '/api/hassio_ingress/tok'
    const v = render(<SpaceOpenGamesStrip spaceId="sp-1" onChanged={() => {}}
                                          games={games(['a', 'b', 'c', 'd', 'e'].map((x) => table(x)))} />)
    expect(v.getAllByRole('article')).toHaveLength(3)
    expect(v.getByRole('link', { name: 'All games (5)' }).getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/spaces/sp-1/games')
    base.prefix = ''
  })
})
