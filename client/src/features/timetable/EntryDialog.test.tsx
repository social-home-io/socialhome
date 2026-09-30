import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()
const apiGet = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: (...a: unknown[]) => apiPut(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() }, loadHouseholdUsers: vi.fn(),
}))

import { ApiError } from '@/api'
import { timetables } from '@/store/timetables'
import { EntryDialog, openEntryDialog, entryDialog } from './EntryDialog'
import { entry, timetable } from './testUtils'
import type { Timetable, TimetableEntry } from '@/types'

let tt: Timetable
let mathe1: TimetableEntry
let mathe2: TimetableEntry
let deutsch: TimetableEntry

beforeEach(() => {
  mathe1 = entry(0, '08:00', '08:45', { title: 'Mathe', label: '1.' })
  deutsch = entry(0, '08:50', '09:35', { title: 'Deutsch', label: '2.' })
  mathe2 = entry(2, '10:00', '10:45', { title: 'Mathe', label: '3.' })
  tt = timetable({ version: 7, entries: [mathe1, deutsch, mathe2] })
  timetables.value = [tt]
  entryDialog.value = null
  for (const f of [apiGet, apiPost, apiPatch, apiPut, apiDelete, showToast]) f.mockReset()
})

const answer = (extra: Partial<Timetable> = {}) =>
  ({ timetable: { ...tt, version: tt.version + 1, ...extra } })

function input(el: Element, value: string) {
  fireEvent.input(el, { target: { value } })
}

