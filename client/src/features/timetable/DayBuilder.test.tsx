import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/preact'

const apiPost = vi.fn()
const apiPut = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      post: (...a: unknown[]) => apiPost(...a),
      put: (...a: unknown[]) => apiPut(...a),
    },
  }
})
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))

import { ApiError } from '@/api'
import { timetables } from '@/store/timetables'
import { DayBuilder, dayBuilder, openDayBuilder } from './DayBuilder'
import { CopyDayDialog, copyDayDialog, openCopyDay } from './CopyDayDialog'
import { addBefore, initialState, newRow, timeRows, toSlots } from './builder'
import { entry, schoolWeek, timetable } from './testUtils'
import type { Timetable } from '@/types'

let tt: Timetable

beforeEach(() => {
  tt = timetable({ version: 3, entries: [] })
  timetables.value = [tt]
  dayBuilder.value = null
  copyDayDialog.value = null
  for (const f of [apiPost, apiPut, showToast, confirmDialog]) f.mockReset()
})

const answer = (extra: Partial<Timetable> = {}) =>
  ({ timetable: { ...timetables.value[0], version: timetables.value[0].version + 1, ...extra } })

function rows(container: Element) {
  return Array.from(container.ownerDocument.querySelectorAll('.sh-timetable-builder__row'))
    .map(li => [
      li.querySelector('.sh-timetable-builder__num')!.textContent,
      li.querySelector('.sh-timetable-builder__time')!.textContent,
    ].join(' '))
}

describe('builder maths', () => {
  it('breaks replace the gap; a length change ripples through the later rows', () => {
    const rs = [newRow('lesson', 45), newRow('lesson', 45), newRow('break', 20), newRow('lesson', 45)]
    const timed = timeRows(rs, 8 * 60, 5)
    expect(timed.map(r => [r.start, r.end, r.label])).toEqual([
      [480, 525, '1.'], [530, 575, '2.'], [575, 595, null], [595, 640, '3.'],
    ])
    rs[0] = { ...rs[0], minutes: 50 }
    expect(timeRows(rs, 480, 5).map(r => r.start)).toEqual([480, 535, 580, 600])
  })

  it('flags a slot past midnight and one that is too short', () => {
    const timed = timeRows([newRow('lesson', 60), newRow('lesson', 3)], 23 * 60, 0)
    expect(timed.map(r => r.error)).toEqual(['midnight', 'short'])
  })

  it('add-before puts a "0." lesson ending a gap before the first slot', () => {
    const state = initialState(timetable({ entries: schoolWeek([0]) }), 0, 'Pause')
    const next = addBefore(state)!
    expect(next.start).toBe('07:10')
    expect(timeRows(next.rows, 7 * 60 + 10, next.gap).slice(0, 2).map(r => [r.label, r.start]))
      .toEqual([['0.', 430], ['1.', 480]])
    expect(addBefore(next)).toBeNull()
  })

  it('a new row keeps the subject of the old entry with the same start and kind', () => {
    const old = [entry(0, '08:00', '08:45', { title: 'Mathe', icon: '🔢', color: 'teal', room: '204' })]
    const slots = toSlots(timeRows([newRow('lesson', 50), newRow('lesson', 45)], 480, 5), old)
    expect(slots[0]).toMatchObject({ start: '08:00', end: '08:50', title: 'Mathe', icon: '🔢',
      color: 'teal', room: '204', label: '1.' })
    expect(slots[1]).toMatchObject({ start: '08:55', title: null, icon: null, color: null })
    // A break at 08:00 does not inherit a lesson's subject.
    const asBreak = toSlots(timeRows([newRow('break', 20)], 480, 5), old)
    expect(asBreak[0]).toMatchObject({ title: null, room: null })
  })
})

