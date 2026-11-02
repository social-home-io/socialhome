import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPut = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    put: (...a: unknown[]) => apiPut(...a),
  },
  ApiError: class extends Error {},
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u-mia', username: 'mia', display_name: 'Mia' } } }))
const people = new Map([
  ['u-mia', { user_id: 'u-mia', display_name: 'Mia', username: 'mia' }],
  ['u-tom', { user_id: 'u-tom', display_name: 'Tom', username: 'tom' }],
  ['u-lena', { user_id: 'u-lena', display_name: 'Lena', username: 'lena' }],
])
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { get value() { return people } },
  loadHouseholdUsers: vi.fn(async () => undefined),
}))

beforeEach(() => { apiGet.mockReset(); apiPut.mockReset(); showToast.mockReset() })
afterEach(() => cleanup())

function audience(extra: Record<string, unknown> = {}) {
  return {
    album_id: 'al-1', audience: 'chosen', user_ids: [], owner_user_id: 'u-mia',
    guardian_ids: ['u-tom'], can_edit: true, ...extra,
  }
}

async function open(extra: Record<string, unknown> = {}) {
  apiGet.mockResolvedValue(audience(extra))
  const onSaved = vi.fn()
  const { AudienceSheet } = await import('./AudienceSheet')
  const view = render(
    <AudienceSheet albumId="al-1" albumName="Diary" onClose={vi.fn()} onSaved={onSaved} />,
  )
  return { view, onSaved }
}

describe('AudienceSheet', () => {
  it("a child's guardian is a fixed, ticked row nobody can untick", async () => {
    const { view } = await open()
    const tom = await view.findByRole('checkbox', { name: /Tom.*guardian/ })
    expect((tom as HTMLInputElement).checked).toBe(true)
    expect((tom as HTMLInputElement).disabled).toBe(true)
    expect(view.getByText(/Guardians always see/)).toBeTruthy()
    expect(view.getByRole('note').textContent).toMatch(/not encryption/)
  })

  it('the owner picks people and saves', async () => {
    apiPut.mockResolvedValue(audience({ user_ids: ['u-lena'] }))
    const { view, onSaved } = await open()
    fireEvent.click(await view.findByRole('checkbox', { name: 'Lena' }))
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith(
      '/api/gallery/albums/al-1/audience', { audience: 'chosen', user_ids: ['u-lena'] }))
    expect(onSaved).toHaveBeenCalledWith('chosen')
  })

  it('switching to everyone sends no people', async () => {
    apiPut.mockResolvedValue(audience({ audience: 'everyone' }))
    const { view } = await open({ user_ids: ['u-lena'] })
    fireEvent.click(await view.findByRole('radio', { name: /Everyone at home/ }))
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith(
      '/api/gallery/albums/al-1/audience', { audience: 'everyone', user_ids: [] }))
  })

  it('a refusal is shown and the sheet stays open', async () => {
    apiPut.mockRejectedValue(new Error('An album that already has photos can’t become private'))
    const { view, onSaved } = await open({ audience: 'everyone' })
    fireEvent.click(await view.findByRole('radio', { name: /Private – only these people/ }))
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      expect.stringMatching(/already has photos/), 'error'))
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('someone who is not the owner reads who sees it', async () => {
    const { view } = await open({ can_edit: false, user_ids: ['u-lena'] })
    expect(await view.findByText('Private – only Mia, Lena, Tom see it.')).toBeTruthy()
    expect(view.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('says so when it could not be read', async () => {
    apiGet.mockRejectedValue(new Error('x'))
    const { AudienceSheet } = await import('./AudienceSheet')
    const view = render(<AudienceSheet albumId="al-1" albumName="Diary" onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(await view.findByRole('alert')).toBeTruthy()
  })
})