describe('EntryDialog — create', () => {
  it('prefills the weekday / time and duration chips set the end', async () => {
    openEntryDialog({ timetableId: 'tt1', entry: null,
      prefill: { weekday: 1, start: '10:00', end: '10:45' } })
    const { getByLabelText, getByRole, getByText } = render(<EntryDialog />)
    expect((getByLabelText('Starts') as HTMLInputElement).value).toBe('10:00')
    expect((getByLabelText('Day') as HTMLSelectElement).value).toBe('1')
    expect(getByRole('button', { name: '45 min' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByRole('button', { name: '90 min' }))
    expect((getByLabelText('Ends') as HTMLInputElement).value).toBe('11:30')
    expect(getByText('ends 11:30')).toBeTruthy()
    // Moving the start keeps the length.
    input(getByLabelText('Starts'), '12:00')
    expect((getByLabelText('Ends') as HTMLInputElement).value).toBe('13:30')
  })

  it('suggests an icon from the title until one is picked by hand', () => {
    openEntryDialog({ timetableId: 'tt1', entry: null })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    input(getByLabelText('Title'), 'Schwimmen')
    expect(getByRole('button', { name: 'Swimming' }).getAttribute('aria-pressed')).toBe('true')
    input(getByLabelText('Title'), 'Musik')
    expect(getByRole('button', { name: 'Music' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByRole('button', { name: 'Art' }))
    input(getByLabelText('Title'), 'Sport')
    expect(getByRole('button', { name: 'Art' }).getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: 'Sport' }).getAttribute('aria-pressed')).toBe('false')
  })

  it('POSTs a new entry with the version', async () => {
    apiPost.mockResolvedValue(answer())
    openEntryDialog({ timetableId: 'tt1', entry: null,
      prefill: { weekday: 1, start: '10:00', end: '10:45' } })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    input(getByLabelText('Title'), 'Kunst')
    input(getByLabelText('Room'), '  B1 ')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(apiPost).toHaveBeenCalledWith('/api/timetables/tt1/entries', {
      version: 7, weekday: 1, kind: 'lesson', title: 'Kunst', icon: '🎨',
      start: '10:00', end: '10:45', room: 'B1', teacher: null, note: null, color: null,
    })
    await waitFor(() => expect(entryDialog.value).toBeNull())
  })

  it('validates the obvious client-side and shows server 422s inline', async () => {
    openEntryDialog({ timetableId: 'tt1', entry: null })
    const { getByLabelText, getByRole, findByRole } = render(<EntryDialog />)
    input(getByLabelText('Ends'), '07:00')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    expect((await findByRole('alert')).textContent).toBe('The end must be after the start.')
    expect(apiPost).not.toHaveBeenCalled()

    input(getByLabelText('Ends'), '08:45')
    apiPost.mockRejectedValue(new ApiError(422, '/x',
      { code: 'UNPROCESSABLE', detail: 'entry overlaps Mathe 08:00–08:45' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(getByRole('alert').textContent)
      .toBe('entry overlaps Mathe 08:00–08:45'))
    expect(entryDialog.value).not.toBeNull()
  })
})

describe('EntryDialog — edit', () => {
  it('PATCHes only the fields that changed, against the snapshot version', async () => {
    apiPatch.mockResolvedValue(answer())
    openEntryDialog({ timetableId: 'tt1', entry: deutsch })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    input(getByLabelText('Teacher'), 'Frau Huber')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalled())
    expect(apiPatch).toHaveBeenCalledWith(`/api/timetables/tt1/entries/${deutsch.id}`,
      { version: 7, teacher: 'Frau Huber' })
  })

  it('a change that landed while the dialog was open 409s, reloads and keeps the dialog', async () => {
    openEntryDialog({ timetableId: 'tt1', entry: deutsch })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    // A WS frame bumps the stored timetable to v8 meanwhile.
    timetables.value = [{ ...tt, version: 8 }]
    apiPatch.mockRejectedValue(new ApiError(409, '/x',
      { code: 'TIMETABLE_CONFLICT', detail: '', current_version: 8 }))
    apiGet.mockResolvedValue({ timetable: { ...tt, version: 8 } })
    input(getByLabelText('Teacher'), 'Frau Huber')
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Someone else just changed this timetable — reloaded', 'info'))
    expect(apiPatch.mock.calls[0][1].version).toBe(7)
    expect(entryDialog.value).not.toBeNull()
  })

  it('lengthening shifts the following lessons in ONE atomic PUT (with one undo)', async () => {
    apiPut.mockResolvedValue(answer())
    openEntryDialog({ timetableId: 'tt1', entry: mathe1 })
    const { getByLabelText, getByRole, queryByLabelText } = render(<EntryDialog />)
    expect(queryByLabelText(/Shift the following lessons/)).toBeNull()
    fireEvent.click(getByRole('button', { name: '60 min' }))
    const box = getByLabelText('Shift the following lessons by +15 min') as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
    const [path, body] = apiPut.mock.calls[0]
    expect(path).toBe('/api/timetables/tt1/entries')
    expect(body.version).toBe(7)
    const byId = Object.fromEntries(body.entries.map((e: TimetableEntry) => [e.id, e]))
    expect(byId[mathe1.id]).toMatchObject({ start: '08:00', end: '09:00' })
    expect(byId[deutsch.id]).toMatchObject({ start: '09:05', end: '09:50' })
    expect(byId[mathe2.id]).toMatchObject({ start: '10:00', end: '10:45' }) // other day
    expect(showToast.mock.calls[0][2].action.label).toBe('Undo')
    await waitFor(() => expect(entryDialog.value).toBeNull())
  })

  it('shortening pulls the following lessons earlier (negative delta)', async () => {
    apiPut.mockResolvedValue(answer())
    openEntryDialog({ timetableId: 'tt1', entry: mathe1 })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    fireEvent.click(getByRole('button', { name: '30 min' }))
    expect(getByLabelText('Shift the following lessons by −15 min')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1))
    const byId = Object.fromEntries(apiPut.mock.calls[0][1].entries.map((e: TimetableEntry) => [e.id, e]))
    expect(byId[mathe1.id]).toMatchObject({ start: '08:00', end: '08:30' })
    expect(byId[deutsch.id]).toMatchObject({ start: '08:35', end: '09:20' })
  })

  it('a refused shift saves nothing and says why inline', async () => {
    apiPut.mockRejectedValue(new ApiError(422, '/x',
      { code: 'UNPROCESSABLE', detail: 'shift would cross midnight' }))
    openEntryDialog({ timetableId: 'tt1', entry: mathe1 })
    const { getByRole } = render(<EntryDialog />)
    fireEvent.click(getByRole('button', { name: '60 min' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(getByRole('alert').textContent).toBe('shift would cross midnight'))
    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    expect(timetables.value[0].version).toBe(7)
    expect(entryDialog.value).not.toBeNull()
  })

  it('applies colour & icon to every lesson of the subject in one PUT', async () => {
    apiPut.mockResolvedValue(answer())
    openEntryDialog({ timetableId: 'tt1', entry: mathe1 })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    fireEvent.click(getByLabelText('Teal'))
    fireEvent.click(getByRole('button', { name: 'Maths' }))
    const box = getByLabelText('Apply colour & icon to all “Mathe” lessons') as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    expect(apiPatch).not.toHaveBeenCalled()
    const [path, body] = apiPut.mock.calls[0]
    expect(path).toBe('/api/timetables/tt1/entries')
    expect(body.version).toBe(7)
    const byId = Object.fromEntries(body.entries.map((e: TimetableEntry) => [e.id, e]))
    expect(byId[mathe1.id]).toMatchObject({ color: 'teal', icon: '🔢' })
    expect(byId[mathe2.id]).toMatchObject({ color: 'teal', icon: '🔢', title: 'Mathe' })
    expect(byId[deutsch.id]).toMatchObject({ color: null, icon: null })
    expect(showToast.mock.calls[0][2].action.label).toBe('Undo')
  })

  it('deletes with an undo toast that puts the entries back, and focuses the grid', async () => {
    const grid = document.createElement('section')
    grid.id = 'sh-tt-grid-tt1'
    grid.tabIndex = -1
    document.body.appendChild(grid)
    apiDelete.mockResolvedValue({ timetable: { ...tt, version: 8, entries: [deutsch, mathe2] } })
    openEntryDialog({ timetableId: 'tt1', entry: mathe1 })
    const { getByRole } = render(<EntryDialog />)
    fireEvent.click(getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith(
      `/api/timetables/tt1/entries/${mathe1.id}?version=7`))
    await waitFor(() => expect(entryDialog.value).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(grid))
    const [message, , opts] = showToast.mock.calls[0]
    expect(message).toBe('Lesson deleted')
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 9 } })
    opts.action.onClick()
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/entries',
      { version: 8, entries: [mathe1, deutsch, mathe2] }))
    grid.remove()
  })
})

describe('EntryDialog — a shared break band', () => {
  it('edits the same break on every day in one PUT', async () => {
    const breaks = [0, 1, 2].map(d => entry(d, '09:35', '09:55', { kind: 'break', title: 'Pause' }))
    const lesson = entry(0, '08:00', '08:45', { title: 'Mathe' })
    tt = timetable({ version: 4, days: [0, 1, 2], entries: [lesson, ...breaks] })
    timetables.value = [tt]
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 5 } })
    openEntryDialog({ timetableId: 'tt1', entry: breaks[0], group: breaks.map(b => b.id) })
    const { getByLabelText, getByRole } = render(<EntryDialog />)
    input(getByLabelText('Title'), 'Hofpause')
    fireEvent.click(getByRole('button', { name: 'Snack' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPut).toHaveBeenCalled())
    const body = apiPut.mock.calls[0][1]
    expect(body.version).toBe(4)
    for (const b of breaks) {
      expect(body.entries.find((e: TimetableEntry) => e.id === b.id))
        .toMatchObject({ title: 'Hofpause', icon: '🍎', weekday: b.weekday, start: '09:35' })
    }
    expect(body.entries.find((e: TimetableEntry) => e.id === lesson.id)).toEqual(lesson)
  })
})
