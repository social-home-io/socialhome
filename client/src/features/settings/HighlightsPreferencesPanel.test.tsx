import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { HighlightsPreferencesPanel } from './HighlightsPreferencesPanel'

const FRIENDS = {
  instance: { members: [
    { user_id: 'u-me', display_name: 'Me' },
    { user_id: 'u-lena', display_name: 'Lena' },
  ] },
  households: [
    { instance_id: 'inst-k', display_name: 'Keller', members: [{ user_id: 'u-sophie', display_name: 'Sophie' }] },
    { instance_id: 'inst-h', display_name: 'Huber', members: [] },
  ],
}

function me(highlights: object | null) {
  currentUser.value = {
    user_id: 'u-me', username: 'me', display_name: 'Me',
    preferences_json: highlights ? JSON.stringify({ highlights }) : '{}',
  } as unknown as User
}

/** The ``highlights`` block of the last PATCH /api/me. */
function lastSaved() {
  const calls = apiMock.patch.mock.calls.filter(c => c[0] === '/api/me')
  return (calls.at(-1)?.[1] as { preferences: { highlights: Record<string, unknown> } })
    .preferences.highlights
}

describe('Settings › Highlights — who sees a new highlight', () => {
  beforeEach(() => {
    apiMock.get.mockReset().mockImplementation(async (p: string) => p === '/api/friends' ? FRIENDS : [])
    apiMock.patch.mockReset().mockResolvedValue({})
  })

  it('saves the chosen households the moment they are picked — no Save button', async () => {
    me(null)
    const { getByRole, findByRole, queryByRole } = render(<HighlightsPreferencesPanel />)
    expect(queryByRole('button', { name: 'Save' })).toBeNull()
    fireEvent.click(getByRole('radio', { name: 'Chosen paired households' }))
    await waitFor(() => expect(lastSaved().default_audience).toEqual({ kind: 'households', ids: [] }))
    fireEvent.click(await findByRole('checkbox', { name: 'Keller' }))
    await waitFor(() => expect(lastSaved().default_audience).toEqual({ kind: 'households', ids: ['inst-k'] }))
    expect(lastSaved().retention_days).toBe(30)
    expect((await findByRole('status')).textContent).toContain('Saved')
  })

  it('shows the saved choice, people included', async () => {
    me({ retention_days: 7, default_audience: { kind: 'users', ids: ['u-sophie'] } })
    const { getByRole, findByRole } = render(<HighlightsPreferencesPanel />)
    expect((getByRole('radio', { name: 'Chosen people' }) as HTMLInputElement).checked).toBe(true)
    expect(((await findByRole('checkbox', { name: /Sophie/ })) as HTMLInputElement).checked).toBe(true)
    expect(((getByRole('checkbox', { name: /Lena/ })) as HTMLInputElement).checked).toBe(false)
    expect((getByRole('spinbutton', { name: /Retention/ }) as HTMLInputElement).value).toBe('7')
  })

  it('back to all paired households drops the picked ids', async () => {
    me({ default_audience: { kind: 'households', ids: ['inst-k'] } })
    const { getByRole } = render(<HighlightsPreferencesPanel />)
    fireEvent.click(getByRole('radio', { name: 'All paired households' }))
    await waitFor(() => expect(lastSaved().default_audience).toEqual({ kind: 'all_paired', ids: [] }))
  })

  it('a failed save says so and keeps the choice on screen', async () => {
    me(null)
    apiMock.patch.mockRejectedValue(new Error('offline'))
    const { getByRole, findByRole } = render(<HighlightsPreferencesPanel />)
    fireEvent.click(getByRole('radio', { name: 'Chosen people' }))
    expect((await findByRole('status')).textContent).toContain("Couldn't save")
    expect((getByRole('radio', { name: 'Chosen people' }) as HTMLInputElement).checked).toBe(true)
  })

  it('retention saves on change, clamped to 1–90 days', async () => {
    me(null)
    const { getByRole } = render(<HighlightsPreferencesPanel />)
    const input = getByRole('spinbutton', { name: /Retention/ })
    fireEvent.change(input, { target: { value: '400' } })
    await waitFor(() => expect(lastSaved().retention_days).toBe(90))
  })
})
