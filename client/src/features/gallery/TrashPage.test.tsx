import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
  ApiError: class extends Error {},
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))
const confirmDialog = vi.fn(async () => true)
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...(a as [])) }))
const me = { value: { user_id: 'u1', is_admin: false } as { user_id: string; is_admin: boolean } }
vi.mock('@/store/auth', () => ({ currentUser: me }))
const people = new Map([['u-lena', { user_id: 'u-lena', display_name: 'Lena', username: 'lena' }]])
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { get value() { return people } },
  loadHouseholdUsers: vi.fn(async () => undefined),
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn() }) }))

const DAY = 86_400_000
function row(id: string, extra: Record<string, unknown> = {}) {
  const deleted = new Date(Date.now() - DAY)
  return {
    id, album_id: 'al-1', uploaded_by: 'u-lena', item_type: 'photo',
    thumbnail_url: `api/media/t-${id}.webp`, deleted_at: deleted.toISOString(),
    deleted_by: 'u-lena', purge_after: new Date(deleted.getTime() + 30 * DAY).toISOString(),
    album_name: 'Sommer 2026', album_in_trash: false, ...extra,
  }
}

beforeEach(() => {
  apiGet.mockReset(); apiPost.mockReset(); apiDelete.mockReset(); showToast.mockReset()
  confirmDialog.mockClear()
  me.value = { user_id: 'u1', is_admin: false }
})
afterEach(() => cleanup())

async function mount(rows: unknown[], locked: unknown[] = []) {
  apiGet.mockImplementation(async (url: string) => (url === '/api/gallery/trash/locked' ? locked : rows))
  const { default: TrashPage } = await import('./TrashPage')
  return render(<TrashPage />)
}

describe('TrashPage', () => {
  it('groups by the day deleted, says who and from where, and how long is left', async () => {
    const view = await mount([row('a'), row('b')])
    expect(await view.findByRole('heading', { name: /Deleted yesterday/ })).toBeTruthy()
    expect(view.getByText(/by Lena/)).toBeTruthy()
    expect(view.getByText(/from “Sommer 2026”/)).toBeTruthy()
    expect(view.getAllByText('29 days')).toHaveLength(2)
    // Not an admin: no "Empty the trash".
    expect(view.queryByRole('button', { name: 'Trash options' })).toBeNull()
  })

  it('restores what was picked and says how many came back', async () => {
    apiPost.mockResolvedValue({ done: ['a'], refused: [] })
    const view = await mount([row('a'), row('b')])
    const tiles = await view.findAllByRole('checkbox')
    fireEvent.click(tiles[0])
    expect(view.getByText('1 selected')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/gallery/bulk', { action: 'restore', item_ids: ['a'] }, {}))
    await waitFor(() => expect(showToast.mock.calls[0][0]).toBe('1 photo restored'))
  })

  it('deletes forever only after asking', async () => {
    apiPost.mockResolvedValue({ done: ['a'], refused: [] })
    const view = await mount([row('a')])
    fireEvent.click(await view.findByRole('checkbox'))
    fireEvent.click(view.getByRole('button', { name: 'Delete forever' }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/gallery/bulk', { action: 'purge', item_ids: ['a'] }, {}))
  })

  it('an admin can empty the trash from ⋯', async () => {
    me.value = { user_id: 'u1', is_admin: true }
    apiDelete.mockResolvedValue({ removed: 2 })
    const view = await mount([row('a'), row('b')])
    fireEvent.click(await view.findByRole('button', { name: 'Trash options' }))
    fireEvent.click(view.getByRole('menuitem', { name: 'Empty the trash' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gallery/trash'))
  })

  it('a private album shows as one locked tile, never a preview', async () => {
    const view = await mount([row('p', { locked: { owner_user_id: 'u-lena', count: 12 } })])
    expect(await view.findByRole('img', { name: /Private album from Lena, 12 photos/ })).toBeTruthy()
    expect(view.container.querySelector('img')).toBeNull()
    expect(view.queryByRole('checkbox')).toBeNull()
  })

  it("an admin's locked tiles come from the server, unpickable, beside the rest", async () => {
    me.value = { user_id: 'u1', is_admin: true }
    const deleted = new Date(Date.now() - DAY)
    const view = await mount([row('a')], [{
      album_id: 'al-p', owner_user_id: 'u-lena', count: 3,
      deleted_at: deleted.toISOString(),
      purge_after: new Date(deleted.getTime() + 30 * DAY).toISOString(),
      album_in_trash: true,
    }])
    expect(await view.findByRole('img', { name: /Private album from Lena, 3 photos/ })).toBeTruthy()
    expect(view.getAllByRole('checkbox')).toHaveLength(1)
    expect(apiGet).toHaveBeenCalledWith('/api/gallery/trash/locked')
  })

  it('says when the trash is empty', async () => {
    const view = await mount([])
    expect(await view.findByText('The trash is empty')).toBeTruthy()
  })
})

describe('daysLeft', () => {
  it('counts whole days, never below zero', async () => {
    const { daysLeft } = await import('./TrashPage')
    const now = Date.parse('2026-11-02T12:00:00Z')
    expect(daysLeft('2026-12-01T12:00:00Z', now)).toBe(29)
    expect(daysLeft('2026-11-02T13:00:00Z', now)).toBe(1)
    expect(daysLeft('2026-11-01T00:00:00Z', now)).toBe(0)
  })
})
