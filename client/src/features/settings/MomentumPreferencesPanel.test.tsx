import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { getMomentsPreferences } from '@/utils/preferences'
import { MomentumPreferencesPanel } from './MomentumPreferencesPanel'

function me(moments: object | null) {
  currentUser.value = {
    user_id: 'u-me', username: 'me', display_name: 'Me',
    preferences_json: moments ? JSON.stringify({ moments }) : '{}',
  } as unknown as User
}

/** The ``moments`` block of the last PATCH /api/me. */
function lastSaved() {
  const calls = apiMock.patch.mock.calls.filter(c => c[0] === '/api/me')
  return (calls.at(-1)?.[1] as { preferences: { moments: Record<string, unknown> } })
    .preferences.moments
}

const selects = (c: Element) => c.querySelectorAll('select')

describe('Settings › Momentum — how long followers keep your moments', () => {
  beforeEach(() => {
    apiMock.patch.mockReset().mockResolvedValue({})
  })

  it('starts at 7 days (the default) and says who it applies to', () => {
    me(null)
    const { container, getByText } = render(<MomentumPreferencesPanel />)
    const retention = selects(container)[0] as HTMLSelectElement
    expect(retention.value).toBe('7')
    expect(retention.selectedOptions[0].textContent).toBe('7\u00a0days (default)')
    expect([...retention.options].map(o => o.value)).toEqual(['1', '3', '7', '14', '30'])
    expect(getByText(/Everyone else sees each moment for 24 hours/)).toBeTruthy()
    expect(getByText(/Your own archive keeps them/)).toBeTruthy()
  })

  it('saves at once, keeping max_hops beside the retention', async () => {
    me({ max_hops: 2 })
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    fireEvent.change(selects(container)[0], { target: { value: '30' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 2, follower_retention_days: 30,
    }))
    expect(getByRole('status').textContent).toBe('Saved')
  })

  it('the hop picker saves at once too, keeping the retention', async () => {
    me({ follower_retention_days: 14 })
    const { container } = render(<MomentumPreferencesPanel />)
    fireEvent.change(selects(container)[1], { target: { value: '1' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 1, follower_retention_days: 14,
    }))
  })

  it('a failed save says so and keeps the pick on screen', async () => {
    me(null)
    apiMock.patch.mockRejectedValueOnce(new Error('offline'))
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    const retention = selects(container)[0] as HTMLSelectElement
    fireEvent.change(retention, { target: { value: '1' } })
    await waitFor(() => expect(getByRole('status').textContent).toMatch(/Couldn't save/))
    expect(retention.value).toBe('1')
    expect(retention.selectedOptions[0].textContent).toBe('1\u00a0day')
  })

  it('a stored value that is not a preset shows as itself', () => {
    me({ follower_retention_days: 10 })
    const { container } = render(<MomentumPreferencesPanel />)
    const retention = selects(container)[0] as HTMLSelectElement
    expect(retention.value).toBe('10')
    expect([...retention.options].map(o => o.value)).toEqual(['1', '3', '7', '10', '14', '30'])
  })
})

describe('Settings › Momentum — a protected account', () => {
  beforeEach(() => {
    apiMock.patch.mockReset().mockResolvedValue({})
  })

  it('shows its fixed circle instead of a 3-hop picker it does not have', async () => {
    currentUser.value = {
      user_id: 'u-kid', username: 'finn', display_name: 'Finn', protected: true,
      preferences_json: JSON.stringify({ moments: { max_hops: 3 } }),
    } as unknown as User
    const { container, getByText, queryByText } = render(<MomentumPreferencesPanel />)
    // Only the retention select; no "Show moments up to".
    expect(selects(container)).toHaveLength(1)
    expect(queryByText(/Show moments up to/)).toBeNull()
    expect(queryByText(/up to 3 households away/)).toBeNull()
    expect(getByText(/you see moments from your household and paired households only/)).toBeTruthy()
    // Retention still saves, max_hops untouched.
    fireEvent.change(selects(container)[0], { target: { value: '14' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 3, follower_retention_days: 14,
    }))
  })
})

describe('getMomentsPreferences — the server\'s defaults and clamps', () => {
  it.each([
    [null, 7, 3],
    [{ follower_retention_days: 0 }, 1, 3],
    [{ follower_retention_days: 365 }, 30, 3],
    [{ follower_retention_days: 'lots' }, 7, 3],
    [{ follower_retention_days: 12.7, max_hops: 2 }, 12, 2],
    [{ max_hops: 9 }, 7, 3],
  ])('%j → %i days, %i hops', (moments, days, hops) => {
    me(moments)
    expect(getMomentsPreferences()).toEqual({
      follower_retention_days: days, max_hops: hops,
    })
  })
})
