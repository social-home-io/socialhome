/**
 * GameLobbyPage — ``/apps/:appId/tables/:sessionId``.
 *
 * Pinned here: both ids are checked before anything is fetched; a good
 * address mounts the lobby for that game; the topbar says Apps.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

const h = vi.hoisted(() => ({
  params: {} as Record<string, string>,
  lobby: vi.fn(),
  title: vi.fn(),
}))
vi.mock('preact-iso', () => ({ useRoute: () => ({ params: h.params, query: {} }) }))
vi.mock('./GameLobby', () => ({
  GameLobby: (p: { appId: string, sessionId: string }) => {
    h.lobby(p)
    return <p>lobby {p.appId} {p.sessionId}</p>
  },
}))
vi.mock('@/store/pageTitle', () => ({ useTitle: h.title }))

import GameLobbyPage, { lobbyParams } from './GameLobbyPage'

describe('GameLobbyPage', () => {
  beforeEach(() => {
    h.lobby.mockReset()
    h.title.mockReset()
  })

  it('mounts the lobby of the game the address names, under the Apps title', () => {
    h.params = { appId: 'gift-draw', sessionId: 's_1-x' }
    const { getByText } = render(<GameLobbyPage />)
    expect(getByText('lobby gift-draw s_1-x')).toBeTruthy()
    expect(h.title).toHaveBeenCalledWith('Apps')
  })

  it.each([
    [{ appId: 'gift-draw', sessionId: '../x' }],
    [{ appId: 'a b', sessionId: 's1' }],
    [{ appId: 'gift-draw', sessionId: 'x'.repeat(129) }],
    [{ appId: 'gift-draw' }],
  ])('a malformed address fetches nothing and says the game is gone (%o)', (params) => {
    h.params = params as Record<string, string>
    const { getByText, getByRole } = render(<GameLobbyPage />)
    expect(h.lobby).not.toHaveBeenCalled()
    expect(getByText('This game isn\'t here any more')).toBeTruthy()
    expect(getByRole('link', { name: 'Back to Apps' }).getAttribute('href')).toBe('/apps')
  })

  it('lobbyParams keeps only well-formed ids', () => {
    expect(lobbyParams({ appId: 'chess', sessionId: 'abc' })).toEqual({ appId: 'chess', sessionId: 'abc' })
    expect(lobbyParams({ appId: 'chess', sessionId: 'a/b' })).toBeNull()
  })
})
