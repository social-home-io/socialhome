import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), patch: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { currentUser } from '@/store/auth'
import { momentReachMax, myMomentReach } from '@/store/momentLimits'
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

/** Each describe starts from a fresh, readable ``/api/moments/limits``. */
function reset(limits: object = { next_allowed_at: null, reach: 3, reach_max: 5 }) {
  apiMock.patch.mockReset().mockResolvedValue({})
  apiMock.put.mockReset().mockResolvedValue({ reach_max: 2 })
  apiMock.get.mockReset().mockResolvedValue(limits)
  myMomentReach.value = null
  momentReachMax.value = null
}

describe('Settings › Momentum — how long followers keep your moments', () => {
  beforeEach(() => reset())

  it('starts at 7 days (the default) and says who it applies to', () => {
    me(null)
    const { container, getByText } = render(<MomentumPreferencesPanel />)
    const retention = selects(container)[0] as HTMLSelectElement
    expect(retention.value).toBe('7')
    expect(retention.selectedOptions[0].textContent).toBe('7 days (default)')
    expect([...retention.options].map(o => o.value)).toEqual(['1', '3', '7', '14', '30'])
    expect(getByText(/Everyone else sees each moment for 24 hours/)).toBeTruthy()
    expect(getByText(/Your own archive keeps them/)).toBeTruthy()
  })

  it('saves at once, keeping max_hops and reach beside the retention', async () => {
    me({ max_hops: 2 })
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    fireEvent.change(selects(container)[0], { target: { value: '30' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 2, follower_retention_days: 30, reach: 3,
    }))
    expect(getByRole('status').textContent).toBe('Saved')
  })

  it('the hop picker saves at once too, keeping the retention', async () => {
    me({ follower_retention_days: 14 })
    const { container } = render(<MomentumPreferencesPanel />)
    fireEvent.change(selects(container)[1], { target: { value: '1' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 1, follower_retention_days: 14, reach: 3,
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
    expect(retention.selectedOptions[0].textContent).toBe('1 day')
  })

  it('a stored value that is not a preset shows as itself', () => {
    me({ follower_retention_days: 10 })
    const { container } = render(<MomentumPreferencesPanel />)
    const retention = selects(container)[0] as HTMLSelectElement
    expect(retention.value).toBe('10')
    expect([...retention.options].map(o => o.value)).toEqual(['1', '3', '7', '10', '14', '30'])
  })
})

describe('Settings › Momentum — show moments up to 5 steps', () => {
  beforeEach(() => reset())

  it('offers 1–5, 3 the default, and no longer claims 3 is every moment', () => {
    me(null)
    const { container } = render(<MomentumPreferencesPanel />)
    const hops = selects(container)[1] as HTMLSelectElement
    expect([...hops.options].map(o => o.value)).toEqual(['1', '2', '3', '4', '5'])
    expect(hops.value).toBe('3')
    expect(hops.selectedOptions[0].textContent).toBe('3 steps (default)')
    expect(container.textContent).not.toMatch(/every moment that reaches you/)
    expect(container.textContent).not.toMatch(/up to 3 households away/)
  })

  it('a stored 5 shows as 5 and saves beside the rest', async () => {
    me({ max_hops: 5, reach: 2, follower_retention_days: 3 })
    const { container } = render(<MomentumPreferencesPanel />)
    const hops = selects(container)[1] as HTMLSelectElement
    expect(hops.value).toBe('5')
    fireEvent.change(hops, { target: { value: '4' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 4, follower_retention_days: 3, reach: 2,
    }))
  })
})

describe('Settings › Momentum — how far my moments go', () => {
  beforeEach(() => reset())

  const reachSelect = (c: Element) =>
    c.querySelector('#sh-momentum-reach') as HTMLSelectElement

  it('offers 1–5, starts at 3, and says how far steps 4 and 5 reach', () => {
    me(null)
    const { container, getByText } = render(<MomentumPreferencesPanel />)
    const reach = reachSelect(container)
    expect(reach).not.toBeNull()
    expect([...reach.options].map(o => o.value)).toEqual(['1', '2', '3', '4', '5'])
    expect(reach.value).toBe('3')
    expect([...reach.options].some(o => o.disabled)).toBe(false)
    expect(getByText('Steps 4 and 5 only reach households where someone looks that far.')).toBeTruthy()
    // Owner 2026-11-24: no word about older Social Home versions here.
    expect(container.textContent).not.toMatch(/older/i)
  })

  it('saves reach in the same moments block as the other two', async () => {
    me({ max_hops: 2, follower_retention_days: 14 })
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(myMomentReach.value).toBe(3))
    apiMock.get.mockResolvedValue({ next_allowed_at: null, reach: 5, reach_max: 5 })
    fireEvent.change(reachSelect(container), { target: { value: '5' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 2, follower_retention_days: 14, reach: 5,
    }))
    expect(getByRole('status').textContent).toBe('Saved')
    // The server says what now applies; the composer and inbox read it.
    await waitFor(() => expect(myMomentReach.value).toBe(5))
    expect(apiMock.get).toHaveBeenCalledTimes(2)
  })

  it('a failed reach save reads nothing again', async () => {
    me(null)
    apiMock.patch.mockRejectedValue(new Error('offline'))
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(myMomentReach.value).toBe(3))
    fireEvent.change(reachSelect(container), { target: { value: '5' } })
    await waitFor(() => expect(getByRole('status').textContent).toMatch(/Couldn't save/))
    expect(apiMock.get).toHaveBeenCalledTimes(1)
    expect(myMomentReach.value).toBe(3)
  })

  it('options above the household maximum are disabled, with a line saying so', async () => {
    reset({ next_allowed_at: null, reach: 2, reach_max: 2 })
    me({ reach: 2 })
    const { container, findByText } = render(<MomentumPreferencesPanel />)
    expect(await findByText('Your household allows up to 2 steps.')).toBeTruthy()
    const reach = reachSelect(container)
    expect([...reach.options].map(o => o.disabled)).toEqual([false, false, true, true, true])
  })

  it('a stored reach above the household maximum shows the maximum, saving nothing', async () => {
    reset({ next_allowed_at: null, reach: 3, reach_max: 3 })
    me({ reach: 5 })
    const { container, findByText } = render(<MomentumPreferencesPanel />)
    expect(await findByText('Your household allows up to 3 steps.')).toBeTruthy()
    // A disabled option must never show as the selected value.
    expect(reachSelect(container).value).toBe('3')
    expect(apiMock.patch).not.toHaveBeenCalled()
  })

  it('a household maximum of 1 reads naturally', async () => {
    reset({ next_allowed_at: null, reach: 1, reach_max: 1 })
    me(null)
    const { findByText } = render(<MomentumPreferencesPanel />)
    expect(await findByText('Your household allows 1 step only.')).toBeTruthy()
  })

  it('limits that cannot be read still offer 1–5 (the server clamps)', async () => {
    reset()
    apiMock.get.mockRejectedValue(new Error('offline'))
    me(null)
    const { container } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/moments/limits'))
    const reach = reachSelect(container)
    expect([...reach.options].some(o => o.disabled)).toBe(false)
    expect(container.textContent).not.toMatch(/Your household allows/)
  })

  it('a non-admin sees no household maximum', async () => {
    me(null)
    const { container } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(momentReachMax.value).toBe(5))
    expect(container.querySelector('#sh-momentum-reach-max')).toBeNull()
  })
})

describe('Settings › Momentum — the household maximum (admins)', () => {
  beforeEach(() => reset())

  function admin() {
    currentUser.value = {
      user_id: 'u-admin', username: 'pascal', display_name: 'Pascal', is_admin: true,
      preferences_json: '{}',
    } as unknown as User
  }
  const maxSelect = (c: Element) =>
    c.querySelector('#sh-momentum-reach-max') as HTMLSelectElement

  it('starts at 5 (no limit) once read, and saves at once', async () => {
    admin()
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(maxSelect(container)?.value).toBe('5'))
    const max = maxSelect(container)
    expect([...max.options].map(o => o.value)).toEqual(['1', '2', '3', '4', '5'])
    expect(max.selectedOptions[0].textContent).toBe('5 steps (default, no limit)')
    apiMock.get.mockResolvedValue({ next_allowed_at: null, reach: 2, reach_max: 2 })
    fireEvent.change(max, { target: { value: '2' } })
    await waitFor(() => expect(apiMock.put).toHaveBeenCalledWith(
      '/api/admin/moments/reach-max', { reach_max: 2 },
    ))
    await waitFor(() => expect(getByRole('status').textContent).toBe('Saved'))
    // The person's own picker follows the new maximum.
    const reach = container.querySelector('#sh-momentum-reach') as HTMLSelectElement
    await waitFor(() => expect([...reach.options].map(o => o.disabled))
      .toEqual([false, false, true, true, true]))
    // …and the reach the server now applies is read again.
    await waitFor(() => expect(myMomentReach.value).toBe(2))
    // Never a moments-preferences write for the household setting.
    expect(apiMock.patch).not.toHaveBeenCalled()
  })

  it('a failed save puts the old value back and says so', async () => {
    admin()
    apiMock.put.mockRejectedValueOnce(new Error('forbidden'))
    const { container, getByRole } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(maxSelect(container)?.value).toBe('5'))
    fireEvent.change(maxSelect(container), { target: { value: '1' } })
    await waitFor(() => expect(getByRole('status').textContent).toMatch(/Couldn't save/))
    expect(maxSelect(container).value).toBe('5')
    expect(momentReachMax.value).toBe(5)
  })

  it('no control while the maximum is unknown', async () => {
    admin()
    apiMock.get.mockRejectedValue(new Error('offline'))
    const { container } = render(<MomentumPreferencesPanel />)
    await waitFor(() => expect(apiMock.get).toHaveBeenCalled())
    expect(maxSelect(container)).toBeNull()
  })
})

describe('Settings › Momentum — a protected account', () => {
  beforeEach(() => reset({ next_allowed_at: null, reach: 1, reach_max: 5 }))

  it('shows its fixed circle instead of pickers it does not have', async () => {
    currentUser.value = {
      user_id: 'u-kid', username: 'finn', display_name: 'Finn', protected: true,
      preferences_json: JSON.stringify({ moments: { max_hops: 3 } }),
    } as unknown as User
    const { container, getByText, queryByText } = render(<MomentumPreferencesPanel />)
    // Only the retention select; no "Show moments up to", no reach.
    expect(selects(container)).toHaveLength(1)
    expect(queryByText(/Show moments up to/)).toBeNull()
    expect(queryByText(/How far my moments go/)).toBeNull()
    expect(getByText(/you see moments from your household and paired households only/)).toBeTruthy()
    expect(getByText(/yours go to the same households/)).toBeTruthy()
    // Retention still saves, max_hops untouched.
    fireEvent.change(selects(container)[0], { target: { value: '14' } })
    await waitFor(() => expect(lastSaved()).toEqual({
      max_hops: 3, follower_retention_days: 14, reach: 3,
    }))
  })
})

describe('getMomentsPreferences — the server\'s defaults and clamps', () => {
  it.each([
    [null, 7, 3, 3],
    [{ follower_retention_days: 0 }, 1, 3, 3],
    [{ follower_retention_days: 365 }, 30, 3, 3],
    [{ follower_retention_days: 'lots' }, 7, 3, 3],
    [{ follower_retention_days: 12.7, max_hops: 2 }, 12, 2, 3],
    [{ max_hops: 4, reach: 5 }, 7, 4, 5],
    [{ max_hops: 5, reach: 1 }, 7, 5, 1],
    // Like the server: a whole number clamps to 1–5, anything else is 3.
    [{ max_hops: 9, reach: 0 }, 7, 5, 1],
    [{ max_hops: '2', reach: 2.5 }, 7, 3, 3],
    [{ max_hops: true, reach: null }, 7, 3, 3],
  ])('%j → %i days, %i hops, reach %i', (moments, days, hops, reach) => {
    me(moments)
    expect(getMomentsPreferences()).toEqual({
      follower_retention_days: days, max_hops: hops, reach,
    })
  })
})
