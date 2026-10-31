/**
 * SpaceGameCard — one game in a space: Join opens the game's screen,
 * Play takes a two-player seat, a missing app asks an admin (also when
 * the join is refused for it), Full is disabled, and Close asks first
 * (the starter, or a moderator for any game).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { ApiError } from '@/api'

const apiPost = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: { post: (...a: unknown[]) => apiPost(...a), get: vi.fn(), put: vi.fn() },
}))
const route = vi.fn()
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route, path: '/spaces/sp-1', url: '/spaces/sp-1', query: {} }),
}))
const user = vi.hoisted(() => ({ value: { user_id: 'u1', is_admin: false, protected: false } as Record<string, unknown> }))
vi.mock('@/store/auth', () => ({ currentUser: user }))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: toast }))
const confirm = vi.hoisted(() => vi.fn())
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { SpaceGameCard } from './SpaceGameCard'
import type { SpaceTable } from './spaceGames'

function table(over: Partial<SpaceTable> = {}): SpaceTable {
  return {
    session_id: 's1', app_id: 'quiz', host_name: 'Anna', host_is_local: false, note: 'Bring snacks',
    min: 3, max: 8, seated: 2, state: 'open', late_join: false,
    created_at: '2026-10-31T10:00:00Z', expires_at: '2099-11-03T12:00:00Z',
    app: { installed: true, enabled: true }, group: null, ...over,
  }
}

const look = { name: 'Quiz', icon: null }

function mount(t: SpaceTable, opts: { canEditGames?: boolean } = {}) {
  const onChanged = vi.fn()
  const view = render(
    <SpaceGameCard spaceId="sp-1" table={t} look={look} entry={null}
                   canEditGames={opts.canEditGames ?? false} onChanged={onChanged} />,
  )
  return { ...view, onChanged }
}

beforeEach(() => {
  apiPost.mockReset()
  route.mockReset()
  toast.mockReset()
  confirm.mockReset()
  user.value = { user_id: 'u1', is_admin: false, protected: false }
})

describe('SpaceGameCard', () => {
  it('shows the app, who started it, the players, the note and how many joined', () => {
    const v = mount(table())
    expect(v.getByRole('heading', { name: 'Quiz' })).toBeTruthy()
    expect(v.getByText('Anna started this game')).toBeTruthy()
    expect(v.getByText(/3–8 players/)).toBeTruthy()
    expect(v.getByText('Bring snacks')).toBeTruthy()
    expect(v.getByText('2 joined')).toBeTruthy()
    expect(v.container.textContent).not.toMatch(/host/i)
  })

  it('Join takes a seat and opens the game', async () => {
    apiPost.mockResolvedValue({ group: { my_state: 'joining' } })
    const v = mount(table())
    fireEvent.click(v.getByRole('button', { name: 'Join Quiz' }))
    await waitFor(() => expect(route).toHaveBeenCalledWith('/apps/quiz/tables/s1'))
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/sp-1/games/s1/join')
    expect(v.onChanged).toHaveBeenCalled()
  })

  it('a two-player game: Play takes the seat — anyone can take it', async () => {
    apiPost.mockResolvedValue({})
    const v = mount(table({ min: 2, max: 2, seated: 1 }))
    expect(v.getByText(/anyone can take the seat/)).toBeTruthy()
    expect(v.getByText('1 of 2')).toBeTruthy()
    fireEvent.click(v.getByRole('button', { name: /Play Quiz/ }))
    await waitFor(() => expect(route).toHaveBeenCalledWith('/apps/quiz/tables/s1'))
  })

  it('a join refused because the app is off here asks an admin', async () => {
    apiPost.mockRejectedValue(new ApiError(403, '/x', { code: 'APP_NOT_ENABLED', detail: 'off' }))
    const v = mount(table())
    fireEvent.click(v.getByRole('button', { name: 'Join Quiz' }))
    await v.findByRole('dialog', { name: 'Ask an admin for Quiz?' })
    expect(route).not.toHaveBeenCalled()
  })

  it('any other refusal is a toast in your language', async () => {
    apiPost.mockRejectedValue(new ApiError(409, '/x', { code: 'APP_GROUP_FULL', detail: 'full' }))
    const v = mount(table())
    fireEvent.click(v.getByRole('button', { name: 'Join Quiz' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('This game is full.', 'error'))
  })

  it('no app here: Ask an admin (a grown-up for a protected account); an admin sees the app', () => {
    const missing = table({ app: { installed: false, enabled: false } })
    let v = mount(missing)
    expect(v.getByRole('button', { name: 'Ask an admin for Quiz' })).toBeTruthy()
    expect(v.getByText('Your home doesn’t have this app yet.'.replace('’', "'"))).toBeTruthy()
    v.unmount()
    user.value = { ...user.value, protected: true }
    v = mount(missing)
    expect(v.getByRole('button', { name: 'Ask a grown-up for Quiz' })).toBeTruthy()
    v.unmount()
    user.value = { ...user.value, protected: false, is_admin: true }
    v = mount(missing)
    expect(v.getByRole('link', { name: 'See Quiz' }).getAttribute('href')).toBe('/apps/quiz/about')
  })

  it('Full is shown and can’t be pressed', () => {
    const v = mount(table({ state: 'full', seated: 8 }))
    expect((v.getByRole('button', { name: 'Full' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('your own game: Open goes to it (no Join)', () => {
    const v = mount(table({ group: { is_host: true, my_state: 'seated' } }))
    expect(v.getByRole('link', { name: 'Open your Quiz game' }).getAttribute('href'))
      .toBe('/apps/quiz/tables/s1')
    expect(v.getByText('You started this game')).toBeTruthy()
  })

  it('the starter closes their game after a confirm', async () => {
    confirm.mockResolvedValue(true)
    apiPost.mockResolvedValue({ ok: true })
    const v = mount(table({ group: { is_host: true, my_state: 'seated' } }))
    fireEvent.click(v.getByRole('button', { name: 'Close the Quiz game' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/spaces/sp-1/games/s1/close'))
    expect(confirm.mock.calls[0][0]).toMatch(/Everyone who joined is told/)
  })

  it('a moderator may close anyone’s game; a member may not; nothing sent on Cancel', async () => {
    confirm.mockResolvedValue(false)
    let v = mount(table(), { canEditGames: true })
    fireEvent.click(v.getByRole('button', { name: 'Close the Quiz game' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(confirm.mock.calls[0][0]).toMatch(/a moderator closed it/)
    expect(apiPost).not.toHaveBeenCalled()
    v.unmount()
    v = mount(table(), { canEditGames: false })
    expect(v.queryByRole('button', { name: 'Close the Quiz game' })).toBeNull()
  })

  it('a finished game says why and offers nothing', () => {
    const v = mount(table({ state: 'closed', end_reason: 'closed_by_moderator' }), { canEditGames: true })
    expect(v.getByText('A moderator closed this game.')).toBeTruthy()
    expect(v.queryByRole('button')).toBeNull()
  })
})
