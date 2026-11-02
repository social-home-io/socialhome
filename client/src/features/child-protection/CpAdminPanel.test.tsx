import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

const apiGet = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/components/Spinner', () => ({ Spinner: () => null }))

const tick = () => new Promise((r) => setTimeout(r, 0))

describe('CpAdminPanel', () => {
  beforeEach(() => apiGet.mockReset())

  it('module exports exist', async () => {
    const mod = await import('./CpAdminPanel')
    expect(mod).toBeTruthy()
  })

  it('ageFromDob computes whole years and ignores bad input', async () => {
    const { ageFromDob } = await import('./CpAdminPanel')
    const today = new Date()
    // Born exactly 8 years ago today → 8.
    const eight = new Date(today.getFullYear() - 8, today.getMonth(), today.getDate())
    expect(ageFromDob(eight.toISOString().slice(0, 10))).toBe(8)
    // Born 10 years ago but the birthday is one day in the future → 9
    // (not yet had this year's birthday). Shift by 2 days to stay clear of
    // any same-day boundary regardless of when the test runs.
    const notYet = new Date(today)
    notYet.setDate(notYet.getDate() + 2)
    notYet.setFullYear(notYet.getFullYear() - 10)
    expect(ageFromDob(notYet.toISOString().slice(0, 10))).toBe(9)
    expect(ageFromDob('')).toBeNull()
    expect(ageFromDob('not-a-date')).toBeNull()
  })

  // Regression: the "Protected" column must reflect /api/cp/protection,
  // NOT user.is_minor — that field is stripped from /api/users as a
  // SENSITIVE_FIELD, so the column was permanently blank before the fix.
  it('shows protection status from /api/cp/protection, not /api/users', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/users') {
        return Promise.resolve([
          { user_id: 'u-admin', username: 'admin', display_name: 'Admin' },
          { user_id: 'u-mia', username: 'mia', display_name: 'Mia' },
        ])
      }
      if (path === '/api/cp/protection') {
        return Promise.resolve({
          users: [
            { user_id: 'u-admin', username: 'admin', is_minor: false, declared_age: 0 },
            { user_id: 'u-mia', username: 'mia', is_minor: true, declared_age: 8 },
          ],
        })
      }
      return Promise.resolve([])
    })

    const { default: CpAdminPanel } = await import('./CpAdminPanel')
    const { container } = render(<CpAdminPanel />)
    await tick()
    await tick()

    const text = container.textContent || ''
    // The protected minor is reflected with their declared age.
    expect(text).toContain('🔒 Yes · age 8')
    // The endpoint that actually carries protection state was queried.
    expect(apiGet).toHaveBeenCalledWith('/api/cp/protection')
  })

  it('stays a table for screen readers when a phone stacks it into cards', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/users') {
        return Promise.resolve([{ user_id: 'u-mia', username: 'mia', display_name: 'Mia' }])
      }
      if (path === '/api/cp/protection') {
        return Promise.resolve({
          users: [{ user_id: 'u-mia', username: 'mia', is_minor: true, declared_age: 8 }],
        })
      }
      return Promise.resolve([])
    })
    const { default: CpAdminPanel } = await import('./CpAdminPanel')
    const view = render(<CpAdminPanel />)
    await tick()
    await tick()
    // The phone CSS sets ``display: block`` on the rows; the explicit
    // roles keep the table, its headers and cells announced.
    const table = view.getByRole('table')
    expect(table.classList.contains('sh-cp-table')).toBe(true)
    expect(view.getAllByRole('columnheader')).toHaveLength(4)
    const cells = view.getAllByRole('cell')
    expect(cells.map(c => c.className)).toEqual([
      'sh-cp-table__name', 'sh-cp-table__user', 'sh-cp-table__status', 'sh-cp-table__actions',
    ])
  })
})