describe('DayBuilder', () => {
  it('starts an empty day from the six-lesson school default, numbered past breaks', () => {
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { container, getByRole } = render(<DayBuilder />)
    expect(getByRole('dialog', { name: 'Set up Monday' })).toBeTruthy()
    expect(rows(container)).toEqual([
      '1. 08:00–08:45', '2. 08:50–09:35', ' 09:35–09:55', '3. 09:55–10:40',
      '4. 10:45–11:30', ' 11:30–11:45', '5. 11:45–12:30', '6. 12:35–13:20',
    ])
    expect(container.textContent).toContain('6 lessons · 08:00–13:20')
  })

  it('ripples a changed length, reorders with ↑/↓ and adds lessons / breaks / an early 0.', () => {
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { container, getByRole, getByLabelText } = render(<DayBuilder />)
    fireEvent.input(getByLabelText('Minutes of lesson 1.'), { target: { value: '60' } })
    expect(rows(container).slice(0, 3)).toEqual(['1. 08:00–09:00', '2. 09:05–09:50', ' 09:50–10:10'])
    fireEvent.click(getByRole('button', { name: 'Move break at 09:50 up' }))
    expect(rows(container).slice(0, 3)).toEqual(['1. 08:00–09:00', ' 09:00–09:20', '2. 09:20–10:05'])
    fireEvent.click(getByRole('button', { name: '+ Add before (0.)' }))
    expect(rows(container)[0]).toBe('0. 07:10–07:55')
    expect(rows(container)[1]).toBe('1. 08:00–09:00')
    const n = rows(container).length
    fireEvent.click(getByRole('button', { name: '+ Break' }))
    fireEvent.click(getByRole('button', { name: '+ Lesson' }))
    expect(rows(container)).toHaveLength(n + 2)
    expect(rows(container)[n + 1]).toMatch(/^7\. /)
    fireEvent.click(getByRole('button', { name: 'Remove lesson 7.' }))
    expect(rows(container)).toHaveLength(n + 1)
  })

  it('shows "ends after midnight" inline and refuses to save', async () => {
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { getByLabelText, getByRole, getAllByText } = render(<DayBuilder />)
    fireEvent.input(getByLabelText('Day starts'), { target: { value: '21:00' } })
    expect(getAllByText('Ends after midnight').length).toBeGreaterThan(0)
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(getByRole('alert').textContent).toBe('Fix the marked slots first.'))
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('saves with generate; a busy day confirms "Replace N lessons on Monday?" and retries', async () => {
    tt = timetable({ version: 3, entries: schoolWeek([0, 1]) })
    timetables.value = [tt]
    apiPost
      .mockRejectedValueOnce(new ApiError(409, '/x', { code: 'DAY_HAS_ENTRIES', detail: 'x', count: 5 }))
      .mockResolvedValueOnce(answer())
    confirmDialog.mockResolvedValue(true)
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { getByRole } = render(<DayBuilder />)
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    expect(confirmDialog.mock.calls[0][0]).toBe('Replace 5 lessons on Monday?')
    const [path, body] = apiPost.mock.calls[1]
    expect(path).toBe('/api/timetables/tt1/days/0/generate')
    expect(body.version).toBe(3)
    expect(body.replace).toBe(true)
    // Unchanged day: the same five slots come back.
    expect(body.slots.map((s: { start: string }) => s.start))
      .toEqual(['08:00', '08:50', '09:35', '09:55', '10:45'])
    await waitFor(() => expect(dayBuilder.value).toBeNull())
  })

  it('keeps the subjects when only the times change', async () => {
    const entries = [
      entry(0, '08:00', '08:45', { title: 'Mathe', icon: '🔢', room: '204', label: '1.' }),
      entry(0, '08:50', '09:35', { title: 'Deutsch', icon: '📖', label: '2.' }),
    ]
    tt = timetable({ version: 3, entries })
    timetables.value = [tt]
    apiPost.mockResolvedValue(answer())
    confirmDialog.mockResolvedValue(true)
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { getByRole, getByLabelText } = render(<DayBuilder />)
    expect((getByLabelText('Title of lesson 2.') as HTMLInputElement).value).toBe('Deutsch')
    fireEvent.input(getByLabelText('Lesson (min)'), { target: { value: '50' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(apiPost.mock.calls[0][1].slots).toEqual([
      { start: '08:00', end: '08:50', kind: 'lesson', label: '1.', title: 'Mathe', icon: '🔢',
        color: null, room: '204', teacher: null, note: null },
      { start: '08:55', end: '09:45', kind: 'lesson', label: '2.', title: 'Deutsch', icon: '📖',
        color: null, room: null, teacher: null, note: null },
    ])
  })

  it('also copies the times onto picked days — without subjects by default — under ONE undo', async () => {
    tt = timetable({ version: 3, entries: schoolWeek([0, 1]) })
    timetables.value = [tt]
    const before = tt.entries
    apiPost
      .mockResolvedValueOnce(answer())
      .mockResolvedValueOnce({ timetable: { ...tt, version: 5 } })
    confirmDialog.mockResolvedValue(true)
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { getByRole } = render(<DayBuilder />)
    const copyTo = getByRole('group', { name: 'Also use these times on' })
    fireEvent.click(within(copyTo).getByRole('button', { name: 'Wednesday' }))
    fireEvent.click(within(copyTo).getByRole('button', { name: 'Friday' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    expect(apiPost.mock.calls[1]).toEqual(['/api/timetables/tt1/days/0/copy',
      { version: 4, to: [2, 4], with_subjects: false }])
    expect(showToast).toHaveBeenCalledTimes(1)
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg).toBe('Times saved for Mon, Wed, Fri')
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 6 } })
    opts.action.onClick()
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/entries',
      { version: 5, entries: before }))
  })
})

describe('DayBuilder — the copy fails after the day was saved', () => {
  it('still offers Undo for the saved day (with its version), toasts the copy error and closes', async () => {
    tt = timetable({ version: 3, entries: schoolWeek([0, 1]) })
    timetables.value = [tt]
    const before = tt.entries
    apiPost
      .mockResolvedValueOnce(answer())
      .mockRejectedValueOnce(new ApiError(422, '/x', { code: 'UNPROCESSABLE', detail: 'target weekday 4 not in days' }))
    confirmDialog.mockResolvedValue(true)
    openDayBuilder({ timetableId: 'tt1', weekday: 0 })
    const { getByRole } = render(<DayBuilder />)
    fireEvent.click(within(getByRole('group', { name: 'Also use these times on' }))
      .getByRole('button', { name: 'Friday' }))
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(dayBuilder.value).toBeNull())
    expect(showToast).toHaveBeenCalledWith('target weekday 4 not in days', 'error')
    const undoCall = showToast.mock.calls.find(c => c[2]?.action)!
    expect(undoCall[0]).toBe('Times for Monday saved')
    apiPut.mockResolvedValue({ timetable: { ...tt, version: 5 } })
    undoCall[2].action.onClick()
    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/timetables/tt1/entries',
      { version: 4, entries: before }))
  })
})

describe('CopyDayDialog', () => {
  it('copies with subjects when ticked, and offers an undo', async () => {
    tt = timetable({ version: 3, entries: schoolWeek([0]) })
    timetables.value = [tt]
    apiPost.mockResolvedValue(answer())
    openCopyDay({ timetableId: 'tt1', weekday: 0 })
    const { getByRole, getByLabelText } = render(<CopyDayDialog />)
    expect(getByRole('dialog', { name: 'Copy Monday to other days' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Copy' }))
    expect(getByRole('alert').textContent).toBe('Pick at least one day.')
    fireEvent.click(getByRole('button', { name: 'All' }))
    fireEvent.click(getByLabelText(/With subjects/))
    fireEvent.click(getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/timetables/tt1/days/0/copy',
      { version: 3, to: [1, 2, 3, 4], with_subjects: true }))
    await waitFor(() => expect(copyDayDialog.value).toBeNull())
    expect(showToast.mock.calls[0][0]).toBe('Mon copied to Tue, Wed, Thu, Fri')
    expect(showToast.mock.calls[0][2].action.label).toBe('Undo')
  })
})
