/**
 * Settings › Games ("Games allowed here"): this home's group games, the
 * catalog's and anything on the list; each switch saves the whole list;
 * one save at a time with "Saving…"; a failure puts it back; a space
 * hosted elsewhere says the host decides; an old host household turns
 * the switches off; the copy names owner, admins AND moderators.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPut = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: { get: (...a: unknown[]) => apiGet(...a), put: (...a: unknown[]) => apiPut(...a) },
}))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import { GamesSection, nextAllowedApps } from './GamesSection'
import { catalog, catalogLoaded, installedApps } from '@/store/apps'
import type { Space } from '@/types'

function space(allowed: string[]): Space {
  return {
    id: 'sp-1', name: 'Lakeside', description: null, emoji: null, space_type: 'private',
    features: { allowed_apps: allowed },
  } as unknown as Space
}

beforeEach(() => {
  apiGet.mockReset()
  apiPut.mockReset()
  toast.mockReset()
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/sp-1/games') return { host_supports: true }
    if (url === '/api/apps') return { apps: installedApps.value }
    return { apps: catalog.value }
  })
  installedApps.value = [{
    app_id: 'quiz', name: 'Quiz', version: '1', enabled: true, capabilities: [], icon: null,
    min_age: 0, tables: true, players: { min: 3, max: 8 },
  }]
  catalog.value = [{
    app_id: 'draw', name: 'Gift draw', latest_version: '1', description: null, icon_url: null,
    capabilities: [], tables: true, players: { min: 3, max: 30 }, min_age: 0,
  }]
  catalogLoaded.value = true
})

describe('GamesSection', () => {
  it('lists this home’s games and the catalog’s with players, rating and where they are', async () => {
    const v = render(<GamesSection space={space([])} onUpdate={() => {}} />)
    const quiz = v.getByRole('switch', { name: 'Quiz' })
    expect((quiz as HTMLInputElement).checked).toBe(false)
    expect(document.getElementById(quiz.getAttribute('aria-describedby')!)?.textContent)
      .toMatch(/3–8 players · Everyone · In your home/)
    expect(v.getByText('Not in your home yet')).toBeTruthy()
    expect(v.container.textContent).toMatch(/The owner, admins and moderators can change this list/)
    expect(v.container.textContent).not.toMatch(/Only admins/)
    expect(v.container.textContent).toMatch(/closes its open games here/)
  })

  it('a switch saves the whole list', async () => {
    apiPut.mockResolvedValue({ allowed_apps: ['quiz', 'draw'], forwarded: false })
    const onUpdate = vi.fn()
    const v = render(<GamesSection space={space(['quiz'])} onUpdate={onUpdate} />)
    fireEvent.click(v.getByRole('switch', { name: 'Gift draw' }))
    expect(v.getByText('Saving…')).toBeTruthy()
    await waitFor(() => expect(onUpdate).toHaveBeenCalled())
    expect(apiPut).toHaveBeenCalledWith('/api/spaces/sp-1/games/allowed', { allowed_apps: ['quiz', 'draw'] })
    expect((v.getByRole('switch', { name: 'Gift draw' }) as HTMLInputElement).checked).toBe(true)
    expect(toast).toHaveBeenCalledWith('Gift draw is allowed here now.', 'success')
  })

  it('a failure puts the switch back', async () => {
    apiPut.mockRejectedValue(new Error('Offline'))
    const v = render(<GamesSection space={space(['quiz'])} onUpdate={() => {}} />)
    fireEvent.click(v.getByRole('switch', { name: 'Quiz' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Offline', 'error'))
    expect((v.getByRole('switch', { name: 'Quiz' }) as HTMLInputElement).checked).toBe(true)
  })

  it('a space hosted elsewhere: the host decides, the list stays until it does', async () => {
    apiPut.mockResolvedValue({ allowed_apps: [], forwarded: true })
    const v = render(<GamesSection space={space([])} onUpdate={() => {}} />)
    fireEvent.click(v.getByRole('switch', { name: 'Quiz' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/host household/), 'info'))
    expect((v.getByRole('switch', { name: 'Quiz' }) as HTMLInputElement).checked).toBe(false)
  })

  it('an old host household: a note, the switches off', async () => {
    apiGet.mockImplementation(async () => ({ host_supports: false }))
    const v = render(<GamesSection space={space([])} onUpdate={() => {}} />)
    await v.findByText(/host household needs an update/)
    expect((v.getByRole('switch', { name: 'Quiz' }) as HTMLInputElement).disabled).toBe(true)
  })

  it('a game on the list nobody here knows can still be taken off', () => {
    const v = render(<GamesSection space={space(['gone'])} onUpdate={() => {}} />)
    expect((v.getByRole('switch', { name: 'gone' }) as HTMLInputElement).checked).toBe(true)
  })

  it('nextAllowedApps adds at the end and removes in place', () => {
    expect(nextAllowedApps(['a', 'b'], 'c', true)).toEqual(['a', 'b', 'c'])
    expect(nextAllowedApps(['a', 'b'], 'a', false)).toEqual(['b'])
    expect(nextAllowedApps(['a'], 'a', true)).toEqual(['a'])
  })
})
