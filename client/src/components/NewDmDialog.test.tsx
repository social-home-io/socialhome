import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

describe('NewDmDialog', () => {
  it('module exports exist', async () => {
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({ currentUser: { value: null } }))
    const mod = await import('./NewDmDialog')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('omits the current user from the recipient list', async () => {
    const get = vi.fn(async () => ([
      { username: 'pascal', display_name: 'Pascal' },
      { username: 'maria',  display_name: 'Maria'  },
      { username: 'lina',   display_name: 'Lina'   },
    ]))
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    // Wait a tick for the api.get to resolve and the signal to settle.
    await new Promise((r) => setTimeout(r, 0))
    const { findByRole, queryByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    expect(queryByText(/Maria/)).toBeTruthy()
    expect(queryByText(/Lina/)).toBeTruthy()
    // The current user must not appear as a self-DM target.
    expect(queryByText(/Pascal/)).toBeNull()
  })
})
