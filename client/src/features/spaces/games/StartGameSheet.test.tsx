/**
 * Start a game: the games on the list this home can play, how many, how
 * long, a note, "Let everyone know" with its daily cap explained; the
 * POST and the game's screen after; a refusal stays in the sheet.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { ApiError } from '@/api'

const apiPost = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: { post: (...a: unknown[]) => apiPost(...a), get: vi.fn() },
}))
const route = vi.fn()
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route, path: '/', url: '/', query: {} }),
}))
const user = vi.hoisted(() => ({ value: { user_id: 'u1', is_admin: false } as Record<string, unknown> }))
vi.mock('@/store/auth', () => ({ currentUser: user }))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import { StartGameSheet } from './StartGameSheet'
import { catalog, installedApps, type InstalledApp } from '@/store/apps'

function app(over: Partial<InstalledApp>): InstalledApp {
  return {
    app_id: 'quiz', name: 'Quiz', version: '1', enabled: true, capabilities: [], icon: null,
    min_age: 0, tables: true, players: { min: 3, max: 8 }, ...over,
  }
}

function mount(allowed: string[]) {
  const onClose = vi.fn()
  const onStarted = vi.fn()
  const v = render(<StartGameSheet spaceId="sp-1" allowed={allowed} open onClose={onClose} onStarted={onStarted} />)
  return { ...v, onClose, onStarted }
}

beforeEach(() => {
  apiPost.mockReset()
  route.mockReset()
  toast.mockReset()
  user.value = { user_id: 'u1', is_admin: false }
  installedApps.value = [app({}), app({ app_id: 'chess', name: 'Chess', players: { min: 2, max: 2 } })]
  catalog.value = [{
    app_id: 'draw', name: 'Gift draw', latest_version: '1', description: null, icon_url: null,
    capabilities: [], tables: true, players: { min: 3, max: 30 },
  }]
})

describe('StartGameSheet', () => {
  it('opens a game with what was chosen and goes to it', async () => {
    apiPost.mockResolvedValue({ table: { app_id: 'quiz', session_id: 'new1' } })
    const v = mount(['quiz', 'chess'])
    fireEvent.click(v.getByRole('radio', { name: /Quiz/ }))
    fireEvent.change(v.getByLabelText('How many can play'), { target: { value: '4' } })
    fireEvent.change(v.getByLabelText('Open for'), { target: { value: '7' } })
    fireEvent.input(v.getByLabelText('Note (optional)'), { target: { value: '  After dinner  ' } })
    fireEvent.click(v.getByRole('checkbox', { name: 'Let everyone know' }))
    fireEvent.click(v.getByRole('button', { name: 'Start game' }))
    await waitFor(() => expect(route).toHaveBeenCalledWith('/apps/quiz/tables/new1'))
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/sp-1/games', {
      app_id: 'quiz', max_players: 4, lobby_days: 7, announce: true, note: 'After dinner',
    })
    expect(v.onStarted).toHaveBeenCalled()
    expect(v.onClose).toHaveBeenCalled()
  })

  it('one game to choose is chosen; defaults: as many as it takes, 3 days, nobody told', async () => {
    apiPost.mockResolvedValue({ table: { app_id: 'quiz', session_id: 'n' } })
    const v = mount(['quiz'])
    expect((v.getByRole('radio', { name: /Quiz/ }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(v.getByRole('button', { name: 'Start game' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(apiPost.mock.calls[0][1]).toEqual({ app_id: 'quiz', max_players: 8, lobby_days: 3, announce: false })
  })

  it('the daily cap of "Let everyone know" is explained before anyone hits it', () => {
    const v = mount(['quiz'])
    const box = v.getByRole('checkbox', { name: 'Let everyone know' })
    const hint = document.getElementById(box.getAttribute('aria-describedby')!)
    expect(hint?.textContent).toMatch(/no push.*one game a day/)
  })

  it('a two-player game has no count to choose and starts when someone takes the seat', () => {
    const v = mount(['chess'])
    expect(v.queryByLabelText('How many can play')).toBeNull()
    expect(v.getByText(/starts as soon as someone takes the seat/)).toBeTruthy()
  })

  it('games on the list this home lacks: Ask an admin, nothing to start', () => {
    const v = mount(['draw'])
    expect(v.getByText('None of the games allowed here is in your home yet.')).toBeTruthy()
    expect(v.getByRole('button', { name: 'Ask an admin for Gift draw' })).toBeTruthy()
    expect((v.getByRole('button', { name: 'Start game' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a refusal is a toast and the sheet stays', async () => {
    apiPost.mockRejectedValue(new ApiError(429, '/x', { code: 'SPACE_TABLE_ANNOUNCE_LIMIT', detail: 'x' }))
    const v = mount(['quiz'])
    fireEvent.click(v.getByRole('button', { name: 'Start game' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      expect.stringMatching(/already let everyone know/), 'error'))
    expect(v.onClose).not.toHaveBeenCalled()
    expect(route).not.toHaveBeenCalled()
  })
})
