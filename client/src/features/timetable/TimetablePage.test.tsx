import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

const apiGet = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: vi.fn(),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))
vi.mock('@/store/pageTitle', () => ({ useTitle: vi.fn() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn(),
  householdDisplayName: (id: string) => id,
  householdPictureUrl: () => null,
}))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u1', username: 'pascal', display_name: 'Pascal' } } }))

import { loaded, selectedId, timetables } from '@/store/timetables'
import TimetablePage from './TimetablePage'
import { closeCreateDialog } from './TimetableCreateDialog'
import { schoolWeek, timetable } from './testUtils'

function renderAt(url: string) {
  window.history.replaceState(null, '', url)
  return render(<LocationProvider><TimetablePage /></LocationProvider>)
}

beforeEach(() => {
  timetables.value = []
  selectedId.value = null
  loaded.value = false
  localStorage.clear()
  closeCreateDialog()
  for (const f of [apiGet, apiPatch, apiDelete, apiPost, confirmDialog]) f.mockReset()
})

describe('TimetablePage', () => {
  it('shows the empty state with both ways to start', async () => {
    apiGet.mockResolvedValue({ timetables: [] })
    const { findByText, getByRole } = renderAt('/calendar?tab=timetable')
    expect(await findByText('Create your first timetable')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Create school timetable' }))
    await waitFor(() => expect(getByRole('dialog')).toBeTruthy())
    expect((getByRole('radio', { name: /School day/ }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Close dialog' }))
    fireEvent.click(getByRole('button', { name: 'Start empty' }))
    expect((getByRole('radio', { name: /Empty/ }) as HTMLInputElement).checked).toBe(true)
  })

  it('opens a single timetable straight away and puts it in the URL', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable({ entries: schoolWeek() })] })
    const { findByRole, queryByRole } = renderAt('/calendar?tab=timetable')
    expect(await findByRole('button', { name: /Emma's timetable, Rename timetable/ })).toBeTruthy()
    // One card plus the compact "+ New" card — no separate full-width button.
    const group = queryByRole('group', { name: 'Timetables' })!
    expect(group.querySelectorAll('.sh-timetable-card')).toHaveLength(1)
    expect(group.lastElementChild!.textContent).toBe('+ New')
    await waitFor(() => expect(window.location.search).toBe('?tab=timetable&tt=tt1'))
  })

  it('lists several timetables as cards and selects on click', async () => {
    apiGet.mockResolvedValue({ timetables: [
      timetable({ id: 'a', name: 'Emma', active_this_week: true }),
      timetable({ id: 'b', name: 'Leo', days: [0, 2, 4], active_this_week: false }),
    ] })
    const { findByRole, getByRole, queryByRole } = renderAt('/calendar?tab=timetable')
    const group = await findByRole('group', { name: 'Timetables' })
    const cards = group.querySelectorAll('.sh-timetable-card')
    expect(cards).toHaveLength(2)
    expect(cards[0].textContent).toContain('Mon–Fri')
    expect(cards[0].textContent).toContain('Active this week')
    expect(cards[1].textContent).toContain('Mon, Wed, Fri')
    expect(cards[1].textContent).toContain('Not this week')
    expect(queryByRole('button', { name: /Rename timetable/ })).toBeNull()
    fireEvent.click(getByRole('button', { name: /Leo/ }))
    expect(getByRole('button', { name: /Leo, Rename timetable/ })).toBeTruthy()
    expect(window.location.search).toBe('?tab=timetable&tt=b')
  })

  it('selects the timetable named in ?tt=', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable({ id: 'a', name: 'Emma' }),
      timetable({ id: 'b', name: 'Leo' })] })
    const { findByRole } = renderAt('/calendar?tab=timetable&tt=b')
    expect(await findByRole('button', { name: /Leo, Rename timetable/ })).toBeTruthy()
  })

  describe('inline rename', () => {
    async function startRename() {
      apiGet.mockResolvedValue({ timetables: [timetable()] })
      const utils = renderAt('/calendar?tab=timetable&tt=tt1')
      fireEvent.click(await utils.findByRole('button', { name: /Rename timetable/ }))
      const input = utils.getByRole('textbox', { name: 'Timetable name' }) as HTMLInputElement
      return { ...utils, input }
    }

    it('Enter saves', async () => {
      const { input, findByRole } = await startRename()
      await waitFor(() => expect(document.activeElement).toBe(input))
      apiPatch.mockResolvedValue({ timetable: timetable({ version: 2, name: 'Emma 4b' }) })
      fireEvent.input(input, { target: { value: 'Emma 4b' } })
      fireEvent.submit(input.form!)
      expect(await findByRole('button', { name: /Emma 4b, Rename timetable/ })).toBeTruthy()
      expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1', { version: 1, name: 'Emma 4b' })
    })

    it('Esc cancels', async () => {
      const { input, getByRole } = await startRename()
      fireEvent.input(input, { target: { value: 'Nope' } })
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(getByRole('button', { name: /Emma's timetable, Rename timetable/ })).toBeTruthy()
      expect(apiPatch).not.toHaveBeenCalled()
    })

    it('an empty name shows an inline error', async () => {
      const { input, findByRole } = await startRename()
      fireEvent.input(input, { target: { value: '  ' } })
      fireEvent.submit(input.form!)
      expect((await findByRole('alert')).textContent).toBe("Name can't be empty")
      expect(apiPatch).not.toHaveBeenCalled()
    })
  })

  it('deletes after confirming', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    apiDelete.mockResolvedValue(null)
    confirmDialog.mockResolvedValue(true)
    const { findByRole, getByRole, findByText } = renderAt('/calendar?tab=timetable&tt=tt1')
    fireEvent.click(await findByRole('button', { name: 'Timetable actions' }))
    fireEvent.click(getByRole('menuitem', { name: 'Delete' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/timetables/tt1'))
    expect(confirmDialog.mock.calls[0][0]).toBe("Delete Emma's timetable? This can't be undone.")
    expect(await findByText('Create your first timetable')).toBeTruthy()
  })

  it('keeps the timetable when the delete is not confirmed', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    confirmDialog.mockResolvedValue(false)
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    fireEvent.click(await findByRole('button', { name: 'Timetable actions' }))
    fireEvent.click(getByRole('menuitem', { name: 'Delete' }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('toggles Picture view and remembers it per timetable', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable({ entries: schoolWeek() })] })
    const { findByRole, container } = renderAt('/calendar?tab=timetable&tt=tt1')
    const toggle = await findByRole('button', { name: /Picture view/ })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.sh-timetable-block--picture')).not.toBeNull()
    expect(JSON.parse(localStorage.getItem('sh-timetable-view:tt1')!).picture).toBe(true)
  })

  it('offers a retry when loading fails', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    const { findByRole } = renderAt('/calendar?tab=timetable')
    const retry = await findByRole('button', { name: 'Try again' })
    apiGet.mockResolvedValue({ timetables: [] })
    fireEvent.click(retry)
    expect(await findByRole('button', { name: 'Create school timetable' })).toBeTruthy()
  })

  it('"+ New" is the last card of the strip and opens the create dialog', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable({ id: 'a', name: 'Emma' }), timetable({ id: 'b', name: 'Leo' })] })
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=a')
    const group = await findByRole('group', { name: 'Timetables' })
    const add = group.lastElementChild as HTMLElement
    expect(add.getAttribute('aria-label')).toBe('New timetable')
    fireEvent.click(add)
    expect(getByRole('dialog', { name: 'New timetable' })).toBeTruthy()
  })

  it('Picture view, List view and the menu share the header row', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable({ entries: schoolWeek() })] })
    const { findByRole, container } = renderAt('/calendar?tab=timetable&tt=tt1')
    const list = await findByRole('button', { name: 'List view' })
    const actions = container.querySelector('.sh-timetable-head__actions')!
    expect(actions.contains(list)).toBe(true)
    expect(actions.contains(container.querySelector('.sh-timetable-toggle--picture'))).toBe(true)
    fireEvent.click(list)
    expect(list.getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.sh-timetable-visual')).toBeNull()
  })

  it('the menu focuses its first item and moves with the arrow keys', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    fireEvent.click(await findByRole('button', { name: 'Timetable actions' }))
    const item = (name: string) => getByRole('menuitem', { name })
    await waitFor(() => expect(document.activeElement).toBe(item('Settings')))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(item('Duplicate'))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(item('New timetable'))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' }) // wraps
    expect(document.activeElement).toBe(item('Settings'))
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' }) // wraps back
    expect(document.activeElement).toBe(item('Delete'))
  })

  it('returns focus to the ··· trigger when a dialog opened from the menu closes', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    const more = await findByRole('button', { name: 'Timetable actions' })
    fireEvent.click(more)
    await waitFor(() => expect(document.activeElement).toBe(getByRole('menuitem', { name: 'Settings' })))
    fireEvent.click(getByRole('menuitem', { name: 'Settings' }))
    await waitFor(() => expect(getByRole('dialog', { name: 'Timetable settings' })).toBeTruthy())
    fireEvent.click(getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(document.activeElement).toBe(more))
  })

  it('rename commits on blur (Esc still cancels)', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    apiPatch.mockResolvedValue({ timetable: timetable({ version: 2, name: 'Blurred' }) })
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    fireEvent.click(await findByRole('button', { name: /Rename timetable/ }))
    const input = getByRole('textbox', { name: 'Timetable name' })
    fireEvent.input(input, { target: { value: 'Blurred' } })
    fireEvent.blur(input)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/timetables/tt1',
      { version: 1, name: 'Blurred' }))
    expect(await findByRole('button', { name: /Blurred, Rename timetable/ })).toBeTruthy()
  })

  it('the ··· menu also offers "New timetable"', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    const { findByRole, getByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    fireEvent.click(await findByRole('button', { name: 'Timetable actions' }))
    fireEvent.click(getByRole('menuitem', { name: 'New timetable' }))
    await waitFor(() => expect(getByRole('dialog', { name: 'New timetable' })).toBeTruthy())
  })

  it('the toggles carry a short label for narrow screens, keeping the full accessible name', async () => {
    apiGet.mockResolvedValue({ timetables: [timetable()] })
    const { findByRole } = renderAt('/calendar?tab=timetable&tt=tt1')
    const pic = await findByRole('button', { name: 'Picture view' })
    expect(pic.querySelector('.sh-timetable-toggle__short')!.textContent).toBe('Pictures')
    const list = await findByRole('button', { name: 'List view' })
    expect(list.querySelector('.sh-timetable-toggle__short')!.textContent).toBe('List')
  })
})
